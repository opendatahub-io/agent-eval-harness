"""Deterministic dataset audit engine (V1 task-generation validity).

Shared by the eval-dataset CLI (``skills/eval-dataset/scripts/audit_dataset.py``)
and the soft execution-path preflights (``skills/eval-run/scripts/workspace.py``,
``agent_eval/harbor/tasks.py``).

Scoring-path purity: stdlib + yaml only — no scipy/pandas/pingouin. It may
import :mod:`agent_eval.config` (also stdlib + yaml).

Design notes:

- All audit checks are deterministic; findings are triage input for the agent
  or a human, never a gate (the CLI exits 0 unless ``--strict``).
- ``dataset_audit.yaml`` and ``manifest.yaml`` live at the dataset ROOT as
  files. Case discovery is directory-only everywhere (workspace.py, collect.py,
  harbor/tasks.py, evalhub adapter, sync_dataset.py), so root-level files are
  invisible to it by construction — :func:`iter_case_dirs` mirrors those sites.
- ``write_audit`` is load-and-merge: it replaces only the audit-owned top-level
  keys and preserves foreign keys (e.g. ``null_probe``) so other tools can
  merge their own sections into the same file.
- The null-agent solvability probe (:func:`audit_null_run` /
  :func:`write_null_probe`) is the inverse merge: it replaces ONLY the
  ``null_probe`` key and preserves the audit-owned sections, so the two write
  paths round-trip in both directions.
- Honest labels: reference resolution is a NECESSARY-not-sufficient condition
  for answerability; contamination detection is verbatim/normalized-substring
  only (paraphrased leakage is out of scope).
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

AUDIT_FILENAME = "dataset_audit.yaml"
MANIFEST_FILENAME = "manifest.yaml"

#: Canonical answer-key/metadata annotation fields. Moved here from
#: generate_synthetic.py so the audit and the generator cannot diverge
#: (generate_synthetic keeps a back-compat alias).
ANNOTATION_FIELDS = {
    "expected_files", "expected_mentions", "expected_rejection",
    "expected_guidance", "expected_constraint", "expected_structure",
    "expected_patterns", "expected_api", "expected_example_type",
    "expected_fields", "expected_components", "expected_interactions",
    "correct_approach", "category", "difficulty", "severity",
    "constraint_type", "topic",
}

#: Annotation fields whose values are short classification labels, not answer
#: content — never scanned for contamination.
CONTAMINATION_EXCLUDED_FIELDS = {
    "category", "difficulty", "severity", "constraint_type", "topic",
}

#: Annotation fields whose entries are documentation/repo paths (the
#: reference-resolution check target).
PATH_ANNOTATION_FIELDS = ("expected_files",)

#: Minimum normalized length for an answer-key string to count as a leak —
#: short labels like "easy" or "yes" are never flagged.
MIN_LEAK_LEN = 12

DEFAULT_DUPLICATE_THRESHOLD = 0.85
DEFAULT_DIFFICULTY_VALUES = ("easy", "medium", "hard")

#: Top-level keys owned (replaced) by :func:`write_audit`. Every other
#: top-level key in dataset_audit.yaml is foreign and preserved verbatim.
AUDIT_OWNED_KEYS = (
    "audit_version", "generated_at", "dataset_path", "parameters",
    "checks", "summary", "cases", "case_hashes",
)

REFERENCE_RESOLUTION_LABEL = (
    "reference resolution — necessary, not sufficient, for answerability; "
    "a path that exists is not thereby verified to answer the question"
)
CONTAMINATION_LABEL = (
    "verbatim/normalized-substring only — paraphrased leakage is out of scope"
)

#: The null-probe statistic label — verbatim everywhere it renders. Under LLM
#: judges the probe is a JOINT task/judge measure, not the paper's pure-V1
#: figure: a null-pass means a degenerate case OR a vacuous judge.
NULL_PROBE_LABEL = (
    "null-pass rate (joint task/judge non-discriminativeness, "
    "upper-bounds 1−V1)"
)

#: Fixed reward threshold for flagging a null-pass via the recomputed
#: composite reward. Deliberately NOT derived from thresholds/normalization
#: semantics — override per invocation with ``--reward-threshold``.
DEFAULT_NULL_REWARD_THRESHOLD = 0.5

_CHECK_NAMES = (
    "structural", "argument_fields", "reference_resolution", "contamination",
    "near_duplicates", "composition", "conditional_judges",
)


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------

@dataclass
class CaseData:
    """Everything the audit checks need from one case directory."""

    case_id: str
    path: Path
    files: list = field(default_factory=list)  # relative paths (str), sorted
    input_path: Optional[Path] = None
    input_data: object = None  # parsed input file content (usually dict)
    input_text: str = ""  # raw input file text
    annotations: dict = field(default_factory=dict)
    answers: object = None  # parsed answers.yaml (None when absent)
    parse_errors: list = field(default_factory=list)


def iter_case_dirs(dataset_root: Path) -> list:
    """Case directories under the dataset root — directories only, sorted.

    Exactly mirrors the discovery sites (workspace.py, collect.py,
    harbor/tasks.py, evalhub adapter, sync_dataset.py) so that root-level
    files like dataset_audit.yaml / manifest.yaml are invisible to all of
    them by construction.
    """
    root = Path(dataset_root)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir())


def _case_files(case_dir: Path) -> list:
    """Sorted relative paths of regular files in a case dir (symlinks skipped)."""
    files = []
    for p in sorted(Path(case_dir).rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        files.append(str(p.relative_to(case_dir)))
    return sorted(files)


def load_case(case_dir: Path) -> CaseData:
    """Load one case directory. Parse failures are recorded, never raised."""
    case_dir = Path(case_dir)
    case = CaseData(case_id=case_dir.name, path=case_dir,
                    files=_case_files(case_dir))

    # Input file — same preference order as workspace._find_input_file.
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            case.input_path = candidate
            break
    if case.input_path is not None:
        try:
            case.input_text = case.input_path.read_text(errors="replace")
        except OSError as e:
            case.parse_errors.append(f"{case.input_path.name}: {e}")
        if case.input_text.strip():
            try:
                if case.input_path.suffix == ".json":
                    case.input_data = json.loads(case.input_text)
                else:
                    case.input_data = yaml.safe_load(case.input_text)
            except Exception as e:
                case.parse_errors.append(f"{case.input_path.name}: {e}")

    ann_path = case_dir / "annotations.yaml"
    if ann_path.is_file():
        try:
            loaded = yaml.safe_load(ann_path.read_text(errors="replace"))
            if isinstance(loaded, dict):
                case.annotations = loaded
            elif loaded is not None:
                case.parse_errors.append("annotations.yaml: not a mapping")
        except Exception as e:
            case.parse_errors.append(f"annotations.yaml: {e}")

    ans_path = case_dir / "answers.yaml"
    if ans_path.is_file():
        try:
            case.answers = yaml.safe_load(ans_path.read_text(errors="replace"))
        except Exception as e:
            case.parse_errors.append(f"answers.yaml: {e}")

    return case


def case_content_hash(case_dir: Path) -> str:
    """sha256 over sorted (relpath, contents) of a case dir's regular files.

    Content hashes — never dir mtimes — so an in-place edit of
    ``case-001/input.yaml`` (which does not change the POSIX dir mtime) is
    caught by the workspace preflight. Symlinks are skipped.
    """
    case_dir = Path(case_dir)
    h = hashlib.sha256()
    entries = []
    for p in case_dir.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        entries.append((str(p.relative_to(case_dir)), p))
    for rel, p in sorted(entries):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\x00")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase + collapse all whitespace (the verbatim-match normalization)."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _leaf_strings(value):
    """Yield every string leaf of a nested dict/list/scalar structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _leaf_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _leaf_strings(v)


