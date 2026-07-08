#!/usr/bin/env python3
"""Render anova reports from existing analysis.json. No re-run.
Summary = head-to-head model comparison pooled across all runs.

Usage:
    python3 report.py [RUNS_DIR]   # default: $AGENT_EVAL_RUNS_DIR or eval/runs

Renders per-run report.{md,html} for every anova-* run, plus a pooled
    anova-summary.html (model comparison) in RUNS_DIR. Reads only analysis.json."""
import json, glob, os, sys, html, datetime
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("AGENT_EVAL_RUNS_DIR", "eval/runs"))
RUNS = sorted(d for d in glob.glob(os.path.join(BASE, "anova-*")) if os.path.isdir(d))
MODEL_ORDER = ["claude-opus-4-6", "claude-sonnet-4-6",
               "claude-haiku-4-5@20251001", "claude-haiku-4-5"]
SHORT = {"claude-opus-4-6": "opus", "claude-sonnet-4-6": "sonnet",
         "claude-haiku-4-5@20251001": "haiku", "claude-haiku-4-5": "haiku"}
SHORT_ORDER = ["opus", "sonnet", "haiku"]
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
def order_models(ms): return [m for m in MODEL_ORDER if m in ms] + [m for m in ms if m not in MODEL_ORDER]
def fnum(x, n=3): return f"{x:.{n}f}" if isinstance(x,(int,float)) else "—"
def cmodel(c):  # model name from a condition summary: flat, nested, or id
    return c.get("model") or c.get("levels",{}).get("model") or c.get("condition_id","?")
def heat_bg(v):  # green-ish scale for 0..1
    if v is None: return "#1d2026"
    r=int(0x2c+(0x2e-0x2c)*v); g=int(0x31+(0xa0-0x31)*v); b=int(0x3b+(0x43-0x3b)*v)
    return f"#{r:02x}{g:02x}{b:02x}"

# ---------- markdown per run ----------
def render_md(rid,d):
    des,conds,an,per=(d.get("design",{}),d.get("condition_summaries",[]),d.get("anova",{}),d.get("per_case",{}))
    cases=sorted({c for m in per.values() for c in m}) if per else []
    L=[f"# ANOVA Report — {rid}","",f"*Generated {NOW} from `analysis.json`.*","","## Condition means (ranked)","",
       "| Rank | Model | Mean | Std | n |","|---|---|---|---|---|"]
    for i,c in enumerate(sorted(conds,key=lambda x:-x.get("mean",0)),1):
        L.append(f"| {i} | {SHORT.get(cmodel(c),cmodel(c))} | {fnum(c.get('mean'))} | {fnum(c.get('std'))} | {c.get('n','?')} |")
    p=an.get("p_value");ng2=an.get("details",[{}])[0].get("ng2") if an.get("details") else None
    L+=["","## ANOVA","",f"- Method: {an.get('method','?')}",f"- F: {fnum(an.get('f_statistic'))}",
        f"- p: {fnum(p,4)}",f"- η²: {fnum(ng2)} ({eff_bucket(ng2)})",
        f"- Result: {'SIGNIFICANT' if an.get('significant') else 'not significant'}"]
    if per and cases:
        ms=order_models(list(per.keys()))
        L+=["","## Per-case scores","","| Case | "+" | ".join(SHORT.get(m,m) for m in ms)+" |","|---"*(len(ms)+1)+"|"]
        for c in cases: L.append("| "+" | ".join([c]+[str(per[m].get(c,"—")) for m in ms])+" |")
    return "\n".join(L)

