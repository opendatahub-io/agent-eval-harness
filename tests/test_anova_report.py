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


def per_judge_analysis():
    d = corrected_analysis()
    d["per_judge"] = {
        "correction": "bh", "family_size": 2, "alpha": 0.05,
        "judges": {
            "quality": {"method": "Repeated-measures ANOVA (pingouin rm_anova)",
                        "terms": {"model": {"p_raw": 0.004, "p_adjusted": 0.008,
                                            "significant": True}},
                        "n_cases": 4, "n_conditions": 2},
            "tests_pass": {"method": "Repeated-measures ANOVA (pingouin rm_anova)",
                           "terms": {"model": {"p_raw": 0.2, "p_adjusted": 0.2,
                                               "significant": False}},
                           "n_cases": 4, "n_conditions": 2},
        },
        "excluded": [{"judge": "always_five",
                      "reason": "constant value — no variance to analyse"}],
    }
    return d


def test_per_judge_section_rendered_markdown_and_html():
    report = load_report_module()
    d = per_judge_analysis()

    markdown = report.render_md("anova-test", d)
    assert "## Per-judge effects (screening)" in markdown
    assert "| quality | model | 0.0040 | 0.0080 | SIGNIFICANT | 4 |" in markdown
    assert "| tests_pass | model | 0.2000 | 0.2000 | not significant | 4 |" in markdown
    assert "Benjamini-Hochberg" in markdown and "family of 2" in markdown
    assert "always_five — constant value" in markdown

    rendered = report.render_html("anova-test", d)
    assert "Per-judge effects (screening)" in rendered
    assert "<td>quality</td><td>model</td><td class=num>0.0040</td>" in rendered
    assert "Benjamini-Hochberg" in rendered
    assert "always_five" in rendered and "constant value" in rendered


def test_no_per_judge_block_renders_no_section():
    report = load_report_module()
    d = corrected_analysis()
    assert "Per-judge effects" not in report.render_md("anova-test", d)
    assert "Per-judge effects" not in report.render_html("anova-test", d)


def test_per_judge_html_escapes_user_controlled_values():
    report = load_report_module()
    d = per_judge_analysis()
    evil = "j<img src=y onerror=alert(1)>"
    d["per_judge"]["judges"] = {evil: d["per_judge"]["judges"]["quality"]}
    d["per_judge"]["excluded"] = [{"judge": evil, "reason": "<script>x</script>"}]
    rendered = report.render_html("anova-test", d)
    assert "<img src=y onerror" not in rendered
    assert "<script>x</script>" not in rendered


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
