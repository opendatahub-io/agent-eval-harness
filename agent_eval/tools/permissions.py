"""Compile eval.yaml permission rules into Claude Code settings patterns.

`permissions.allow` / `permissions.deny` in eval.yaml accept two forms, which
this module normalizes into the string patterns Claude Code's ``settings.json``
expects (so both the local runner and the Harbor task package emit valid rules):

- **Simple** — a tool name or pre-formed pattern string, passed through as-is
  (e.g. ``"Skill"``, ``"Write(artifacts/**)"``).
- **Path-based** — a dict ``{"path": "eval/", "tools": ["Read", "Grep"]}``,
  used by prompt-mode / ``workspace_mode: repo`` evals to keep the agent out of
  the answer key (``eval/``, ``eval.yaml``, ``eval.md``, ``tmp/``).

Semantics that shape the conversion (per Claude Code permission docs):
- Path specifiers use **gitignore syntax** — a directory needs ``dir/**`` to
  match recursively; ``dir/*`` only matches direct children.
- ``Read``/``Edit`` deny cover the built-in file tools *and* recognized bash
  file commands (``cat``/``head``/``tail``/``sed``). They do NOT cover arbitrary
  subprocess reads (``python3 -c``, ``awk``) — that needs OS ``sandbox`` denyRead.
- ``Bash(...)`` matches the **command string**, not a file path, so a path-based
  rule can never be expressed as ``Bash(path)`` — we skip Bash entirely.
"""

# Tools whose rules are scoped by a file/path argument (gitignore-style).
PATH_SCOPED_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob")


def _pattern_for(path: str) -> str:
    """A directory (trailing ``/``) matches recursively via ``**``; a file is verbatim."""
    return f"{path}**" if path.endswith("/") else path


def compile_permission_rules(rules, *, harden_bash: bool = False) -> list:
    """Normalize a permissions allow/deny list into Claude Code pattern strings.

    Args:
        rules: list mixing plain strings and path-based dicts (or None).
        harden_bash: for **deny** lists — when a path rule lists ``Bash`` (which
            can't be expressed as a path pattern), also emit ``Read``/``Edit`` so
            the deny actually covers file reads/writes (incl. recognized bash file
            commands). Leave False for **allow** lists so we never over-grant.

    Returns:
        Deduplicated list of pattern strings, order preserved.
    """
    out: list = []
    seen: set = set()

    def add(pattern: str) -> None:
        if pattern not in seen:
            seen.add(pattern)
            out.append(pattern)

    for rule in rules or []:
        if isinstance(rule, dict):
            path = rule.get("path", "")
            if not path:
                continue
            tools = rule.get("tools", []) or []
            pattern = _pattern_for(path)
            emitted = [t for t in tools if t in PATH_SCOPED_TOOLS]
            if harden_bash and "Bash" in tools:
                for t in ("Read", "Edit"):
                    if t not in emitted:
                        emitted.append(t)
            for tool in emitted:
                add(f"{tool}({pattern})")
        else:
            add(rule)

    return out
