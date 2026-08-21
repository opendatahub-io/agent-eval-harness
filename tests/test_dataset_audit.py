"""Tests for the deterministic dataset audit engine (agent_eval.dataset_audit)."""

import sys
from pathlib import Path

import pytest
import yaml

from agent_eval.config import EvalConfig
from agent_eval.dataset_audit import (
    ANNOTATION_FIELDS,
    AUDIT_FILENAME,
    MANIFEST_FILENAME,
    case_content_hash,
    check_argument_fields,
    check_composition,
    check_conditional_judges,
    check_contamination,
    check_near_duplicates,
    check_reference_resolution,
    condition_scope,
    evaluate_condition,
    iter_case_dirs,
    load_audit,
    load_case,
    run_audit,
    write_audit,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCORE_PY = REPO_ROOT / "skills" / "eval-run" / "scripts" / "score.py"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_config(tmp_path, *, generation=None, judges=None, execution=None,
                workspace_files=None):
    """Write a minimal eval.yaml under tmp_path and load it."""
    config_data = {
        "name": "audit-test",
        "execution": execution or {"mode": "case",
                                   "prompt": "{{ input.prompt }}"},
        "dataset": {"path": "dataset", "schema": "input.yaml with prompt"},
        "outputs": [{"path": "output", "schema": "stdout"}],
    }
    if workspace_files:
        config_data["dataset"]["workspace"] = {"files": workspace_files}
    if generation is not None:
        config_data["generation"] = generation
    if judges is not None:
        config_data["judges"] = judges
    config_path = tmp_path / "eval.yaml"
    config_path.write_text(yaml.dump(config_data))
    (tmp_path / "dataset").mkdir(exist_ok=True)
    return EvalConfig.from_yaml(config_path)


def make_case(dataset_dir, name, *, input_data=None, input_text=None,
              annotations=None, answers=None, files=None):
    case = Path(dataset_dir) / name
    case.mkdir(parents=True)
    if input_text is not None:
        (case / "input.yaml").write_text(input_text)
    elif input_data is not None:
        (case / "input.yaml").write_text(yaml.dump(input_data))
    if annotations is not None:
        (case / "annotations.yaml").write_text(yaml.dump(annotations))
    if answers is not None:
        (case / "answers.yaml").write_text(yaml.dump(answers))
    for rel, content in (files or {}).items():
        p = case / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return case


def load_cases(dataset_dir):
    return [load_case(d) for d in iter_case_dirs(dataset_dir)]


# ---------------------------------------------------------------------------
# Reference resolution (C6)
# ---------------------------------------------------------------------------

class TestReferenceResolution:
    def test_found_missing_and_label(self, tmp_path):
        config = make_config(tmp_path)
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("real doc")
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"expected_files": ["docs/a.md",
                                                  "docs/missing.md"]})
        block = check_reference_resolution(load_cases(dataset), config)
        assert block["status"] == "findings"
        assert len(block["findings"]) == 1
        assert block["findings"][0]["entry"] == "docs/missing.md"
        # Honest label: necessary, not sufficient, for answerability
        assert "necessary" in block["label"]
        assert "not" in block["label"] and "sufficient" in block["label"]

    def test_skipped_when_no_path_fields(self, tmp_path):
        """No expected_files anywhere → silently skipped, never warned."""
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        block = check_reference_resolution(load_cases(dataset), config)
        assert block["status"] == "skipped"
        assert block["findings"] == []

    def test_skipped_when_no_root_derivable(self, tmp_path):
        config = make_config(tmp_path)
        config.config_dir = None
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"expected_files": ["docs/a.md"]})
        block = check_reference_resolution(load_cases(dataset), config)
        assert block["status"] == "skipped"
        assert block["findings"] == []

    def test_entry_point_parent_is_second_root(self, tmp_path):
        """A generation.context entry_point with a dir component adds a root."""
        config = make_config(tmp_path, generation={
            "strategy": "synthetic",
            "context": {"documentation_structure": {
                "entry_point": "repo/CLAUDE.md"}},
            "seeds": [{"category": "navigation",
                       "builtin": "docs/navigation", "count": 1}],
        })
        (tmp_path / "repo" / "docs").mkdir(parents=True)
        (tmp_path / "repo" / "docs" / "b.md").write_text("doc")
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"expected_files": ["docs/b.md"]})
        block = check_reference_resolution(load_cases(dataset), config)
        assert block["findings"] == []


