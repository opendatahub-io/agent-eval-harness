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

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

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

from agent_eval.config import EvalConfig, _is_valid_eval_name


def _get_runs_dir(eval_name: str = ""):
    """Get runs directory from env or default, optionally scoped by eval name."""
    base = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    if eval_name:
        if not _is_valid_eval_name(eval_name):
            raise ValueError(f"Invalid eval name for path: {eval_name!r}")
        return base / eval_name
    return base


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
    runs_dir = Path(runs_dir) if runs_dir else _get_runs_dir(
        config.skill if config else "")
    case_dir = Path(case_dir).resolve()
    record = {"files": {}, "tool_calls": [], "case_dir": str(case_dir)}

    # --- Annotations (from dataset case directory) ---
    record["annotations"] = {}
    case_id = case_dir.name
    if config.dataset.path:
        dataset_root = config.resolve_path(config.dataset.path).resolve()
        annotations_path = (dataset_root / case_id / "annotations.yaml").resolve()
        if (annotations_path.is_relative_to(dataset_root)
                and annotations_path.is_file()
                and not annotations_path.is_symlink()):
            try:
                with open(annotations_path) as f:
                    record["annotations"] = yaml.safe_load(f) or {}
            except (yaml.YAMLError, OSError):
                pass
            # Load annotation-referenced files into the record.
            # Only treat values as file paths if they look like filenames:
            # - Short enough to be a valid filename (< 256 chars)
            # - No newlines (multi-line strings are descriptions, not paths)
            # - No spaces at start/end (paths are usually trimmed)
            for key, val in record["annotations"].items():
                if isinstance(val, str) and not val.startswith("/"):
                    # Skip values that don't look like filenames
                    if len(val) > 255 or "\n" in val or val != val.strip():
                        continue
                    try:
                        ref_path = (dataset_root / case_id / val).resolve()
                    except OSError:
                        # Path resolution can fail for invalid characters
                        continue
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

    # --- Modified files (in-place edits collected by collect.py) ---
    _SKIP_MODIFIED_PREFIXES = {".work", "subagents", "hooks"}
    modified_dir = case_dir / "_modified"
    if modified_dir.exists():
        modified = {}
        for f in sorted(modified_dir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            _resolve_under(case_dir, f)
            rel = str(f.relative_to(modified_dir))
            if any(rel.startswith(pfx) for pfx in _SKIP_MODIFIED_PREFIXES):
                continue
            try:
                content = f.read_text()
                record["files"][f"_modified/{rel}"] = content
                modified[rel] = content
            except UnicodeDecodeError:
                record["files"][f"_modified/{rel}"] = {
                    "_binary": True, "path": str(f), "name": f.name}
        if modified:
            record["modified_files"] = modified

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

    # --- Events (structured event stream) ---
    events_path = case_dir / "events.json"
    if not events_path.exists() and run_id and runs_dir:
        events_path = runs_dir / run_id / "events.json"
    if events_path.exists():
        try:
            with open(events_path) as f:
                record["events"] = json.load(f)
            if not isinstance(record["events"], list):
                print(f"  Warning: events.json is not a list in {events_path}",
                      file=sys.stderr)
                record["events"] = []
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: malformed events.json in {events_path}: {e}",
                  file=sys.stderr)
            record["events"] = []
    else:
        record["events"] = []

    # --- Conversation text (convenience key for check judges) ---
    if record["events"]:
        from agent_eval.events import extract_conversation_text
        record["conversation"] = extract_conversation_text(record["events"])
    else:
        record["conversation"] = ""

    # --- Logs (if traces config enables them) ---
    if run_id:
        if config.traces.stdout:
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

    # --- Tool call outputs (derived from events, fallback to raw stdout) ---
    tool_outputs = [o for o in config.outputs if o.tool]
    if tool_outputs:
        events = record.get("events", [])
        if events:
            record["tool_calls"] = _extract_tool_calls_from_events(
                events, tool_outputs)
        else:
            stdout_text = ""
            if run_id:
                stdout_path = case_dir / "stdout.log"
                if not stdout_path.exists():
                    stdout_path = runs_dir / run_id / "stdout.log"
                if stdout_path.exists():
                    try:
                        stdout_text = stdout_path.read_text()
                    except OSError:
                        pass
            if stdout_text:
                record["tool_calls"] = _extract_tool_calls(
                    stdout_text, tool_outputs)

    return record


