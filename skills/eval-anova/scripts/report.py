#!/usr/bin/env python3
"""Render eval-anova's deep ANOVA detail report from an anova.json artifact.

Reads a single anova.json (written by analyze.analyze_runs) and renders the
per-experiment ANOVA detail — condition means (ranked), F / p / eta-squared
tiles, a significance badge, and a per-case matrix. The pooled cross-model
comparison (leaderboard, model x task heatmap) lives in /eval-compare, which
surfaces these same statistics; this is the statistics-forward companion view.

Usage:
    python3 report.py [PATH]
    #  PATH may be an anova.json file, a dir containing one, or a runs dir.
    #  Default: $AGENT_EVAL_RUNS_DIR or eval/runs. Reads only anova.json."""
import json, os, sys, html, datetime
from pathlib import Path

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--ink:#e8eaed;--muted:#9aa0aa;--line:#2c313b;
--accent:#6ea8fe;--pass:#2ea043;--passbg:#0f2a16;--failink:#6b7280;--warn:#8b949e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:16px 0}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 14px;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.ctr,th.ctr{text-align:center}
.meta{display:grid;grid-template-columns:auto 1fr;gap:6px 18px;font-size:14px}
.meta dt{color:var(--muted)}.meta dd{margin:0}
.tiles{display:flex;gap:12px;flex-wrap:wrap}
.tile{flex:1;min-width:120px;background:#11141a;border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.tile .v{font-size:24px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:4px 12px;border-radius:999px;font-size:13px;font-weight:600}
.badge.sig{background:var(--passbg);color:#56d364;border:1px solid #1f6f33}
.badge.nsig{background:#1d2026;color:var(--muted);border:1px solid var(--line)}
.bar{height:8px;border-radius:4px;background:#262b34;overflow:hidden;margin-top:5px;min-width:80px}
.bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#9b8cff)}
.rank1{color:#ffd166;font-weight:700}
.win{color:#56d364;font-weight:600}
.cell-pass{color:#56d364;font-weight:600;text-align:center}
.cell-fail{color:var(--failink);text-align:center}
.heat{text-align:center;font-variant-numeric:tabular-nums;border-radius:4px}
.callout{border-left:3px solid var(--accent);padding:10px 14px;background:#12151b;border-radius:0 8px 8px 0;color:#c9ced6;font-size:14px;margin-top:6px}
.callout.sig{border-left-color:var(--pass)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
footer{color:var(--warn);font-size:12px;margin-top:28px;border-top:1px solid var(--line);padding-top:14px}
"""

def page(t, b):
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(t)}</title><style>{CSS}</style></head>"
            f"<body><div class=wrap>{b}</div></body></html>")

def eff_bucket(v): return "n/a" if v is None else ("small" if v<0.06 else "medium" if v<0.14 else "large")
def order_models(ms): return sorted(ms)
def fnum(x, n=3): return f"{x:.{n}f}" if isinstance(x,(int,float)) else "—"
def esc(x): return html.escape(str(x))  # user-controlled values (model/case/task ids, run_id)
def cmodel(c):  # model name from a condition summary: flat, nested, or id
    if not c:
        return "?"
    return c.get("model") or c.get("levels",{}).get("model") or c.get("condition_id","?")
def sig_any(an):
    sig=an.get("significant")
    if isinstance(sig,dict):
        return any(bool(v) for v in sig.values())
    return bool(sig)
def pmap(an):
    vals=an.get("p_values")
    if isinstance(vals,dict) and vals:
        return vals
    factor=an.get("factor") or "effect"
    return {factor: an.get("p_value")}
def best_p(an):
    vals=[v for v in pmap(an).values() if isinstance(v,(int,float))]
    return min(vals) if vals else None
def sig_for(an,factor):
    sig=an.get("significant")
    return bool(sig.get(factor)) if isinstance(sig,dict) else bool(sig)
def factor_label(an):
    factors=an.get("factors")
    if isinstance(factors,list) and factors:
        return ", ".join(str(f) for f in factors)
    return str(an.get("factor","model"))
def anova_md_lines(an):
    lines=["","## ANOVA","",f"- Method: {an.get('method','?')}"]
    if "p_values" in an:
        lines.append(f"- Factors: {factor_label(an)}")
        for factor,p in pmap(an).items():
            result="SIGNIFICANT" if sig_for(an,factor) else "not significant"
            lines.append(f"- {factor}: p: {fnum(p,4)} — {result}")
        lines.append(f"- Result: {'SIGNIFICANT' if sig_any(an) else 'not significant'}")
        return lines
    p=an.get("p_value");ng2=an.get("details",[{}])[0].get("ng2") if an.get("details") else None
    lines += [f"- F: {fnum(an.get('f_statistic'))}",f"- p: {fnum(p,4)}",
              f"- η²: {fnum(ng2)} ({eff_bucket(ng2)})",
              f"- Result: {'SIGNIFICANT' if sig_any(an) else 'not significant'}"]
    return lines
def factor_p_table(an):
    if "p_values" not in an:
        return ""
    rows="".join(
        f"<tr><td>{html.escape(str(factor))}</td><td class=num>{fnum(p,4)}</td>"
        f"<td>{'SIGNIFICANT' if sig_for(an,factor) else 'not significant'}</td></tr>"
        for factor,p in pmap(an).items()
    )
    return f"<table style='margin-top:14px'><thead><tr><th>Factor</th><th class=num>p</th><th>Result</th></tr></thead><tbody>{rows}</tbody></table>"

# ---------- markdown per run ----------
def render_md(rid,d):
    des,conds,an,per=(d.get("design",{}),d.get("condition_summaries",[]),d.get("anova",{}),d.get("per_case",{}))
    cases=sorted({c for m in per.values() for c in m}) if per else []
    L=[f"# ANOVA Report — {rid}","",f"*Generated {NOW} from `anova.json`.*","","## Condition means (ranked)","",
       "| Rank | Model | Mean | Std | n |","|---|---|---|---|---|"]
    for i,c in enumerate(sorted(conds,key=lambda x:-x.get("mean",0)),1):
        L.append(f"| {i} | {cmodel(c)} | {fnum(c.get('mean'))} | {fnum(c.get('std'))} | {c.get('n','?')} |")
    L+=anova_md_lines(an)
    if per and cases:
        ms=order_models(list(per.keys()))
        L+=["","## Per-case scores","","| Case | "+" | ".join(m for m in ms)+" |","|---"*(len(ms)+1)+"|"]
        for c in cases: L.append("| "+" | ".join([c]+[str(per[m].get(c,"—")) for m in ms])+" |")
    return "\n".join(L)

# ---------- styled html per run ----------
def render_html(rid,d):
    des,conds,an,per=(d.get("design",{}),d.get("condition_summaries",[]),d.get("anova",{}),d.get("per_case",{}))
    cases=sorted({c for m in per.values() for c in m}) if per else []
    p,sig,F=best_p(an),sig_any(an),an.get("f_statistic")
    ng2=an.get("details",[{}])[0].get("ng2") if an.get("details") else None
    computed=isinstance(p,(int,float))
    badge=(f"<span class='badge sig'>SIGNIFICANT &nbsp;p={fnum(p,3)}</span>" if sig
           else f"<span class='badge nsig'>not significant"+(f" &nbsp;p={fnum(p,3)}" if computed else " · no variance")+"</span>")
    levels=", ".join(str(x) for fv in des.get("factors",{}).values() for x in fv)
    meta=("<dl class=meta>"+f"<dt>Factor</dt><dd>{html.escape(factor_label(an))}</dd>"
          f"<dt>Levels</dt><dd>{html.escape(levels)}</dd>"
          f"<dt>Cases</dt><dd>{des.get('n_cases',len(cases))} — {', '.join(esc(c) for c in cases) or '—'}</dd>"
          f"<dt>Replications</dt><dd>{des.get('replications','?')}</dd>"
          f"<dt>Run</dt><dd>{html.escape(d.get('timestamp','?'))}</dd></dl>")
    rows=""
    for i,c in enumerate(sorted(conds,key=lambda x:-x.get("mean",0)),1):
        m=c.get("mean",0) or 0
        rk="rank1" if i==1 else ""
        rows+=(f"<tr><td class=num>{i}</td><td class='{rk}'>{esc(cmodel(c))}</td>"
               f"<td class=num>{fnum(m)}</td><td class=num>{fnum(c.get('std'))}</td><td class=num>{c.get('n','?')}</td>"
               f"<td style='width:160px'><div class=bar><i style='width:{min(100.0,max(0.0,m*100)):.0f}%'></i></div></td></tr>")
    means=f"<table><thead><tr><th>#</th><th>Model</th><th class=num>Mean</th><th class=num>Std</th><th class=num>n</th><th></th></tr></thead><tbody>{rows}</tbody></table>"
    effect_label="factors" if "p_values" in an else "η² (effect)"
    effect_value=str(len(pmap(an))) if "p_values" in an else f"{fnum(ng2)}"+(f" · {eff_bucket(ng2)}" if ng2 is not None else "")
    tiles="".join(f"<div class=tile><div class=k>{k}</div><div class=v>{v}</div></div>" for k,v in
        [("F-statistic",fnum(F)),("p-value",fnum(p,4)),
         (effect_label,effect_value),("alpha",str(an.get("alpha",0.05)))])
    if sig:
        top=max(conds,key=lambda x:x.get("mean",0))
        call=f"<div class='callout sig'>Statistically detectable effect. Best: <b>{esc(cmodel(top))}</b> (mean {fnum(top['mean'])}).</div>"
    elif computed:
        call=(f"<div class=callout>Not significant at n={des.get('n_cases','?')} cases, "
              f"{des.get('replications','?')} replication(s) — small n / high variance can mask real effects.</div>")
    else:
        call="<div class=callout>ANOVA not computable: zero variance (every condition scored identically).</div>"
    anova=f"<div class=tiles>{tiles}</div>{factor_p_table(an)}{call}<div class=sub style='margin-top:12px'>{html.escape(an.get('method','—'))}</div>"
    matrix=""
    if per and cases:
        ms=order_models(list(per.keys()))
        head="<tr><th>Case</th>"+"".join(f"<th class=ctr>{esc(m)}</th>" for m in ms)+"</tr>"
        body=""
        for c in cases:
            tds=f"<td>{esc(c)}</td>"
            for m in ms:
                v=per[m].get(c)
                tds+=("<td class=cell-pass>✓</td>" if v in (1,1.0) else
                      "<td class=cell-fail>·</td>" if v in (0,0.0) else f"<td class=cell-fail>{'—' if v is None else esc(v)}</td>")
            body+=f"<tr>{tds}</tr>"
        matrix=f"<div class=card><h2>Per-case scores</h2><table><thead>{head}</thead><tbody>{body}</tbody></table></div>"
    body=(f"<h1>ANOVA — {esc(rid)}</h1><div class=sub>{badge}</div>"
          f"<div class=card><h2>Experiment</h2>{meta}</div>"
          f"<div class=card><h2>Condition means (ranked)</h2>{means}</div>"
          f"<div class=card><h2>ANOVA</h2>{anova}</div>{matrix}"
          f"<footer>Generated {NOW} from <code>anova.json</code> · composite scores in [0,1].</footer>")
    return page(f"ANOVA — {rid}",body)

def _resolve_artifact(base):
    """Locate the anova.json to render: a file, a dir containing one, or a
    runs dir with a single one below it."""
    p = Path(base)
    if p.is_file():
        return p
    if p.is_dir():
        direct = p / "anova.json"
        if direct.is_file():
            return direct
        matches = list(p.rglob("anova.json"))
        if len(matches) == 1:
            return matches[0]
    return None

def main():
    base = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
    artifact = _resolve_artifact(base)
    if artifact is None:
        print(f"No anova.json found under {base}", file=sys.stderr)
        return 1
    try:
        with open(artifact) as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Cannot read {artifact}: {exc}", file=sys.stderr)
        return 1
    rid = d.get("run_id") or d.get("design", {}).get("experiment_id") or "anova"
    outdir = artifact.parent
    (outdir / "anova-report.html").write_text(render_html(rid, d))
    (outdir / "anova-report.md").write_text(render_md(rid, d) + "\n")
    print(f"Rendered {outdir / 'anova-report.html'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