def _finding(case_id, message, severity="warning", **extra):
    entry = {"case": case_id, "severity": severity, "message": message}
    entry.update(extra)
    return entry


def _block(findings, status=None, **extra):
    block = {"status": status or ("ok" if not findings else "findings"),
             "finding_count": len(findings)}
    block.update(extra)
    block["findings"] = findings
    return block


def _skipped(reason, **extra):
    block = {"status": "skipped", "reason": reason, "finding_count": 0}
    block.update(extra)
    block["findings"] = []
    return block


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_structural(cases) -> dict:
    """Mechanizes SKILL.md Step 6 item 5 (subset): empty files, parse failures."""
    findings = []
    for case in cases:
        if case.input_path is None:
            findings.append(_finding(
                case.case_id, "no input.{yaml,yml,json} file", severity="error"))
        for err in case.parse_errors:
            findings.append(_finding(
                case.case_id, f"parse failure: {err}", severity="error"))
        for rel in case.files:
            try:
                content = (case.path / rel).read_bytes()
            except OSError:
                continue
            if not content.strip():
                findings.append(_finding(
                    case.case_id,
                    f"empty or whitespace-only file: {rel}", file=rel))
    return _block(findings)


def check_argument_fields(cases, config) -> dict:
    """Mechanizes SKILL.md Step 6 item 3: argument-template field presence.

    Only applies when ``execution.mode == "case"`` with a non-empty
    ``execution.arguments``. Brace templates (``{field}`` / ``{field?}``) are
    checked exactly; Jinja templates get best-effort ``input.<field>``
    extraction, and any complex construct (``input.get(``, ``input[``,
    filters) makes the whole check indeterminate rather than guessing.
    """
    execution = getattr(config, "execution", None)
    mode = getattr(execution, "mode", "case") or "case"
    arguments = getattr(execution, "arguments", "") or ""
    if mode != "case" or not arguments.strip():
        return _skipped(
            "only case-mode argument templates are checked "
            "(execution.mode != 'case' or no execution.arguments)")

    if "{{" in arguments or "{%" in arguments:
        required = set(re.findall(r"input\.([A-Za-z_]\w*)", arguments))
        required.discard("get")
        optional = set()
        if any(marker in arguments
               for marker in ("input.get(", "input[", "|", "{%")):
            return _skipped(
                "Jinja template uses complex expressions — field presence is "
                "indeterminate; verify manually",
                status="indeterminate",
                template=arguments,
                extracted_fields=sorted(required))
    else:
        required, optional = set(), set()
        for token in re.findall(r"\{([\w-]+\??)\}", arguments):
            (optional if token.endswith("?") else required).add(
                token.rstrip("?"))

    findings = []
    for case in cases:
        if case.input_data is None:
            continue  # structural check already flags missing/unparseable input
        data = case.input_data if isinstance(case.input_data, dict) else {}
        for name in sorted(required):
            if name not in data:
                findings.append(_finding(
                    case.case_id,
                    f"required argument field '{name}' missing from "
                    f"{case.input_path.name if case.input_path else 'input'}",
                    field=name))
            else:
                value = data[name]
                if value is None or (isinstance(value, str)
                                     and not value.strip()):
                    findings.append(_finding(
                        case.case_id,
                        f"required argument field '{name}' is empty",
                        field=name))
    return _block(findings, template=arguments,
                  required_fields=sorted(required),
                  optional_fields=sorted(optional))