def _extract_tool_calls_from_events(events, tool_outputs):
    """Extract tool calls from structured events matching configured patterns."""
    tool_patterns = [o.tool for o in tool_outputs]
    calls = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        if event.get("parent_tool_use_id"):
            continue
        for tool in event.get("tools", []):
            name = tool.get("name", "")
            for pattern in tool_patterns:
                if pattern in name or name == pattern:
                    calls.append({
                        "name": name,
                        "input": tool.get("input", {}),
                    })
                    break
    return calls


def _extract_tool_calls(stdout_text, tool_outputs):
    """Extract tool calls from raw stream-json stdout (fallback when no events)."""
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
        if obj.get("parent_tool_use_id"):
            continue
        message = obj.get("message", {})
        for block in message.get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
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

class _OutputsProxy(dict):
    """Dict subclass whose __str__ renders files as formatted text.

    Provides backward compatibility for prompt templates using {{ outputs }}
    (bare variable) which expects formatted file listings, while allowing
    {{ outputs.files }}, {{ outputs.conversation }} etc. for structured access.
    """

    def __str__(self):
        files = self.get("files", {})
        parts = []
        for path, content in sorted(files.items()):
            if isinstance(content, dict) and content.get("_binary"):
                parts.append(f"\n### {path}\n\n<binary: {content['name']}>\n")
            else:
                parts.append(f"\n### {path}\n\n{content}\n")
        return "".join(parts)


def _render_jinja2_template(template_text, arguments, outputs):
    """Render a Jinja2 template with arguments and outputs as variables.

    Template variables available:
    - {{ outputs }} - formatted file listings (via __str__) or dict access
    - {{ outputs.files }}, {{ outputs.events }}, etc. - structured access
    - {{ arguments }} - judge arguments from eval.yaml
    - {{ annotations }} - formatted annotation text
    - {{ conversation }} - root-level assistant text from events
    """
    from jinja2 import Environment
    env = Environment()
    env.filters["tojson"] = lambda v: json.dumps(v, indent=2, default=str)

    out = _OutputsProxy(outputs or {})

    # Pre-render annotations as formatted text for {{ annotations }}
    ann = out.get("annotations", {})
    ann_text = ""
    for key, val in sorted(ann.items()):
        ann_text += f"- **{key}**: {val}\n"
    for key in sorted(out):
        if key.startswith("annotation_") and key.endswith("_content"):
            field = key[len("annotation_"):-len("_content")]
            ann_text += f"\n### {field} (file content)\n\n{out[key]}\n"

    # Pre-render conversation text for {{ conversation }}
    conversation = out.get("conversation", "")
    if not conversation and out.get("events"):
        from agent_eval.events import extract_conversation_text
        conversation = extract_conversation_text(out["events"])

    template = env.from_string(template_text)
    return template.render(
        arguments=arguments or {},
        outputs=out,
        annotations=ann_text,
        conversation=conversation,
    )


