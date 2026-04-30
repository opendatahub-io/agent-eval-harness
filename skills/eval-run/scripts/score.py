#!/usr/bin/env python3
"""Scoring CLI for eval runs.

Loads all files from each case's collected output directories into a
record dict. Passes the record to judges — they know what to do with
it via their description/check/prompt.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/score.py judges --run-id <id> --config eval.yaml
    python3 ${CLAUDE_SKILL_DIR}/scripts/score.py pairwise --run-id <id> --baseline <id> --config eval.yaml
    python3 ${CLAUDE_SKILL_DIR}/scripts/score.py regression --run-id <id> --config eval.yaml
"""

import argparse
import importlib
import json
import os
import re
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.config import EvalConfig


def _get_runs_dir():
    """Get runs directory from env or default."""
    return Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))


def _resolve_under(root: Path, candidate: Path) -> Path:
    """Ensure a path resolves under root. Raises ValueError if it escapes."""
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes root directory: {candidate}")
    return resolved


# ---------------------------------------------------------------------------
# Case record loading — reads all files, no schema interpretation
# ---------------------------------------------------------------------------

def load_case_record(case_dir, config, run_id=None, runs_dir=None):
    """Load all outputs, execution metadata, and traces for a case.

    Returns a dict with:
    - files: file artifact contents (from path outputs)
    - tool_calls: captured tool calls (from tool outputs)
    - Execution metadata: exit_code, duration_s, token_usage, cost_usd, num_turns
    - Logs: stdout, stderr (if traces config enables them)
    """
    runs_dir = Path(runs_dir) if runs_dir else _get_runs_dir()
    case_dir = Path(case_dir).resolve()
    record = {"files": {}, "tool_calls": [], "case_dir": str(case_dir)}

    # --- Annotations (from dataset case directory) ---
    record["annotations"] = {}
    case_id = case_dir.name
    if config.dataset_path:
        dataset_root = Path(config.dataset_path).resolve()
        annotations_path = (dataset_root / case_id / "annotations.yaml").resolve()
        if (annotations_path.is_relative_to(dataset_root)
                and annotations_path.is_file()
                and not annotations_path.is_symlink()):
            try:
                with open(annotations_path) as f:
                    record["annotations"] = yaml.safe_load(f) or {}
            except (yaml.YAMLError, OSError):
                pass
            # Load annotation-referenced files into the record
            for key, val in record["annotations"].items():
                if isinstance(val, str) and not val.startswith("/"):
                    ref_path = (dataset_root / case_id / val).resolve()
                    if (ref_path.is_file() and not ref_path.is_symlink()
                            and ref_path.is_relative_to(dataset_root)):
                        try:
                            record[f"annotation_{key}_content"] = ref_path.read_text()
                        except (UnicodeDecodeError, OSError):
                            pass

    # --- File artifacts (from path outputs) ---
    for output in config.outputs:
        if not output.path:
            continue
        out_path = output.path
        artifact_dir = case_dir / out_path
        if not artifact_dir.exists():
            continue
        _resolve_under(case_dir, artifact_dir)
        for f in sorted(artifact_dir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            _resolve_under(case_dir, f)
            rel = str(f.relative_to(case_dir))
            try:
                record["files"][rel] = f.read_text()
            except UnicodeDecodeError:
                record["files"][rel] = {"_binary": True, "path": str(f), "name": f.name}

    # Convenience keys for the first file in each path output dir
    for output in config.outputs:
        if not output.path:
            continue
        artifact_dir = case_dir / output.path
        if not artifact_dir.exists():
            continue
        for f in sorted(artifact_dir.iterdir()):
            if f.is_file() and not f.is_symlink():
                key = Path(output.path).name or "main"
                try:
                    record[f"{key}_content"] = f.read_text()
                    record[f"{key}_file"] = str(f)
                except UnicodeDecodeError:
                    pass
                break

    # --- Execution metadata (from run_result.json) ---
    if run_id and config.traces.metrics:
        run_result_path = runs_dir / run_id / "run_result.json"
        if run_result_path.exists():
            try:
                with open(run_result_path) as f:
                    meta = json.load(f)
                record["exit_code"] = meta.get("exit_code")
                record["duration_s"] = meta.get("duration_s")
                record["token_usage"] = meta.get("token_usage")
                record["cost_usd"] = meta.get("cost_usd")
                record["num_turns"] = meta.get("num_turns")
            except (json.JSONDecodeError, OSError):
                pass

    # --- Logs (if traces config enables them) ---
    # In case mode, stdout/stderr are per-case at case_dir/stdout.log.
    # In batch mode, they're at runs_dir/run_id/stdout.log.
    if run_id:
        if config.traces.stdout:
            # Try case-level first (case mode), fall back to run-level (batch)
            stdout_path = case_dir / "stdout.log"
            if not stdout_path.exists():
                stdout_path = runs_dir / run_id / "stdout.log"
            if stdout_path.exists():
                try:
                    record["stdout"] = stdout_path.read_text()
                except OSError:
                    pass
        if config.traces.stderr:
            stderr_path = case_dir / "stderr.log"
            if not stderr_path.exists():
                stderr_path = runs_dir / run_id / "stderr.log"
            if stderr_path.exists():
                try:
                    record["stderr"] = stderr_path.read_text()
                except OSError:
                    pass

    # --- Tool call outputs (from stream-json in stdout) ---
    # Tool calls are extracted regardless of traces.stdout setting —
    # read stdout.log directly if not already in the record
    tool_outputs = [o for o in config.outputs if o.tool]
    if tool_outputs:
        stdout_text = record.get("stdout", "")
        if not stdout_text and run_id:
            stdout_path = case_dir / "stdout.log"
            if not stdout_path.exists():
                stdout_path = runs_dir / run_id / "stdout.log"
            if stdout_path.exists():
                try:
                    stdout_text = stdout_path.read_text()
                except OSError:
                    pass
        if stdout_text:
            tool_calls = _extract_tool_calls(stdout_text, tool_outputs)
            record["tool_calls"] = tool_calls

    return record


def _extract_tool_calls(stdout_text, tool_outputs):
    """Extract tool calls from stream-json stdout matching configured patterns."""
    tool_patterns = [o.tool for o in tool_outputs]
    calls = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "assistant":
            continue
        # Skip foreground subagent messages (Claude Code >= 2.1.108 streams
        # them in stdout).  We only want root-level tool calls here.
        if obj.get("parent_tool_use_id"):
            continue
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            # Check if this tool call matches any configured pattern
            for pattern in tool_patterns:
                if pattern in name or name == pattern:
                    calls.append({
                        "name": name,
                        "input": block.get("input", {}),
                    })
                    break
    return calls


# ---------------------------------------------------------------------------
# Judge loading and scoring
# ---------------------------------------------------------------------------

def load_judges(config, project_root=None):
    """Load all judges from config.

    Judge types determined by which fields are set:
    - check: inline Python snippet
    - prompt/prompt_file: LLM judge
    - module/function: external code judge
    """
    judges = []
    for jc in config.judges:
        if jc.name == "pairwise":
            continue  # Pairwise is only used by score.py pairwise, not regular scoring
        if jc.check:
            scorer = _make_inline_check(jc)
        elif jc.prompt or jc.prompt_file:
            scorer = _load_llm_judge(jc, config, project_root)
        elif jc.module and jc.function:
            scorer = _load_code_judge(jc, project_root)
        else:
            print(f"  Warning: judge '{jc.name}' has no check, prompt, or module",
                  file=sys.stderr)
            continue
        if scorer:
            judges.append((jc.name, scorer, jc.condition))
    return judges


def score_cases(judges, case_dirs, config, run_id=None):
    """Score all cases with all judges in parallel."""
    if not case_dirs:
        return {"per_case": {}, "aggregated": {n: {"values": [], "mean": None, "pass_rate": None} for n, _, _c in judges}}
    per_case = {}
    aggregated = {name: {"values": []} for name, _, _c in judges}
    parallelism = min(len(case_dirs), os.cpu_count() or 4)
    lock = threading.Lock()
    completed = 0

    def _score_case(case_dir):
        case_id = case_dir.name
        record = load_case_record(case_dir, config, run_id=run_id)
        case_results = {}
        for name, scorer, condition in judges:
            # Check condition — skip if it evaluates to False
            if condition:
                try:
                    annotations = record.get("annotations", {})
                    if not eval(condition, {"__builtins__": {}},
                                {"annotations": annotations, "outputs": record}):
                        case_results[name] = {
                            "value": None,
                            "rationale": f"Skipped: condition '{condition}' is false",
                        }
                        continue
                except Exception as e:
                    case_results[name] = {
                        "value": None,
                        "rationale": f"Condition error: {e}",
                    }
                    continue
            try:
                result = scorer(outputs=record)
                # Normalize — accepts (bool, str) tuples, Feedback, primitives
                if isinstance(result, tuple) and len(result) == 2:
                    case_results[name] = {
                        "value": result[0],
                        "rationale": result[1],
                    }
                elif hasattr(result, "value"):
                    case_results[name] = {
                        "value": result.value,
                        "rationale": getattr(result, "rationale", ""),
                    }
                elif isinstance(result, (bool, int, float, str)):
                    case_results[name] = {"value": result, "rationale": ""}
                else:
                    case_results[name] = {"value": result, "rationale": ""}
            except Exception as e:
                case_results[name] = {"value": None, "error": str(e)}
        return case_id, case_results

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_score_case, d): d for d in case_dirs}
        for future in as_completed(futures):
            completed += 1
            case_id, case_results = future.result()
            per_case[case_id] = case_results
            with lock:
                for name, result in case_results.items():
                    if name in aggregated and result.get("value") is not None:
                        aggregated[name]["values"].append(result["value"])
                print(f"  [{completed}/{len(case_dirs)}] {case_id}", flush=True)

    # Compute aggregates
    for name in aggregated:
        values = aggregated[name]["values"]
        if not values:
            aggregated[name]["mean"] = None
            aggregated[name]["pass_rate"] = None
            continue
        if all(isinstance(v, bool) for v in values):
            aggregated[name]["pass_rate"] = sum(values) / len(values)
            aggregated[name]["mean"] = aggregated[name]["pass_rate"]
        elif all(isinstance(v, (int, float)) for v in values):
            aggregated[name]["mean"] = sum(values) / len(values)
            aggregated[name]["pass_rate"] = None
        else:
            aggregated[name]["mean"] = None
            aggregated[name]["pass_rate"] = None

    return {"per_case": per_case, "aggregated": aggregated}