# ---------------------------------------------------------------------------
# Contamination (C8)
# ---------------------------------------------------------------------------

LONG_GUIDANCE = "Suggest enabling signature verification in the operator config"


class TestContamination:
    def test_flags_leak_into_input(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": f"A question. {LONG_GUIDANCE}"},
            annotations={"expected_guidance": LONG_GUIDANCE})
        block = check_contamination(load_cases(dataset), config)
        leaks = [f for f in block["findings"]
                 if f.get("source") == "annotations.expected_guidance"]
        assert len(leaks) == 1
        assert leaks[0]["case"] == "case-001"

    def test_whitelists_sanctioned_answers_yaml(self, tmp_path):
        """answers.yaml presence is NEVER a finding — only leakage counts."""
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": "What is the recommended rollout?"},
            answers={"priority": "Answer Normal unless data loss is involved"})
        block = check_contamination(load_cases(dataset), config)
        assert block["findings"] == []

    def test_answers_leaf_pasted_into_input_is_one_finding(self, tmp_path):
        answer_text = "Answer Normal unless data loss is involved"
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": f"What next? Hint: {answer_text}"},
            answers={"priority": answer_text})
        block = check_contamination(load_cases(dataset), config)
        leaks = [f for f in block["findings"]
                 if f.get("source") == "answers.yaml"]
        assert len(leaks) == 1
        assert leaks[0]["case"] == "case-001"
        assert "input.yaml" in leaks[0]["surface"]

    def test_min_length_guard(self, tmp_path):
        """Short values (< MIN_LEAK_LEN normalized chars) are never flagged."""
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": "short mention of easy things"},
            annotations={"expected_mentions": ["easy", "short"]})
        block = check_contamination(load_cases(dataset), config)
        assert [f for f in block["findings"]
                if "expected_mentions" in str(f.get("source"))] == []

    def test_excluded_label_fields_never_needles(self, tmp_path):
        """category/difficulty/… are labels, not answer content."""
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": "a navigation-category-style question x"},
            annotations={"category": "navigation-category-style question x"})
        block = check_contamination(load_cases(dataset), config)
        assert block["findings"] == []

    def test_misplaced_fields_and_todo_placeholders(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": "q", "expected_files": ["a.md"],
                        "project_key": "TODO_JIRA_PROJECT_KEY"})
        block = check_contamination(load_cases(dataset), config)
        misplaced = [f for f in block["findings"] if f.get("field")]
        assert [f["field"] for f in misplaced] == ["expected_files"]
        todos = [f for f in block["findings"] if f.get("placeholder")]
        assert len(todos) == 1
        assert todos[0]["placeholder"] == "TODO_JIRA_PROJECT_KEY"
        assert todos[0]["severity"] == "info"

    def test_companion_files_are_agent_visible(self, tmp_path):
        config = make_config(tmp_path, workspace_files=["src/"])
        dataset = tmp_path / "dataset"
        make_case(
            dataset, "case-001",
            input_data={"prompt": "fix the bug"},
            annotations={"expected_guidance": LONG_GUIDANCE},
            files={"src/notes.md": f"context: {LONG_GUIDANCE}"})
        block = check_contamination(load_cases(dataset), config)
        leaks = [f for f in block["findings"]
                 if f.get("surface") == "src/notes.md"]
        assert len(leaks) == 1


# ---------------------------------------------------------------------------
# Near duplicates
# ---------------------------------------------------------------------------

class TestNearDuplicates:
    def test_flags_identical_prompts(self, tmp_path):
        dataset = tmp_path / "dataset"
        dataset.mkdir()
        make_case(dataset, "case-001",
                  input_data={"prompt": "How do I configure the operator?"})
        make_case(dataset, "case-002",
                  input_data={"prompt": "How do I configure the operator?"})
        block = check_near_duplicates(load_cases(dataset), threshold=0.85)
        assert len(block["findings"]) == 1
        finding = block["findings"][0]
        assert sorted(finding["cases"]) == ["case-001", "case-002"]
        assert finding["similarity"] >= 0.99

    def test_respects_threshold(self, tmp_path):
        dataset = tmp_path / "dataset"
        dataset.mkdir()
        make_case(dataset, "case-001",
                  input_data={"prompt": "How do I configure the operator?"})
        make_case(dataset, "case-002",
                  input_data={"prompt": "Where are the workflow documents "
                                        "for release planning stored?"})
        high = check_near_duplicates(load_cases(dataset), threshold=0.95)
        assert high["findings"] == []
        low = check_near_duplicates(load_cases(dataset), threshold=0.05)
        assert len(low["findings"]) == 1
        assert low["threshold"] == 0.05