def load_judges(config, project_root=None):
    """Load all judges from config.

    Judge types (determined by which fields are set):
    - builtin: resolves via BuiltinJudgeRegistry
    - check: inline Python snippet
    - prompt/prompt_file: LLM judge
    - module/function: external code judge

    Returns list of (name, scorer, condition, judge_type) 4-tuples.
    """
    # Duplicate name validation
    seen_names = set()
    for jc in config.judges:
        if jc.name == "pairwise":
            continue
        if jc.name in seen_names:
            raise ValueError(f"Duplicate judge name '{jc.name}' in eval.yaml")
        seen_names.add(jc.name)

    registry = None
    judges = []
    for jc in config.judges:
        if jc.name == "pairwise":
            continue

        if jc.builtin:
            # Validate mutual exclusivity
            conflicting = [f for f in ("check", "prompt", "prompt_file",
                                       "module", "function")
                           if getattr(jc, f, "")]
            if conflicting:
                raise ValueError(
                    f"Judge '{jc.name}': 'builtin' is mutually exclusive "
                    f"with {', '.join(conflicting)}")
            # Lazy registry instantiation
            if registry is None:
                from agent_eval.judges import BuiltinJudgeRegistry
                registry = BuiltinJudgeRegistry()
                registry.discover()
            entry = registry.get(jc.builtin)
            scorer = _make_builtin_scorer(entry, jc, config)
            judge_type = "builtin"
        elif jc.check:
            scorer = _make_inline_check(jc)
            judge_type = "check"
        elif jc.prompt or jc.prompt_file:
            scorer = _load_llm_judge(jc, config, project_root)
            judge_type = "llm"
        elif jc.module and jc.function:
            scorer = _load_code_judge(jc, project_root)
            judge_type = "code"
        else:
            print(f"  Warning: judge '{jc.name}' has no check, prompt, or module",
                  file=sys.stderr)
            continue
        if scorer:
            judges.append((jc.name, scorer, jc.condition, judge_type))
    return judges


def _make_builtin_scorer(entry, jc, config):
    """Create a scorer callable from a BuiltinJudgeEntry."""
    if entry.kind == "python":
        fn = getattr(entry.module, entry.function_name)
        arguments = jc.arguments

        def scorer(outputs=None, **kwargs):
            return fn(outputs or {}, **arguments)

        return scorer

    elif entry.kind == "llm":
        prompt_text = entry.prompt_path.read_text()
        arguments = jc.arguments
        judge_model = _resolve_judge_model(jc, config)

        def scorer(outputs=None, **kwargs):
            out = outputs or {}
            rendered = _render_jinja2_template(prompt_text, arguments, out)
            images = _extract_images(out)
            raw = _call_judge_llm(rendered, judge_model,
                                  _BOOL_SYSTEM_PROMPT, images=images)
            return _parse_bool_response(raw)

        return scorer

    raise ValueError(f"Unknown builtin judge kind: {entry.kind}")


def _extract_images(outputs):
    """Extract base64-encoded images from binary file entries in outputs."""
    import base64
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    images = []
    for path, content in sorted((outputs or {}).get("files", {}).items()):
        if not isinstance(content, dict) or not content.get("_binary"):
            continue
        suffix = Path(path).suffix.lower()
        if suffix not in image_extensions:
            continue
        try:
            with open(content["path"], "rb") as img_f:
                b64 = base64.standard_b64encode(img_f.read()).decode()
            media_type = ("image/jpeg" if suffix in (".jpg", ".jpeg")
                          else f"image/{suffix.lstrip('.')}")
            images.append({"label": path, "media_type": media_type, "data": b64})
        except OSError:
            pass
    return images


_BOOL_SYSTEM_PROMPT = (
    "You are a judge evaluating agent outputs. "
    "Return a JSON object with 'passed' (boolean) and 'rationale' (string).")

_SCORE_SYSTEM_PROMPT = (
    "You are a judge evaluating skill outputs. "
    "Return a JSON object with 'score' (integer 1-5) and 'rationale' (string).")


