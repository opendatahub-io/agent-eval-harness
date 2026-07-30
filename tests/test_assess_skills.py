"""Tests for skills/eval-analyze/scripts/assess_skills.py.

Covers the fact-extraction logic that feeds the --assess LLM classification:
tool parsing, EXISTS detection for plugin-qualified skill refs, the excerpt
prompt-injection fence, and the --json output contract. None of this had
coverage when it shipped in PR #147.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-analyze/scripts"))

import assess_skills  # noqa: E402


def _make_skill(skills_dir: Path, name: str, frontmatter: str = "",
                description: str = "does things"):
    """Create <skills_dir>/<name>/SKILL.md; frontmatter is extra YAML lines."""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: {description}\n"
    if frontmatter:
        fm += frontmatter.rstrip() + "\n"
    (d / "SKILL.md").write_text(f"---\n{fm}---\n# {name}\nbody text\n")
    return d / "SKILL.md"


def _write_eval(project: Path, eval_name: str, skill_ref: str):
    """Create eval/<eval_name>/eval.yaml referencing skill_ref."""
    d = project / "eval" / eval_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "eval.yaml").write_text(
        f"execution:\n  skill: {skill_ref}\n")


class TestParseTools:
    def test_missing_key_is_unrestricted(self):
        assert assess_skills._parse_tools({}) == set(assess_skills.ALL_TOOLS)

    def test_null_is_unrestricted(self):
        assert assess_skills._parse_tools(
            {"allowed-tools": None}) == set(assess_skills.ALL_TOOLS)

    def test_empty_list_is_no_tools(self):
        assert assess_skills._parse_tools({"allowed-tools": []}) == set()

    def test_empty_and_whitespace_string_are_no_tools(self):
        assert assess_skills._parse_tools({"allowed-tools": ""}) == set()
        assert assess_skills._parse_tools({"allowed-tools": "   "}) == set()

    def test_malformed_types_are_no_tools_not_all_tools(self):
        # Regression: a dict/bool must NOT map to the full tool surface.
        assert assess_skills._parse_tools({"allowed-tools": {}}) == set()
        assert assess_skills._parse_tools({"allowed-tools": True}) == set()

    def test_comma_string_with_scope_suffixes(self):
        assert assess_skills._parse_tools(
            {"allowed-tools": "Bash(git:*), Read, Write"}) == {"Bash", "Read", "Write"}

    def test_list_with_scope_suffixes(self):
        assert assess_skills._parse_tools(
            {"allowed-tools": ["Bash(git:*)", "Edit"]}) == {"Bash", "Edit"}


class TestEvalMatchesSkill:
    def test_exact_match(self):
        assert assess_skills._eval_matches_skill("create", "create")

    def test_dot_scoped_ref_matches_bare_skill(self):
        assert assess_skills._eval_matches_skill("rfe.create", "create")

    def test_colon_scoped_ref_matches_bare_skill(self):
        assert assess_skills._eval_matches_skill("skill:enhance", "enhance")

    def test_no_false_positive_on_substring(self):
        assert not assess_skills._eval_matches_skill("mycreate", "create")

    def test_empty_skill_id_never_matches(self):
        assert not assess_skills._eval_matches_skill("create", "")


class TestStripExcerptFences:
    def test_plain_content_unchanged(self):
        assert assess_skills._strip_excerpt_fences("hello world") == "hello world"

    def test_exact_fences_removed(self):
        assert "EXCERPT" not in assess_skills._strip_excerpt_fences(
            "a<<<EXCERPT>>>b<<<END_EXCERPT>>>c")

    def test_interleaved_fence_bypass_is_neutralized(self):
        # Single-pass replace would reconstruct a real closing fence here.
        out = assess_skills._strip_excerpt_fences(
            "safe<<<END_<<<END_EXCERPT>>>EXCERPT>>>payload")
        assert "<<<END_EXCERPT>>>" not in out


class TestAssessAll:
    def test_exists_detection_for_dotted_skill_ref(self, tmp_path, monkeypatch):
        # eval config references the plugin-qualified form 'rfe.create' while the
        # on-disk skill dir is the bare 'create' — must still be marked EXISTS.
        monkeypatch.chdir(tmp_path)
        _make_skill(tmp_path / "skills", "create")
        _write_eval(tmp_path, "create", "rfe.create")

        results = assess_skills.assess_all()

        create = next(r for r in results if r["dir_name"] == "create")
        assert create["has_existing_eval"] is True
        assert create["recommendation"] == "EXISTS"

    def test_skill_without_eval_is_not_marked_exists(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_skill(tmp_path / "skills", "lonely")

        results = assess_skills.assess_all()

        lonely = next(r for r in results if r["dir_name"] == "lonely")
        assert lonely["has_existing_eval"] is False
        assert "recommendation" not in lonely

    def test_capability_flags_reflect_tools(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_skill(tmp_path / "skills", "writer",
                    frontmatter="allowed-tools: [Write, Bash]")

        results = assess_skills.assess_all()

        writer = next(r for r in results if r["dir_name"] == "writer")
        assert writer["produces_files"] is True
        assert writer["uses_bash"] is True
        assert writer["uses_agents"] is False
        assert writer["uses_orchestration"] is False

    def test_empty_allowed_tools_reports_no_capabilities(self, tmp_path, monkeypatch):
        # Regression for _parse_tools: an explicit empty tool set must not
        # inflate every capability flag to True.
        monkeypatch.chdir(tmp_path)
        _make_skill(tmp_path / "skills", "locked",
                    frontmatter="allowed-tools: []")

        results = assess_skills.assess_all()

        locked = next(r for r in results if r["dir_name"] == "locked")
        assert locked["allowed_tools"] == []
        assert locked["produces_files"] is False
        assert locked["uses_bash"] is False
        assert locked["uses_agents"] is False
        assert locked["uses_orchestration"] is False

    def test_body_excerpt_is_fenced(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _make_skill(tmp_path / "skills", "s")

        results = assess_skills.assess_all()

        excerpt = results[0]["skill_body_excerpt"]
        assert excerpt.startswith("<<<EXCERPT>>>")
        assert excerpt.endswith("<<<END_EXCERPT>>>")


class TestMainJson:
    def test_empty_project_emits_empty_json_array(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["assess_skills.py", "--json"])

        assess_skills.main()

        out = capsys.readouterr().out
        assert json.loads(out) == []