# ---------------------------------------------------------------------------
# Composition (C5, C7-adjacent)
# ---------------------------------------------------------------------------

class TestComposition:
    def _synthetic_config(self, tmp_path, count=5):
        return make_config(tmp_path, generation={
            "strategy": "synthetic",
            "context": {"type": "repo"},
            "seeds": [{"category": "navigation",
                       "builtin": "docs/navigation", "count": count}],
        })

    def test_seed_requested_vs_realized(self, tmp_path):
        config = self._synthetic_config(tmp_path, count=5)
        dataset = tmp_path / "dataset"
        for i in range(4):
            make_case(dataset, f"case-00{i + 1}",
                      input_data={"prompt": f"q{i}"},
                      annotations={"category": "navigation"})
        block = check_composition(load_cases(dataset), config)
        assert block["seeds"] == [{"category": "navigation",
                                   "requested": 5, "realized": 4}]
        assert any("requested 5, realized 4" in f["message"]
                   for f in block["findings"])
        assert block["by_category"] == {"navigation": 4}

    def test_undeclared_category_flagged(self, tmp_path):
        config = self._synthetic_config(tmp_path, count=1)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        make_case(dataset, "case-002", input_data={"prompt": "q2"},
                  annotations={"category": "rogue"})
        block = check_composition(load_cases(dataset), config)
        assert any("declared by no generation seed" in f["message"]
                   for f in block["findings"])

    def test_difficulty_presence_conditional_no_field_no_warning(
            self, tmp_path):
        """C5: a dataset with NO difficulty fields never warns."""
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        block = check_composition(load_cases(dataset), config)
        assert "by_difficulty" not in block
        assert [f for f in block["findings"]
                if "difficulty" in f.get("message", "")] == []

    def test_difficulty_vocabulary_flagged_when_present(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"difficulty": "easy"})
        make_case(dataset, "case-002", input_data={"prompt": "q2"},
                  annotations={"difficulty": "extreme"})
        block = check_composition(load_cases(dataset), config)
        bad = [f for f in block["findings"]
               if "difficulty" in f.get("message", "")]
        assert len(bad) == 1
        assert "extreme" in bad[0]["message"]
        assert block["by_difficulty"] == {"easy": 1, "extreme": 1}

    def test_custom_difficulty_vocabulary(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"difficulty": "trivial"})
        block = check_composition(load_cases(dataset), config,
                                  difficulty_values=["trivial", "brutal"])
        assert block["findings"] == []


# ---------------------------------------------------------------------------
# Conditional judges (C4) + cross-pin against score.py
# ---------------------------------------------------------------------------

NAV_CONDITION = "annotations.get('category') == 'navigation'"


class TestConditionalJudges:
    def _config(self, tmp_path, condition):
        return make_config(tmp_path, judges=[{
            "name": "nav_judge", "llm_rubric": "Cited the right doc",
            "feedback_type": "bool", "if": condition,
        }])

    def test_annotations_only_both_branches(self, tmp_path):
        config = self._config(tmp_path, NAV_CONDITION)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        make_case(dataset, "case-002", input_data={"prompt": "q"},
                  annotations={"category": "authoring"})
        block = check_conditional_judges(load_cases(dataset), config)
        row = block["judges"][0]
        assert row["scope"] == "annotations-only"
        assert row["coverage"] == "both"
        assert (row["true"], row["false"]) == (1, 1)
        assert block["findings"] == []

    def test_annotations_only_always_runs(self, tmp_path):
        config = self._config(tmp_path, NAV_CONDITION)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        block = check_conditional_judges(load_cases(dataset), config)
        assert block["judges"][0]["coverage"] == "always-runs"
        assert any("always runs" in f["message"] for f in block["findings"])

    def test_annotations_only_never_runs(self, tmp_path):
        config = self._config(tmp_path, NAV_CONDITION)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "authoring"})
        block = check_conditional_judges(load_cases(dataset), config)
        assert block["judges"][0]["coverage"] == "never-runs"
        assert any("never runs" in f["message"] for f in block["findings"])

    def test_outputs_dependent_indeterminate_never_uncovered(self, tmp_path):
        """C4: outputs-referencing conditions are indeterminate wholesale."""
        config = self._config(tmp_path, "outputs.get('files')")
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        block = check_conditional_judges(load_cases(dataset), config)
        row = block["judges"][0]
        assert row["scope"] == "outputs-dependent"
        assert row["coverage"] == "indeterminate"
        assert block["findings"] == []  # never reported as uncovered

    def test_templated_condition_indeterminate(self, tmp_path):
        config = self._config(tmp_path,
                              "{{ annotations.category }} == 'navigation'")
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        block = check_conditional_judges(load_cases(dataset), config)
        row = block["judges"][0]
        assert row["scope"] == "templated"
        assert row["coverage"] == "indeterminate"
        assert block["findings"] == []

    def test_invalid_condition_flagged(self, tmp_path):
        config = self._config(tmp_path, "inputs.get('x')")
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        block = check_conditional_judges(load_cases(dataset), config)
        assert block["judges"][0]["scope"] == "invalid"
        assert any("could not be analyzed" in f["message"]
                   for f in block["findings"])

    def test_syntax_error_condition_is_invalid(self):
        assert condition_scope("annotations.get(") == "invalid"

    def test_no_conditional_judges_skipped(self, tmp_path):
        config = make_config(tmp_path, judges=[{
            "name": "plain", "check": "return (True, 'ok')"}])
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        block = check_conditional_judges(load_cases(dataset), config)
        assert block["status"] == "skipped"