def _call_judge_llm(prompt, model, system_prompt, images=None, max_tokens=4096):
    """Call the Anthropic API with a judge prompt. Returns raw response text."""
    client = _get_anthropic_client()
    if images:
        content_parts = [{"type": "text", "text": prompt}]
        for img in images:
            content_parts.append({"type": "text",
                                  "text": f"\n**Image: {img['label']}**"})
            content_parts.append({"type": "image", "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            }})
        user_message = content_parts
    else:
        user_message = prompt
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text.strip()


def _rationale_field(text):
    """Extract a JSON `rationale` string value, unescaped, or None.

    Escaped-quote-aware so the value isn't cut at the first embedded quote.
    """
    m = re.search(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"')
    except json.JSONDecodeError:
        return m.group(1)


def _parse_bool_response(text):
    """Parse {"passed": bool, "rationale": str} from LLM response.

    When no structured `rationale` field is present, fall back to the full
    response text (it renders as markdown in the report) rather than a
    200-char slice that truncates mid-word.
    """
    match = re.search(r'"passed"\s*:\s*(true|false)', text, re.IGNORECASE)
    if match:
        passed = match.group(1).lower() == "true"
        rationale = _rationale_field(text) or text.strip()
        return (passed, rationale)
    return (False, text.strip() or "Could not parse judge response")


def _parse_score_response(text):
    """Parse {"score": int, "rationale": str} from an LLM response, with fallbacks.

    Never truncates the rationale: when the judge returns prose instead of the
    requested JSON (observed with opus-4-8), the full response text is used as
    the rationale rather than a 200-char slice that cuts off mid-word.
    """
    # 1. Clean JSON object (handles escapes, newlines, embedded quotes).
    obj = _loads_json_object(text)
    if isinstance(obj, dict) and obj.get("score") is not None:
        try:
            rationale = str(obj.get("rationale") or "").strip() or text.strip()
            return (int(obj["score"]), rationale)
        except (ValueError, TypeError):
            pass
    # 2. Regex score + escaped-quote-aware rationale; full text if absent.
    match = re.search(r'"score"\s*:\s*(\d+)', text)
    if match:
        return (int(match.group(1)), _rationale_field(text) or text.strip())
    # 3. Prose fallbacks — keep the full text as the rationale.
    explicit = re.search(
        r'(?:overall|score|rating)\s*[=:]\s*(\d)\b'
        r'|(\d)\s*/\s*5'
        r'|\*\*(\d)\*\*\s*/\s*5',
        text, re.IGNORECASE)
    if explicit:
        score_val = int(next(g for g in explicit.groups() if g))
        return (score_val, text.strip())
    nums = re.findall(r'\b([1-5])\b', text)
    if nums:
        return (int(nums[-1]), text.strip())
    return (3, text.strip() or "Could not parse score")


def _loads_json_object(text):
    """Best-effort parse of a single JSON object from a response (code fences
    or surrounding prose tolerated). Returns a dict or None."""
    t = text.strip()
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', t, re.DOTALL)
    if fence:
        t = fence.group(1)
    for candidate in (t, t[t.find("{"):t.rfind("}") + 1] if "{" in t and "}" in t else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate, strict=False)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def score_cases(judges, case_dirs, config, run_id=None):
    """Score all cases with all judges in parallel."""
    if not case_dirs:
        return {"per_case": {}, "aggregated": {n: {"values": [], "mean": None, "pass_rate": None} for n, *_ in judges}}
    per_case = {}
    aggregated = {name: {"values": []} for name, *_ in judges}
    parallelism = min(len(case_dirs), os.cpu_count() or 4)
    lock = threading.Lock()
    completed = 0

    def _score_case(case_dir):
        case_id = case_dir.name
        record = load_case_record(case_dir, config, run_id=run_id)
        case_results = {}
        for name, scorer, condition, judge_type in judges:
            # Check condition — skip if it evaluates to False
            if condition:
                try:
                    annotations = record.get("annotations", {})
                    if not eval(condition, {"__builtins__": {}},
                                {"annotations": annotations, "outputs": record}):
                        case_results[name] = {
                            "value": None,
                            "rationale": f"Skipped: condition '{condition}' is false",
                            "judge_type": judge_type,
                        }
                        continue
                except Exception as e:
                    case_results[name] = {
                        "value": None,
                        "rationale": f"Condition error: {e}",
                        "judge_type": judge_type,
                    }
                    continue
            try:
                result = scorer(outputs=record)
                # Normalize — accepts (bool, str) tuples, Feedback, primitives
                if isinstance(result, tuple) and len(result) == 2:
                    case_results[name] = {
                        "value": result[0],
                        "rationale": result[1],
                        "judge_type": judge_type,
                    }
                elif hasattr(result, "value"):
                    case_results[name] = {
                        "value": result.value,
                        "rationale": getattr(result, "rationale", ""),
                        "judge_type": judge_type,
                    }
                elif isinstance(result, (bool, int, float, str)):
                    case_results[name] = {"value": result, "rationale": "",
                                          "judge_type": judge_type}
                else:
                    case_results[name] = {"value": result, "rationale": "",
                                          "judge_type": judge_type}
            except Exception as e:
                case_results[name] = {"value": None, "error": str(e),
                                      "judge_type": judge_type}
        return case_id, case_results

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_score_case, d): d for d in case_dirs}
        for future in as_completed(futures):
            completed += 1
            try:
                case_id, case_results = future.result()
            except Exception as e:
                case_dir = futures[future]
                case_id = case_dir.name
                case_results = {name: {"value": None, "error": str(e),
                                       "judge_type": jt}
                                for name, _, _, jt in judges}
                print(f"  [{completed}/{len(case_dirs)}] {case_id} ERROR: {e}",
                      file=sys.stderr, flush=True)
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
    arguments = jc.arguments
    wrapped = f"def _check(outputs, arguments):\n{textwrap.indent(source, '    ')}"
    code = compile(wrapped, f"<check:{jc.name}>", "exec")
    ns = {"__builtins__": __builtins__}
    exec(code, ns)
    check_fn = ns["_check"]

    def scorer(outputs=None, **kwargs):
        return check_fn(outputs or {}, arguments or {})

    return scorer


def _load_code_judge(jc, project_root=None):
    if project_root and str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    mod = importlib.import_module(jc.module)
    fn = getattr(mod, jc.function)
    if jc.arguments:
        arguments = jc.arguments

        def scorer(outputs=None, **kwargs):
            return fn(outputs=outputs, **arguments)

        return scorer
    return fn


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

    # Anthropic path (direct client, supports Vertex AI)
    if (os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or os.environ.get("ANTHROPIC_API_KEY")):
        judge_model = _resolve_judge_model(jc, config)
        if jc.feedback_type == "bool":
            system_prompt = _BOOL_SYSTEM_PROMPT
            parser = _parse_bool_response
        else:
            system_prompt = _SCORE_SYSTEM_PROMPT
            parser = _parse_score_response
        arguments = jc.arguments

        def scorer(outputs=None, **kwargs):
            out = outputs or {}
            rendered = _render_jinja2_template(prompt, arguments, out)
            images = _extract_images(out)
            raw = _call_judge_llm(rendered, judge_model, system_prompt,
                                  images=images)
            return parser(raw)

        return scorer

    # MLflow make_judge fallback (requires OpenAI-compatible API key)
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
    reasoning_ab: Optional[dict] = None
    reasoning_ba: Optional[dict] = None

    @property
    def winner(self) -> str:
        if self.error or not self.pref_ab or not self.pref_ba:
            return "error"
        if self.pref_ab == "A" and self.pref_ba == "B":
            return "A"
        elif self.pref_ab == "B" and self.pref_ba == "A":
            return "B"
        return "tie"

    @property
    def reasoning(self) -> Optional[str]:
        """Overall reasoning from the canonical (A=run_a) judge call.

        Judges don't reliably use the schema's `reasoning` key — observed
        variants include `analysis`, `rationale`, `explanation`, `scratchpad`,
        and `summary`. Search common key names and return the first non-empty
        string value so reasoning isn't silently dropped.
        """
        return _extract_reasoning_text(self.reasoning_ab)

    @property
    def dimensions(self) -> Optional[dict]:
        """Per-dimension judgments from the canonical (A=run_a) judge call.

        Tolerant of the `dimensions`/`scores` key-name variants judges use.
        """
        if isinstance(self.reasoning_ab, dict):
            for key in ("dimensions", "scores"):
                val = self.reasoning_ab.get(key)
                if isinstance(val, dict) and val:
                    return val
        return None


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

        # Render the FULL artifact set per side (task + review + feasibility +
        # auto-fix reports, etc.) — not just the first file. Using _first_content
        # here meant the judge never saw the review/feasibility files, so the
        # calibration and feasibility-depth dimensions could never be evaluated.
        output_a = _format_outputs_for_pairwise(record_a)
        output_b = _format_outputs_for_pairwise(record_b)

        if not output_a or not output_b:
            return PairwiseResult(case_id=case_id,
                                  error=f"Missing output: a={bool(output_a)}, b={bool(output_b)}")
        result = PairwiseResult(case_id=case_id)

        msg_ab = f"## Output A\n\n{output_a}\n\n## Output B\n\n{output_b}"
        pref_ab, err = _call_judge(client, comparison_prompt, msg_ab, model)
        if pref_ab:
            result.pref_ab = pref_ab.get("preferred")
            result.reasoning_ab = pref_ab
        else:
            result.error = f"AB failed: {err}"
            return result

        msg_ba = f"## Output A\n\n{output_b}\n\n## Output B\n\n{output_a}"
        pref_ba, err = _call_judge(client, comparison_prompt, msg_ba, model)
        if pref_ba:
            result.pref_ba = pref_ba.get("preferred")
            result.reasoning_ba = pref_ba
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
        "per_case": [{"case_id": r.case_id, "winner": r.winner, "error": r.error,
                      "reasoning": r.reasoning, "dimensions": r.dimensions}
                     for r in results],
    }


