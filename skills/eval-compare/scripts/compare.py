#!/usr/bin/env python3
"""Compare evaluation results across multiple models or runs.

Usage:
    compare.py discover <input-dir>
    compare.py generate <input-dir> --output <dir> [--title TEXT] [--overview TEXT]
"""

import argparse
import json
import shutil
import sys
from collections import defaultdict
from html import escape
from pathlib import Path

import yaml


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def discover_runs(input_dir):
    input_dir = Path(input_dir)
    runs = []
    for d in sorted(input_dir.iterdir()):
        if not d.is_dir():
            continue
        summary_path = d / "summary.yaml"
        if not summary_path.exists():
            continue
        summary = load_yaml(summary_path)
        run = {
            "dir": str(d),
            "name": d.name,
            "summary": summary,
            "run_result": None,
            "html_report": None,
        }
        result_path = d / "run_result.json"
        if result_path.exists():
            run["run_result"] = load_json(result_path)
        html_path = d / "eval-report-summary.html"
        if html_path.exists():
            run["html_report"] = str(html_path)
        runs.append(run)
    return runs


def get_model(run):
    if run["run_result"]:
        return run["run_result"].get("model", "unknown")
    run_id = run["summary"].get("run_id", "")
    for token in run_id.split("-"):
        if token.startswith("claude"):
            return run_id.split("-", 3)[-1] if "claude" in run_id else "unknown"
    return "unknown"


def get_metric(run, key, default=None):
    rr = run["run_result"] or {}
    if key == "cost_usd":
        return rr.get("cost_usd", default)
    if key == "num_turns":
        return rr.get("num_turns", default)
    if key == "wall_clock_s":
        return rr.get("wall_clock_s", default)
    if key == "output_tokens":
        return (rr.get("token_usage") or {}).get("output", default)
    if key == "cache_hit_rate":
        return (run["summary"].get("run_metrics") or {}).get("cache_hit_rate", default)
    if key == "cost_per_turn":
        return (run["summary"].get("run_metrics") or {}).get("cost_per_turn_usd", default)
    if key == "output_tokens_per_turn":
        return (run["summary"].get("run_metrics") or {}).get("output_tokens_per_turn", default)
    return default


def get_judge_mean(run, judge_name):
    judges = run["summary"].get("judges", {})
    j = judges.get(judge_name, {})
    if isinstance(j, dict):
        v = j.get("mean")
        if v is not None:
            return v
        return j.get("pass_rate")
    return None


def get_case_score(run, case_name, judge_name):
    per_case = run["summary"].get("per_case", {})
    case = per_case.get(case_name, {})
    judge = case.get(judge_name, {})
    return judge.get("value")


def get_all_judge_names(runs):
    names = set()
    for r in runs:
        for k in (r["summary"].get("judges") or {}):
            names.add(k)
    return sorted(names)


def get_all_case_names(runs):
    names = set()
    for r in runs:
        for k in (r["summary"].get("per_case") or {}):
            names.add(k)
    return sorted(names)


def group_by_model(runs):
    groups = defaultdict(list)
    for r in runs:
        groups[get_model(r)].append(r)
    return dict(groups)