def check_reference_resolution(cases, config) -> dict:
    """Resolve path-bearing annotation entries against the documentation root.

    NECESSARY-not-sufficient: an ``annotations.expected_files`` entry that
    does not exist can never answer the question, but existence does not
    verify that it does — the label states this explicitly.

    Roots: the eval.yaml directory (``config.config_dir`` — the same
    convention validate_eval.py uses for ``generation.context``'s
    ``entry_point``), plus the entry_point's parent directory when
    ``generation.context.documentation_structure.entry_point`` carries one.
    Silently skipped (status ``skipped``, no findings) when no root is
    derivable or no case declares a path-bearing annotation field.
    """
    declared = []  # (case, field, [entries])
    for case in cases:
        for field_name in PATH_ANNOTATION_FIELDS:
            value = case.annotations.get(field_name)
            if isinstance(value, str):
                entries = [value]
            elif isinstance(value, (list, tuple)):
                entries = [e for e in value if isinstance(e, str)]
            else:
                entries = []
            entries = [e.strip() for e in entries if e and e.strip()]
            if entries:
                declared.append((case, field_name, entries))

    if not declared:
        return _skipped(
            "no path-bearing annotation fields "
            f"({', '.join(PATH_ANNOTATION_FIELDS)}) declared by any case",
            label=REFERENCE_RESOLUTION_LABEL)

    config_dir = getattr(config, "config_dir", None)
    if config_dir is None:
        return _skipped(
            "no documentation/repo root derivable (config_dir unknown)",
            label=REFERENCE_RESOLUTION_LABEL)

    roots = [Path(config_dir)]
    generation = getattr(config, "generation", None)
    context = getattr(generation, "context", None)
    if isinstance(context, dict):
        doc_structure = context.get("documentation_structure")
        entry_point = (doc_structure.get("entry_point", "")
                       if isinstance(doc_structure, dict) else "")
        if isinstance(entry_point, str) and entry_point:
            ep = (Path(entry_point) if Path(entry_point).is_absolute()
                  else Path(config_dir) / entry_point)
            if ep.parent not in roots:
                roots.append(ep.parent)

    findings = []
    checked = 0
    for case, field_name, entries in declared:
        for entry in entries:
            checked += 1
            p = Path(entry)
            candidates = [p] if p.is_absolute() else [r / entry for r in roots]
            if not any(c.exists() for c in candidates):
                findings.append(_finding(
                    case.case_id,
                    f"annotations.{field_name} entry does not resolve: "
                    f"{entry}",
                    field=field_name, entry=entry))
    return _block(findings, label=REFERENCE_RESOLUTION_LABEL,
                  roots=[str(r) for r in roots], entries_checked=checked)


def _answer_key_values(case):
    """(source, normalized-needle) pairs of answer-key content for one case."""
    pairs = []
    for key, value in case.annotations.items():
        if key in CONTAMINATION_EXCLUDED_FIELDS:
            continue
        for leaf in _leaf_strings(value):
            pairs.append((f"annotations.{key}", leaf))
    if case.answers is not None:
        for leaf in _leaf_strings(case.answers):
            pairs.append(("answers.yaml", leaf))
    out = []
    seen = set()
    for source, leaf in pairs:
        needle = _normalize(leaf)
        if len(needle) < MIN_LEAK_LEN:
            continue
        if (source, needle) in seen:
            continue
        seen.add((source, needle))
        out.append((source, needle))
    return out