def _compute_pairwise_stability(runs):
    """Summarize judge stochasticity across repeated pairwise runs.

    `runs` is a list of compare_runs() result dicts. Returns per-run win/tie
    counts plus per-case verdict agreement: which cases gave the same verdict
    every run (stable) vs flipped, so readers can tell signal from noise.
    """
    from collections import Counter
    n = len(runs)
    # Per-case verdicts across runs, preserving case order from the first run.
    case_order = [pc["case_id"] for pc in runs[0].get("per_case", [])]
    verdicts = {cid: [] for cid in case_order}
    for r in runs:
        for pc in r.get("per_case", []):
            verdicts.setdefault(pc["case_id"], []).append(pc.get("winner", "error"))

    flipped = []
    stable = 0
    for cid in case_order:
        vs = verdicts.get(cid, [])
        if len(set(vs)) <= 1:
            stable += 1
        else:
            majority = Counter(vs).most_common(1)[0][0]
            flipped.append({"case_id": cid, "verdicts": vs, "majority": majority})
    total = len(case_order)
    return {
        "runs": n,
        "wins_a_counts": [r["wins_a"] for r in runs],
        "wins_b_counts": [r["wins_b"] for r in runs],
        "tie_counts": [r["ties"] for r in runs],
        "total_cases": total,
        "stable_cases": stable,
        "agreement_rate": (stable / total) if total else 0.0,
        "flipped_cases": flipped,
    }


