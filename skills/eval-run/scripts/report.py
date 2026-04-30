#!/usr/bin/env python3
"""Generate an HTML report from eval run results.

Reads summary.yaml, run_result.json, eval.yaml, and optionally
review.yaml + a baseline run to produce a self-contained HTML report.
Works with any skill — reads judges, thresholds, and outputs dynamically.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/report.py \\
        --run-id <id> \\
        --config eval.yaml \\
        [--baseline <baseline-id>] \\
        [--open]
"""

import argparse
import base64
import difflib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Helpers (ported from rfe-creator eval/reporting/report.py)
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pct(val) -> str:
    if val is None:
        return "?"
    return f"{val * 100:.0f}%" if isinstance(val, float) else str(val)


def _pairwise_badge(winner: str) -> str:
    badges = {"A": ("pw-win", "WIN"), "B": ("pw-loss", "LOSS"),
              "tie": ("pw-tie", "TIE")}
    cls, label = badges.get(winner, ("pw-error", "ERR"))
    return f'<span class="pw-badge {cls}">{label}</span>'


def _word_diff_markup(old_line: str, new_line: str):
    sm = difflib.SequenceMatcher(None, old_line.split(), new_line.split())
    left_parts, right_parts = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        old_words = " ".join(old_line.split()[i1:i2])
        new_words = " ".join(new_line.split()[j1:j2])
        if op == "equal":
            left_parts.append(_esc(old_words))
            right_parts.append(_esc(new_words))
        elif op == "replace":
            left_parts.append(f'<span class="wdel">{_esc(old_words)}</span>')
            right_parts.append(f'<span class="wadd">{_esc(new_words)}</span>')
        elif op == "delete":
            left_parts.append(f'<span class="wdel">{_esc(old_words)}</span>')
        elif op == "insert":
            right_parts.append(f'<span class="wadd">{_esc(new_words)}</span>')
    return " ".join(left_parts), " ".join(right_parts)


def _side_by_side_diff(a: str, b: str, left_label: str = "",
                       right_label: str = "", context: int = 3) -> str:
    a_lines, b_lines = a.splitlines(), b.splitlines()
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)
    rows = [f'<tr class="hdr"><td class="ln"></td>'
            f'<td class="left">{_esc(left_label)}</td>'
            f'<td class="sep"></td>'
            f'<td class="ln"></td>'
            f'<td class="right">{_esc(right_label)}</td></tr>']

    for group in sm.get_grouped_opcodes(context):
        rows.append('<tr class="hdr"><td class="ln" colspan="2">...</td>'
                    '<td class="sep"></td>'
                    '<td class="ln" colspan="2">...</td></tr>')
        for op, i1, i2, j1, j2 in group:
            if op == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    rows.append(
                        f'<tr><td class="ln">{i+1}</td>'
                        f'<td class="left">{_esc(a_lines[i])}</td>'
                        f'<td class="sep"></td>'
                        f'<td class="ln">{j+1}</td>'
                        f'<td class="right">{_esc(b_lines[j])}</td></tr>')
            elif op == "replace":
                for k in range(max(i2 - i1, j2 - j1)):
                    ai = i1 + k if i1 + k < i2 else None
                    bj = j1 + k if j1 + k < j2 else None
                    if ai is not None and bj is not None:
                        lh, rh = _word_diff_markup(a_lines[ai], b_lines[bj])
                        rows.append(
                            f'<tr class="mod"><td class="ln">{ai+1}</td>'
                            f'<td class="left">{lh}</td><td class="sep"></td>'
                            f'<td class="ln">{bj+1}</td>'
                            f'<td class="right">{rh}</td></tr>')
                    elif ai is not None:
                        rows.append(
                            f'<tr class="del"><td class="ln">{ai+1}</td>'
                            f'<td class="left">{_esc(a_lines[ai])}</td>'
                            f'<td class="sep"></td><td class="ln"></td>'
                            f'<td class="right"></td></tr>')
                    elif bj is not None:
                        rows.append(
                            f'<tr class="add"><td class="ln"></td>'
                            f'<td class="left"></td><td class="sep"></td>'
                            f'<td class="ln">{bj+1}</td>'
                            f'<td class="right">{_esc(b_lines[bj])}</td></tr>')
            elif op == "delete":
                for i in range(i1, i2):
                    rows.append(
                        f'<tr class="del"><td class="ln">{i+1}</td>'
                        f'<td class="left">{_esc(a_lines[i])}</td>'
                        f'<td class="sep"></td><td class="ln"></td>'
                        f'<td class="right"></td></tr>')
            elif op == "insert":
                for j in range(j1, j2):
                    rows.append(
                        f'<tr class="add"><td class="ln"></td>'
                        f'<td class="left"></td><td class="sep"></td>'
                        f'<td class="ln">{j+1}</td>'
                        f'<td class="right">{_esc(b_lines[j])}</td></tr>')

    return f'<table class="diff-table">{"".join(rows)}</table>'


# ---------------------------------------------------------------------------
# Data loading (standalone — no agent_eval imports)
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _read_text(path: Path, max_lines: int = 200) -> str:
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n\n... truncated ({len(lines)} lines total)"
        return "\n".join(lines)
    except (UnicodeDecodeError, OSError):
        return ""


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}