def _agent_visible_files(case, config):
    """(relative-name, raw-text) of files the agent sees in the workspace.

    The input file plus companion files under ``dataset.workspace.files``
    roots inside the case dir. ``answers.yaml`` is NEVER part of this
    surface: its workspace copy is sanctioned (workspace.py copies it for
    the AskUserQuestion hook), so its presence is never a finding — only
    leakage INTO input/companion files counts.
    """
    surfaces = []
    if case.input_path is not None and case.input_text:
        surfaces.append((case.input_path.name, case.input_text))

    dataset = getattr(config, "dataset", None)
    workspace = getattr(dataset, "workspace", None)
    for entry in (getattr(workspace, "files", None) or []):
        src = case.path / entry
        if src.is_symlink():
            continue
        if src.is_file():
            candidates = [src]
        elif src.is_dir():
            candidates = sorted(
                p for p in src.rglob("*")
                if p.is_file() and not p.is_symlink())
        else:
            continue
        for p in candidates:
            rel = str(p.relative_to(case.path))
            if p.name == "answers.yaml":
                continue  # sanctioned copy — never scanned as agent-visible
            try:
                surfaces.append((rel, p.read_text(errors="replace")))
            except OSError:
                continue
    return surfaces


def check_contamination(cases, config) -> dict:
    """Answer-key leakage into agent-visible files (verbatim only).

    Three finding kinds:
    - normalized-verbatim occurrence of ``answers.yaml`` / non-label
      annotation content inside input.yaml or companion files;
    - answer-key fields (ANNOTATION_FIELDS) present in input.yaml — extends
      the generation-only ``_fix_misplaced_annotation_fields`` guard to
      hand-authored cases;
    - leftover ``TODO_`` placeholders (informational — they must be replaced
      with real values before /eval-run).
    """
    findings = []
    for case in cases:
        surfaces = _agent_visible_files(case, config)
        needles = _answer_key_values(case)
        for surface_name, raw_text in surfaces:
            haystack = _normalize(raw_text)
            for source, needle in needles:
                if needle in haystack:
                    findings.append(_finding(
                        case.case_id,
                        f"answer-key content from {source} appears verbatim "
                        f"in agent-visible {surface_name}",
                        source=source, surface=surface_name))
            for placeholder in sorted(set(
                    re.findall(r"TODO_[A-Z0-9_]+", raw_text))):
                findings.append(_finding(
                    case.case_id,
                    f"leftover placeholder {placeholder} in {surface_name} — "
                    "replace with a real value before /eval-run",
                    severity="info",
                    surface=surface_name, placeholder=placeholder))
        if isinstance(case.input_data, dict):
            misplaced = sorted(ANNOTATION_FIELDS & set(case.input_data))
            for name in misplaced:
                findings.append(_finding(
                    case.case_id,
                    f"answer-key/annotation field '{name}' present in "
                    f"{case.input_path.name if case.input_path else 'input'} "
                    "— move it to annotations.yaml",
                    field=name))
    return _block(findings, label=CONTAMINATION_LABEL)