def _format_outputs_for_pairwise(record):
    """Render the full set of skill-output files for a case as markdown.

    Mirrors how the regular LLM judges see {{ outputs }} (via _OutputsProxy):
    every artifact file (RFE task, review with rubric scores, feasibility
    review, auto-fix reports, originals) is included so the pairwise judge can
    actually evaluate the calibration and feasibility dimensions — not just the
    task file. Returns "" when the case produced no files.
    """
    files = record.get("files") or {}
    parts = []
    for path, content in sorted(files.items()):
        if isinstance(content, dict) and content.get("_binary"):
            parts.append(f"\n### {path}\n\n<binary: {content.get('name', '?')}>\n")
        else:
            parts.append(f"\n### {path}\n\n{content}\n")
    return "".join(parts)


_REASONING_KEYS = ("reasoning", "analysis", "rationale", "explanation",
                   "scratchpad", "summary", "justification", "notes")


def _extract_reasoning_text(parsed):
    """Pull the overall reasoning prose from a judge's JSON, tolerant of the
    field name. Judges paraphrase the schema (observed: `analysis`,
    `scratchpad`, `rationale`, …), so try known aliases, then fall back to the
    longest string value that isn't the verdict itself."""
    if not isinstance(parsed, dict):
        return None
    for key in _REASONING_KEYS:
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # Fallback: the longest free-text string field (excludes short verdicts
    # like "B"/"tie" and the 'preferred' key).
    best = None
    for k, v in parsed.items():
        if k == "preferred":
            continue
        if isinstance(v, str) and len(v.strip()) > 40:
            if best is None or len(v) > len(best):
                best = v
    return best


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