class TestConditionEvaluatorCrossPin:
    """The audit's evaluator must match score.py's condition semantics.

    score.py evaluates `eval(condition, {"__builtins__": {}},
    {"annotations": annotations, "outputs": rec})`. The audit restricts
    itself to annotations-only conditions, where the `outputs` binding is
    irrelevant — this pins both the literal form in score.py and semantic
    parity on a shared table of conditions.
    """

    # (condition, annotations) — annotations-only conditions, including one
    # that raises identically under both evaluators.
    TABLE = [
        (NAV_CONDITION, {"category": "navigation"}),
        (NAV_CONDITION, {"category": "authoring"}),
        ("not annotations.get('dedup_is_duplicate')", {}),
        ("not annotations.get('dedup_is_duplicate')",
         {"dedup_is_duplicate": True}),
        ("annotations.get('difficulty') in ('easy', 'medium')",
         {"difficulty": "hard"}),
        ("annotations.get('count', 0) > 2", {"count": 5}),
        ("annotations['missing_key']", {}),  # raises KeyError in both
        ("annotations.get('x') and annotations.get('y')",
         {"x": 1, "y": 0}),
    ]

    def test_score_py_still_uses_the_pinned_eval_form(self):
        source = SCORE_PY.read_text()
        assert 'eval(condition, {"__builtins__": {}},' in source, (
            "score.py's condition evaluator changed — update "
            "agent_eval.dataset_audit.evaluate_condition to match and "
            "refresh this cross-pin")
        assert '{"annotations": annotations, "outputs": rec}' in source

    @pytest.mark.parametrize("condition, annotations", TABLE)
    def test_parity_with_score_py_semantics(self, condition, annotations):
        rec = {"annotations": annotations, "files": {}}

        def score_py_semantics():
            # Verbatim form from score.py's _score_case
            return bool(eval(condition, {"__builtins__": {}},
                             {"annotations": annotations, "outputs": rec}))

        try:
            expected = score_py_semantics()
        except Exception as e:
            with pytest.raises(type(e)):
                evaluate_condition(condition, annotations)
        else:
            assert evaluate_condition(condition, annotations) == expected


# ---------------------------------------------------------------------------
# Argument fields
# ---------------------------------------------------------------------------