def _is_image_file(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES


def _image_to_data_uri(path: Path) -> str:
    mime = _IMAGE_MIME.get(path.suffix.lower(), "application/octet-stream")
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except OSError:
        return ""


def _find_gold_standard_image(case_id: str, dataset_path: str):
    """Find a gold standard image for a case from the dataset annotations."""
    if not dataset_path:
        return None
    case_ds = Path(dataset_path) / case_id
    ann_path = case_ds / "annotations.yaml"
    if not ann_path.is_file():
        return None
    ann = _load_yaml(ann_path)
    gold_name = ann.get("gold_diagram")
    if not gold_name:
        return None
    stem = Path(gold_name).stem
    full_stem = gold_name  # e.g. "gold-standard.drawio"
    for suffix in _IMAGE_SUFFIXES:
        for candidate_name in [f"{full_stem}{suffix}", f"{stem}{suffix}"]:
            candidate = case_ds / candidate_name
            if candidate.is_file():
                return candidate
    return None


def _read_case_input(dataset_path: str, case_id: str) -> str:
    """Read the input file from a dataset case directory."""
    case_dir = Path(dataset_path) / case_id
    if not case_dir.exists():
        return ""
    for suffix in (".yaml", ".yml", ".json"):
        candidate = case_dir / f"input{suffix}"
        if candidate.is_file():
            return _read_text(candidate, max_lines=100)
    # Fallback: first parseable file
    for f in sorted(case_dir.iterdir()):
        if f.is_file() and f.suffix in (".yaml", ".yml", ".json"):
            return _read_text(f, max_lines=100)
    return ""


# ---------------------------------------------------------------------------
# HTML section generators
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #cbd1da;
  --surface: #ffffff;
  --surface-2: #f1f5f9;
  --surface-3: #e2e8f0;
  --border: #e2e8f0;
  --border-strong: #cbd5e1;
  --text: #0f172a;
  --text-muted: #64748b;
  --text-soft: #475569;
  --accent: #2563eb;
  --accent-strong: #1e3a8a;
  --accent-soft: #dbeafe;
  --success: #16a34a;
  --success-soft: #dcfce7;
  --success-border: #86efac;
  --danger: #dc2626;
  --danger-soft: #fee2e2;
  --danger-border: #fca5a5;
  --warning: #d97706;
  --warning-soft: #fef3c7;
  --warning-border: #fcd34d;
  --neutral-soft: #f1f5f9;
  --neutral-border: #cbd5e1;
  --code-bg: #eef2f7;
  --shadow: 0 1px 3px rgba(15,23,42,.06), 0 1px 2px rgba(15,23,42,.04);
  --shadow-strong: 0 4px 12px rgba(15,23,42,.08), 0 1px 3px rgba(15,23,42,.05);
  --diff-add-bg: #e6ffec;
  --diff-add-strong: #acf2bd;
  --diff-del-bg: #ffeef0;
  --diff-del-strong: #fdb8c0;
  --diff-hdr-bg: #f0f0f0;
  --case-pass-accent: #16a34a;
  --case-fail-accent: #dc2626;
  --case-pw-a-accent: #16a34a;
  --case-pw-b-accent: #dc2626;
  --case-pw-tie-accent: #d97706;
  --case-pw-error-accent: #94a3b8;
  color-scheme: light;
}
:root[data-theme="dark"] {
  --bg: #0b1220;
  --surface: #0f1729;
  --surface-2: #162033;
  --surface-3: #1e293b;
  --border: #1e2c44;
  --border-strong: #334155;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --text-soft: #cbd5e1;
  --accent: #60a5fa;
  --accent-strong: #93c5fd;
  --accent-soft: #1e3a8a;
  --success: #4ade80;
  --success-soft: #14532d;
  --success-border: #166534;
  --danger: #f87171;
  --danger-soft: #7f1d1d;
  --danger-border: #991b1b;
  --warning: #fbbf24;
  --warning-soft: #78350f;
  --warning-border: #92400e;
  --neutral-soft: #1e293b;
  --neutral-border: #334155;
  --code-bg: #1e293b;
  --shadow: 0 1px 3px rgba(0,0,0,.4), 0 1px 2px rgba(0,0,0,.3);
  --shadow-strong: 0 4px 14px rgba(0,0,0,.5), 0 1px 3px rgba(0,0,0,.35);
  --diff-add-bg: rgba(74,222,128,.08);
  --diff-add-strong: rgba(74,222,128,.28);
  --diff-del-bg: rgba(248,113,113,.08);
  --diff-del-strong: rgba(248,113,113,.28);
  --diff-hdr-bg: #1e293b;
  --case-pass-accent: #4ade80;
  --case-fail-accent: #f87171;
  --case-pw-a-accent: #4ade80;
  --case-pw-b-accent: #f87171;
  --case-pw-tie-accent: #fbbf24;
  --case-pw-error-accent: #64748b;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { background: var(--bg); }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 15px; line-height: 1.55; max-width: 1200px; margin: 0 auto; padding: 1.5em 1.5em 4em; color: var(--text); background: var(--bg); transition: background-color .15s ease, color .15s ease; }
h1 { padding-bottom: 0; margin: 0 0 0.55em; font-size: 1.9em; letter-spacing: -0.015em; font-weight: 700; color: var(--accent-strong); }
.report-header { margin: 0 0 1.6em; padding-bottom: 1.1em; border-bottom: 2px solid var(--border-strong); }
.header-meta { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center; }
.meta-chip { display: inline-flex; align-items: center; gap: 8px; background: var(--surface); border: 1px solid var(--border-strong); border-radius: 999px; padding: 5px 13px 5px 12px; font-size: 0.95em; color: var(--text); box-shadow: var(--shadow); line-height: 1.4; }
.meta-chip .meta-label { font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-muted); font-weight: 700; }
.meta-chip code { background: var(--code-bg); padding: 1px 7px; border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.92em; color: var(--text); }
h2 { margin-top: 1.5em; letter-spacing: -0.005em; }
table { border-collapse: separate; border-spacing: 0; width: 100%; margin: 1em 0; font-variant-numeric: tabular-nums; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
th, td { border-bottom: 1px solid var(--border); padding: 9px 12px; text-align: left; }
tr:last-child td { border-bottom: none; }
th { background: var(--surface-2); font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); font-weight: 600; }
tbody tr:nth-child(even) td { background: color-mix(in srgb, var(--surface-2) 50%, transparent); }
tbody tr:hover td { background: var(--accent-soft); }
.pass, .fail, .warn, .skip { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.85em; font-weight: 600; line-height: 1.4; border: 1px solid transparent; }
.pass { color: var(--success); background: var(--success-soft); border-color: var(--success-border); }
.fail { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.warn { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-border); }
.skip { color: var(--text-muted); background: var(--neutral-soft); border-color: var(--neutral-border); font-weight: 500; }
.metric-row td:last-child { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.judge-type { font-size: 0.85em; color: var(--text-muted); }
.subsection-heading { margin-top: 1.4em; font-size: 0.95em; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
.model-heading { margin: 1.2em 0 0.4em; font-size: 0.92em; font-weight: 600; color: var(--text-soft); }
.model-heading code { background: var(--code-bg); padding: 2px 8px; border-radius: 4px; font-size: 0.95em; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); }
.config-table { table-layout: fixed; }
.config-table th:first-child, .config-table td:first-child { width: 22%; }
.config-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 0.9em 1.6em; margin: 0.4em 0 0.8em; padding: 0; }
.kv { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.kv dt { font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; margin: 0; }
.kv dd { margin: 0; font-weight: 500; font-variant-numeric: tabular-nums; word-break: break-word; line-height: 1.35; color: var(--text); }
.kv dd.bl { color: var(--text-muted); font-size: 0.85em; font-weight: 400; }
.kv dd.bl::before { content: "vs "; opacity: 0.7; }
.kv dd.bl .delta { font-weight: 600; font-size: 0.95em; padding-left: 4px; }
details.eval-params { margin: 1.2em 0 0; padding: 0; }
details.eval-params > summary { cursor: pointer; padding: 6px 0; list-style: none; display: flex; align-items: center; gap: 8px; user-select: none; min-width: 0; }
details.eval-params > summary::-webkit-details-marker { display: none; }
details.eval-params > summary::before { content: "▸"; font-size: 0.9em; transition: transform .15s ease; display: inline-block; color: var(--text-muted); flex-shrink: 0; }
details.eval-params[open] > summary::before { transform: rotate(90deg); }
details.eval-params > summary:hover .eval-params-label { color: var(--accent); }
.eval-params-label { font-size: 0.95em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; flex-shrink: 0; }
.eval-params-preview { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.85em; color: var(--text-soft); padding-left: 8px; border-left: 1px solid var(--border-strong); margin-left: 2px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
details.eval-params[open] .eval-params-preview { display: none; }
details.eval-params > .config-grid { margin-top: 0.5em; }
details.eval-params > .run-command { margin: 0.6em 0 0; }
.run-command { margin: 0.4em 0 1em; }
.run-command-label { display: block; font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; margin-bottom: 4px; }
.run-command pre { margin: 0; background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; overflow-x: auto; font-size: 0.85em; line-height: 1.4; }
.run-command code { background: transparent; padding: 0; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); white-space: pre; }
.usage-table th code { background: var(--code-bg); padding: 2px 7px; border-radius: 4px; font-size: 0.95em; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); text-transform: none; letter-spacing: 0; font-weight: 500; }
.usage-table td { font-variant-numeric: tabular-nums; }
.usage-table .cur-val { display: block; font-weight: 500; }
.usage-table .bl-val { display: block; font-size: 0.82em; color: var(--text-muted); margin-top: 1px; }
.usage-table .bl-val::before { content: "vs "; opacity: 0.7; }
.usage-table .delta { font-weight: 600; font-size: 0.9em; padding-left: 4px; }
.delta-good { color: var(--success); }
.delta-bad { color: var(--danger); }
.delta-flat { color: var(--text-muted); font-weight: 500; }
.baseline-row td { background: var(--surface-2); font-style: italic; color: var(--text-muted); }
.rationale { font-size: 0.88em; color: var(--text-soft); }
.rationale p { margin: 0 0 0.5em; line-height: 1.5; }
.rationale p:last-child { margin-bottom: 0; }
.rationale ul, .rationale ol { margin: 0.3em 0 0.5em 1.2em; padding: 0; }
.rationale li { margin: 0.2em 0; }
.rationale code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 0.9em; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.rationale strong { color: var(--text); }
details.case { margin: 1em 0; border: 1px solid var(--border); border-left-width: 4px; border-radius: 8px; padding: 1em 1.2em; background: var(--surface); box-shadow: var(--shadow); }
details.case.case-pass { border-left-color: var(--case-pass-accent); }
details.case.case-fail { border-left-color: var(--case-fail-accent); }
details.case.case-pw-a { border-left-color: var(--case-pw-a-accent); }
details.case.case-pw-b { border-left-color: var(--case-pw-b-accent); }
details.case.case-pw-tie { border-left-color: var(--case-pw-tie-accent); }
details.case.case-pw-error { border-left-color: var(--case-pw-error-accent); }
details.case > summary { cursor: pointer; font-weight: 600; padding: 0.2em 0; font-size: 1.02em; list-style: none; display: flex; align-items: center; gap: 0.6em; }
details.case > summary::-webkit-details-marker { display: none; }
details.case > summary::before { content: "▸"; color: var(--text-muted); transition: transform .15s ease; display: inline-block; }
details.case[open] > summary::before { transform: rotate(90deg); }
details.case > summary:hover { color: var(--accent); }
.info-box { background: var(--accent-soft); border: 1px solid var(--border); border-radius: 6px; padding: 0.8em 1em; margin: 0.6em 0; font-size: 0.9em; }
.feedback-box { background: var(--warning-soft); border: 1px solid var(--warning-border); border-radius: 6px; padding: 0.8em 1em; margin: 0.6em 0; font-size: 0.9em; color: var(--text); }
.file-badge { display: inline-block; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82em; background: var(--surface-2); border: 1px solid var(--border-strong); border-radius: 5px; padding: 3px 10px; margin: 1em 0 0.5em 0; color: var(--text-soft); }
.pw-badge { display: inline-block; font-size: 0.78em; font-weight: 700; padding: 2px 9px; border-radius: 999px; margin-left: 8px; border: 1px solid transparent; letter-spacing: 0.04em; }
.pw-win { background: var(--success-soft); color: var(--success); border-color: var(--success-border); }
.pw-loss { background: var(--danger-soft); color: var(--danger); border-color: var(--danger-border); }
.pw-tie { background: var(--warning-soft); color: var(--warning); border-color: var(--warning-border); }
.pw-error { background: var(--neutral-soft); color: var(--text-muted); border-color: var(--neutral-border); }
.diff-table { width: 100%; border-collapse: collapse; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82em; table-layout: fixed; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.diff-table td { padding: 1px 6px; vertical-align: top; white-space: pre-wrap; word-wrap: break-word; border: 1px solid var(--border); }
.diff-table .ln { width: 35px; min-width: 35px; color: var(--text-muted); text-align: right; background: var(--surface-2); user-select: none; white-space: nowrap; }
.diff-table .left, .diff-table .right { width: calc(50% - 35px); background: var(--surface); }
.diff-table .sep { width: 1px; padding: 0; background: var(--border-strong); }
.diff-table tr.mod .left { background: var(--diff-del-bg); }
.diff-table tr.mod .right { background: var(--diff-add-bg); }
.diff-table tr.add .right { background: var(--diff-add-bg); }
.diff-table tr.add .left { background: var(--surface-2); }
.diff-table tr.del .left { background: var(--diff-del-bg); }
.diff-table tr.del .right { background: var(--surface-2); }
.diff-table tr.hdr td { background: var(--diff-hdr-bg); color: var(--text-muted); font-weight: 600; }
.diff-table .wdel { background: var(--diff-del-strong); border-radius: 2px; padding: 0 2px; }
.diff-table .wadd { background: var(--diff-add-strong); border-radius: 2px; padding: 0 2px; }
pre.output { background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 0.9em 1em; font-size: 0.88em; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; color: var(--text); font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.html-preview { width: 100%; height: 80vh; max-height: 2000px; border: 1px solid var(--border-strong); border-radius: 6px; margin: 0.5em 0; background: #fff; color-scheme: light; }
.section, .analysis { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent); border-radius: 8px; padding: 1.3em 1.6em 1.4em; margin: 1.5em 0; box-shadow: var(--shadow); }
.section > h2:first-child, h2.section-heading { margin: 0 0 0.7em; padding: 0; border: none; font-size: 1.3em; color: var(--accent-strong); letter-spacing: -0.005em; }
h2.section-heading { margin: 1.8em 0 0.8em; }
.section-intro { color: var(--text-muted); margin: 0 0 1em; font-size: 0.92em; }
.section > table:last-child, .section > details:last-child { margin-bottom: 0; }
.analysis { border-left-width: 5px; margin-bottom: 2em; box-shadow: var(--shadow-strong); }
.analysis-banner { display: flex; align-items: center; gap: 0.7em; margin-bottom: 0.3em; }
.analysis-banner h2 { margin: 0; padding: 0; border: none; font-size: 1.4em; color: var(--accent-strong); letter-spacing: -0.005em; }
.analysis-body h2 { margin-top: 1.6em; padding-bottom: 0.35em; font-size: 1.1em; color: var(--accent-strong); border-bottom: 1px solid var(--border); }
.analysis-body h2:first-of-type { margin-top: 0; font-size: 1.2em; border-bottom-width: 2px; padding-bottom: 0.4em; border-bottom-color: var(--accent); }
.analysis-body h3 { margin-top: 1em; color: var(--text-soft); font-size: 1em; }
.analysis-body li { margin: 0.3em 0; line-height: 1.55; }
.analysis-body p { line-height: 1.6; }
.analysis-body code { background: var(--code-bg); padding: 1px 6px; border-radius: 4px; font-size: 0.88em; font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--text); }
.analysis-body table { font-size: 0.92em; }
.analysis-body hr { border: none; border-top: 1px dashed var(--border-strong); margin: 1.4em 0; }
.section-intro code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; font-size: 0.92em; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
#theme-toggle { position: fixed; top: 16px; right: 16px; z-index: 1000; background: var(--surface); color: var(--text); border: 1px solid var(--border-strong); border-radius: 999px; width: 38px; height: 38px; padding: 0; cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; box-shadow: var(--shadow); transition: background-color .15s ease, transform .15s ease, border-color .15s ease; }
#theme-toggle:hover { background: var(--surface-2); border-color: var(--accent); transform: scale(1.05); }
#theme-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media print {
  :root { color-scheme: light; --bg: #fff; --surface: #fff; --text: #000; --text-muted: #444; }
  #theme-toggle { display: none; }
  .section, .analysis, details.case { box-shadow: none; break-inside: avoid; }
}

/* Image artifacts */
.img-artifact { margin: 0.5em 0; }
.img-artifact img { max-width: 100%; height: auto; border: 1px solid var(--border); border-radius: 6px; background: #fff; }
.img-label { display: inline-block; font-size: 0.78em; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; padding: 2px 9px; border-radius: 999px; margin-bottom: 6px; }
.img-label-gen { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent); }
.img-label-ref { background: var(--success-soft); color: var(--success); border: 1px solid var(--success-border); }
.img-label-bl { background: var(--warning-soft); color: var(--warning); border: 1px solid var(--warning-border); }

/* Comparison widget */
.img-compare { margin: 0.8em 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface); }
.img-compare-tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); background: var(--surface-2); }
.img-compare-tab { padding: 7px 16px; font-size: 0.85em; font-weight: 600; color: var(--text-muted); background: transparent; border: none; cursor: pointer; border-bottom: 2px solid transparent; transition: color .15s, border-color .15s; }
.img-compare-tab:hover { color: var(--text); }
.img-compare-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.img-compare-panel { display: none; padding: 1em; }
.img-compare-panel.active { display: block; }