# ---------- styled html per run ----------
def render_html(rid,d):
    des,conds,an,per=(d.get("design",{}),d.get("condition_summaries",[]),d.get("anova",{}),d.get("per_case",{}))
    cases=sorted({c for m in per.values() for c in m}) if per else []
    p,sig,F=an.get("p_value"),an.get("significant"),an.get("f_statistic")
    ng2=an.get("details",[{}])[0].get("ng2") if an.get("details") else None
    computed=isinstance(p,(int,float))
    badge=(f"<span class='badge sig'>SIGNIFICANT &nbsp;p={fnum(p,3)}</span>" if sig
           else f"<span class='badge nsig'>not significant"+(f" &nbsp;p={fnum(p,3)}" if computed else " · no variance")+"</span>")
    levels=", ".join(SHORT.get(x,x) for fv in des.get("factors",{}).values() for x in fv)
    meta=("<dl class=meta>"+f"<dt>Factor</dt><dd>{an.get('factor','model')}</dd>"
          f"<dt>Levels</dt><dd>{html.escape(levels)}</dd>"
          f"<dt>Cases</dt><dd>{des.get('n_cases',len(cases))} — {', '.join(cases) or '—'}</dd>"
          f"<dt>Replications</dt><dd>{des.get('replications','?')}</dd>"
          f"<dt>Run</dt><dd>{html.escape(d.get('timestamp','?'))}</dd></dl>")
    rows=""
    for i,c in enumerate(sorted(conds,key=lambda x:-x.get("mean",0)),1):
        m=c.get("mean",0) or 0
        rk="rank1" if i==1 else ""
        rows+=(f"<tr><td class=num>{i}</td><td class='{rk}'>{SHORT.get(cmodel(c),cmodel(c))}</td>"
               f"<td class=num>{fnum(m)}</td><td class=num>{fnum(c.get('std'))}</td><td class=num>{c.get('n','?')}</td>"
               f"<td style='width:160px'><div class=bar><i style='width:{m*100:.0f}%'></i></div></td></tr>")
    means=f"<table><thead><tr><th>#</th><th>Model</th><th class=num>Mean</th><th class=num>Std</th><th class=num>n</th><th></th></tr></thead><tbody>{rows}</tbody></table>"
    tiles="".join(f"<div class=tile><div class=k>{k}</div><div class=v>{v}</div></div>" for k,v in
        [("F-statistic",fnum(F)),("p-value",fnum(p,4)),
         ("η² (effect)",f"{fnum(ng2)}"+(f" · {eff_bucket(ng2)}" if ng2 is not None else "")),("alpha",str(an.get("alpha",0.05)))])
    if sig:
        top=max(conds,key=lambda x:x.get("mean",0))
        call=f"<div class='callout sig'>Statistically detectable effect. Best: <b>{SHORT.get(cmodel(top),cmodel(top))}</b> (mean {fnum(top['mean'])}).</div>"
    elif computed:
        call="<div class=callout>Not significant at n=3, 1 replication — small n / high variance can mask real effects.</div>"
    else:
        call="<div class=callout>ANOVA not computable: zero variance (every condition scored identically).</div>"
    anova=f"<div class=tiles>{tiles}</div>{call}<div class=sub style='margin-top:12px'>{html.escape(an.get('method','—'))}</div>"
    matrix=""
    if per and cases:
        ms=order_models(list(per.keys()))
        head="<tr><th>Case</th>"+"".join(f"<th class=ctr>{SHORT.get(m,m)}</th>" for m in ms)+"</tr>"
        body=""
        for c in cases:
            tds=f"<td>{c}</td>"
            for m in ms:
                v=per[m].get(c)
                tds+=("<td class=cell-pass>✓</td>" if v in (1,1.0) else
                      "<td class=cell-fail>·</td>" if v in (0,0.0) else f"<td class=cell-fail>{'—' if v is None else v}</td>")
            body+=f"<tr>{tds}</tr>"
        matrix=f"<div class=card><h2>Per-case scores</h2><table><thead>{head}</thead><tbody>{body}</tbody></table></div>"
    body=(f"<p><a href='../anova-summary.html'>← model comparison</a></p>"
          f"<h1>ANOVA — {rid}</h1><div class=sub>{badge}</div>"
          f"<div class=card><h2>Experiment</h2>{meta}</div>"
          f"<div class=card><h2>Condition means (ranked)</h2>{means}</div>"
          f"<div class=card><h2>ANOVA</h2>{anova}</div>{matrix}"
          f"<footer>Generated {NOW} from <code>analysis.json</code> · scores are composite pass/fail (1.0=pass).</footer>")
    return page(f"ANOVA — {rid}",body)