def _token_jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"\w+", a))
    tb = set(re.findall(r"\w+", b))
    if not ta and not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def check_near_duplicates(cases, threshold=DEFAULT_DUPLICATE_THRESHOLD) -> dict:
    """Pairwise similarity over normalized case input text.

    A pair is flagged when max(difflib sequence ratio, token-set Jaccard)
    >= *threshold*. Findings are warnings listing the pair + scores.
    """
    findings = []
    texts = [(case.case_id, _normalize(case.input_text))
             for case in cases if _normalize(case.input_text)]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a_id, a = texts[i]
            b_id, b = texts[j]
            jaccard = _token_jaccard(a, b)
            sequence = 0.0
            matcher = difflib.SequenceMatcher(None, a, b)
            if (matcher.real_quick_ratio() >= threshold
                    and matcher.quick_ratio() >= threshold):
                sequence = matcher.ratio()
            score = max(sequence, jaccard)
            if score >= threshold:
                findings.append({
                    "cases": [a_id, b_id],
                    "severity": "warning",
                    "similarity": round(score, 3),
                    "metric": ("sequence" if sequence >= jaccard
                               else "jaccard"),
                    "sequence_ratio": round(sequence, 3),
                    "jaccard": round(jaccard, 3),
                    "message": (f"near-duplicate inputs: {a_id} ~ {b_id} "
                                f"(similarity {score:.3f})"),
                })
    return _block(findings, threshold=threshold, pairs_compared=(
        len(texts) * (len(texts) - 1) // 2))


def check_composition(cases, config, difficulty_values=None) -> dict:
    """Category/difficulty composition vs the declared generation seeds.

    - ``by_category``: realized counts (from ``annotations.category``, the
      derived category channel), including a ``(none)`` bucket.
    - For synthetic configs with seeds: per-seed requested vs realized, plus
      categories realized but declared by no seed.
    - Difficulty is PRESENCE-CONDITIONAL: the distribution and vocabulary are
      computed only over cases that carry ``annotations.difficulty``; a
      dataset without the field never warns.
    """
    vocabulary = tuple(difficulty_values or DEFAULT_DIFFICULTY_VALUES)
    findings = []
    by_category = Counter(
        str(case.annotations.get("category") or "(none)") for case in cases)
    block_extra = {"by_category": dict(sorted(by_category.items()))}

    generation = getattr(config, "generation", None)
    seeds = getattr(generation, "seeds", None) or []
    if getattr(generation, "strategy", "") == "synthetic" and seeds:
        seed_table = []
        for seed in seeds:
            category = getattr(seed, "category", "")
            requested = getattr(seed, "count", 0)
            realized = by_category.get(category, 0)
            seed_table.append({"category": category,
                               "requested": requested,
                               "realized": realized})
            if realized != requested:
                findings.append({
                    "severity": "warning", "category": category,
                    "message": (f"category '{category}': requested "
                                f"{requested}, realized {realized}"),
                })
        declared = {getattr(s, "category", "") for s in seeds}
        for category in sorted(set(by_category) - declared - {"(none)"}):
            findings.append({
                "severity": "warning", "category": category,
                "message": (f"category '{category}' realized "
                            f"({by_category[category]} case(s)) but declared "
                            "by no generation seed"),
            })
        block_extra["seeds"] = seed_table

    with_difficulty = [case for case in cases
                       if "difficulty" in case.annotations]
    if with_difficulty:
        distribution = Counter(
            str(case.annotations.get("difficulty"))
            for case in with_difficulty)
        block_extra["by_difficulty"] = dict(sorted(distribution.items()))
        for value, count in sorted(distribution.items()):
            if value not in vocabulary:
                findings.append({
                    "severity": "warning", "difficulty": value,
                    "message": (f"difficulty '{value}' ({count} case(s)) is "
                                f"outside the vocabulary {list(vocabulary)}"),
                })
    # No difficulty field anywhere → no distribution, and NEVER a warning.
    return _block(findings, **block_extra)


def condition_scope(condition: str) -> str:
    """Classify a judge ``if:`` condition for branch-coverage analysis.

    - ``templated``: contains ``{{`` — indeterminate wholesale.
    - ``annotations-only``: parses and references only ``annotations``.
    - ``outputs-dependent``: references ``outputs`` (execution results that
      do not exist at dataset time) — indeterminate, never uncovered.
    - ``invalid``: syntax error or a name score.py would not bind.
    """
    if "{{" in condition:
        return "templated"
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError:
        return "invalid"
    names = {node.id for node in ast.walk(tree)
             if isinstance(node, ast.Name)}
    if "outputs" in names:
        return "outputs-dependent" if names <= {"annotations", "outputs"} \
            else "invalid"
    if names <= {"annotations"}:
        return "annotations-only"
    return "invalid"


def evaluate_condition(condition: str, annotations: dict) -> bool:
    """Evaluate an annotations-only judge condition for one case.

    Same evaluation semantics as score.py's per-case condition check —
    ``eval(condition, {"__builtins__": {}}, {"annotations": ..., "outputs":
    ...})`` — restricted to the ``annotations`` binding (only
    annotations-only conditions reach this function, so ``outputs`` is
    irrelevant). tests/test_dataset_audit.py cross-pins this against
    score.py's form on a shared condition table.
    """
    return bool(eval(condition, {"__builtins__": {}},
                     {"annotations": annotations}))


def check_conditional_judges(cases, config) -> dict:
    """Branch reachability of conditional judges over the dataset.

    Restricted to judges whose ``if:`` condition references only
    ``annotations``. Conditions referencing ``outputs`` or containing ``{{``
    templates are reported INDETERMINATE wholesale — never as uncovered.
    """
    findings = []
    rows = []
    for judge in (getattr(config, "judges", None) or []):
        condition = getattr(judge, "condition", "") or ""
        if not condition:
            continue
        name = getattr(judge, "name", "") or "?"
        scope = condition_scope(condition)
        row = {"judge": name, "condition": condition, "scope": scope}
        if scope in ("outputs-dependent", "templated"):
            row["coverage"] = "indeterminate"
            row["reason"] = (
                "condition depends on execution results that do not exist "
                "at dataset time" if scope == "outputs-dependent"
                else "condition contains a {{ template — not analyzable "
                     "at dataset time")
            rows.append(row)
            continue
        if scope == "invalid":
            row["coverage"] = "indeterminate"
            findings.append({
                "severity": "warning", "judge": name,
                "message": (f"judge '{name}': condition {condition!r} could "
                            "not be analyzed (syntax error or unknown name) "
                            "— it will error at scoring time"),
            })
            rows.append(row)
            continue
        true_n = false_n = error_n = 0
        for case in cases:
            try:
                if evaluate_condition(condition, case.annotations):
                    true_n += 1
                else:
                    false_n += 1
            except Exception:
                error_n += 1
        if true_n and false_n:
            coverage = "both"
        elif error_n and not true_n and not false_n:
            coverage = "all-error"
        elif true_n:
            coverage = "always-runs"
        else:
            coverage = "never-runs"
        row.update({"coverage": coverage, "true": true_n,
                    "false": false_n, "errors": error_n})
        if coverage == "never-runs":
            findings.append({
                "severity": "warning", "judge": name,
                "message": (f"judge '{name}' never runs — its condition is "
                            "false for every case; add a case where it is "
                            "true"),
            })
        elif coverage == "always-runs":
            findings.append({
                "severity": "warning", "judge": name,
                "message": (f"judge '{name}' always runs — its condition is "
                            "true for every case; the false branch is "
                            "uncovered"),
            })
        elif coverage == "all-error":
            findings.append({
                "severity": "warning", "judge": name,
                "message": (f"judge '{name}': condition errored on every "
                            "case"),
            })
        rows.append(row)
    if not rows:
        return _skipped("no judges with an `if:` condition", judges=[])
    return _block(findings, judges=rows)


# ---------------------------------------------------------------------------
# Orchestration + persistence
# ---------------------------------------------------------------------------

def _timestamp(now) -> str:
    if now is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(now, str):
        return now
    return now.isoformat()


def run_audit(config, *, dataset_root=None,
              duplicate_threshold=DEFAULT_DUPLICATE_THRESHOLD,
              difficulty_values=None, now=None) -> dict:
    """Run every audit check over all case dirs under the dataset root.

    Args:
        config: EvalConfig (dataset/generation/judges/execution are read).
        dataset_root: override the dataset root (default:
            ``config.resolve_path(config.dataset.path)``).
        duplicate_threshold: near-duplicate similarity threshold in (0, 1].
        difficulty_values: difficulty vocabulary (default easy/medium/hard);
            only validated when a case carries a difficulty field.
        now: ``generated_at`` timestamp (datetime or ISO string; default:
            system clock). The CLI passes ``--timestamp`` through here.

    Returns:
        The audit dict (audit-owned keys only) — pass to :func:`write_audit`.
    """
    root = (Path(dataset_root) if dataset_root is not None
            else config.resolve_path(config.dataset.path))
    cases = [load_case(d) for d in iter_case_dirs(root)]

    if not cases:
        checks = {name: _skipped("empty dataset") for name in _CHECK_NAMES}
    else:
        checks = {
            "structural": check_structural(cases),
            "argument_fields": check_argument_fields(cases, config),
            "reference_resolution": check_reference_resolution(cases, config),
            "contamination": check_contamination(cases, config),
            "near_duplicates": check_near_duplicates(
                cases, duplicate_threshold),
            "composition": check_composition(cases, config,
                                             difficulty_values),
            "conditional_judges": check_conditional_judges(cases, config),
        }

    counts = Counter(
        finding.get("severity", "warning")
        for check in checks.values()
        for finding in check.get("findings", []))
    summary = {
        "cases": len(cases),
        "errors": counts.get("error", 0),
        "warnings": counts.get("warning", 0),
        "info": counts.get("info", 0),
    }
    return {
        "audit_version": 1,
        "generated_at": _timestamp(now),
        "dataset_path": str(root),
        "parameters": {
            "duplicate_threshold": duplicate_threshold,
            "difficulty_values": list(
                difficulty_values or DEFAULT_DIFFICULTY_VALUES),
        },
        "checks": checks,
        "summary": summary,
        "cases": {case.case_id: {"files": case.files} for case in cases},
        "case_hashes": {case.case_id: case_content_hash(case.path)
                        for case in cases},
    }


def write_audit(audit: dict, dataset_root) -> Path:
    """Write ``dataset_audit.yaml`` at the dataset root — LOAD-AND-MERGE.

    Replaces only the audit-owned top-level keys (:data:`AUDIT_OWNED_KEYS`);
    any foreign top-level key already in the file (e.g. ``null_probe``, a
    future ``construct_fidelity``) is preserved verbatim so other tools can
    merge their sections into the same file. The audit lives at the dataset
    root as a FILE — invisible to dir-only case discovery.
    """
    path = Path(dataset_root) / AUDIT_FILENAME
    foreign = {}
    if path.is_file():
        try:
            existing = yaml.safe_load(path.read_text())
            if isinstance(existing, dict):
                foreign = {k: v for k, v in existing.items()
                           if k not in AUDIT_OWNED_KEYS}
        except Exception:
            pass  # corrupt file — rewrite from the fresh audit alone
    merged = {k: audit[k] for k in AUDIT_OWNED_KEYS if k in audit}
    merged.update(foreign)
    path.write_text(yaml.safe_dump(merged, sort_keys=False,
                                   allow_unicode=True))
    return path


def load_audit(dataset_root) -> Optional[dict]:
    """Load ``dataset_audit.yaml`` from the dataset root; None when missing
    or corrupt (the preflight treats both as 'not audited')."""
    path = Path(dataset_root) / AUDIT_FILENAME
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def audit_preflight_warnings(dataset_root, case_dirs, max_names=5) -> list:
    """Soft V1 preflight for the execution paths — warning strings, or [].

    Compares the audit's stored per-case CONTENT hashes (never dir mtimes)
    against the current case dirs: an in-place edit of ``case-001/input.yaml``
    changes the content hash but not the directory mtime. Only the SELECTED
    case dirs are checked, so a ``--cases`` subset never warns about
    unselected cases. Callers print the strings as warnings and must treat
    this as a nudge — never a gate.
    """
    audit = load_audit(dataset_root)
    if audit is None:
        return [
            f"{AUDIT_FILENAME} not found at {dataset_root} — dataset has "
            "not been audited; run audit_dataset.py (/eval-dataset Step 6) "
            "before scoring"]
    recorded = audit.get("case_hashes")
    if not isinstance(recorded, dict):
        return [
            f"{AUDIT_FILENAME} at {dataset_root} has no case_hashes — "
            "re-run audit_dataset.py to refresh it"]

    stale, unrecorded = [], []
    for case_dir in case_dirs:
        case_dir = Path(case_dir)
        name = case_dir.name
        if name not in recorded:
            unrecorded.append(name)
        elif case_content_hash(case_dir) != recorded[name]:
            stale.append(name)

    def _names(names):
        listed = ", ".join(names[:max_names])
        if len(names) > max_names:
            listed += f", … ({len(names)} total)"
        return listed

    warnings = []
    if stale:
        warnings.append(
            "dataset audit is stale — case content changed since "
            f"{AUDIT_FILENAME} was written: {_names(stale)}; re-run "
            "audit_dataset.py")
    if unrecorded:
        warnings.append(
            f"case(s) not covered by {AUDIT_FILENAME}: {_names(unrecorded)}; "
            "re-run audit_dataset.py")
    return warnings


# ---------------------------------------------------------------------------
# Null-agent solvability probe (audit_dataset.py --null-run)
# ---------------------------------------------------------------------------

class NullRunError(Exception):
    """The null run cannot be audited (missing/unscored summary.yaml).

    The CLI maps this to exit 2 with the message as guidance.
    """


_NULL_RUN_GUIDANCE = (
    "score the null run first: python3 score.py judges --run-id <id> "
    "--config <config> --samples 3. Note the batch-mode limitation: a null "
    "run produces zero artifacts, so batch-mode collection creates no "
    "per-case run dirs and scoring exits before per_case exists — the probe "
    "requires a case-mode eval."
)


def _null_bool_passes(per_judge: dict) -> list:
    """(name, record) pairs of bool judges that awarded the null agent a pass.

    A judge counts iff its reduced value is boolean ``True`` and the record
    carries no ``error`` key. ``if:``-skipped records (value None, rationale
    ``Skipped: ...``), condition-error and errored records (value None +
    ``error``) never count; numeric ``1`` never counts (bool isinstance).
    """
    passes = []
    for name in sorted(per_judge):
        rec = per_judge[name]
        if not isinstance(rec, dict):
            continue
        value = rec.get("value")
        if rec.get("error") or not isinstance(value, bool) or not value:
            continue
        passes.append((name, rec))
    return passes


def _null_low_confidence(name: str, rec: dict, config) -> bool:
    """True when a passing bool judge is a stochastic (LLM/agent) verdict
    with no sampling evidence — single-sample verdicts are marked, never
    trusted. Deterministic judges (check/code/python builtins) are never
    low-confidence. Builtin LLM judges are never sampled (pinned to n=1 at
    scoring time), so they are always low-confidence."""
    stability = rec.get("stability") or {}
    samples = stability.get("samples")
    sampled = isinstance(samples, int) and samples > 1
    judge_type = rec.get("judge_type")
    if judge_type in ("llm", "agent"):
        return not sampled
    if judge_type == "builtin":
        jc = next((j for j in (getattr(config, "judges", None) or [])
                   if getattr(j, "name", None) == name), None)
        builtin_name = (getattr(jc, "builtin", "") if jc else "") or name
        try:  # filesystem scan only — never executes judge modules
            from agent_eval.judges import builtin_judge_kind
            return builtin_judge_kind(builtin_name) == "llm"
        except Exception:
            return False  # unknown builtin — do not guess
    return False


def audit_null_run(run_dir, config, *,
                   reward_threshold=DEFAULT_NULL_REWARD_THRESHOLD,
                   now=None) -> dict:
    """Audit a scored null run (``--agent null``) for non-discriminative cases.

    Reads ONLY ``<run_dir>/summary.yaml``'s ``per_case`` per-judge records and
    RECOMPUTES the composite reward per case via
    :func:`agent_eval.harbor.reward.compose_reward` (the reward is never
    stored in summary.yaml). A case is a ``null_pass`` when (a) ANY bool judge
    passed (:func:`_null_bool_passes` — skipped/errored never count), or
    (b) the recomputed reward is >= *reward_threshold* AND at least one judge
    actually produced a value (an all-skipped case composes to the vacuous
    gates-only reward 1.0 and must not flag on it).

    Returns the ``null_probe`` block for :func:`write_null_probe`. Raises
    :class:`NullRunError` (CLI exit 2) when the run has no scored per_case —
    including the documented batch-mode limitation.
    """
    run_dir = Path(run_dir)
    summary_path = run_dir / "summary.yaml"
    if not summary_path.is_file():
        raise NullRunError(
            f"no summary.yaml in {run_dir} — {_NULL_RUN_GUIDANCE}")
    try:
        summary = yaml.safe_load(summary_path.read_text()) or {}
    except Exception as e:
        raise NullRunError(f"unreadable summary.yaml in {run_dir}: {e}")
    per_case = summary.get("per_case") if isinstance(summary, dict) else None
    if not isinstance(per_case, dict) or not per_case:
        mode = ""
        try:
            meta = json.loads((run_dir / "run_result.json").read_text())
            if meta.get("execution_mode") == "batch":
                mode = ("this is a batch-mode run (execution.mode: batch) — "
                        "the probe does not support batch mode; ")
        except Exception:
            pass
        raise NullRunError(
            f"summary.yaml in {run_dir} has no scored per_case — "
            f"{mode}{_NULL_RUN_GUIDANCE}")

    # Lazy import: keeps module import light; reward.py is stdlib+yaml only.
    from agent_eval.harbor.reward import compose_reward, judge_ranges

    ranges = judge_ranges(config)
    reward_cfg = getattr(config, "reward", None)

    cases = {}
    null_pass_count = 0
    for case_id in sorted(per_case):
        per_judge = per_case[case_id]
        per_judge = per_judge if isinstance(per_judge, dict) else {}
        reward, _metrics = compose_reward(
            per_judge, reward_cfg=reward_cfg, judge_ranges=ranges)
        bool_passes = _null_bool_passes(per_judge)
        scored_any = any(isinstance(rec, dict) and rec.get("value") is not None
                         for rec in per_judge.values())
        null_pass = bool(bool_passes) or (
            scored_any and reward >= reward_threshold)
        entries = []
        low_confidence = False
        for name, rec in bool_passes:
            unsampled = _null_low_confidence(name, rec, config)
            low_confidence = low_confidence or unsampled
            entry = {
                "judge": name,
                "rationale": rec.get("rationale", ""),
                "judge_type": rec.get("judge_type", ""),
                "low_confidence": unsampled,
            }
            stability = rec.get("stability")
            if isinstance(stability, dict) and "samples" in stability:
                entry["samples"] = stability["samples"]
            entries.append(entry)
        if null_pass:
            null_pass_count += 1
        cases[case_id] = {
            "null_pass": null_pass,
            "passing_bool_judges": entries,
            "reward": round(float(reward), 4),
            "low_confidence": low_confidence,
        }

    return {
        "run_dir": str(run_dir),
        "generated_at": _timestamp(now),
        "reward_threshold": float(reward_threshold),
        "label": NULL_PROBE_LABEL,
        "null_pass_rate": round(null_pass_count / len(cases), 4),
        "cases": cases,
    }


def write_null_probe(null_probe: dict, dataset_root) -> Path:
    """Merge the ``null_probe`` key into dataset_audit.yaml — LOAD-AND-MERGE.

    The inverse of :func:`write_audit`'s ownership: replaces ONLY the
    ``null_probe`` top-level key; the audit-owned sections and any other
    foreign key are preserved verbatim, so a probe re-run and a full re-audit
    round-trip in both directions. Creates the file when the dataset has not
    been audited yet.
    """
    path = Path(dataset_root) / AUDIT_FILENAME
    existing = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass  # corrupt file — rewrite with the probe section alone
    existing["null_probe"] = null_probe
    path.write_text(yaml.safe_dump(existing, sort_keys=False,
                                   allow_unicode=True))
    return path
