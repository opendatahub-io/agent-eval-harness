import importlib.util
from pathlib import Path


def load_report_module():
    path = Path(__file__).parent.parent / "skills" / "eval-anova" / "scripts" / "report.py"
    spec = importlib.util.spec_from_file_location("eval_anova_report", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def multi_factor_analysis():
    return {
        "timestamp": "2026-07-08T00:00:00Z",
        "design": {
            "factors": {
                "model": ["claude-opus-4-6", "claude-haiku-4-5"],
                "effort": ["low", "high"],
            },
            "n_cases": 1,
            "replications": 1,
        },
        "condition_summaries": [
            {
                "condition_id": "opus-low",
                "model": "claude-opus-4-6",
                "effort": "low",
                "mean": 0.75,
                "std": 0.0,
                "n": 1,
            },
            {
                "condition_id": "haiku-high",
                "model": "claude-haiku-4-5",
                "effort": "high",
                "mean": 0.25,
                "std": 0.0,
                "n": 1,
            },
        ],
        "anova": {
            "p_values": {"model": 0.01, "effort": 0.2},
            "significant": {"model": True, "effort": False},
            "method": "Mixed-effects model (statsmodels mixedlm)",
            "alpha": 0.05,
            "factors": ["model", "effort"],
        },
        "per_case": {
            "model=claude-opus-4-6, effort=low": {"case-a": 0.75},
            "model=claude-haiku-4-5, effort=high": {"case-a": 0.25},
        },
    }


def corrected_analysis():
    """A post-correction artifact: per-term raw + adjusted p, interaction row."""
    d = multi_factor_analysis()
    d["anova"] = {
        "p_values": {"model": 0.01, "effort": 0.2, "model:effort": 0.7},
        "p_adjusted": {"model": 0.03, "effort": 0.4, "model:effort": 0.7},
        "significant": {"model": True, "effort": False, "model:effort": False},
        "correction": "holm",
        "family_size": 3,
        "method": "Mixed-effects model (statsmodels mixedlm, per-term Wald tests)",
        "alpha": 0.05,
        "factors": ["model", "effort"],
    }
    return d


def test_mixed_effects_render_markdown_factor_p_values():
    report = load_report_module()
    markdown = report.render_md("anova-test", multi_factor_analysis())

    assert "- Factors: model, effort" in markdown
    assert "- model: p: 0.0100 — SIGNIFICANT" in markdown
    assert "- effort: p: 0.2000 — not significant" in markdown
    assert "model=claude-opus-4-6, effort=low" in markdown
    # legacy artifact (no correction fields) — no unlabelled adjusted value
    assert "p-adj" not in markdown


def test_mixed_effects_render_html_factor_p_values():
    report = load_report_module()
    rendered = report.render_html("anova-test", multi_factor_analysis())

    assert "SIGNIFICANT" in rendered
    assert "<td>model</td><td class=num>0.0100</td>" in rendered
    assert "<td>effort</td><td class=num>0.2000</td>" in rendered
    assert "model, effort" in rendered
    assert "p (adj)" not in rendered  # no correction fields → no adjusted column


def test_corrected_render_markdown_shows_raw_adjusted_and_method():
    report = load_report_module()
    markdown = report.render_md("anova-test", corrected_analysis())

    assert "- model: p: 0.0100 · p-adj (Holm): 0.0300 — SIGNIFICANT" in markdown
    assert "- model:effort: p: 0.7000 · p-adj (Holm): 0.7000 — not significant" in markdown
    assert "family of 3 term test(s)" in markdown


def test_corrected_render_html_shows_raw_adjusted_and_method():
    report = load_report_module()
    rendered = report.render_html("anova-test", corrected_analysis())

    assert "p (raw)" in rendered and "p (adj)" in rendered
    assert "<td>model</td><td class=num>0.0100</td><td class=num>0.0300</td>" in rendered
    assert "<td>model:effort</td>" in rendered  # interaction row
    assert "Holm" in rendered  # the adjusted column names its method


def contrasts_analysis():
    """An artifact carrying post-hoc pairwise contrasts (top-level key)."""
    d = corrected_analysis()
    d["contrasts"] = {
        "model": {
            "correction": "holm",
            "family": "pairwise level contrasts within factor 'model'",
            "family_size": 2,
            "contrast_type": "reference-cell",
            "omnibus_p_adjusted": 0.03,
            "note": "Estimates are reference-cell contrasts from the fitted "
                    "model — level differences at the other factors' reference "
                    "levels, NOT marginal means (the model includes interactions).",
            "pairs": [
                {"a": "claude-opus-4-6", "b": "claude-haiku-4-5",
                 "estimate": 0.5, "se": 0.1, "p_raw": 0.004,
                 "p_adjusted": 0.008, "significant": True},
                {"a": "claude-opus-4-6", "b": "claude-x",
                 "estimate": 0.01, "se": None, "p_raw": None,
                 "p_adjusted": None, "significant": False,
                 "reason": "no finite p — degenerate pair, excluded from the "
                           "correction family"},
            ],
        },
    }
    return d


def test_contrasts_render_markdown_names_method_and_keeps_raw_p():
    report = load_report_module()
    markdown = report.render_md("anova-test", contrasts_analysis())

    assert "## Pairwise contrasts (post-hoc)" in markdown
    assert "### model" in markdown
    assert "Holm-corrected across the 2 contrast(s) within this factor" in markdown
    assert "Omnibus p-adj: 0.0300" in markdown
    # raw p stays visible next to the adjusted one; the degenerate pair shows
    # no fabricated value
    assert "| claude-opus-4-6 | claude-haiku-4-5 | 0.500 | 0.100 | 0.0040 | 0.0080 | SIGNIFICANT |" in markdown
    assert "| claude-opus-4-6 | claude-x | 0.010 | — | — | — | no test |" in markdown
    assert "NOT marginal means" in markdown


def test_contrasts_render_html_table_and_escaping():
    report = load_report_module()
    d = contrasts_analysis()
    evil = "m<script>alert(1)</script>"
    d["contrasts"]["model"]["pairs"][0]["a"] = evil
    rendered = report.render_html("anova-test", d)

    assert "Pairwise contrasts (post-hoc)" in rendered
    assert "Holm-corrected across the 2 contrast(s)" in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<td class=num>0.0040</td><td class=num>0.0080</td>" in rendered


def test_no_contrasts_key_renders_no_section():
    report = load_report_module()
    markdown = report.render_md("anova-test", corrected_analysis())
    rendered = report.render_html("anova-test", corrected_analysis())
    assert "Pairwise contrasts" not in markdown
    assert "Pairwise contrasts" not in rendered


def test_degenerate_contrast_block_shows_reason_not_table():
    report = load_report_module()
    d = corrected_analysis()
    d["contrasts"] = {"model": {
        "correction": "holm", "family_size": 0, "contrast_type": "paired",
        "omnibus_p_adjusted": None, "note": "", "pairs": [],
        "reason": "No variance in response — no pairwise tests computed.",
    }}
    markdown = report.render_md("anova-test", d)
    assert "No pairwise tests: No variance in response" in markdown
    rendered = report.render_html("anova-test", d)
    assert "No pairwise tests: No variance in response" in rendered


def test_render_html_escapes_user_controlled_ids():
    """Model/case ids and run_id come from user-controlled dataset dir names and
    eval.yaml; they must be HTML-escaped so a hostile name can't inject script
    into the generated report (stored XSS)."""
    report = load_report_module()
    evil = "x<img src=y onerror=alert(1)>"
    analysis = {
        "timestamp": "t",
        "design": {"factors": {"model": [evil]}, "n_cases": 1, "replications": 1},
        "condition_summaries": [
            {"condition_id": evil, "model": evil, "mean": 0.5, "std": 0.0, "n": 1}
        ],
        "anova": {"factor": "model", "p_value": 0.2, "significant": False,
                  "f_statistic": 1.0, "method": "m"},
        "per_case": {evil: {evil: 0.5}},
    }
    rendered = report.render_html(evil, analysis)
    assert "<img src=y onerror" not in rendered
    assert "&lt;img" in rendered