/* Side-by-side */
.img-compare-sbs { display: flex; gap: 1em; }
.img-compare-sbs > div { flex: 1; min-width: 0; }
.img-compare-sbs img { width: 100%; height: auto; border: 1px solid var(--border); border-radius: 4px; background: #fff; }

/* Swipe */
.img-compare-swipe-imgs { display: grid; border: 1px solid var(--border); border-radius: 4px; background: #fff; overflow: hidden; }
.img-compare-swipe-imgs > img { grid-area: 1/1; display: block; width: 100%; height: auto; }
.img-compare-swipe-imgs .swipe-ref { z-index: 1; clip-path: inset(0 50% 0 0); }
.img-compare-swipe-imgs .swipe-line { grid-area: 1/1; z-index: 2; justify-self: start; width: 3px; background: var(--accent); pointer-events: none; margin-left: 50%; }
.img-compare-swipe input[type="range"] { display: block; width: 100%; margin: 8px 0 0; accent-color: var(--accent); }
.img-compare-swipe-labels { display: flex; justify-content: space-between; padding: 4px 1em; font-size: 0.82em; color: var(--text-muted); }

/* Onion */
.img-compare-onion-wrap { display: grid; border: 1px solid var(--border); border-radius: 4px; background: #fff; overflow: hidden; }
.img-compare-onion-wrap > img { grid-area: 1/1; display: block; width: 100%; height: auto; }
.img-compare-onion input[type="range"] { display: block; width: 100%; margin: 8px 0 0; accent-color: var(--accent); }
.img-compare-onion .onion-label { text-align: center; font-size: 0.82em; color: var(--text-muted); margin-top: 2px; }
"""

THEME_SCRIPT = """
(function () {
  try {
    var stored = localStorage.getItem('eval-report-theme');
    var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    var theme = stored || (prefersDark ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
"""

TOGGLE_SCRIPT = """
(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  function paint() {
    var t = document.documentElement.getAttribute('data-theme') || 'light';
    btn.textContent = t === 'dark' ? '\\u2600' : '\\u263E';
    btn.setAttribute('aria-label', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    btn.title = btn.getAttribute('aria-label');
  }
  btn.addEventListener('click', function () {
    var current = document.documentElement.getAttribute('data-theme') || 'light';
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('eval-report-theme', next); } catch (e) {}
    paint();
  });
  paint();
})();
"""

IMAGE_COMPARE_SCRIPT = """
(function () {
  document.querySelectorAll('.img-compare').forEach(function (widget) {
    var tabs = widget.querySelectorAll('.img-compare-tab');
    var panels = widget.querySelectorAll('.img-compare-panel');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) { t.classList.remove('active'); });
        panels.forEach(function (p) { p.classList.remove('active'); });
        tab.classList.add('active');
        var target = widget.querySelector('#' + tab.dataset.panel);
        if (target) target.classList.add('active');
      });
    });
    var swipe = widget.querySelector('.img-compare-swipe');
    if (swipe) {
      (function (sw) {
        var sl = sw.querySelector('input[type="range"]');
        var ref = sw.querySelector('.swipe-ref');
        var ln = sw.querySelector('.swipe-line');
        if (sl && ref) {
          function update() {
            var p = sl.value;
            ref.style.clipPath = 'inset(0 ' + (100 - p) + '% 0 0)';
            if (ln) ln.style.marginLeft = p + '%';
          }
          sl.addEventListener('input', update);
          update();
        }
      })(swipe);
    }
    var onion = widget.querySelector('.img-compare-onion');
    if (onion) {
      (function (on) {
        var sl = on.querySelector('input[type="range"]');
        var top = on.querySelector('.onion-top');
        var lbl = on.querySelector('.onion-label');
        if (sl && top) {
          function update() {
            top.style.opacity = sl.value / 100;
            if (lbl) lbl.textContent = 'Generated opacity: ' + sl.value + '%';
          }
          sl.addEventListener('input', update);
          update();
        }
      })(onion);
    }
  });
})();
"""


def _render_header(config, run_id, run_result, baseline_id=None):
    title = "Skill Eval Report"
    skill = config.get("skill", "")
    date = run_result.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    chips = []
    if skill:
        chips.append(
            f'<span class="meta-chip"><span class="meta-label">Skill</span>'
            f'<code>{_esc(skill)}</code></span>'
        )
    chips.append(
        f'<span class="meta-chip"><span class="meta-label">Run</span>'
        f'<code>{_esc(run_id)}</code></span>'
    )
    if baseline_id:
        chips.append(
            f'<span class="meta-chip meta-baseline"><span class="meta-label">Baseline</span>'
            f'<code>{_esc(baseline_id)}</code></span>'
        )
    chips.append(
        f'<span class="meta-chip"><span class="meta-label">Date</span>'
        f'{_esc(str(date)[:19])}</span>'
    )

    return (
        '<header class="report-header">\n'
        f'  <h1>{_esc(title)}</h1>\n'
        f'  <div class="header-meta">{"".join(chips)}</div>\n'
        '</header>\n'
    )


def _render_run_config(run_result, baseline_result=None):
    has_bl = baseline_result is not None
    fields = [
        ("Model", "model"),
        ("Subagent Model", "subagent_model"),
        ("Effort", "effort"),
        ("Agent", "agent"),
        ("Agent Version", "agent_version"),
        ("Duration", "duration_s"),
        ("Cost", "cost_usd"),
        ("Turns", "num_turns"),
        ("Exit Code", "exit_code"),
    ]
    # Numeric fields where a baseline delta is meaningful (lower is better)
    numeric_lower_better = {"duration_s", "cost_usd", "num_turns"}
    # Optional fields that are hidden when not set on either run, to avoid
    # showing rows of "—" for knobs the user isn't using (e.g. effort).
    optional_fields = {"effort"}

    def _lookup(rr, key):
        if not rr:
            return None
        if key in rr:
            return rr.get(key)
        return (rr.get("eval_params") or {}).get(key)

    def _fmt(key, val):
        if val is None or val == "":
            return "—"
        if key == "duration_s":
            try:
                v = float(val)
                return f"{v / 60:.0f} min" if v >= 60 else f"{v:.0f}s"
            except (TypeError, ValueError):
                return str(val)
        if key == "cost_usd":
            try:
                return f"${float(val):.2f}"
            except (TypeError, ValueError):
                return str(val)
        if key == "num_turns" and isinstance(val, int):
            return f"{val:,}"
        return str(val)

    def _scalar_delta(key, cur_raw, bl_raw):
        """Delta tuple (arrow, str, css_class) for numeric fields, else None."""
        if key not in numeric_lower_better:
            return None
        if not isinstance(cur_raw, (int, float)) or not isinstance(bl_raw, (int, float)):
            return None
        if bl_raw == 0:
            return None
        diff = cur_raw - bl_raw
        if diff == 0:
            return ("→", "0%", "delta-flat")
        pct = (diff / bl_raw) * 100
        arrow = "↑" if diff > 0 else "↓"
        sign = "+" if diff > 0 else ""
        return (arrow, f"{sign}{pct:.1f}%",
                "delta-good" if diff < 0 else "delta-bad")

    html = '<h2>Run Configuration</h2>\n<dl class="config-grid">\n'
    for label, key in fields:
        cur_raw = _lookup(run_result, key)
        cur = _fmt(key, cur_raw)
        # Hide optional knobs (e.g. effort) when neither side set them, to
        # avoid showing a row of "—" for a feature the user isn't using.
        if (key in optional_fields and cur == "—"
                and (not has_bl or _fmt(key, _lookup(baseline_result, key)) == "—")):
            continue
        html += '<div class="kv">'
        html += f'<dt>{label}</dt>'
        html += f'<dd>{_esc(cur)}</dd>'
        if has_bl:
            bl_raw = _lookup(baseline_result, key)
            bl = _fmt(key, bl_raw)
            if bl != cur:
                d = _scalar_delta(key, cur_raw, bl_raw)
                delta_html = (f' <span class="delta {d[2]}">{d[0]} {d[1]}</span>'
                              if d else "")
                html += f'<dd class="bl">{_esc(bl)}{delta_html}</dd>'
        html += '</div>\n'
    html += "</dl>\n"
    html += _render_model_usage(run_result, baseline_result)
    html += _render_eval_params(run_result)
    return html


def _render_eval_params(run_result):
    """Render the user-facing eval parameters that defined the run."""
    params = run_result.get("eval_params") or {}
    if not params:
        return ""

    # Short params shown as kv chips
    short_fields = [
        ("Execution Mode", "execution_mode"),
        ("Max Budget", "max_budget_usd"),
        ("Timeout", "timeout_s"),
        ("Effort", "effort"),
        ("MLflow Experiment", "mlflow_experiment"),
    ]

    def _fmt(key, val):
        if val is None or val == "":
            return None
        if key == "max_budget_usd":
            try:
                return f"${float(val):.0f}"
            except (TypeError, ValueError):
                return str(val)
        if key == "timeout_s":
            try:
                v = int(val)
                return f"{v // 60} min" if v >= 60 else f"{v}s"
            except (TypeError, ValueError):
                return str(val)
        return str(val)

    chips = []
    for label, key in short_fields:
        formatted = _fmt(key, params.get(key))
        if formatted is None:
            continue
        chips.append(
            '<div class="kv">'
            f'<dt>{label}</dt>'
            f'<dd>{_esc(formatted)}</dd>'
            '</div>'
        )

    body = ""
    if chips:
        body += '<dl class="config-grid">\n' + "\n".join(chips) + '\n</dl>\n'

    skill_args = params.get("skill_args")
    if skill_args:
        body += (
            '<div class="run-command">'
            '<span class="run-command-label">Skill Args</span>'
            f'<pre><code>/{_esc(params.get("skill", "skill"))} {_esc(skill_args)}</code></pre>'
            '</div>\n'
        )

    if not body:
        return ""

    # One-line preview shown alongside the summary when collapsed.
    # Prefer skill args (the dataset/invocation), fall back to a compact
    # comma-joined view of the short params.
    preview_text = ""
    if skill_args:
        preview_text = f'/{params.get("skill", "skill")} {skill_args}'
    else:
        bits = [_fmt(k, params.get(k))
                for _, k in short_fields if _fmt(k, params.get(k))]
        preview_text = " · ".join(bits)

    preview_html = (
        f'<span class="eval-params-preview">{_esc(preview_text)}</span>'
        if preview_text else ""
    )

    return (
        '<details class="eval-params">\n'
        f'<summary><span class="eval-params-label">Parameters</span>{preview_html}</summary>\n'
        f'{body}'
        '</details>\n'
    )


def _render_model_usage(run_result, baseline_result=None):
    """Render model usage: token counts, cost, cache hit rate, and derived
    per-turn / per-Mtok efficiency metrics. One column per model plus an
    optional Total column when more than one model is present.

    Per-turn rows (cost / turn, output tokens / turn) require num_turns,
    which the runner only reports at the whole-run level. They render in
    the Total column for multi-model runs, and in the single model's
    column for single-model runs (where they're unambiguous)."""
    tokens = run_result.get("token_usage") or {}
    bl_tokens = (baseline_result or {}).get("token_usage") or {}
    per_model = run_result.get("per_model_usage") or {}
    bl_per_model = (baseline_result or {}).get("per_model_usage") or {}

    if not tokens and not bl_tokens and not per_model and not bl_per_model:
        return ""

    has_bl = bool(bl_tokens) or bool(bl_per_model)

    def _compute(usage, key):
        """Return numeric value for a key, or None if not derivable."""
        if not usage:
            return None
        inp = usage.get("input", 0) or 0
        out = usage.get("output", 0) or 0
        cr = usage.get("cache_read", 0) or 0
        cw = usage.get("cache_create", 0) or 0
        cost = usage.get("cost_usd")
        turns = usage.get("num_turns")
        total_in = inp + cr + cw
        total_tokens = total_in + out
        if key == "input":         return inp or None
        if key == "output":        return out or None
        if key == "cache_read":    return cr or None
        if key == "cache_create":  return cw or None
        if key == "cost_usd":      return cost
        if key == "hit":           return (cr / total_in) if total_in else None
        if key == "cpt":           return (cost / turns) if (cost is not None and turns) else None
        if key == "opt":           return (out / turns) if (out and turns) else None
        if key == "mtok":          return (cost / total_tokens * 1_000_000) if (cost is not None and total_tokens) else None
        return None

    def _fmt(usage, key):
        v = _compute(usage, key)
        if v is None:
            return "—"
        if key == "hit":
            return f"{v:.1%}"
        if key == "cost_usd":
            return f"${v:.2f}"
        if key == "cpt":
            return f"${v:.4f}"
        if key == "mtok":
            return f"${v:.2f}"
        if key == "opt":
            return f"{v:,.0f}"
        return f"{v:,}"

    # Higher-is-better metrics; everything else assumed lower-is-better.
    # cache_read raw count is higher-better as a proxy for cache exploitation.
    higher_better = {"cache_read", "hit"}
    # Percentage-point delta (vs %) for these rate-like metrics
    pp_delta = {"hit"}

    def _delta(cur, bl, key):
        """Return (arrow, delta_str, css_class) or (None, None, None)."""
        cur_v = _compute(cur, key)
        bl_v = _compute(bl, key)
        if cur_v is None or bl_v is None or bl_v == 0:
            return (None, None, None)
        diff = cur_v - bl_v
        if key in pp_delta:
            if abs(diff) < 0.0005:
                return ("→", "0pp", "delta-flat")
            arrow = "↑" if diff > 0 else "↓"
            sign = "+" if diff > 0 else ""
            cls = "delta-good" if (diff > 0) == (key in higher_better) else "delta-bad"
            return (arrow, f"{sign}{diff * 100:.1f}pp", cls)
        if diff == 0:
            return ("→", "0%", "delta-flat")
        pct = (diff / bl_v) * 100
        arrow = "↑" if diff > 0 else "↓"
        sign = "+" if diff > 0 else ""
        cls = "delta-good" if (diff > 0) == (key in higher_better) else "delta-bad"
        return (arrow, f"{sign}{pct:.1f}%", cls)

    # Inject cost_usd and num_turns into a usage dict so derived metrics
    # can be computed. Used for the Total column (whole-run aggregate).
    def _enrich(usage_dict, run_data):
        if not usage_dict and not run_data:
            return None
        merged = dict(usage_dict or {})
        if run_data:
            if "cost_usd" in run_data:
                merged["cost_usd"] = run_data.get("cost_usd")
            if "num_turns" in run_data:
                merged["num_turns"] = run_data.get("num_turns")
        return merged

    cur_total = _enrich(tokens, run_result)
    bl_total = _enrich(bl_tokens, baseline_result)
    models = sorted(set(per_model) | set(bl_per_model))
    show_total = bool(cur_total or bl_total) and len(models) != 1

    # Build per-model usage dicts. Inject per-model num_turns when the runner
    # captured it (cross-model runs); fall back to whole-run num_turns when a
    # side's run had only one model (per-turn rows are then unambiguous).
    cur_pmt = run_result.get("per_model_turns") or {}
    bl_pmt = (baseline_result or {}).get("per_model_turns") or {}
    cur_per_model = {m: dict(per_model.get(m) or {}) for m in models}
    bl_per_model_d = {m: dict(bl_per_model.get(m) or {}) for m in models}
    cur_only_model = next(iter(per_model)) if len(per_model) == 1 else None
    bl_only_model = next(iter(bl_per_model)) if len(bl_per_model) == 1 else None
    for m in models:
        if cur_pmt.get(m) is not None:
            cur_per_model[m].setdefault("num_turns", cur_pmt[m])
        elif m == cur_only_model and run_result.get("num_turns") is not None:
            cur_per_model[m].setdefault("num_turns", run_result["num_turns"])
        if bl_pmt.get(m) is not None:
            bl_per_model_d[m].setdefault("num_turns", bl_pmt[m])
        elif (m == bl_only_model and baseline_result
              and baseline_result.get("num_turns") is not None):
            bl_per_model_d[m].setdefault("num_turns", baseline_result["num_turns"])

    # Build column list: optional Total, then each model
    columns = []
    if show_total:
        columns.append(("Total", cur_total, bl_total))
    for m in models:
        columns.append((m, cur_per_model.get(m) or None, bl_per_model_d.get(m) or None))

    if not columns:
        return ""

    metrics = [
        ("Input tokens", "input"),
        ("Output tokens", "output"),
        ("Cache read", "cache_read"),
        ("Cache write", "cache_create"),
        ("Cache hit", "hit"),
        ("Cost", "cost_usd"),
        ("Cost / turn", "cpt"),
        ("Output tokens / turn", "opt"),
        ("Cost / Mtok", "mtok"),
    ]

    html = '<h3 class="subsection-heading">Model Usage</h3>\n'
    html += '<table class="usage-table">\n'
    html += '<tr><th></th>'
    for label, _, _ in columns:
        if label == "Total":
            html += f'<th>{label}</th>'
        else:
            html += f'<th><code>{_esc(label)}</code></th>'
    html += '</tr>\n'

    for mlabel, mkey in metrics:
        html += f'<tr><th>{mlabel}</th>'
        for _, cur, bl in columns:
            cur_v = _fmt(cur, mkey)
            html += '<td>'
            html += f'<span class="cur-val">{cur_v}</span>'
            if has_bl:
                bl_v = _fmt(bl, mkey)
                if bl_v != cur_v:
                    arrow, delta_str, dcls = _delta(cur, bl, mkey)
                    delta_html = (f' <span class="delta {dcls}">{arrow} {delta_str}</span>'
                                  if delta_str else "")
                    html += f'<span class="bl-val">{bl_v}{delta_html}</span>'
            html += '</td>'
        html += '</tr>\n'
    html += '</table>\n'
    return html


def _render_scoring_summary(summary, config, baseline_summary=None):
    judges = summary.get("judges", {})
    thresholds = config.get("thresholds", {})
    bl_judges = baseline_summary.get("judges", {}) if baseline_summary else {}
    has_bl = bool(bl_judges)

    # Build judge type/model lookup from config
    judge_info = {}
    default_model = (config.get("models", {}).get("judge")
                     or os.environ.get("EVAL_JUDGE_MODEL")
                     or "—")
    for jc in config.get("judges", []):
        jname = jc.get("name", "")
        if jc.get("check"):
            judge_info[jname] = ("check", "—")
        elif jc.get("prompt") or jc.get("prompt_file"):
            judge_info[jname] = ("llm", jc.get("model") or default_model)
        elif jc.get("module"):
            judge_info[jname] = ("code", "—")

    html = "<h2>Scoring Summary</h2>\n<table>\n"
    html += f"<tr><th>Judge</th><th>Type</th><th>Metric</th><th>Value</th>"
    if has_bl:
        html += "<th>Baseline</th>"
    html += "<th>Threshold</th><th>Status</th></tr>\n"

    for judge_name, agg in sorted(judges.items()):
        if not isinstance(agg, dict):
            continue
        # Determine metric type and value
        pass_rate = agg.get("pass_rate")
        mean = agg.get("mean")

        if pass_rate is not None:
            metric_name = "pass_rate"
            metric_val = f"{pass_rate:.0%}"
        elif mean is not None:
            metric_name = "mean"
            metric_val = f"{mean:.2f}"
        else:
            metric_name = "—"
            metric_val = "—"

        # Baseline
        bl_val = ""
        if has_bl and judge_name in bl_judges:
            bl_agg = bl_judges[judge_name]
            if isinstance(bl_agg, dict):
                bl_pr = bl_agg.get("pass_rate")
                bl_mn = bl_agg.get("mean")
                if bl_pr is not None:
                    bl_val = f"{bl_pr:.0%}"
                elif bl_mn is not None:
                    bl_val = f"{bl_mn:.2f}"

        # Threshold and status
        thresh = thresholds.get(judge_name, {})
        thresh_str = "—"
        status_cls = "skip"
        status_label = "—"

        if pass_rate is None and mean is None:
            # No aggregatable values — distinguish skip from error by
            # checking per-case results for this judge
            per_case = summary.get("per_case", {})
            has_errors = any(
                isinstance((per_case.get(c) or {}).get(judge_name), dict)
                and (per_case[c][judge_name].get("error")
                     or per_case[c][judge_name].get("value") is False)
                for c in per_case
            )
            if has_errors:
                status_cls = "fail"
                status_label = "ERROR"
            else:
                status_cls = "skip"
                status_label = "SKIP"
        elif isinstance(thresh, dict):
            if "min_pass_rate" in thresh and pass_rate is not None:
                thresh_str = f"&ge; {_pct(thresh['min_pass_rate'])}"
                ok = pass_rate >= thresh["min_pass_rate"]
                status_cls = "pass" if ok else "fail"
                status_label = "PASS" if ok else "FAIL"
            elif "min_mean" in thresh and mean is not None:
                thresh_str = f"&ge; {thresh['min_mean']}"
                ok = mean >= thresh["min_mean"]
                status_cls = "pass" if ok else "fail"
                status_label = "PASS" if ok else "FAIL"

        jtype, jmodel = judge_info.get(judge_name, ("—", "—"))
        type_label = jtype if jtype == "check" else f'{jtype} ({jmodel.split("@")[0]})'
        html += f'<tr class="metric-row"><td>{_esc(judge_name)}</td>'
        html += f'<td class="judge-type">{_esc(type_label)}</td>'
        html += f"<td>{metric_name}</td><td>{metric_val}</td>"
        if has_bl:
            html += f"<td>{bl_val}</td>"
        html += f'<td>{thresh_str}</td>'
        html += f'<td><span class="{status_cls}">{status_label}</span></td></tr>\n'

    # Pairwise summary row (if available)
    pw = summary.get("pairwise")
    if pw and not pw.get("error"):
        wins_a = pw.get("wins_a", 0)
        wins_b = pw.get("wins_b", 0)
        ties = pw.get("ties", 0)
        errors = pw.get("errors", 0)
        total = wins_a + wins_b + ties + errors
        pw_val = f"{wins_a}W / {wins_b}L / {ties}T"
        if errors:
            pw_val += f" / {errors}E"
        if wins_a > wins_b:
            pw_status_cls, pw_status = "pass", "WIN"
        elif wins_b > wins_a:
            pw_status_cls, pw_status = "fail", "LOSS"
        else:
            pw_status_cls, pw_status = "skip", "TIE"
        pw_jc = next((j for j in config.get("judges", []) if j.get("name") == "pairwise"), {})
        pw_model = pw_jc.get("model") or default_model
        html += (f'<tr class="metric-row"><td>pairwise</td>'
                 f'<td class="judge-type">llm ({pw_model.split("@")[0]})</td>'
                 f'<td>comparison</td><td>{pw_val}</td>')
        if has_bl:
            html += "<td>—</td>"
        html += f'<td>—</td><td><span class="{pw_status_cls}">{pw_status}</span></td></tr>\n'

    html += "</table>\n"
    return html


def _render_regressions(summary, config):
    judges = summary.get("judges", {})
    thresholds = config.get("thresholds", {})
    regressions = []

    for judge_name, thresh in thresholds.items():
        agg = judges.get(judge_name, {})
        if not isinstance(agg, dict) or not isinstance(thresh, dict):
            continue
        if "min_pass_rate" in thresh:
            rate = agg.get("pass_rate")
            if rate is not None and rate < thresh["min_pass_rate"]:
                regressions.append((judge_name, "pass_rate",
                                    f">= {_pct(thresh['min_pass_rate'])}", _pct(rate)))
        if "min_mean" in thresh:
            mean = agg.get("mean")
            if mean is not None and mean < thresh["min_mean"]:
                regressions.append((judge_name, "mean",
                                    f">= {thresh['min_mean']}", f"{mean:.2f}"))

    if not regressions:
        return ""

    html = "<h2>Regressions</h2>\n<table>\n"
    html += "<tr><th>Judge</th><th>Metric</th><th>Threshold</th><th>Actual</th></tr>\n"
    for judge, metric, expected, actual in regressions:
        html += (f'<tr><td>{_esc(judge)}</td><td>{metric}</td>'
                 f'<td>{expected}</td><td><span class="fail">{actual}</span></td></tr>\n')
    html += "</table>\n"
    return html


def _render_pairwise(summary):
    pw = summary.get("pairwise")
    if not pw:
        return ""

    html = "<h2>Pairwise Comparison</h2>\n"
    html += (f"<p><strong>{_esc(str(pw.get('run_a', 'A')))}</strong> vs "
             f"<strong>{_esc(str(pw.get('run_b', 'B')))}</strong> "
             f"({pw.get('cases_compared', '?')} cases)</p>\n")
    html += (f"<p>Wins: {pw.get('wins_a', 0)} | "
             f"Losses: {pw.get('wins_b', 0)} | "
             f"Ties: {pw.get('ties', 0)}")
    if pw.get("errors"):
        html += f" | Errors: {pw['errors']}"
    html += "</p>\n"

    per_case = pw.get("per_case", [])
    if per_case:
        html += "<p>"
        for pc in per_case:
            cid = pc.get("case_id", "?")
            winner = pc.get("winner", "error")
            html += f"{_esc(cid)} {_pairwise_badge(winner)} "
        html += "</p>\n"

    return html


def _parse_analysis_frontmatter(content: str):
    """Split optional YAML frontmatter from the analysis body.

    Returns (meta_dict, body_markdown). When the file has no frontmatter,
    meta_dict is empty and body_markdown is the unchanged content.
    """
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    # Find the closing '---' line (must be on its own line, after line 0)
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, content
    raw = "\n".join(lines[1:end_idx])
    try:
        meta = yaml.safe_load(raw) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")
    return meta, body


def _build_analysis_subtitle(meta, summary, run_result, baseline_summary):
    """Construct a dynamic subtitle paragraph for the analysis section.

    Pulls the analysis author from the frontmatter, run shape from summary,
    and execution metadata from run_result. Falls back gracefully when fields
    are missing.
    """
    author_agent = (meta or {}).get("agent")
    author_model = (meta or {}).get("model")
    author_date = (meta or {}).get("date")
    if author_agent and author_model:
        author_html = (f"<strong>{_esc(str(author_agent))}</strong> "
                       f"(<code>{_esc(str(author_model))}</code>)")
    elif author_model:
        author_html = f"<strong><code>{_esc(str(author_model))}</code></strong>"
    elif author_agent:
        author_html = f"<strong>{_esc(str(author_agent))}</strong>"
    else:
        author_html = "<strong>Claude Code</strong>"

    date_html = (f" on <code>{_esc(str(author_date))}</code>"
                 if author_date else "")

    judge_count = len((summary or {}).get("judges") or {})
    case_count = len((summary or {}).get("per_case") or {})

    scope_bits = []
    if judge_count:
        scope_bits.append(f"{judge_count} judge score{'s' if judge_count != 1 else ''}")
    if case_count:
        scope_bits.append(f"{case_count} case{'s' if case_count != 1 else ''}")
    scope = " across ".join(scope_bits) if scope_bits else "this run's results"

    rr = run_result or {}
    metric_parts = []
    dur = rr.get("duration_s")
    if isinstance(dur, (int, float)) and dur > 0:
        metric_parts.append(f"{dur / 60:.0f} min" if dur >= 60 else f"{dur:.0f}s")
    cost = rr.get("cost_usd")
    if isinstance(cost, (int, float)) and cost > 0:
        metric_parts.append(f"${cost:.2f}")
    turns = rr.get("num_turns")
    if isinstance(turns, int) and turns > 0:
        metric_parts.append(f"{turns:,} turns")
    exit_code = rr.get("exit_code")
    if exit_code is not None:
        metric_parts.append(f"exit {exit_code}")
    metrics_html = f" ({', '.join(metric_parts)})" if metric_parts else ""

    baseline_html = ""
    bl_run = (baseline_summary or {}).get("run_id")
    if bl_run:
        baseline_html = f" Baseline: <code>{_esc(str(bl_run))}</code>."

    return (
        f"Generated by {author_html}{date_html} from {scope}, "
        f"plus execution metadata{metrics_html}.{baseline_html} "
        "Recommendation leads; failure patterns, root causes, and regressions "
        "follow as supporting evidence."
    )


def _render_analysis(run_dir, summary=None, run_result=None, baseline_summary=None):
    """Render the agent's analysis (recommendation + supporting findings) if saved.

    The analysis is wrapped in a visually distinct callout and placed near the
    top of the report so the agent's recommendation is highly visible. The
    subtitle is built dynamically from optional YAML frontmatter (author model,
    date) plus the run's summary and run_result.
    """
    analysis_path = run_dir / "analysis.md"
    if not analysis_path.exists():
        return ""

    content = analysis_path.read_text().strip()
    if not content:
        return ""

    meta, body_md = _parse_analysis_frontmatter(content)
    subtitle = _build_analysis_subtitle(meta, summary, run_result, baseline_summary)
    body = _md_to_html(body_md)
    return (
        '<section class="analysis">\n'
        '<div class="analysis-banner"><h2>Analysis</h2></div>\n'
        f'<p class="section-intro">{subtitle}</p>\n'
        f'<div class="analysis-body">\n{body}\n</div>\n'
        '</section>\n'
    )


def _normalize_escapes(text: str) -> str:
    """Convert JSON-style escape sequences (literal \\n, \\t) sometimes
    emitted by judge LLMs inside their rationale into the real characters.
    Only handles the safe subset; ignores \\x / \\u to avoid surprises."""
    if not text or "\\" not in text:
        return text
    return (text.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\t", "\t"))


import re as _re
from urllib.parse import urlsplit as _urlsplit

_SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


def _safe_href(raw: str) -> str | None:
    """Return a safe href value or None if the URL must be rejected.

    Defends against XSS via judge rationales / analysis.md content like
    `[click](javascript:alert(1))` or attribute-injection via stray quotes.
    Allows http/https/mailto URLs and relative paths/anchors only.

    `raw` is expected to already be HTML-escaped (since `_md_inline` escapes
    the full text before running the link regex), so we do not re-escape.
    """
    href = raw.strip()
    if not href:
        return None
    parsed = _urlsplit(href)
    if parsed.scheme:
        if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
            return None
    elif not href.startswith(("/", "#", "./", "../")):
        return None
    return href


def _md_inline(text: str) -> str:
    """Apply inline markdown formatting (bold, italic, code, links) to text.
    Always HTML-escapes first."""
    text = _esc(text)
    text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = _re.sub(r'`(.+?)`', r'<code>\1</code>', text)

    def _link_sub(match):
        label = match.group(1)
        href = _safe_href(match.group(2))
        if href is None:
            return label
        return f'<a href="{href}" rel="noopener noreferrer">{label}</a>'

    text = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _link_sub, text)
    return text


def _md_to_html(md_text):
    """Convert markdown to HTML. Handles headers, lists, tables, code blocks,
    bold, italic, inline code, and links."""
    import re

    lines = md_text.splitlines()
    out = []
    i = 0
    list_stack = []  # stack of "ul" or "ol"

    def _close_lists():
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    _inline = _md_inline

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            _close_lists()
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            out.append(f'<pre class="output">{_esc(chr(10).join(code_lines))}</pre>')
            i += 1
            continue

        # Table (line starts with |)
        if stripped.startswith("|") and "|" in stripped[1:]:
            _close_lists()
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            out.append(_md_table_to_html(table_lines))
            continue

        # Headers — # maps to h1, ## to h2, ### to h3
        if stripped.startswith("### "):
            _close_lists()
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            _close_lists()
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
            i += 1
            continue
        if stripped.startswith("# "):
            _close_lists()
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
            i += 1
            continue

        # Unordered list
        if stripped.startswith("- "):
            if not list_stack or list_stack[-1] != "ul":
                if list_stack and list_stack[-1] == "ol":
                    out.append(f"</{list_stack.pop()}>")
                list_stack.append("ul")
                out.append("<ul>")
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            i += 1
            continue

        # Ordered list
        m = re.match(r'^(\d+)\.\s(.+)', stripped)
        if m:
            if not list_stack or list_stack[-1] != "ol":
                if list_stack and list_stack[-1] == "ul":
                    out.append(f"</{list_stack.pop()}>")
                list_stack.append("ol")
                out.append("<ol>")
            out.append(f"<li>{_inline(m.group(2))}</li>")
            i += 1
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            _close_lists()
            out.append("<hr>")
            i += 1
            continue

        # Blank line
        if not stripped:
            _close_lists()
            i += 1
            continue

        # Paragraph
        _close_lists()
        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    _close_lists()
    return "\n".join(out)


def _md_table_to_html(table_lines):
    """Convert markdown table lines to an HTML table."""
    if len(table_lines) < 2:
        return ""

    def _parse_row(line):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return cells

    headers = _parse_row(table_lines[0])

    # Skip separator row (line with dashes/colons)
    data_start = 1
    if len(table_lines) > 1 and all(
            c.strip().replace("-", "").replace(":", "").replace(" ", "") == ""
            for c in table_lines[1].strip().strip("|").split("|")):
        data_start = 2

    html = "<table>\n<tr>"
    for h in headers:
        html += f"<th>{_md_inline(h)}</th>"
    html += "</tr>\n"

    for row_line in table_lines[data_start:]:
        cells = _parse_row(row_line)
        html += "<tr>"
        for c in cells:
            # Color-code PASS/FAIL cells: wrap content in a badge span so the
            # `display: inline-block` badge style does not break <td> layout.
            stripped = c.strip()
            # Strip bold/italic markdown for keyword matching
            bare = stripped.strip("*_")
            inner = _md_inline(c)
            if bare in ("PASS", "FIXED"):
                html += f'<td><span class="pass">{inner}</span></td>'
            elif bare in ("FAIL", "REGRESSION"):
                html += f'<td><span class="fail">{inner}</span></td>'
            elif bare in ("SKIP", "SKIPPED"):
                html += f'<td><span class="skip">{inner}</span></td>'
            else:
                html += f"<td>{inner}</td>"
        html += "</tr>\n"

    html += "</table>"
    return html


_img_compare_counter = 0


def _render_image_compare(gen_uri, ref_uri, gen_label="Generated",
                          ref_label="Reference", ref_class="img-label-ref"):
    global _img_compare_counter
    _img_compare_counter += 1
    uid = f"ic{_img_compare_counter}"
    return (
        f'<div class="img-compare">\n'
        f'  <div class="img-compare-tabs">\n'
        f'    <button class="img-compare-tab active" data-panel="{uid}-sbs">Side by side</button>\n'
        f'    <button class="img-compare-tab" data-panel="{uid}-swipe">Swipe</button>\n'
        f'    <button class="img-compare-tab" data-panel="{uid}-onion">Onion</button>\n'
        f'  </div>\n'
        f'  <div id="{uid}-sbs" class="img-compare-panel active">\n'
        f'    <div class="img-compare-sbs">\n'
        f'      <div><span class="img-label {ref_class}">{_esc(ref_label)}</span><br>'
        f'<img src="{ref_uri}" loading="lazy" alt="{_esc(ref_label)}"></div>\n'
        f'      <div><span class="img-label img-label-gen">{_esc(gen_label)}</span><br>'
        f'<img src="{gen_uri}" loading="lazy" alt="{_esc(gen_label)}"></div>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'  <div id="{uid}-swipe" class="img-compare-panel">\n'
        f'    <div class="img-compare-swipe">\n'
        f'      <div class="img-compare-swipe-imgs">\n'
        f'        <img src="{gen_uri}" alt="{_esc(gen_label)}">\n'
        f'        <img class="swipe-ref" src="{ref_uri}" alt="{_esc(ref_label)}">\n'
        f'        <div class="swipe-line"></div>\n'
        f'      </div>\n'
        f'      <input type="range" min="0" max="100" value="50">\n'
        f'    </div>\n'
        f'    <div class="img-compare-swipe-labels">'
        f'<span>◀ {_esc(gen_label)}</span><span>{_esc(ref_label)} ▶</span></div>\n'
        f'  </div>\n'
        f'  <div id="{uid}-onion" class="img-compare-panel">\n'
        f'    <div class="img-compare-onion">\n'
        f'      <div class="img-compare-onion-wrap">\n'
        f'        <img src="{ref_uri}" alt="{_esc(ref_label)}">\n'
        f'        <img class="onion-top" src="{gen_uri}" alt="{_esc(gen_label)}" style="opacity:0.5">\n'
        f'      </div>\n'
        f'      <input type="range" min="0" max="100" value="50">\n'
        f'      <div class="img-compare-swipe-labels">'
        f'<span>◀ {_esc(ref_label)}</span>'
        f'<span class="onion-label">{_esc(gen_label)} opacity: 50%</span>'
        f'<span>{_esc(gen_label)} ▶</span></div>\n'
        f'    </div>\n'
        f'  </div>\n'
        f'</div>\n'
    )


def _render_standalone_image(data_uri, label):
    return (
        f'<div class="img-artifact">\n'
        f'  <img src="{data_uri}" loading="lazy" alt="{_esc(label)}">\n'
        f'</div>\n'
    )


def _shared_output_paths(config):
    """Return set of output paths with batch_pattern '*' (shared across cases)."""
    return {o.get("path") for o in config.get("outputs", [])
            if o.get("batch_pattern") == "*" and o.get("path")}


def _render_shared_outputs(run_dir, config):
    """Render shared output files (batch_pattern='*') once before per-case."""
    shared_paths = _shared_output_paths(config)
    if not shared_paths:
        return ""

    cases_dir = run_dir / "cases"
    if not cases_dir.exists():
        return ""

    # Read shared files from the first case directory
    first_case = next((d for d in sorted(cases_dir.iterdir()) if d.is_dir()), None)
    if not first_case:
        return ""

    html = "<h2>Shared Outputs</h2>\n"
    html += '<p class="section-intro">These files are shared across all cases (not repeated per-case below).</p>\n'
    for out_path in sorted(shared_paths):
        shared_entry = first_case / out_path
        if not shared_entry.exists():
            continue
        if shared_entry.is_file():
            files = [shared_entry]
        else:
            files = sorted(f for f in shared_entry.rglob("*") if f.is_file())
        for f in files:
            rel = f.relative_to(first_case)
            if f.suffix == ".html":
                try:
                    html_content = f.read_text()
                    srcdoc = html_content.replace("&", "&amp;").replace('"', "&quot;")
                    html += (f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                             f'<iframe class="html-preview" srcdoc="{srcdoc}" '
                             f'sandbox="allow-same-origin"'
                             f'></iframe>\n')
                except (UnicodeDecodeError, OSError):
                    html += (f'<div class="file-badge">{_esc(str(rel))} '
                             f'<span class="skip">(could not read)</span></div>\n')
            else:
                content = _read_text(f, max_lines=200)
                if content:
                    html += (f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                             f'<pre class="output">{_esc(content)}</pre>\n')
    return html


def _render_per_case(summary, run_dir, config, baseline_dir, review):
    per_case = summary.get("per_case", {})
    if not per_case:
        return ""

    dataset_path = config.get("dataset", {}).get("path", "")
    shared_paths = _shared_output_paths(config)
    output_paths = [o.get("path", ".") for o in config.get("outputs", [])
                    if o.get("path") and o.get("path") not in shared_paths]
    feedback = review.get("feedback", {}) if review else {}
    cases_dir = run_dir / "cases"
    bl_cases_dir = baseline_dir / "cases" if baseline_dir else None

    # Build pairwise lookup per case
    pw_by_case = {}
    pw = summary.get("pairwise", {})
    for pc in pw.get("per_case", []):
        pw_by_case[pc.get("case_id", "")] = pc.get("winner", "error")

    html = '<h2 class="section-heading">Per-Case Details</h2>\n'
    if baseline_dir:
        html += (f'<p class="section-intro">Comparing <strong>{run_dir.name}</strong> vs '
                 f'<strong>{baseline_dir.name}</strong></p>\n')

    for case_id in sorted(per_case.keys()):
        case_results = per_case[case_id]
        if not isinstance(case_results, dict):
            continue

        case_dir = cases_dir / case_id
        label = case_id

        # Count pass/fail/skip — bool judges: True=pass, False=fail
        # Numeric judges (LLM): pass if score >= threshold min_mean
        # None values: skipped (condition was false)
        thresholds = config.get("thresholds", {})
        passed = 0
        failed = 0
        skipped = 0
        total = len(case_results)
        for jname, r in case_results.items():
            if not isinstance(r, dict):
                continue
            val = r.get("value")
            if val is None:
                skipped += 1
            elif val is True:
                passed += 1
            elif val is False:
                failed += 1
            elif isinstance(val, (int, float)):
                thresh = thresholds.get(jname, {})
                min_mean = thresh.get("min_mean") if isinstance(thresh, dict) else None
                if min_mean is not None:
                    if val >= min_mean:
                        passed += 1
                    else:
                        failed += 1
                else:
                    passed += 1  # no threshold defined, count as pass
        status = "pass" if failed == 0 else "fail"

        # Pairwise badge or pass/fail accent — applied as a left-border class
        pw_badge = ""
        if case_id in pw_by_case:
            pw_winner = pw_by_case[case_id]
            pw_badge = f" {_pairwise_badge(pw_winner)}"
            pw_class_map = {"A": "case-pw-a", "B": "case-pw-b",
                            "tie": "case-pw-tie", "error": "case-pw-error"}
            accent_class = pw_class_map.get(pw_winner, "case-pw-error")
        else:
            accent_class = f"case-{status}"

        scored = total - skipped
        skip_note = f", {skipped} skip" if skipped else ""
        html += (f'<details open class="case {accent_class}"><summary>'
                 f'<span class="{status}">{_esc(str(label))}</span> '
                 f'<span class="skip">({passed}/{scored} pass{skip_note})</span>'
                 f'{pw_badge}</summary>\n')

        # Judge results table
        html += '<table><tr><th>Judge</th><th>Value</th><th>Rationale</th></tr>\n'
        for jname, jresult in sorted(case_results.items()):
            if not isinstance(jresult, dict):
                continue
            val = jresult.get("value")
            rat = str(jresult.get("rationale", ""))
            err = jresult.get("error", "")

            if val is None:
                val_html = '<span class="skip">SKIP</span>'
            elif val is True:
                val_html = '<span class="pass">PASS</span>'
            elif val is False:
                val_html = '<span class="fail">FAIL</span>'
            elif isinstance(val, (int, float)):
                thresh = thresholds.get(jname, {})
                min_mean = thresh.get("min_mean") if isinstance(thresh, dict) else None
                if min_mean is not None and val < min_mean:
                    val_html = f'<span class="fail">{val}</span>'
                else:
                    val_html = f'<span class="pass">{val}</span>'
            else:
                val_html = _esc(str(val)[:100])

            if err:
                rat = f"ERROR: {err}"

            rat_html = _md_to_html(_normalize_escapes(rat)) if rat else ""
            html += (f'<tr><td>{_esc(jname)}</td><td>{val_html}</td>'
                     f'<td class="rationale">{rat_html}</td></tr>\n')
        html += "</table>\n"

        # Human feedback
        case_feedback = feedback.get(case_id, "")
        if case_feedback:
            html += (f'<div class="feedback-box"><strong>Human feedback:</strong> '
                     f'{_esc(str(case_feedback))}</div>\n')

        # Input data
        if dataset_path:
            input_text = _read_case_input(dataset_path, case_id)
            if input_text:
                html += (f'<details open><summary>Input</summary>'
                         f'<pre class="output">{_esc(input_text)}</pre></details>\n')

        # Output files — when baseline is provided, skip files under output_paths
        # since those will appear in the baseline diff section below.
        has_baseline = bl_cases_dir and (bl_cases_dir / case_id).exists()

        # Resolve gold standard image for comparison
        gold_img_path = _find_gold_standard_image(case_id, dataset_path)
        gold_data_uri = _image_to_data_uri(gold_img_path) if gold_img_path else ""

        # Execution logs at the case root — not skill output, exclude from
        # the report.  Only exclude root-level files, not nested ones with the
        # same name (a skill could legitimately produce artifacts/stdout.log).
        _EXEC_LOG_PATHS = {"stdout.log", "stderr.log", "run_result.json"}
        if case_dir.exists():
            files = sorted(f for f in case_dir.rglob("*") if f.is_file()
                           and str(f.relative_to(case_dir)) not in _EXEC_LOG_PATHS
                           and not str(f.relative_to(case_dir)).startswith("subagents/")
                           and not any(str(f.relative_to(case_dir)).startswith(sp)
                                       for sp in shared_paths))
            if has_baseline:
                # Exclude files under output_paths — they'll be in the diff
                files = [f for f in files
                         if not any(str(f.relative_to(case_dir)).startswith(op)
                                    for op in output_paths)]
            if files:
                html += "<details open><summary>Output files</summary>\n"
                for f in files:
                    rel = f.relative_to(case_dir)
                    if f.suffix == ".html":
                        # Render HTML files inline in a sandboxed iframe
                        try:
                            html_content = f.read_text()
                            # Escape for srcdoc attribute (double-escape quotes)
                            srcdoc = (html_content
                                      .replace("&", "&amp;")
                                      .replace('"', "&quot;"))
                            html += (f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                                     f'<iframe class="html-preview" srcdoc="{srcdoc}" '
                                     f'sandbox="allow-same-origin" '
                                     f'onload="this.style.height=this.contentDocument.documentElement.scrollHeight+20+\'px\'"'
                                     f'></iframe>\n')
                        except (UnicodeDecodeError, OSError):
                            html += (f'<div class="file-badge">{_esc(str(rel))} '
                                     f'<span class="skip">(could not read)</span></div>\n')
                    elif _is_image_file(f):
                        data_uri = _image_to_data_uri(f)
                        if data_uri:
                            html += f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                            if gold_data_uri:
                                html += _render_image_compare(
                                    data_uri, gold_data_uri,
                                    gen_label="Generated",
                                    ref_label="Gold Standard",
                                    ref_class="img-label-ref")
                            else:
                                html += _render_standalone_image(data_uri, str(rel))
                        else:
                            size = f.stat().st_size
                            html += (f'<div class="file-badge">{_esc(str(rel))} '
                                     f'<span class="skip">({size} bytes, unreadable)</span></div>\n')
                    else:
                        content = _read_text(f, max_lines=200)
                        if content:
                            html += (f'<div class="file-badge">{_esc(str(rel))}</div>\n'
                                     f'<pre class="output">{_esc(content)}</pre>\n')
                        else:
                            size = f.stat().st_size
                            html += (f'<div class="file-badge">{_esc(str(rel))} '
                                     f'<span class="skip">({size} bytes, binary)</span></div>\n')
                html += "</details>\n"

        # Baseline diff
        if has_baseline:
            bl_case_dir = bl_cases_dir / case_id
            diffs = []
            for out_path in output_paths:
                curr_dir = case_dir / out_path if out_path != "." else case_dir
                base_dir = bl_case_dir / out_path if out_path != "." else bl_case_dir
                if not curr_dir.exists() and not base_dir.exists():
                    continue
                curr_files = {f.name: f for f in curr_dir.iterdir() if f.is_file()} if curr_dir.exists() else {}
                base_files = {f.name: f for f in base_dir.iterdir() if f.is_file()} if base_dir.exists() else {}
                for name in sorted(set(curr_files) | set(base_files)):
                    curr_f = curr_files.get(name)
                    base_f = base_files.get(name)
                    # Image comparison
                    if ((curr_f and _is_image_file(curr_f))
                            or (base_f and _is_image_file(base_f))):
                        curr_uri = _image_to_data_uri(curr_f) if curr_f else ""
                        base_uri = _image_to_data_uri(base_f) if base_f else ""
                        if curr_uri and base_uri:
                            diffs.append((f"{out_path}/{name}",
                                _render_image_compare(
                                    curr_uri, base_uri,
                                    gen_label="Current",
                                    ref_label="Baseline",
                                    ref_class="img-label-bl")))
                        elif curr_uri:
                            diffs.append((f"{out_path}/{name} (new)",
                                _render_standalone_image(curr_uri, name)))
                        continue
                    # Text comparison
                    try:
                        ct = curr_f.read_text() if curr_f else ""
                        bt = base_f.read_text() if base_f else ""
                    except (UnicodeDecodeError, OSError):
                        continue
                    if ct != bt:
                        diff_html = _side_by_side_diff(
                            bt, ct,
                            left_label=f"baseline/{out_path}/{name}",
                            right_label=f"current/{out_path}/{name}")
                        diffs.append((f"{out_path}/{name}", diff_html))

            if diffs:
                html += "<details open><summary>Baseline diff</summary>\n"
                for fname, diff_html in diffs:
                    html += f'<div class="file-badge">{_esc(fname)}</div>\n{diff_html}\n'
                html += "</details>\n"

        html += "</details>\n"

    return html


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _wrap_section(content: str) -> str:
    """Wrap a section's HTML in a styled card. Returns empty if content is empty."""
    if not content.strip():
        return ""
    return f'<section class="section">\n{content}</section>\n'


def generate_report(config, summary, run_result, run_dir,
                    review=None, baseline_dir=None,
                    baseline_summary=None, baseline_result=None):
    name = config.get("name", "Eval")
    run_id = summary.get("run_id", run_dir.name)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(name)} — {_esc(run_id)}</title>