# ---------- SUMMARY = pooled head-to-head model comparison ----------
def render_summary(run_items, pooled, by_task, tasks, n_runs, incomplete):
    models=[m for m in SHORT_ORDER if m in pooled] + [m for m in pooled if m not in SHORT_ORDER]
    # leaderboard
    def mean(xs): return sum(xs)/len(xs) if xs else 0.0
    lb=sorted(models,key=lambda m:-mean(pooled[m]))
    lrows=""
    for i,m in enumerate(lb,1):
        xs=pooled[m]; mu=mean(xs); passes=sum(1 for s in xs if s in (1,1.0))
        rk="rank1" if i==1 else ""
        lrows+=(f"<tr><td class=num>{i}</td><td class='{rk}'>{m}</td>"
                f"<td class=num>{mu:.3f}</td><td class=num>{passes}/{len(xs)}</td>"
                f"<td class=num>{(passes/len(xs)*100 if xs else 0):.0f}%</td>"
                f"<td style='width:200px'><div class=bar><i style='width:{mu*100:.0f}%'></i></div></td></tr>")
    leaderboard=(f"<table><thead><tr><th>#</th><th>Model</th><th class=num>Mean score</th>"
                 f"<th class=num>Passes</th><th class=num>Pass rate</th><th></th></tr></thead><tbody>{lrows}</tbody></table>")
    # model x task heatmap (mean over runs)
    head="<tr><th>Task</th>"+"".join(f"<th class=ctr>{m}</th>" for m in models)+"<th class=ctr>best</th></tr>"
    trows=""
    for t in tasks:
        cells=""; vals={m:(mean(by_task[m][t]) if by_task[m][t] else None) for m in models}
        best=max((v for v in vals.values() if v is not None), default=None)
        for m in models:
            v=vals[m]
            cls="win" if (v is not None and best is not None and v>=best>0) else ""
            disp="—" if v is None else f"{v:.2f}"
            cells+=f"<td class='heat {cls}' style='background:{heat_bg(v)}'>{disp}</td>"
        winner=", ".join(m for m in models if vals[m] is not None and best and vals[m]>=best>0) or "—"
        trows+=f"<tr><td>{t}</td>{cells}<td class=ctr>{winner}</td></tr>"
    heatmap=f"<table><thead>{head}</thead><tbody>{trows}</tbody></table>"
    # overall winner callout
    champ=lb[0] if lb else "—"
    champ_mu=mean(pooled[champ]) if lb else 0
    runner=lb[1] if len(lb)>1 else None
    sig_runs=[it for it in run_items if it["sig"]]
    note=(f"Across <b>{n_runs} runs</b> ({sum(len(pooled[m]) for m in models)} scored model-cases), "
          f"<b>{champ}</b> leads with mean {champ_mu:.3f}"
          + (f", ahead of {runner} ({mean(pooled[runner]):.3f})." if runner else ".")
          + f" Only <b>{len(sig_runs)}/{n_runs}</b> individual run(s) reached statistical significance "
          f"(p&lt;0.05), and scores are mostly failures — treat rankings as <b>exploratory</b>, not conclusive.")
    # per-run table (secondary)
    rrows=""
    for it in run_items:
        cls=" style='background:#11251a'" if it["sig"] else ""
        sigtxt="<span class='badge sig'>yes</span>" if it["sig"] else "<span class=sub>no</span>"
        rrows+=(f"<tr{cls}><td><a href='{it['run']}/report.html'>{it['run']}</a></td>"
                f"<td class=num>{it['cases']}</td><td>{it['best']}</td>"
                f"<td class=num>{it['F']}</td><td class=num>{it['p']}</td><td>{sigtxt}</td></tr>")
    inc=(f"<div class=sub style='margin-top:14px'>Incomplete (no analysis): {', '.join(incomplete)}</div>" if incomplete else "")
    body=(f"<h1>Model Comparison — ANOVA</h1>"
          f"<div class=sub>opus vs sonnet vs haiku · 3 coding tasks · {n_runs} runs pooled</div>"
          f"<div class=card><div class=callout>{note}</div></div>"
          f"<div class=card><h2>Overall leaderboard (pooled across all runs &amp; cases)</h2>{leaderboard}</div>"
          f"<div class=card><h2>Model × task — mean score (across runs)</h2>{heatmap}"
          f"<div class=sub style='margin-top:10px'>Greener = higher mean pass rate. Best per task highlighted.</div></div>"
          f"<div class=card><h2>Individual runs</h2><table><thead><tr><th>Run</th><th class=num>Cases</th>"
          f"<th>Best</th><th class=num>F</th><th class=num>p</th><th>Sig</th></tr></thead><tbody>{rrows}</tbody></table>{inc}</div>"
          f"<footer>Generated {NOW} from <code>analysis.json</code> files · no re-run.</footer>")
    return page("Model Comparison — ANOVA",body)

def main():
    written,run_items,incomplete=[],[],[]
    pooled={}; by_task={}; tasks=[]
    for run in RUNS:
        aj=os.path.join(run,"analysis.json")
        if not os.path.exists(aj): incomplete.append(os.path.basename(run)); continue
        try:
            with open(aj) as f: d=json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: skipping {aj}: {exc}", flush=True)
            incomplete.append(os.path.basename(run)); continue
        rid=d.get("run_id",os.path.basename(run))
        Path(os.path.join(run,"report.md")).write_text(render_md(rid,d)+"\n")
        Path(os.path.join(run,"report.html")).write_text(render_html(rid,d))
        written.append(rid)
        an=d.get("anova",{});conds=d.get("condition_summaries",[]);per=d.get("per_case",{})
        best=max(conds,key=lambda x:x.get("mean",0)) if conds else None
        run_items.append({"run":rid,"cases":d.get("design",{}).get("n_cases","—"),
            "best":f"{SHORT.get(cmodel(best),cmodel(best))} ({fnum(best['mean'],2)})" if best else "—",
            "F":fnum(an.get("f_statistic"),2),"p":fnum(an.get("p_value"),3),"sig":bool(an.get("significant"))})
        for model,cs in per.items():
            sm=SHORT.get(model,model); pooled.setdefault(sm,[]); by_task.setdefault(sm,{})
            for case,score in cs.items():
                if case not in tasks: tasks.append(case)
                pooled[sm].append(score); by_task[sm].setdefault(case,[]).append(score)
    tasks=sorted(tasks)
    Path(os.path.join(BASE,"anova-summary.html")).write_text(render_summary(run_items,pooled,by_task,tasks,len(written),incomplete))
    print(f"Rendered {len(written)} run reports + pooled model-comparison summary. Incomplete: {incomplete or 'none'}")
    print("Models pooled:", {m:len(v) for m,v in pooled.items()})

if __name__ == "__main__":
    main()