# Per-dimension verdict shape, reused for each comparison dimension.
_PAIRWISE_DIM_SCHEMA = {
    "type": "object",
    "properties": {
        "preferred": {"type": "string", "enum": ["A", "B", "tie", "n/a"]},
        "reasoning": {"type": "string",
                      "description": "Why this dimension favors A, B, or neither — cite specific content."},
    },
    "required": ["preferred", "reasoning"],
}

# Forced-output tool for the pairwise judge. Using tool_choice guarantees the
# model returns exactly these fields instead of improvising key names
# (observed: opus-4-8 emits `analysis`/`score_A`/`confidence` instead of the
# documented `reasoning`/`dimensions`, which silently drops the per-dimension
# breakdown). Structured output works uniformly across judge models.
_PAIRWISE_DIM_NAMES = ("rfe_quality", "calibration", "feasibility", "revision")
_PAIRWISE_TOOL = {
    "name": "submit_comparison",
    "description": ("Submit the blind pairwise comparison of outputs A and B. "
                    "Provide the overall verdict, thorough overall reasoning, and "
                    "a per-dimension breakdown."),
    "input_schema": {
        "type": "object",
        "properties": {
            "preferred": {"type": "string", "enum": ["A", "B", "tie"],
                          "description": "Which output is stronger overall."},
            "reasoning": {"type": "string",
                          "description": ("Thorough, self-contained overall reasoning — "
                                          "several sentences citing specific content from "
                                          "both outputs across RFE quality, assessment "
                                          "calibration, feasibility depth, and revision "
                                          "effectiveness. This is the primary field; make it "
                                          "complete on its own.")},
            "dimensions": {
                "type": "object",
                "description": "Per-dimension verdict and reasoning.",
                "properties": {
                    "rfe_quality": _PAIRWISE_DIM_SCHEMA,
                    "calibration": _PAIRWISE_DIM_SCHEMA,
                    "feasibility": _PAIRWISE_DIM_SCHEMA,
                    "revision": _PAIRWISE_DIM_SCHEMA,
                },
            },
        },
        "required": ["preferred", "reasoning"],
    },
}


def _normalize_pairwise_input(data):
    """Recombine the flat per-dimension fields into a `dimensions` dict.

    The tool schema is flat (top-level rfe_quality/calibration/feasibility/
    revision), but a model may also nest them under `dimensions`. Collect
    dimension-shaped objects ({preferred, reasoning}) from either location so
    downstream always sees `data["dimensions"]` as a dict.
    """
    dims = {}
    nested = data.get("dimensions")
    if isinstance(nested, dict):
        for k, v in nested.items():
            if isinstance(v, dict):
                dims[k] = v
    for name in _PAIRWISE_DIM_NAMES:
        v = data.pop(name, None)
        if isinstance(v, dict):
            dims[name] = v
    if dims:
        data["dimensions"] = dims
    elif not isinstance(data.get("dimensions"), dict):
        data.pop("dimensions", None)
    return data


