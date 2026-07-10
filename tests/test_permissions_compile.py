"""Tests for compiling eval.yaml permission rules into Claude Code patterns."""

from agent_eval.tools.permissions import compile_permission_rules


def test_directory_path_is_recursive():
    # A trailing-slash path must recurse with ** (not * which is direct-children only).
    out = compile_permission_rules([{"path": "eval/", "tools": ["Read"]}])
    assert out == ["Read(eval/**)"]


def test_file_path_is_verbatim():
    out = compile_permission_rules([{"path": "eval.yaml", "tools": ["Read", "Edit"]}])
    assert out == ["Read(eval.yaml)", "Edit(eval.yaml)"]


def test_path_scoped_tools_only():
    out = compile_permission_rules([{"path": "eval/", "tools": ["Read", "Edit", "Grep", "Glob"]}])
    assert out == ["Read(eval/**)", "Edit(eval/**)", "Grep(eval/**)", "Glob(eval/**)"]


def test_bash_is_skipped_no_op_pattern():
    # Bash(path) matches the command string, not a file path — never emit it.
    out = compile_permission_rules([{"path": "eval/", "tools": ["Bash"]}])
    assert out == []


def test_harden_bash_adds_read_edit_for_deny():
    # deny lists that name Bash (intending file protection) get Read/Edit coverage.
    out = compile_permission_rules(
        [{"path": "eval/", "tools": ["Grep", "Bash"]}], harden_bash=True
    )
    assert "Read(eval/**)" in out and "Edit(eval/**)" in out
    assert "Grep(eval/**)" in out
    assert not any(p.startswith("Bash(") for p in out)


def test_no_harden_for_allow_does_not_over_grant():
    # allow lists must NOT gain Read/Edit just because Bash was named.
    out = compile_permission_rules([{"path": "docs/", "tools": ["Bash"]}])
    assert out == []


def test_string_rules_pass_through():
    out = compile_permission_rules(["Skill", "Write(artifacts/**)"])
    assert out == ["Skill", "Write(artifacts/**)"]


def test_mixed_and_dedup():
    out = compile_permission_rules(
        [
            "Skill",
            {"path": "eval/", "tools": ["Read"]},
            {"path": "eval/", "tools": ["Read"]},  # duplicate
        ]
    )
    assert out == ["Skill", "Read(eval/**)"]


def test_empty_and_none():
    assert compile_permission_rules([]) == []
    assert compile_permission_rules(None) == []


def test_template_deny_block_compiles_correctly():
    # The exact recommended prompt-mode deny block should yield recursive Read
    # patterns and never a non-recursive '*' or a Bash path pattern.
    deny = [
        {"path": "eval/", "tools": ["Read", "Edit", "Grep", "Glob"]},
        {"path": "eval.yaml", "tools": ["Read", "Edit", "Grep"]},
        {"path": "tmp/", "tools": ["Read", "Edit", "Grep", "Glob"]},
    ]
    out = compile_permission_rules(deny, harden_bash=True)
    assert "Read(eval/**)" in out
    assert "Read(eval.yaml)" in out
    assert "Read(tmp/**)" in out
    assert not any(p.startswith("Bash(") for p in out)
    assert not any(p == "Read(eval/*)" for p in out)  # non-recursive form must be gone