<script>{THEME_SCRIPT}</script>
<style>{CSS}</style>
</head>
<body>
<button id="theme-toggle" type="button" aria-label="Toggle theme">\u263E</button>
"""
    baseline_id = None
    if baseline_summary:
        baseline_id = baseline_summary.get("run_id")
    if not baseline_id and baseline_dir:
        baseline_id = baseline_dir.name
    html += _render_header(config, run_id, run_result, baseline_id)
    html += _wrap_section(_render_run_config(run_result, baseline_result))
    html += _render_analysis(run_dir, summary, run_result, baseline_summary)
    html += _wrap_section(_render_scoring_summary(summary, config, baseline_summary))
    html += _wrap_section(_render_regressions(summary, config))
    html += _wrap_section(_render_shared_outputs(run_dir, config))
    html += _render_per_case(summary, run_dir, config, baseline_dir, review)
    global _img_compare_counter
    _img_compare_counter = 0
    html += f"\n<script>{TOGGLE_SCRIPT}</script>\n"
    html += f"<script>{IMAGE_COMPARE_SCRIPT}</script>\n"
    html += "</body>\n</html>\n"
    return html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", default="eval.yaml")
    parser.add_argument("--baseline", default=None,
                        help="Baseline run ID for comparison")
    parser.add_argument("--open", action="store_true",
                        help="Open report in browser")
    args = parser.parse_args()

    runs_dir = Path(os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    run_dir = runs_dir / args.run_id
    baseline_dir = runs_dir / args.baseline if args.baseline else None

    if not run_dir.exists():
        print(f"ERROR: run directory not found: {run_dir}", file=sys.stderr)
        sys.exit(1)

    config = _load_yaml(Path(args.config))
    summary = _load_yaml(run_dir / "summary.yaml")
    run_result = _load_json(run_dir / "run_result.json")
    review = _load_yaml(run_dir / "review.yaml") or None

    baseline_summary = _load_yaml(baseline_dir / "summary.yaml") if baseline_dir else None
    baseline_result = _load_json(baseline_dir / "run_result.json") if baseline_dir else None

    html = generate_report(
        config=config,
        summary=summary,
        run_result=run_result,
        run_dir=run_dir,
        review=review,
        baseline_dir=baseline_dir,
        baseline_summary=baseline_summary,
        baseline_result=baseline_result,
    )

    output_path = run_dir / "report.html"
    output_path.write_text(html)
    print(f"REPORT: {output_path}")

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{output_path.resolve()}")


if __name__ == "__main__":
    main()
