"""Tests for skills/eval-analyze/scripts/find_skills.py.

Covers the CWE-22 path-traversal / symlink guard (``_is_under_cwd``) applied to
``list_skills()`` and ``find_skill()``. This logic shipped in PR #147 without any
test coverage, even though the repo already unit-tests the equivalent guard for
config.py in ``test_path_validation.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "skills/eval-analyze/scripts"))

import find_skills  # noqa: E402


def _make_skill(skills_dir: Path, name: str, description: str = "does things"):
    """Create <skills_dir>/<name>/SKILL.md with minimal frontmatter."""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody\n"
    )
    return d / "SKILL.md"


def _isolated_project(tmp_path, monkeypatch):
    """chdir into <tmp>/project and return (project, external) dirs.

    ``external`` sits outside the project (a sibling under tmp_path) so symlinks
    pointing at it genuinely escape the project boundary.
    """
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    monkeypatch.chdir(project)
    return project, external


class TestIsUnderCwd:
    def test_path_inside_cwd_is_accepted(self, tmp_path, monkeypatch):
        project, _ = _isolated_project(tmp_path, monkeypatch)
        target = _make_skill(project / "skills", "a")
        resolved = find_skills._is_under_cwd("skills/a/SKILL.md")
        assert resolved == target.resolve()

    def test_symlink_escaping_cwd_is_rejected(self, tmp_path, monkeypatch):
        project, external = _isolated_project(tmp_path, monkeypatch)
        (external / "SKILL.md").write_text("x")
        (project / "skills").mkdir()
        (project / "skills" / "evil").symlink_to(external, target_is_directory=True)
        # Path resolves outside CWD and under no trusted root -> rejected.
        assert find_skills._is_under_cwd("skills/evil/SKILL.md") is None

    def test_trusted_root_outside_cwd_is_accepted(self, tmp_path, monkeypatch):
        # A skill dir that resolves outside CWD but under an explicitly trusted
        # root (e.g. a symlinked skills/ root) must still be accepted — this is
        # the branch that would be dead if trusted_roots were always under CWD.
        project, external = _isolated_project(tmp_path, monkeypatch)
        (external / "shared").mkdir()
        (external / "shared" / "SKILL.md").write_text("x")
        trusted = (external.resolve(),)
        resolved = find_skills._is_under_cwd(
            external / "shared" / "SKILL.md", trusted)
        assert resolved == (external / "shared" / "SKILL.md").resolve()

    def test_traversal_string_is_rejected(self, tmp_path, monkeypatch):
        _isolated_project(tmp_path, monkeypatch)
        assert find_skills._is_under_cwd("../../../../etc/passwd") is None


class TestListSkills:
    def test_returns_real_skills_with_resolved_paths(self, tmp_path, monkeypatch):
        project, _ = _isolated_project(tmp_path, monkeypatch)
        _make_skill(project / "skills", "alpha", "first skill")
        _make_skill(project / "skills", "beta", "second skill")

        skills = find_skills.list_skills()

        by_name = {s["dir_name"]: s for s in skills}
        assert set(by_name) == {"alpha", "beta"}
        assert by_name["alpha"]["description"] == "first skill"
        # Paths are returned resolved (absolute) so they match the guard.
        for s in skills:
            assert Path(s["path"]).is_absolute()
            assert Path(s["path"]).is_file()

    def test_symlinked_escape_is_skipped_with_actionable_warning(
            self, tmp_path, monkeypatch, capsys):
        project, external = _isolated_project(tmp_path, monkeypatch)
        _make_skill(project / "skills", "real")
        # A skill symlinked to a directory outside the project.
        (external / "SKILL.md").write_text(
            "---\nname: escaped\ndescription: outside\n---\nbody\n")
        (project / "skills" / "escaped").symlink_to(
            external, target_is_directory=True)

        skills = find_skills.list_skills()

        assert {s["dir_name"] for s in skills} == {"real"}
        err = capsys.readouterr().err
        assert "resolves outside the project" in err

    def test_harness_skills_are_excluded(self, tmp_path, monkeypatch):
        project, _ = _isolated_project(tmp_path, monkeypatch)
        _make_skill(project / "skills", "eval-run")   # harness skill
        _make_skill(project / "skills", "mine")       # project skill

        skills = find_skills.list_skills()

        assert {s["dir_name"] for s in skills} == {"mine"}


class TestFindSkill:
    def test_finds_by_directory_name(self, tmp_path, monkeypatch):
        project, _ = _isolated_project(tmp_path, monkeypatch)
        target = _make_skill(project / "skills", "enhancer")

        found = find_skills.find_skill("enhancer")

        assert found == target.resolve()
        assert found.is_file()

    def test_strips_colon_scope_prefix(self, tmp_path, monkeypatch):
        project, _ = _isolated_project(tmp_path, monkeypatch)
        target = _make_skill(project / "skills", "enhance")

        assert find_skills.find_skill("skill:enhance") == target.resolve()

    def test_traversal_name_returns_none(self, tmp_path, monkeypatch):
        _isolated_project(tmp_path, monkeypatch)
        assert find_skills.find_skill("../../../../etc/passwd") is None

    def test_symlinked_escape_is_not_returned(self, tmp_path, monkeypatch):
        project, external = _isolated_project(tmp_path, monkeypatch)
        (external / "SKILL.md").write_text(
            "---\nname: escaped\ndescription: outside\n---\nbody\n")
        (project / "skills").mkdir()
        (project / "skills" / "escaped").symlink_to(
            external, target_is_directory=True)

        assert find_skills.find_skill("escaped") is None
