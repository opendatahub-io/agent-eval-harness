"""Tests for the report markdown renderer (skills/eval-run/scripts/report.py).

Focus: _md_to_html paragraph handling. Soft-wrapped source lines must be
joined into a single <p> so analysis.md paragraphs reflow to the container
width instead of each wrapped line becoming its own narrow paragraph.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

from report import _md_to_html


def test_wrapped_paragraph_joins_into_single_p():
    md = "This is a paragraph\nthat was hard-wrapped\nacross three lines."
    html = _md_to_html(md)
    assert html.count("<p>") == 1
    assert "<p>This is a paragraph that was hard-wrapped across three lines.</p>" in html


def test_blank_line_separates_paragraphs():
    md = "First paragraph\nstill first.\n\nSecond paragraph\nstill second."
    html = _md_to_html(md)
    assert html.count("<p>") == 2
    assert "<p>First paragraph still first.</p>" in html
    assert "<p>Second paragraph still second.</p>" in html


def test_single_line_paragraph_unchanged():
    assert _md_to_html("Just one line.") == "<p>Just one line.</p>"


def test_paragraph_stops_at_block_constructs():
    # A paragraph immediately followed (no blank line) by each block type must
    # not absorb that block.
    md = (
        "Lead paragraph here\n"
        "wrapped a bit.\n"
        "## A heading\n"
        "Para before list\n"
        "- item one\n"
        "- item two\n"
        "Para before table\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
    )
    html = _md_to_html(md)
    assert "<p>Lead paragraph here wrapped a bit.</p>" in html
    assert "<h2>A heading</h2>" in html
    assert "<p>Para before list</p>" in html
    assert "<ul>" in html and "<li>item one</li>" in html
    assert "<p>Para before table</p>" in html
    assert "<table>" in html


def test_paragraph_stops_at_fenced_code():
    md = "Some prose\nthat wraps.\n```\ncode line\n```\n"
    html = _md_to_html(md)
    assert "<p>Some prose that wraps.</p>" in html
    assert "code line" in html
    assert html.count("<p>") == 1


# --- run-configuration cost rows (spec 014 judge usage) ----------------------

from report import _render_run_config  # noqa: E402

_RUN = {"model": "m", "cost_usd": 1.5, "duration_s": 10, "num_turns": 3, "exit_code": 0}


def _summary(judge_cost=0.25, requests=8, unpriced=0, total=1.75, source="complete"):
    return {"judge_usage": {"judge_cost_usd": judge_cost, "requests": requests,
                            "requests_missing_cost": unpriced},
            "total_cost_usd": total, "total_cost_source": source}


def test_run_config_renders_judge_and_total_cost_rows():
    html = _render_run_config(_RUN, summary=_summary())
    assert "<dt>Judge Cost</dt>" in html and "$0.25 (8 calls)" in html
    assert "<dt>Total Cost</dt>" in html and "$1.75 (complete)" in html
    # The agent cost row is untouched — judge spend is not folded into it.
    assert "<dt>Cost</dt><dd>$1.50</dd>" in html


def test_run_config_shows_null_total_with_its_source():
    """Null-cost arithmetic: a judge-only or agent-only total is shown as
    n/a with the source, never re-derived from an estimate."""
    html = _render_run_config(_RUN, summary=_summary(
        judge_cost=None, requests=4, unpriced=4, total=None, source="agent-only"))
    assert "n/a (4 calls, 4 unpriced)" in html
    assert "n/a (agent-only)" in html


def test_run_config_without_judge_usage_has_no_cost_rows():
    html = _render_run_config(_RUN, summary={"judges": {}})
    assert "Judge Cost" not in html and "Total Cost" not in html
    assert "Judge Cost" not in _render_run_config(_RUN)


def test_run_config_cost_rows_show_the_baseline_column():
    html = _render_run_config(_RUN, baseline_result=_RUN, summary=_summary(),
                              baseline_summary=_summary(judge_cost=0.10, total=1.60))
    assert '<dd class="bl">$0.10 (8 calls)</dd>' in html
    assert '<dd class="bl">$1.60 (complete)</dd>' in html
    # A baseline that recorded no judge usage still gets the row (with a dash).
    html = _render_run_config(_RUN, baseline_result=_RUN, summary=_summary(),
                              baseline_summary={"judges": {}})
    assert "<dt>Judge Cost</dt>" in html and '<dd class="bl">—</dd>' in html


# --- cost provenance panel (spec 014 PR-4) ------------------------------------

from report import _render_cost_provenance  # noqa: E402

_RECONCILED = {
    "cost_usd": 0.0612, "cost_usd_estimate": 2.51, "cost_source": "openrouter:generation",
    "cost_confidence": "high",
    "cost_coverage": {"requests": 40, "requests_priced": 40, "requests_missing_cost": 0,
                      "key_usage_delta_usd": 0.0631, "key_usage_settle_s": 21.0},
    "hook_cost_usd": 0.004,
    "budget": {"invocation_usd": 5.0, "cli_cap_usd": 250.0, "run_usd": None,
               "enforcement": "cli-estimate", "exceeded": None},
    "provider": {"key_scope": "operator", "key_hash": "sha256:1a2b3c4d", "key_exposed_to_agent": True},
    "routing": {"enforcement": "audit", "policy": "strict", "sha": "9f3a",
                "snapshot": "provider/routing_snapshot.json",
                "declared": {"z-ai/glm-5.2": {"order": ["z-ai", "novita"], "allow_fallbacks": False}},
                "served": {"novita/fp8": 38, "z-ai": 2}, "audited": 40, "compliant": 40,
                "violations": [], "unattributed": 0, "degraded": False, "audit_complete": True},
    "cost_warnings": ["ledger sum $0.0612 differs from key-usage delta $0.0631 by 3.0%"],
}


def test_provenance_panel_renders_rows_and_warnings():
    html = _render_cost_provenance(_RECONCILED)
    assert "<h2>Cost Provenance</h2>" in html
    for text in ("openrouter:generation", "high — 40 / 40 requests priced", "$2.51 (×41)",
                 "$0.0631 (-3.0%)", "$0.0040", "CLI cap $250.00", "enforcement: cli-estimate",
                 "operator sha256:1a2b3c4d — exposed to the agent", "novita/fp8: 38, z-ai: 2",
                 "40 compliant / 40 audited", "provider/routing_snapshot.json (9f3a)",
                 "order=['z-ai', 'novita']"):
        assert text in html, text
    assert "banner" not in html
    assert "differs from key-usage delta" in html


def test_provenance_panel_banners_for_the_loud_states():
    rr = dict(_RECONCILED, cost_source="unavailable", cost_usd=None,
              routing=dict(_RECONCILED["routing"], degraded=True, degraded_reason="violations",
                           violations=[{"gen_id": "gen-1"}], audit_complete=False),
              budget=dict(_RECONCILED["budget"], exceeded="run", exceeded_reason="post-hoc"))
    html = _render_cost_provenance(rr)
    assert html.count("banner-red") == 4
    assert "Cost unavailable" in html and "Routing degraded: violations" in html
    assert "audit incomplete" in html and "Budget exceeded (run: post-hoc)" in html
    amber = _render_cost_provenance(dict(_RECONCILED, routing=dict(
        _RECONCILED["routing"], violations=[{"gen_id": "gen-1"}], degraded=False)))
    assert "banner-amber" in amber and "policy: warn" in amber


def test_provenance_panel_absent_without_a_cost_source():
    assert _render_cost_provenance({"cost_usd": 1.0}) == ""
    assert _render_cost_provenance(None) == ""
    assert "Cost Provenance" in _render_run_config(_RECONCILED)
    assert "Cost Provenance" not in _render_run_config(_RUN)