def aggregate(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return {"avg": None, "min": None, "max": None, "count": 0}
    return {
        "avg": sum(clean) / len(clean),
        "min": min(clean),
        "max": max(clean),
        "count": len(clean),
    }


MODEL_COLORS = {
    "claude-opus-4-6": "#58a6ff",
    "claude-opus-4-7": "#bc8cff",
    "claude-opus-4-8": "#db6d28",
    "claude-sonnet-4-6": "#f0883e",
    "claude-sonnet-4-6[1m]": "#f0883e",
    "claude-haiku-4-5": "#f85149",
}

MODEL_SHORT = {
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-8": "Opus 4.8",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-6[1m]": "Sonnet 4.6 [1M]",
    "claude-haiku-4-5": "Haiku 4.5",
}


def short_name(model):
    return MODEL_SHORT.get(model, model)


def color_for(model):
    return MODEL_COLORS.get(model, "#8b949e")


def fmt(v, fmt_type="num"):
    if v is None:
        return "--"
    if fmt_type == "usd":
        return f"${v:,.2f}"
    if fmt_type == "pct":
        return f"{v * 100:.1f}%"
    if fmt_type == "int":
        return f"{int(v):,}"
    if fmt_type == "time":
        return f"{int(v / 60)} min"
    if fmt_type == "tokens":
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        return f"{int(v / 1000)}K"
    return f"{v:.2f}"


def fmt_range(agg, fmt_type="num"):
    if agg["count"] == 0:
        return "--"
    if agg["count"] == 1:
        return fmt(agg["avg"], fmt_type)
    return f"{fmt(agg['avg'], fmt_type)} ({fmt(agg['min'], fmt_type)}-{fmt(agg['max'], fmt_type)})"


CSS = """
:root {
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2333;
  --border: #30363d; --text: #e6edf3; --text-muted: #8b949e;
  --accent: #58a6ff; --green: #3fb950; --yellow: #d29922;
  --red: #f85149; --orange: #db6d28; --purple: #bc8cff;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.6; }
.header { background: linear-gradient(135deg, #1a1e2e 0%, #0d1117 100%);
           border-bottom: 1px solid var(--border); padding: 24px 32px; }
.header h1 { font-size: 24px; font-weight: 600; margin-bottom: 4px; }
.header .subtitle { color: var(--text-muted); font-size: 14px; }
.tab-bar { display: flex; background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 0 16px; overflow-x: auto; }
.tab-bar button { background: none; border: none; color: var(--text-muted); padding: 12px 20px;
                  cursor: pointer; font-size: 14px; font-weight: 500;
                  border-bottom: 2px solid transparent; white-space: nowrap; transition: all 0.15s; }
.tab-bar button:hover { color: var(--text); background: rgba(255,255,255,0.03); }
.tab-bar button.active { border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.page { max-width: 1400px; margin: 0 auto; padding: 24px 32px; }
.overview { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px;
            padding: 20px 24px; margin-bottom: 20px; font-size: 14px; line-height: 1.7; color: var(--text-muted); }
.verdict { background: linear-gradient(135deg, rgba(63,185,80,0.08) 0%, rgba(88,166,255,0.06) 100%);
           border: 1px solid rgba(63,185,80,0.25); border-radius: 12px;
           padding: 24px 28px; margin-bottom: 28px; }
.verdict h2 { font-size: 18px; color: var(--green); margin-bottom: 10px; }
.verdict p { font-size: 15px; line-height: 1.7; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
         gap: 16px; margin-bottom: 28px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
        padding: 20px 24px; position: relative; }
.card .badge { position: absolute; top: -10px; right: 16px; font-size: 11px; font-weight: 700;
               padding: 3px 10px; border-radius: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.card h3 { font-size: 18px; font-weight: 600; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px; }
.stat { display: flex; flex-direction: column; }
.stat .label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; display: block; }
.stat .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; display: block; }
.green { color: var(--green); } .yellow { color: var(--yellow); }
.red { color: var(--red); } .muted { color: var(--text-muted); }
section { margin-bottom: 32px; }
section h2 { font-size: 17px; font-weight: 600; margin-bottom: 14px; padding-bottom: 8px;
             border-bottom: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th { text-align: left; padding: 10px 12px; background: var(--surface);
           border-bottom: 1px solid var(--border); font-weight: 600; color: var(--text-muted);
           text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
tbody td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
tbody tr:hover { background: rgba(255,255,255,0.02); }
.best { background: rgba(63,185,80,0.08); font-weight: 600; color: var(--green); }
.worst { background: rgba(248,81,73,0.06); color: var(--red); }
.insight { background: var(--surface2); border-left: 3px solid var(--accent); padding: 14px 18px;
           margin: 14px 0; border-radius: 0 6px 6px 0; font-size: 14px; line-height: 1.6; }
.iframe-wrap { width: 100%; height: calc(100vh - 90px); border: none; }
.run-link { padding: 8px 16px; background: var(--surface); border-bottom: 1px solid var(--border); font-size: 12px; }
.sub-bar { display: flex; align-items: center; gap: 4px; padding: 8px 16px;
           background: var(--surface); border-bottom: 1px solid var(--border); }
.sub-bar button { background: none; border: none; color: var(--text-muted); padding: 6px 14px;
                  cursor: pointer; font-size: 13px; border-bottom: 2px solid transparent; font-weight: 500; }
.sub-bar button.active { color: var(--accent); border-bottom-color: var(--accent); }
@media (max-width: 900px) { .cards { grid-template-columns: 1fr; } .page { padding: 16px; } }
"""


def _rank_color(value, all_values, higher_is_better):
    """Color a value green/yellow/red based on its rank among all_values."""
    if value is None:
        return "muted"
    clean = sorted([v for v in all_values if v is not None],
                   reverse=higher_is_better)
    if len(clean) < 2:
        return ""
    if value == clean[0]:
        return "green"
    if value == clean[-1]:
        return "red"
    return "yellow"


def best_worst_indices(values, higher_is_better=True):
    clean = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(clean) < 2:
        return None, None
    if higher_is_better:
        best_i = max(clean, key=lambda x: x[1])[0]
        worst_i = min(clean, key=lambda x: x[1])[0]
    else:
        best_i = min(clean, key=lambda x: x[1])[0]
        worst_i = max(clean, key=lambda x: x[1])[0]
    if values[best_i] == values[worst_i]:
        return None, None
    return best_i, worst_i


def render_comparison_table(models, rows, higher_is_better_map=None):
    if higher_is_better_map is None:
        higher_is_better_map = {}
    html = "<table><thead><tr><th>Metric</th>"
    for m in models:
        html += f"<th>{escape(short_name(m))}</th>"
    html += "</tr></thead><tbody>"
    for label, values, fmt_type in rows:
        higher = higher_is_better_map.get(label, True)
        best_i, worst_i = best_worst_indices(values, higher)
        html += f"<tr><td>{escape(label)}</td>"
        for i, v in enumerate(values):
            cls = ""
            if i == best_i:
                cls = ' class="best"'
            elif i == worst_i:
                cls = ' class="worst"'
            html += f"<td{cls}>{fmt(v, fmt_type)}</td>"
        html += "</tr>"
    html += "</tbody></table>"
    return html


def generate_report(runs, title, overview, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    groups = group_by_model(runs)
    # Sort models: best analysis quality first
    models = sorted(groups.keys(),
                    key=lambda m: aggregate([get_judge_mean(r, "analysis_quality")
                                             for r in groups[m]]).get("avg") or 0,
                    reverse=True)

    model_aggs = {}
    for m, model_runs in groups.items():
        agg = {}
        for key in ["cost_usd", "num_turns", "wall_clock_s", "output_tokens",
                     "cache_hit_rate", "cost_per_turn", "output_tokens_per_turn"]:
            agg[key] = aggregate([get_metric(r, key) for r in model_runs])
        for judge in get_all_judge_names(runs):
            agg[f"judge_{judge}"] = aggregate([get_judge_mean(r, judge) for r in model_runs])
        model_aggs[m] = agg

    best_quality_model = max(models, key=lambda m: (model_aggs[m].get("judge_analysis_quality", {}).get("avg") or 0))
    cheapest_model = min(models, key=lambda m: (model_aggs[m]["cost_usd"].get("avg") or float("inf")))

    # Copy HTML reports
    for r in runs:
        if r["html_report"]:
            dest = output_dir / r["name"]
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r["html_report"], dest / "eval-report-summary.html")

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
<style>{CSS}</style>
</head>
<body>

<div class="header">
  <h1>{escape(title)}</h1>
  <div class="subtitle">{len(runs)} eval runs across {len(models)} models</div>
</div>

<div class="tab-bar" id="tabBar">
  <button class="active" data-tab="comparison" data-color="var(--accent)">Comparison</button>
"""
    for m in models:
        model_runs = groups[m]
        label = short_name(m)
        if len(model_runs) > 1:
            label += f" ({len(model_runs)} runs)"
        c = color_for(m)
        html += f'  <button data-tab="{escape(m)}" data-color="{c}">{escape(label)}</button>\n'

    html += "</div>\n\n"

    # Comparison tab
    html += '<div class="tab-content active" id="tab-comparison">\n<div class="page">\n'

    if overview:
        html += f'<div class="overview">{overview}</div>\n'

    # Bottom Line: auto-generated summary, agent replaces with LLM analysis in Step 3
    html += '<div class="verdict">\n<h2>Bottom Line</h2>\n<p>'
    for m in models:
        agg = model_aggs[m]
        aq = agg.get("judge_analysis_quality", {}).get("avg")
        cost = agg["cost_usd"].get("avg")
        parts = []
        if aq is not None:
            parts.append(f"{aq:.2f}/5 quality")
        if cost is not None:
            parts.append(f"${cost:.2f}/run")
        detail = ", ".join(parts)
        html += f'<strong>{escape(short_name(m))}</strong>: {detail}. '
    html += "</p>\n</div>\n\n"

    # Pre-compute all values for relative coloring
    all_aq = [model_aggs[m].get("judge_analysis_quality", {}).get("avg") for m in models]
    all_rs = [model_aggs[m].get("judge_revert_scoring_accuracy", {}).get("avg") for m in models]
    all_cost = [model_aggs[m]["cost_usd"].get("avg") for m in models]
    all_wall = [model_aggs[m]["wall_clock_s"].get("avg") for m in models]

    # Model cards
    html += '<div class="cards">\n'
    for m in models:
        agg = model_aggs[m]
        c = color_for(m)
        extra_style = ""
        badge = ""
        if m == best_quality_model and m == cheapest_model:
            extra_style = f' style="border-color: var(--green);"'
            badge = '<div class="badge" style="background: var(--green); color: #000;">Best Value</div>'
        elif m == best_quality_model:
            extra_style = f' style="border-color: var(--green);"'
            badge = '<div class="badge" style="background: var(--green); color: #000;">Best Quality</div>'

        aq = agg.get("judge_analysis_quality", {}).get("avg")
        rs = agg.get("judge_revert_scoring_accuracy", {}).get("avg")
        cost = agg["cost_usd"].get("avg")
        turns = agg["num_turns"].get("avg")
        out_tok = agg["output_tokens"].get("avg")
        wall = agg["wall_clock_s"].get("avg")

        html += f'<div class="card"{extra_style}>{badge}\n'
        html += f'  <h3><span class="dot" style="background:{c}"></span> {escape(short_name(m))}'
        n = len(groups[m])
        if n > 1:
            html += f' <span style="font-size:11px; color:var(--text-muted); font-weight:400;">({n} runs avg)</span>'
        html += '</h3>\n  <div class="stats">\n'

        html += f'    <div class="stat"><span class="label">Analysis Quality</span><span class="value {_rank_color(aq, all_aq, True)}">{fmt(aq, "num") if aq else "--"}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Revert Scoring</span><span class="value {_rank_color(rs, all_rs, True)}">{fmt(rs, "num") if rs else "--"}</span></div>\n'
        html += f'    <div class="stat"><span class="label">{"Avg Run Cost" if n > 1 else "Total Cost"}</span><span class="value {_rank_color(cost, all_cost, False)}">{fmt(cost, "usd")}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Wall Clock</span><span class="value {_rank_color(wall, all_wall, False)}">{fmt(wall, "time")}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Output Tokens</span><span class="value">{fmt(out_tok, "tokens")}</span></div>\n'
        html += f'    <div class="stat"><span class="label">Turns</span><span class="value">{fmt(turns, "int")}</span></div>\n'
        html += '  </div>\n</div>\n'
    html += '</div>\n\n'

    # Cost table
    html += '<section>\n<h2>Cost &amp; Efficiency</h2>\n'
    cost_rows = []
    for label, key, ft, hib in [
        ("Total Cost", "cost_usd", "usd", False),
        ("Output Tokens", "output_tokens", "tokens", False),
        ("Tokens / Turn", "output_tokens_per_turn", "int", True),
        ("Total Turns", "num_turns", "int", False),
        ("Wall Clock", "wall_clock_s", "time", False),
        ("Cost / Turn", "cost_per_turn", "usd", False),
        ("Cache Hit Rate", "cache_hit_rate", "pct", True),
    ]:
        values = [model_aggs[m][key].get("avg") for m in models]
        cost_rows.append((label, values, ft))
    hib_map = {"Total Cost": False, "Output Tokens": False, "Total Turns": False,
               "Wall Clock": False, "Cost / Turn": False, "Tokens / Turn": True, "Cache Hit Rate": True}
    html += render_comparison_table(models, cost_rows, hib_map)
    html += '</section>\n\n'

    # Quality table
    all_judges = get_all_judge_names(runs)
    html += '<section>\n<h2>Quality Scores</h2>\n'
    quality_rows = []
    for judge in all_judges:
        values = []
        for m in models:
            agg = model_aggs[m].get(f"judge_{judge}", {})
            v = agg.get("avg")
            n = agg.get("count", 0)
            values.append(v)
        is_pass_rate = all(v is not None and v <= 1.0 for v in values if v is not None)
        ft = "pct" if is_pass_rate and judge not in ("analysis_quality", "revert_scoring_accuracy") else "num"
        label = judge.replace("_", " ").title()
        quality_rows.append((label, values, ft))
    html += render_comparison_table(models, quality_rows)
    html += '</section>\n\n'

    # Per-case analysis quality
    all_cases = get_all_case_names(runs)
    if all_cases:
        html += '<section>\n<h2>Per-Case Analysis Quality</h2>\n'
        html += "<table><thead><tr><th>Case</th>"
        for m in models:
            html += f"<th>{escape(short_name(m))}</th>"
        html += "</tr></thead><tbody>"
        for case in all_cases:
            case_short = case.replace("case-", "").replace("-", " ", 1).split(" ", 1)
            label = case_short[1] if len(case_short) > 1 else case
            html += f"<tr><td>{escape(label)}</td>"
            values = []
            for m in models:
                scores = [get_case_score(r, case, "analysis_quality") for r in groups[m]]
                agg = aggregate(scores)
                values.append(agg["avg"])
            best_i, worst_i = best_worst_indices(values, True)
            for i, v in enumerate(values):
                cls = ""
                if i == best_i:
                    cls = ' class="best"'
                elif i == worst_i:
                    cls = ' class="worst"'
                if v is not None:
                    n = model_aggs[models[i]].get("judge_analysis_quality", {}).get("count", 1)
                    cell = f"{v:.1f}" if n > 1 else f"{int(v)}"
                else:
                    cell = "--"
                html += f"<td{cls}>{cell}</td>"
            html += "</tr>"
        html += "</tbody></table>\n</section>\n\n"

    html += '</div>\n</div>\n\n'

    # Report tabs for each model
    for m in models:
        model_runs = groups[m]
        c = color_for(m)

        if len(model_runs) == 1:
            r = model_runs[0]
            html += f'<div class="tab-content" id="tab-{escape(m)}">\n'
            if r["html_report"]:
                html += f'  <iframe class="iframe-wrap" src="{r["name"]}/eval-report-summary.html"></iframe>\n'
            else:
                html += '  <div class="page"><p>No HTML report available for this run.</p></div>\n'
            html += '</div>\n\n'
        else:
            html += f'<div class="tab-content" id="tab-{escape(m)}">\n'
            html += f'  <div class="sub-bar" id="subbar-{escape(m)}">\n'
            for j, r in enumerate(model_runs):
                active = " active" if j == 0 else ""
                act_style = f"color:var(--accent); border-bottom-color:var(--accent);" if j == 0 else ""
                html += f'    <button class="{active}" data-sub="{j}" style="{act_style}">Run {j + 1} ({r["name"]})</button>\n'
            html += '  </div>\n'
            for j, r in enumerate(model_runs):
                display = "" if j == 0 else ' style="display:none;"'
                html += f'  <div class="sub-panel" data-model="{escape(m)}" data-idx="{j}"{display}>\n'
                if r["html_report"]:
                    html += f'    <iframe class="iframe-wrap" src="{r["name"]}/eval-report-summary.html"></iframe>\n'
                else:
                    html += '    <div class="page"><p>No HTML report available.</p></div>\n'
                html += '  </div>\n'
            html += '</div>\n\n'

    # JavaScript
    html += """<script>
document.getElementById('tabBar').addEventListener('click', e => {
  if (e.target.tagName !== 'BUTTON') return;
  document.querySelectorAll('.tab-bar button').forEach(b => {
    b.classList.remove('active'); b.style.color = ''; b.style.borderBottomColor = '';
  });
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  const c = e.target.dataset.color;
  e.target.classList.add('active');
  e.target.style.color = c; e.target.style.borderBottomColor = c;
  document.getElementById('tab-' + e.target.dataset.tab).classList.add('active');
});
const initTab = document.querySelector('.tab-bar button.active');
if (initTab) { initTab.style.color = initTab.dataset.color; initTab.style.borderBottomColor = initTab.dataset.color; }

document.querySelectorAll('.sub-bar').forEach(bar => {
  bar.addEventListener('click', e => {
    if (e.target.tagName !== 'BUTTON') return;
    bar.querySelectorAll('button').forEach(b => {
      b.classList.remove('active'); b.style.color = 'var(--text-muted)'; b.style.borderBottomColor = 'transparent';
    });
    e.target.classList.add('active');
    e.target.style.color = 'var(--accent)'; e.target.style.borderBottomColor = 'var(--accent)';
    const idx = e.target.dataset.sub;
    const parent = bar.parentElement;
    parent.querySelectorAll('.sub-panel').forEach(p => p.style.display = 'none');
    parent.querySelector('.sub-panel[data-idx="' + idx + '"]').style.display = '';
  });
});
</script>
</body>
</html>"""

    out_path = output_dir / "index.html"
    with open(out_path, "w") as f:
        f.write(html)
    return str(out_path)


def cmd_discover(args):
    runs = discover_runs(args.input_dir)
    if not runs:
        print(json.dumps({"error": "No valid runs found", "runs": []}))
        sys.exit(1)
    out = []
    for r in runs:
        entry = {
            "name": r["name"],
            "model": get_model(r),
            "cost_usd": get_metric(r, "cost_usd"),
            "analysis_quality": get_judge_mean(r, "analysis_quality"),
            "revert_scoring": get_judge_mean(r, "revert_scoring_accuracy"),
            "has_html": r["html_report"] is not None,
        }
        out.append(entry)
    print(json.dumps({"runs": out}, indent=2))


def cmd_generate(args):
    runs = discover_runs(args.input_dir)
    if not runs:
        print("ERROR: No valid runs found", file=sys.stderr)
        sys.exit(1)
    path = generate_report(runs, args.title, args.overview, args.output)
    groups = group_by_model(runs)
    print(f"Report generated: {path}")
    print(f"Runs: {len(runs)} across {len(groups)} models")
    for m, model_runs in groups.items():
        aq = aggregate([get_judge_mean(r, "analysis_quality") for r in model_runs])
        cost = aggregate([get_metric(r, "cost_usd") for r in model_runs])
        print(f"  {short_name(m)}: {len(model_runs)} run(s), "
              f"AQ={fmt(aq['avg'], 'num')}, cost={fmt(cost['avg'], 'usd')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("input_dir")

    p_generate = sub.add_parser("generate")
    p_generate.add_argument("input_dir")
    p_generate.add_argument("--output", required=True)
    p_generate.add_argument("--title", default="Model Comparison")
    p_generate.add_argument("--overview", default=None)

    args = parser.parse_args()
    if args.command == "discover":
        cmd_discover(args)
    elif args.command == "generate":
        cmd_generate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