class TestArgumentFields:
    def test_simple_brace_template_missing_field(self, tmp_path):
        config = make_config(tmp_path, execution={
            "mode": "case", "skill": "my-skill",
            "arguments": '--key {strat_key} --note {note?}'})
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001",
                  input_data={"strat_key": "abc", "note": "n"})
        make_case(dataset, "case-002", input_data={"prompt": "q"})
        block = check_argument_fields(load_cases(dataset), config)
        assert block["required_fields"] == ["strat_key"]
        assert block["optional_fields"] == ["note"]
        assert len(block["findings"]) == 1
        assert block["findings"][0]["case"] == "case-002"
        assert block["findings"][0]["field"] == "strat_key"

    def test_empty_required_field_flagged(self, tmp_path):
        config = make_config(tmp_path, execution={
            "mode": "case", "skill": "my-skill", "arguments": "{prompt}"})
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "   "})
        block = check_argument_fields(load_cases(dataset), config)
        assert len(block["findings"]) == 1
        assert "empty" in block["findings"][0]["message"]

    def test_jinja_simple_fields_checked(self, tmp_path):
        config = make_config(tmp_path, execution={
            "mode": "case", "skill": "my-skill",
            "arguments": '--p {{ input.priority }} "{{ input.prompt }}"'})
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        block = check_argument_fields(load_cases(dataset), config)
        assert block["required_fields"] == ["priority", "prompt"]
        assert len(block["findings"]) == 1
        assert block["findings"][0]["field"] == "priority"

    def test_jinja_complex_expression_indeterminate(self, tmp_path):
        config = make_config(tmp_path, execution={
            "mode": "case", "skill": "my-skill",
            "arguments": "{{ input.get('x', '') }}"})
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        block = check_argument_fields(load_cases(dataset), config)
        assert block["status"] == "indeterminate"
        assert block["findings"] == []

    def test_batch_mode_skipped(self, tmp_path):
        config = make_config(tmp_path, execution={
            "mode": "batch", "skill": "my-skill",
            "arguments": "--input batch.yaml"})
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        block = check_argument_fields(load_cases(dataset), config)
        assert block["status"] == "skipped"


# ---------------------------------------------------------------------------
# Hashing (C3), persistence, discovery invisibility
# ---------------------------------------------------------------------------

class TestCaseContentHash:
    def test_changes_on_in_place_edit(self, tmp_path):
        """C3: an in-place edit changes the content hash (dir mtime doesn't)."""
        case = make_case(tmp_path, "case-001", input_data={"prompt": "before"})
        before = case_content_hash(case)
        (case / "input.yaml").write_text("prompt: after\n")
        assert case_content_hash(case) != before

    def test_stable_for_identical_content(self, tmp_path):
        a = make_case(tmp_path / "a", "case-001",
                      input_data={"prompt": "same"})
        b = make_case(tmp_path / "b", "case-001",
                      input_data={"prompt": "same"})
        assert case_content_hash(a) == case_content_hash(b)

    def test_sensitive_to_file_renames(self, tmp_path):
        case = make_case(tmp_path, "case-001", input_data={"prompt": "x"})
        before = case_content_hash(case)
        (case / "input.yaml").rename(case / "input.yml")
        assert case_content_hash(case) != before


class TestAuditPersistence:
    def _run(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  annotations={"category": "navigation"})
        audit = run_audit(config, now="2026-08-21T00:00:00+00:00")
        return config, dataset, audit

    def test_write_load_roundtrip(self, tmp_path):
        _, dataset, audit = self._run(tmp_path)
        path = write_audit(audit, dataset)
        assert path.name == AUDIT_FILENAME
        loaded = load_audit(dataset)
        assert loaded == audit
        assert loaded["generated_at"] == "2026-08-21T00:00:00+00:00"
        assert "case-001" in loaded["case_hashes"]
        assert loaded["cases"]["case-001"]["files"] == [
            "annotations.yaml", "input.yaml"]

    def test_load_and_merge_preserves_foreign_null_probe(self, tmp_path):
        """Round-trip: foreign top-level keys survive an audit re-write."""
        config, dataset, audit = self._run(tmp_path)
        write_audit(audit, dataset)

        # A foreign tool (e.g. the PR5 null probe) merges its own section.
        path = dataset / AUDIT_FILENAME
        data = yaml.safe_load(path.read_text())
        data["null_probe"] = {"null_pass_rate": 0.2, "n": 5}
        path.write_text(yaml.safe_dump(data, sort_keys=False))

        fresh = run_audit(config, now="2026-08-22T00:00:00+00:00")
        write_audit(fresh, dataset)
        merged = load_audit(dataset)
        assert merged["null_probe"] == {"null_pass_rate": 0.2, "n": 5}
        assert merged["generated_at"] == "2026-08-22T00:00:00+00:00"
        for key in ("audit_version", "checks", "summary", "cases",
                    "case_hashes", "parameters", "dataset_path"):
            assert key in merged

    def test_corrupt_audit_loads_as_none_and_is_rewritten(self, tmp_path):
        _, dataset, audit = self._run(tmp_path)
        (dataset / AUDIT_FILENAME).write_text("{{{[not yaml")
        assert load_audit(dataset) is None
        write_audit(audit, dataset)  # must not raise
        assert load_audit(dataset) is not None