def _call_judge(client, system_prompt, user_message, model, max_tokens=16384):
    try:
        response = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=("You are a blind judge comparing two outputs, A and B. "
                    "Call the submit_comparison tool exactly once with your verdict "
                    "and reasoning. Put ALL of your reasoning inside the tool input — "
                    "do not write any text outside the tool call."),
            tools=[_PAIRWISE_TOOL],
            tool_choice={"type": "tool", "name": "submit_comparison"},
            messages=[
                {"role": "user", "content": f"{system_prompt}\n\n{user_message}"},
            ],
        )
        # Preferred path: read the forced tool_use block directly — no text
        # parsing, no improvised keys.
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_comparison":
                return _normalize_pairwise_input(dict(block.input)), None
        # Fallback: model emitted text despite tool_choice (rare) — parse it.
        text = "".join(getattr(b, "text", "") for b in response.content
                       if getattr(b, "type", None) == "text")
        parsed = _extract_judge_json(text) if text else None
        if parsed is not None:
            return parsed, None
        # Retry once with a larger budget if the response was truncated.
        if response.stop_reason == "max_tokens" and max_tokens < 32768:
            return _call_judge(client, system_prompt, user_message, model,
                               max_tokens=max_tokens * 2)
        return None, (f"No submit_comparison tool_use in response "
                      f"(stop_reason={response.stop_reason})")
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
    # The model is instructed to return only JSON, so the object usually spans
    # the first '{' to the last '}'. Try that whole span — robust to a stray
    # leading/trailing sentence the model occasionally adds.
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        try:
            return _loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass
    # Fallback: scan for a balanced JSON object containing "preferred", tracking
    # string state so braces *inside* string values (e.g. "{cluster}-autoscaler"
    # echoed from feasibility content) don't throw off the depth counter.
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        escaped = False
        for end in range(start, len(text)):
            ch = text[end]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
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
    # top-level "preferred" verdict is still extractable. Try to also recover the
    # overall reasoning string so the verdict isn't left rationale-less.
    m = re.search(r'"preferred"\s*:\s*"(A|B|tie)"', text)
    if m:
        recovered = {"preferred": m.group(1)}
        rm = re.search(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if rm:
            try:
                recovered["reasoning"] = json.loads(f'"{rm.group(1)}"')
            except json.JSONDecodeError:
                recovered["reasoning"] = rm.group(1)
        return recovered
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
    runs_dir = _get_runs_dir(config.skill)
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
    runs_dir = _get_runs_dir(config.skill)
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

    repeat = max(1, getattr(args, "repeat", 1) or 1)
    suffix = f", repeat={repeat}" if repeat > 1 else ""
    print(f"Pairwise comparison: {args.run_id} vs {args.baseline} "
          f"({len(case_ids)} cases, model={model}{suffix})")

    runs = []
    for i in range(repeat):
        if repeat > 1:
            print(f"  --- repeat {i + 1}/{repeat} ---")
        r = compare_runs(
            run_dir, baseline_dir, config, case_ids,
            prompt=pairwise_jc.prompt if pairwise_jc else None,
            prompt_file=prompt_file,
            model=model,
        )
        if "error" in r:
            print(f"ERROR: {r['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"  A wins: {r['wins_a']} | B wins: {r['wins_b']} | "
              f"Ties: {r['ties']} | Errors: {r['errors']}")
        runs.append(r)

    # The first run is the primary (its per-case reasoning is rendered).
    result = runs[0]
    if repeat > 1:
        result["stability"] = _compute_pairwise_stability(runs)
        st = result["stability"]
        print(f"  Stability over {repeat} runs: "
              f"B wins {st['wins_b_counts']}, ties {st['tie_counts']}; "
              f"{st['stable_cases']}/{st['total_cases']} cases gave the same "
              f"verdict every run ({st['agreement_rate']:.0%} agreement)")
        if st["flipped_cases"]:
            print("  Flipped cases:")
            for fc in st["flipped_cases"]:
                print(f"    {fc['case_id']}: {'/'.join(fc['verdicts'])} "
                      f"(majority {fc['majority']})")

    _merge_summary(args.run_id, "pairwise", result, runs_dir)


def cmd_regression(args):
    config = EvalConfig.from_yaml(args.config)
    runs_dir = _get_runs_dir(config.skill)
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
    jdg_p.add_argument("--config", required=True)

    # pairwise
    pw_p = subparsers.add_parser("pairwise", help="Pairwise comparison")
    pw_p.add_argument("--run-id", required=True)
    pw_p.add_argument("--baseline", required=True)
    pw_p.add_argument("--config", required=True)
    pw_p.add_argument("--judge", default=None,
                      help="Name of judge from eval.yaml to use")
    pw_p.add_argument("--prompt-file", default=None,
                      help="Override comparison prompt file")
    pw_p.add_argument("--model", default=None,
                      help="Override judge model")
    pw_p.add_argument("--repeat", type=int, default=1,
                      help="Run the comparison N times and record verdict "
                           "stability (tie/win variance + per-case agreement)")

    # regression
    reg_p = subparsers.add_parser("regression", help="Threshold checks")
    reg_p.add_argument("--run-id", required=True)
    reg_p.add_argument("--config", required=True)
    reg_p.add_argument("--baseline", default=None)

    args = parser.parse_args()
    {"judges": cmd_judges, "pairwise": cmd_pairwise,
     "regression": cmd_regression}[args.command](args)


if __name__ == "__main__":
    main()