def _make_inline_check(jc):
    """Create a scorer from an inline check script."""
    source = jc.check
    wrapped = f"def _check(outputs):\n{textwrap.indent(source, '    ')}"
    code = compile(wrapped, f"<check:{jc.name}>", "exec")
    ns = {"__builtins__": __builtins__}
    exec(code, ns)
    check_fn = ns["_check"]

    def scorer(outputs=None, **kwargs):
        return check_fn(outputs or {})

    return scorer


def _load_code_judge(jc, project_root=None):
    if project_root and str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    mod = importlib.import_module(jc.module)
    return getattr(mod, jc.function)


def _resolve_judge_model(jc, config):
    """Resolve LLM judge model: per-judge > models.judge > env > error."""
    model = jc.model or config.models.judge or os.environ.get("EVAL_JUDGE_MODEL")
    if not model:
        raise RuntimeError(
            f"No model configured for LLM judge '{jc.name}'. Set per-judge "
            "'model:', top-level 'models.judge:' in eval.yaml, or "
            "EVAL_JUDGE_MODEL env var.")
    return model


def _load_llm_judge(jc, config, project_root=None):
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    prompt = jc.prompt
    if not prompt and jc.prompt_file:
        prompt_path = Path(jc.prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = root / prompt_path
        _resolve_under(root, prompt_path)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Judge prompt not found: {prompt_path}")
        prompt = prompt_path.read_text()
    if not prompt:
        raise ValueError(f"LLM judge '{jc.name}' requires prompt or prompt_file")
    # Append context files to the prompt
    for ctx_path in jc.context:
        path = Path(ctx_path)
        if not path.is_absolute():
            path = root / path
        _resolve_under(root, path)
        if path.exists():
            prompt += f"\n\n## Context: {path.name}\n\n{path.read_text()}"

    # Use direct Anthropic client when Vertex AI is configured (MLflow's
    # make_judge uses litellm which requires OpenAI API key by default)
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") or os.environ.get("ANTHROPIC_API_KEY"):
        judge_model = _resolve_judge_model(jc, config)
        return _make_anthropic_llm_judge(jc.name, prompt, judge_model)

    # MLflow make_judge (requires OpenAI-compatible API key)
    try:
        from mlflow.genai.judges import make_judge
        kwargs = {"name": jc.name, "instructions": prompt}
        if jc.feedback_type:
            kwargs["feedback_value_type"] = _parse_feedback_type(jc.feedback_type)
        return make_judge(**kwargs)
    except ImportError:
        pass

    raise RuntimeError(f"LLM judge '{jc.name}' requires ANTHROPIC_VERTEX_PROJECT_ID, "
                       "ANTHROPIC_API_KEY, or OPENAI_API_KEY")


def _make_anthropic_llm_judge(name, prompt, judge_model):
    """Create an LLM judge using the Anthropic client directly.

    Falls back to this when MLflow make_judge fails (e.g., no OpenAI key).
    Supports Vertex AI via ANTHROPIC_VERTEX_PROJECT_ID.
    """
    import re

    def scorer(outputs=None, **kwargs):
        import base64
        client = _get_anthropic_client()
        image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        image_blocks = []

        # Render {{ outputs }} template variable
        rendered_prompt = prompt
        if outputs and "{{ outputs }}" in rendered_prompt:
            files = outputs.get("files", {})
            output_text = ""
            for path, content in sorted(files.items()):
                if isinstance(content, dict) and content.get("_binary"):
                    suffix = Path(path).suffix.lower()
                    if suffix in image_extensions:
                        try:
                            with open(content["path"], "rb") as img_f:
                                b64 = base64.standard_b64encode(img_f.read()).decode()
                            media_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else f"image/{suffix.lstrip('.')}"
                            image_blocks.append({"label": path, "media_type": media_type, "data": b64})
                            output_text += f"\n### {path}\n\n[image attached below]\n"
                        except OSError:
                            output_text += f"\n### {path}\n\n<binary: {content['name']}>\n"
                    else:
                        output_text += f"\n### {path}\n\n<binary: {content['name']}>\n"
                else:
                    output_text += f"\n### {path}\n\n{content}\n"
            rendered_prompt = rendered_prompt.replace("{{ outputs }}", output_text)

        # Render {{ annotations }} template variable
        if outputs and "{{ annotations }}" in rendered_prompt:
            ann = outputs.get("annotations", {})
            ann_text = ""
            for key, val in sorted(ann.items()):
                ann_text += f"- **{key}**: {val}\n"
            # Include annotation file contents
            for key in sorted(outputs):
                if key.startswith("annotation_") and key.endswith("_content"):
                    field = key[len("annotation_"):-len("_content")]
                    ann_text += f"\n### {field} (file content)\n\n{outputs[key]}\n"
            rendered_prompt = rendered_prompt.replace("{{ annotations }}", ann_text)

        # Build message content — multimodal if images present
        if image_blocks:
            content_parts = [{"type": "text", "text": rendered_prompt}]
            for img in image_blocks:
                content_parts.append({"type": "text", "text": f"\n**Image: {img['label']}**"})
                content_parts.append({"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}})
            user_message = content_parts
        else:
            user_message = rendered_prompt

        response = client.messages.create(
            model=judge_model,
            max_tokens=1024,
            system="You are a judge evaluating skill outputs. "
                   "Return a JSON object with 'score' (integer 1-5) and 'rationale' (string).",
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        # Extract score from JSON response
        try:
            # Try parsing as JSON — search for {"score": N} anywhere
            match = re.search(r'"score"\s*:\s*(\d+)', text)
            if match:
                score_val = int(match.group(1))
                rationale_match = re.search(r'"rationale"\s*:\s*"([^"]*)"', text)
                rationale = rationale_match.group(1) if rationale_match else text[:200]
                return (score_val, rationale)
        except (ValueError, AttributeError):
            pass
        # Fallback: find "Score: N" or "Overall: N" or "N/5" patterns
        explicit = re.search(
            r'(?:overall|score|rating)\s*[=:]\s*(\d)\b'
            r'|(\d)\s*/\s*5'
            r'|\*\*(\d)\*\*\s*/\s*5',
            text, re.IGNORECASE)
        if explicit:
            score_val = int(next(g for g in explicit.groups() if g))
            return (score_val, text[:200])
        # Last resort: find the last standalone 1-5 digit (conclusion is at end)
        nums = re.findall(r'\b([1-5])\b', text)
        if nums:
            return (int(nums[-1]), text[:200])
        return (3, f"Could not parse score from: {text[:200]}")

    return scorer


def _parse_feedback_type(type_str):
    mapping = {"int": int, "float": float, "bool": bool, "str": str}
    if type_str in mapping:
        return mapping[type_str]
    if type_str.startswith("Literal"):
        from typing import Literal
        inner = type_str[len("Literal["):-1]
        values = tuple(v.strip().strip("'\"") for v in inner.split(","))
        return Literal[values]
    return str


# ---------------------------------------------------------------------------
# Pairwise comparison
# ---------------------------------------------------------------------------

BUILTIN_COMPARISON_PROMPT = (Path(__file__).parent.parent
                             / "prompts" / "comparison-judge.md")


@dataclass
class PairwiseResult:
    case_id: str
    pref_ab: Optional[str] = None
    pref_ba: Optional[str] = None
    error: Optional[str] = None

    @property
    def winner(self) -> str:
        if self.error or not self.pref_ab or not self.pref_ba:
            return "error"
        if self.pref_ab == "A" and self.pref_ba == "B":
            return "A"
        elif self.pref_ab == "B" and self.pref_ba == "A":
            return "B"
        return "tie"


def compare_runs(run_a_dir, run_b_dir, config, case_ids,
                 prompt=None, prompt_file=None, model=None):
    """Compare two runs using position-swapped LLM judge."""
    comparison_prompt = prompt
    if not comparison_prompt and prompt_file:
        comparison_prompt = Path(prompt_file).read_text()
    if not comparison_prompt and BUILTIN_COMPARISON_PROMPT.exists():
        comparison_prompt = BUILTIN_COMPARISON_PROMPT.read_text()
    if not comparison_prompt:
        comparison_prompt = ("Compare outputs A and B. Return JSON: "
                             "{\"reasoning\": \"...\", \"preferred\": \"A\" or \"B\" or \"tie\"}")

    try:
        client = _get_anthropic_client()
    except Exception as e:
        return {"error": str(e)}

    def _compare_case(case_id):
        record_a = load_case_record(run_a_dir / "cases" / case_id, config)
        record_b = load_case_record(run_b_dir / "cases" / case_id, config)

        output_a = _first_content(record_a)
        output_b = _first_content(record_b)

        if not output_a or not output_b:
            return PairwiseResult(case_id=case_id,
                                  error=f"Missing output: a={bool(output_a)}, b={bool(output_b)}")
        result = PairwiseResult(case_id=case_id)

        msg_ab = f"## Output A\n\n{output_a}\n\n## Output B\n\n{output_b}"
        pref_ab, err = _call_judge(client, comparison_prompt, msg_ab, model)
        if pref_ab:
            result.pref_ab = pref_ab.get("preferred")
        else:
            result.error = f"AB failed: {err}"
            return result

        msg_ba = f"## Output A\n\n{output_b}\n\n## Output B\n\n{output_a}"
        pref_ba, err = _call_judge(client, comparison_prompt, msg_ba, model)
        if pref_ba:
            result.pref_ba = pref_ba.get("preferred")
        else:
            result.error = f"BA failed: {err}"
        return result

    parallelism = min(len(case_ids), os.cpu_count() or 4)
    results = []
    completed = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_compare_case, cid): cid for cid in case_ids}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            with lock:
                completed += 1
                status = r.winner if not r.error else f"error: {r.error}"
                print(f"    [{completed}/{len(case_ids)}] {r.case_id}... {status}",
                      flush=True)

    wins_a = sum(1 for r in results if r.winner == "A")
    wins_b = sum(1 for r in results if r.winner == "B")
    ties = sum(1 for r in results if r.winner == "tie")
    errors = sum(1 for r in results if r.winner == "error")

    return {
        "run_a": run_a_dir.name, "run_b": run_b_dir.name,
        "cases_compared": len(results),
        "wins_a": wins_a, "wins_b": wins_b,
        "ties": ties, "errors": errors,
        "per_case": [{"case_id": r.case_id, "winner": r.winner, "error": r.error}
                     for r in results],
    }


def _first_content(record):
    """Get the first *_content value from a record."""
    for k, v in record.items():
        if k.endswith("_content") and v:
            return v
    return None


def _get_anthropic_client():
    project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    region = os.environ.get("CLOUD_ML_REGION", "us-east5")
    if project_id:
        from anthropic import AnthropicVertex
        return AnthropicVertex(project_id=project_id, region=region)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    raise RuntimeError("Set ANTHROPIC_VERTEX_PROJECT_ID or ANTHROPIC_API_KEY")


def _call_judge(client, system_prompt, user_message, model, max_tokens=8192):
    try:
        response = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=("You are a judge comparing two outputs. "
                    "You MUST end your response with a JSON object that follows "
                    "the schema specified in the user prompt and includes a "
                    "'preferred' field set to 'A', 'B', or 'tie'."),
            messages=[
                {"role": "user", "content": f"{system_prompt}\n\n{user_message}"},
            ],
        )
        text = response.content[0].text
        parsed = _extract_judge_json(text)
        if parsed is not None:
            return parsed, None
        # Retry once with a larger budget if the response was truncated.
        if response.stop_reason == "max_tokens" and max_tokens < 16384:
            return _call_judge(client, system_prompt, user_message, model,
                               max_tokens=max_tokens * 2)
        return None, (f"Could not parse JSON from response "
                      f"(stop_reason={response.stop_reason}): {text[:200]}")
    except Exception as e:
        return None, str(e)


def _extract_judge_json(text):
    """Extract a JSON object containing 'preferred' from a judge response."""
    # strict=False allows unescaped control characters (e.g. literal newlines)
    # inside strings — judges often format their reasoning with real newlines.
    def _loads(s):
        return json.loads(s, strict=False)

    # Try code blocks first.
    if "```json" in text:
        json_text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        json_text = text.split("```")[1].split("```")[0]
    else:
        json_text = text
    try:
        return _loads(json_text.strip())
    except json.JSONDecodeError:
        pass
    # Fallback: extract the outermost balanced JSON object containing "preferred".
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
            if depth == 0:
                candidate = text[start:end + 1]
                if '"preferred"' in candidate:
                    try:
                        return _loads(candidate)
                    except json.JSONDecodeError:
                        pass
                break
    # Last-resort recovery: judge wrote a partial/unclosed JSON object but the
    # top-level "preferred" verdict is still extractable. Lose the rationale,
    # keep the verdict — better than counting the case as an error.
    m = re.search(r'"preferred"\s*:\s*"(A|B|tie)"', text)
    if m:
        return {"preferred": m.group(1)}
    return None


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------

@dataclass
class Regression:
    judge_name: str
    metric: str
    baseline_value: str
    current_value: str
    detail: str = ""


def detect_regressions(current_results, thresholds, baseline_results=None):
    regressions = []
    for judge_name, threshold in thresholds.items():
        current = current_results.get(judge_name)
        if current is None:
            continue
        if "min_pass_rate" in threshold:
            rate = current.get("pass_rate", 1.0)
            if rate < threshold["min_pass_rate"]:
                regressions.append(Regression(judge_name, "pass_rate",
                                              f">= {threshold['min_pass_rate']}", str(rate)))
        if "min_mean" in threshold:
            mean = current.get("mean")
            if mean is not None and mean < threshold["min_mean"]:
                regressions.append(Regression(judge_name, "mean",
                                              f">= {threshold['min_mean']}", str(mean)))
        if "min_win_rate" in threshold:
            win_rate = current.get("win_rate", 0)
            if win_rate < threshold["min_win_rate"]:
                regressions.append(Regression(judge_name, "win_rate",
                                              f">= {threshold['min_win_rate']}", str(win_rate)))
        if baseline_results:
            baseline = baseline_results.get(judge_name)
            if baseline and current:
                for key in ("mean", "pass_rate"):
                    curr_val = current.get(key)
                    base_val = baseline.get(key)
                    if curr_val is not None and base_val is not None:
                        if curr_val < base_val - 0.5:
                            regressions.append(Regression(
                                judge_name, f"{key}_vs_baseline",
                                str(base_val), str(curr_val), "Degraded vs baseline"))
    return regressions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _get_case_dirs(run_id, runs_dir):
    cases_dir = runs_dir / run_id / "cases"
    if not cases_dir.exists():
        print(f"No cases directory: {cases_dir}", file=sys.stderr)
        sys.exit(1)
    return sorted(d for d in cases_dir.iterdir() if d.is_dir())


def _merge_summary(run_id, key, data, runs_dir=None):
    runs_dir = runs_dir or _get_runs_dir()
    summary_path = runs_dir / run_id / "summary.yaml"
    summary = {}
    if summary_path.exists():
        with open(summary_path) as f:
            summary = yaml.safe_load(f) or {}
    summary["run_id"] = run_id
    summary[key] = data
    with open(summary_path, "w") as f:
        yaml.dump(summary, f, default_flow_style=False, allow_unicode=True)


def compute_run_metrics(run_result):
    """Derive model/runner-level efficiency metrics from run_result.json.

    These are workload-agnostic: cost per turn, output tokens per turn,
    cache hit rate, and effective per-million-token prices. They stay
    flat across runs of the same model+effort, so they're useful for
    cross-model and cross-effort comparisons.

    Returns None if the required fields are missing.
    """
    if not run_result:
        return None
    cost = run_result.get("cost_usd")
    turns = run_result.get("num_turns")
    tokens = run_result.get("token_usage") or {}
    inp = tokens.get("input", 0) or 0
    out = tokens.get("output", 0) or 0
    cr = tokens.get("cache_read", 0) or 0
    cw = tokens.get("cache_create", 0) or 0
    total_in = inp + cr + cw

    total_tokens = total_in + out

    metrics = {}
    if isinstance(cost, (int, float)) and isinstance(turns, int) and turns > 0:
        metrics["cost_per_turn_usd"] = round(cost / turns, 6)
    if isinstance(turns, int) and turns > 0 and out:
        metrics["output_tokens_per_turn"] = round(out / turns, 2)
    if total_in > 0:
        metrics["cache_hit_rate"] = round(cr / total_in, 6)
    # Effective $/Mtok across all token types (input + cache_read + cache_create
    # + output), weighted by actual volume. Captures cache benefit: a high
    # cache_read share pulls this below the model's list price. Useful for
    # cross-model comparison at fixed effort and similar workload patterns.
    if isinstance(cost, (int, float)) and total_tokens > 0:
        metrics["cost_per_mtok_usd"] = round(cost / total_tokens * 1_000_000, 4)
    return metrics or None


def cmd_judges(args):
    config = EvalConfig.from_yaml(args.config)
    runs_dir = _get_runs_dir()
    case_dirs = _get_case_dirs(args.run_id, runs_dir)
    project_root = Path.cwd()

    judges = load_judges(config, project_root)
    print(f"Scoring {len(case_dirs)} cases with {len(judges)} judges: "
          f"{[n for n, *_ in judges]}")

    judge_results = score_cases(judges, case_dirs, config, run_id=args.run_id)

    for name, agg in judge_results.get("aggregated", {}).items():
        mean = agg.get("mean")
        rate = agg.get("pass_rate")
        if rate is not None:
            print(f"  {name}: pass_rate={rate:.1%}")
        elif mean is not None:
            print(f"  {name}: mean={mean:.2f}")

    _merge_summary(args.run_id, "judges", {
        name: {k: v for k, v in agg.items() if k != "values"}
        for name, agg in judge_results.get("aggregated", {}).items()
    }, runs_dir)
    _merge_summary(args.run_id, "per_case", judge_results.get("per_case", {}), runs_dir)

    # Workload-agnostic run metrics for cross-run / cross-model comparison
    rr_path = runs_dir / args.run_id / "run_result.json"
    if rr_path.exists():
        with open(rr_path) as f:
            run_result = json.load(f)
        run_metrics = compute_run_metrics(run_result)
        if run_metrics:
            _merge_summary(args.run_id, "run_metrics", run_metrics, runs_dir)
            for k, v in run_metrics.items():
                if "rate" in k:
                    print(f"  {k}: {v:.1%}")
                elif "cost" in k:
                    print(f"  {k}: ${v:.4f}")
                else:
                    print(f"  {k}: {v:,.1f}")

    # Regression detection
    has_regressions = False
    if config.thresholds:
        current_agg = judge_results.get("aggregated", {})
        regressions = detect_regressions(current_agg, config.thresholds)
        if regressions:
            has_regressions = True
            print(f"\n  REGRESSIONS: {len(regressions)} detected")
            for r in regressions:
                print(f"    [{r.judge_name}] {r.metric}: "
                      f"{r.baseline_value} -> {r.current_value}")
        else:
            print("\n  REGRESSIONS: 0")

    if has_regressions:
        sys.exit(1)


def cmd_pairwise(args):
    config = EvalConfig.from_yaml(args.config)
    runs_dir = _get_runs_dir()
    case_dirs = _get_case_dirs(args.run_id, runs_dir)
    case_ids = [d.name for d in case_dirs]

    run_dir = runs_dir / args.run_id
    baseline_dir = runs_dir / args.baseline

    if not baseline_dir.exists():
        print(f"Baseline not found: {baseline_dir}", file=sys.stderr)
        sys.exit(1)

    # Find pairwise judge config
    judge_name = args.judge
    pairwise_jc = None
    if judge_name:
        pairwise_jc = next((j for j in config.judges if j.name == judge_name), None)
    if not pairwise_jc:
        pairwise_jc = next((j for j in config.judges
                            if j.prompt or j.prompt_file), None)

    model = (args.model
             or (pairwise_jc.model if pairwise_jc else "")
             or config.models.judge
             or os.environ.get("EVAL_JUDGE_MODEL"))
    if not model:
        print("ERROR: no pairwise judge model configured. Set --model, "
              "pairwise judge 'model:', 'models.judge:' in eval.yaml, or "
              "EVAL_JUDGE_MODEL env var.", file=sys.stderr)
        sys.exit(1)
    prompt_file = args.prompt_file or (pairwise_jc.prompt_file if pairwise_jc else "")

    print(f"Pairwise comparison: {args.run_id} vs {args.baseline} "
          f"({len(case_ids)} cases, model={model})")

    result = compare_runs(
        run_dir, baseline_dir, config, case_ids,
        prompt=pairwise_jc.prompt if pairwise_jc else None,
        prompt_file=prompt_file,
        model=model,
    )

    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"  A wins: {result['wins_a']}")
    print(f"  B wins: {result['wins_b']}")
    print(f"  Ties:   {result['ties']}")
    print(f"  Errors: {result['errors']}")

    _merge_summary(args.run_id, "pairwise", result, runs_dir)