class TestDiscoveryInvisibility:
    def test_root_files_invisible_to_case_discovery(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        make_case(dataset, "case-002", input_data={"prompt": "r"})
        before = [d.name for d in iter_case_dirs(dataset)]

        audit = run_audit(config)
        write_audit(audit, dataset)
        (dataset / MANIFEST_FILENAME).write_text("manifest_version: 1\n")

        after = [d.name for d in iter_case_dirs(dataset)]
        assert after == before == ["case-001", "case-002"]


# ---------------------------------------------------------------------------
# run_audit orchestration
# ---------------------------------------------------------------------------

class TestRunAudit:
    def test_empty_dataset_all_checks_skipped(self, tmp_path):
        config = make_config(tmp_path)
        audit = run_audit(config)
        assert audit["summary"]["cases"] == 0
        assert all(check["status"] == "skipped"
                   for check in audit["checks"].values())
        assert audit["case_hashes"] == {}

    def test_single_case_no_crash(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        audit = run_audit(config)
        assert audit["summary"]["cases"] == 1
        assert audit["checks"]["near_duplicates"]["findings"] == []

    def test_summary_counts_by_severity(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001",
                  input_data={"key": "TODO_JIRA_PROJECT_KEY"},
                  files={"empty.md": ""})
        audit = run_audit(config)
        assert audit["summary"]["warnings"] >= 1  # empty file
        assert audit["summary"]["info"] >= 1  # TODO_ placeholder

    def test_parameters_recorded(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"})
        audit = run_audit(config, duplicate_threshold=0.7,
                          difficulty_values=["a", "b"])
        assert audit["parameters"] == {"duplicate_threshold": 0.7,
                                       "difficulty_values": ["a", "b"]}

    def test_unparseable_input_is_error(self, tmp_path):
        config = make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_text="{{{[not yaml")
        audit = run_audit(config)
        assert audit["summary"]["errors"] >= 1


# ---------------------------------------------------------------------------
# Shared-constant contract with generate_synthetic
# ---------------------------------------------------------------------------

def test_annotation_fields_shared_with_generator():
    """The generator's alias must be the canonical set (cannot diverge)."""
    sys.path.insert(0, str(REPO_ROOT / "skills" / "eval-dataset" / "scripts"))
    import generate_synthetic

    assert generate_synthetic._ANNOTATION_FIELDS is ANNOTATION_FIELDS


# ---------------------------------------------------------------------------
# CLI (audit_dataset.py)
# ---------------------------------------------------------------------------

class TestCLI:
    def _setup(self, tmp_path):
        make_config(tmp_path)
        dataset = tmp_path / "dataset"
        make_case(dataset, "case-001", input_data={"prompt": "q"},
                  files={"empty.md": ""})  # one warning finding
        return tmp_path / "eval.yaml", dataset

    def _main(self):
        sys.path.insert(0, str(
            REPO_ROOT / "skills" / "eval-dataset" / "scripts"))
        from audit_dataset import main
        return main

    def test_exit_zero_on_findings_by_default(self, tmp_path, monkeypatch,
                                              capsys):
        config_path, dataset = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path)])
        self._main()()  # no SystemExit → exit 0
        out = capsys.readouterr().out
        assert "Audit written to" in out
        assert (dataset / AUDIT_FILENAME).is_file()

    def test_strict_exits_one_on_findings(self, tmp_path, monkeypatch):
        config_path, _ = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path), "--strict"])
        with pytest.raises(SystemExit) as exc:
            self._main()()
        assert exc.value.code == 1

    @pytest.mark.parametrize("value", ["0", "1.5", "-0.2"])
    def test_rejects_out_of_range_duplicate_threshold(
            self, tmp_path, monkeypatch, value):
        config_path, _ = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path),
                             "--duplicate-threshold", value])
        with pytest.raises(SystemExit) as exc:
            self._main()()
        assert exc.value.code == 2

    def test_timestamp_flag_recorded(self, tmp_path, monkeypatch):
        config_path, dataset = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["audit_dataset.py", "--config",
                             str(config_path),
                             "--timestamp", "2026-08-21T12:00:00+00:00"])
        self._main()()
        audit = load_audit(dataset)
        assert audit["generated_at"] == "2026-08-21T12:00:00+00:00"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
