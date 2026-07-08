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


def test_mixed_effects_render_markdown_factor_p_values():
    report = load_report_module()
    markdown = report.render_md("anova-test", multi_factor_analysis())

    assert "- Factors: model, effort" in markdown
    assert "- model: p: 0.0100 — SIGNIFICANT" in markdown
    assert "- effort: p: 0.2000 — not significant" in markdown
    assert "model=claude-opus-4-6, effort=low" in markdown


def test_mixed_effects_render_html_factor_p_values():
    report = load_report_module()
    rendered = report.render_html("anova-test", multi_factor_analysis())

    assert "SIGNIFICANT" in rendered
    assert "<td>model</td><td class=num>0.0100</td>" in rendered
    assert "<td>effort</td><td class=num>0.2000</td>" in rendered
    assert "model, effort" in rendered