def cmd_regression(args):
    config = EvalConfig.from_yaml(args.config)
    runs_dir = _get_runs_dir()
    summary_path = runs_dir / args.run_id / "summary.yaml"
    if not summary_path.exists():
        print(f"No summary found. Run judges first.", file=sys.stderr)
        sys.exit(1)

    with open(summary_path) as f:
        summary = yaml.safe_load(f) or {}

    current_agg = summary.get("judges", {})
    baseline_agg = None
    if args.baseline:
        baseline_path = runs_dir / args.baseline / "summary.yaml"
        if baseline_path.exists():
            with open(baseline_path) as f:
                baseline_agg = (yaml.safe_load(f) or {}).get("judges", {})

    regressions = detect_regressions(current_agg, config.thresholds, baseline_agg)
    if regressions:
        print(f"REGRESSIONS: {len(regressions)} detected")
        for r in regressions:
            print(f"  [{r.judge_name}] {r.metric}: "
                  f"{r.baseline_value} -> {r.current_value}")
        sys.exit(1)
    else:
        print("REGRESSIONS: 0")


def main():
    parser = argparse.ArgumentParser(
        description="Scoring CLI for eval runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # judges
    jdg_p = subparsers.add_parser("judges", help="Run all judges")
    jdg_p.add_argument("--run-id", required=True)
    jdg_p.add_argument("--config", default="eval.yaml")

    # pairwise
    pw_p = subparsers.add_parser("pairwise", help="Pairwise comparison")
    pw_p.add_argument("--run-id", required=True)
    pw_p.add_argument("--baseline", required=True)
    pw_p.add_argument("--config", default="eval.yaml")
    pw_p.add_argument("--judge", default=None,
                      help="Name of judge from eval.yaml to use")
    pw_p.add_argument("--prompt-file", default=None,
                      help="Override comparison prompt file")
    pw_p.add_argument("--model", default=None,
                      help="Override judge model")

    # regression
    reg_p = subparsers.add_parser("regression", help="Threshold checks")
    reg_p.add_argument("--run-id", required=True)
    reg_p.add_argument("--config", default="eval.yaml")
    reg_p.add_argument("--baseline", default=None)

    args = parser.parse_args()
    {"judges": cmd_judges, "pairwise": cmd_pairwise,
     "regression": cmd_regression}[args.command](args)


if __name__ == "__main__":
    main()
