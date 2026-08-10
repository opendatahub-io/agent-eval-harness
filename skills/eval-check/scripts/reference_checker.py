#!/usr/bin/env python3
"""Check cross-component references in a Claude Code agent setup.

Scans skills, commands, and eval.yaml files for references to other
components and verifies they resolve. Detects broken references, missing
scripts, and orphan skills.

Inspired by the dependency analysis in harness-eval
(https://github.com/redhat-community-ai-tools/harness-eval).
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_SIZE = 1_000_000

# Skill names may be plugin-namespaced (`rfe.create`, `diagram-skills:skill-diagram`),
# so allow "." and ":" inside a name but never as the final character.
_NAME = r"(\w(?:[\w.:-]*\w)?)"

# High-confidence: an explicit skill path or the documented Skill-tool phrasing.
# An unresolved match here is a genuine broken reference.
_SKILL_REF_PATTERNS = [
    re.compile(r"Skill\s+tool\s+to\s+invoke\s+/" + _NAME),
    re.compile(r"skills/" + _NAME + r"/"),
]

# Low-confidence: a bare `/name` in prose is indistinguishable from a Claude Code
# built-in (/help, /model), another plugin's command, or a filesystem path. These
# are reported as "unresolved" rather than broken and never affect the exit code.
_BACKTICK_SLASH_PATTERN = re.compile(r"`/" + _NAME + r"`")

# URLs routinely contain a "skills/<name>/" segment (a GitHub tree/blob link to
# someone else's repo). Those are not references to a local component, so matches
# landing inside a URL are ignored.
_URL_PATTERN = re.compile(r"\w+://\S+")

_SCRIPT_REF_SAME_SKILL = re.compile(
    r"\$\{CLAUDE_SKILL_DIR\}/((?:\.\./)*(?:[\w-]+/)*scripts/[\w./-]+\.py)"
)
_SCRIPT_REF_CROSS_SKILL = re.compile(
    r"\$\{CLAUDE_PLUGIN_ROOT\}/skills/([\w-]+/scripts/[\w./-]+\.py)"
)

_PLACEHOLDER_NAMES = {
    "skill-name", "my-skill", "my-skill-name", "name", "foo", "bar",
    "example", "your-skill", "target-skill",
}

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)


def _parse_frontmatter(content: str) -> dict:
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    try:
        import yaml
        parsed = yaml.safe_load(m.group(1))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


@dataclass
class Reference:
    source_type: str
    source_name: str
    target_type: str
    target_name: str
    exists: bool
    # "strong" — an unresolved target is a broken reference.
    # "weak"   — an unresolved target is merely unresolved (see _BACKTICK_SLASH_PATTERN).
    confidence: str = "strong"

    def to_dict(self) -> dict:
        return {
            "source": f"{self.source_type}/{self.source_name}",
            "target": f"{self.target_type}/{self.target_name}",
            "exists": self.exists,
        }


@dataclass
class ReferenceReport:
    references: list[Reference] = field(default_factory=list)
    broken_refs: list[Reference] = field(default_factory=list)
    unresolved_refs: list[Reference] = field(default_factory=list)
    missing_scripts: list[Reference] = field(default_factory=list)
    orphan_skills: list[str] = field(default_factory=list)
    eval_configs: list[dict] = field(default_factory=list)


def _resolves(name: str, known_names: set[str]) -> bool:
    """A reference resolves by exact name, or by its local segment.

    Plugin-namespaced references (`rfe.create`, `plugin:skill`) may point at a
    skill whose directory is named either way round: the directory itself can be
    literally `rfe.create`, or it can be `create` inside a plugin named `rfe`.
    """
    if name in known_names:
        return True
    return any(
        sep in name and name.rsplit(sep, 1)[-1] in known_names
        for sep in (".", ":")
    )


def _read_text_safe(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError, UnicodeDecodeError):
        return ""


def find_skills(root: Path) -> list[dict]:
    skills = []
    search_dirs = [root / ".claude" / "skills", root / "skills"]
    plugin_json = root / ".claude-plugin" / "plugin.json"
    if plugin_json.exists():
        try:
            plugin = json.loads(_read_text_safe(plugin_json))
            for path in plugin.get("skills", []):
                resolved = (root / path).resolve()
                if resolved.is_relative_to(root):
                    search_dirs.append(resolved)
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass

    root_resolved = root.resolve()
    seen: set[Path] = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for skill_md in search_dir.rglob("SKILL.md"):
            if not skill_md.resolve().is_relative_to(root_resolved):
                continue
            skill_dir = skill_md.parent
            if skill_dir in seen:
                continue
            seen.add(skill_dir)
            content = _read_text_safe(skill_md)
            if not content:
                continue
            skills.append({
                "name": skill_dir.name,
                "path": str(skill_md.relative_to(root)),
                "content": content,
                "dir": skill_dir,
                "frontmatter": _parse_frontmatter(content),
            })
    return skills


def find_commands(root: Path) -> list[dict]:
    commands = []
    search_dirs = [root / ".claude" / "commands", root / "commands"]
    root_resolved = root.resolve()
    seen: set[str] = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md_file in search_dir.rglob("*.md"):
            if not md_file.resolve().is_relative_to(root_resolved):
                continue
            name = md_file.stem
            if name in seen:
                continue
            seen.add(name)
            content = _read_text_safe(md_file)
            if content:
                commands.append({
                    "name": name,
                    "path": str(md_file.relative_to(root)),
                    "content": content,
                })
    return commands


def _is_generated_task_bundle(yaml_file: Path) -> bool:
    """True for the verifier eval.yaml bundled inside a Harbor task package.

    /eval-dataset writes one package per case, each carrying a copy of the
    project's eval.yaml under ``<task>/[steps/<id>/]tests/``. They are generated
    artifacts, not configs to validate — counting them multiplies a single
    logical reference by the number of cases.
    """
    if yaml_file.parent.name != "tests":
        return False
    return any(
        (ancestor / "task.toml").exists()
        for ancestor in list(yaml_file.parents)[1:5]
    )


def find_eval_configs(root: Path) -> list[dict]:
    configs = []
    seen: set[Path] = set()
    root_resolved = root.resolve()
    for yaml_file in root.rglob("eval.yaml"):
        if ".git" in yaml_file.parts or "__pycache__" in yaml_file.parts:
            continue
        if _is_generated_task_bundle(yaml_file):
            continue
        resolved = yaml_file.resolve()
        if not resolved.is_relative_to(root_resolved):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        content = _read_text_safe(yaml_file)
        if not content:
            continue
        try:
            import yaml
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                continue
            execution = parsed.get("execution") or {}
            runner = parsed.get("runner") or {}
            runner_type = runner.get("type", "claude-code")
            skill = execution.get("skill") or parsed.get("skill")
            if isinstance(skill, str) and skill:
                configs.append({
                    "path": str(yaml_file.relative_to(root)),
                    "name": parsed.get("name", yaml_file.parent.name),
                    "skill": skill,
                    "runner_type": runner_type if isinstance(runner_type, str) else "claude-code",
                })
        except Exception:
            continue
    return configs


def check_skill_references(
    skills: list[dict],
    commands: list[dict],
    known_names: set[str],
) -> list[Reference]:
    refs: list[Reference] = []
    seen: set[tuple[str, str, str, str]] = set()

    all_components = [
        (s, "skill") for s in skills
    ] + [
        (c, "command") for c in commands
    ]

    for comp, comp_type in all_components:
        content = comp["content"]
        url_spans = [m.span() for m in _URL_PATTERN.finditer(content)]

        def _in_url(pos: int) -> bool:
            return any(start <= pos < end for start, end in url_spans)

        strong_names: set[str] = set()
        weak_names: set[str] = set()

        for pattern in _SKILL_REF_PATTERNS:
            for match in pattern.finditer(content):
                if _in_url(match.start()):
                    continue
                strong_names.add(match.group(1))

        for match in _BACKTICK_SLASH_PATTERN.finditer(content):
            if _in_url(match.start()):
                continue
            weak_names.add(match.group(1))
        weak_names -= strong_names

        found_names = {n: "strong" for n in strong_names}
        found_names.update({n: "weak" for n in weak_names})

        for ref_name, confidence in found_names.items():
            if ref_name == comp["name"] or ref_name in _PLACEHOLDER_NAMES:
                continue
            key = (comp_type, comp["name"], "skill", ref_name)
            if key in seen:
                continue
            seen.add(key)
            refs.append(Reference(
                source_type=comp_type,
                source_name=comp["name"],
                target_type="skill",
                target_name=ref_name,
                exists=_resolves(ref_name, known_names),
                confidence=confidence,
            ))
    return refs


def check_script_references(
    skills: list[dict],
    root: Path,
) -> list[Reference]:
    refs: list[Reference] = []
    seen: set[tuple[str, str]] = set()
    root_resolved = root.resolve()
    skills_bases = [root / "skills", root / ".claude" / "skills"]
    for skill in skills:
        skill_dir = skill["dir"]
        content = skill["content"]

        matches: list[tuple[str, Path]] = []
        for match in _SCRIPT_REF_SAME_SKILL.finditer(content):
            rel = match.group(1)
            matches.append((rel, (skill_dir / rel).resolve()))
        for match in _SCRIPT_REF_CROSS_SKILL.finditer(content):
            rel = match.group(1)
            candidates = [(base / rel).resolve() for base in skills_bases]
            # Prefer a base where the script actually exists: `root/skills/<rel>`
            # is always inside the root whether or not it is there, so selecting
            # on containment alone would make the `.claude/skills` base dead code.
            # Always append something so an escaping path still gets reported.
            resolved = next(
                (c for c in candidates if c.exists()),
                next((c for c in candidates if c.is_relative_to(root_resolved)),
                     candidates[0]),
            )
            matches.append((f"skills/{rel}", resolved))

        for display_path, resolved in matches:
            base_name = resolved.name
            if base_name.split(".")[0] in _PLACEHOLDER_NAMES:
                continue
            key = (skill["name"], display_path)
            if key in seen:
                continue
            seen.add(key)
            if not resolved.is_relative_to(root_resolved):
                # A path pointing outside the project (shared plugin dir, monorepo
                # sibling) is unverifiable from here, not proven missing — surface
                # it, but never fail the run on it.
                refs.append(Reference(
                    source_type="skill",
                    source_name=skill["name"],
                    target_type="script",
                    target_name=f"{display_path} (outside project root)",
                    exists=False,
                    confidence="weak",
                ))
                continue
            refs.append(Reference(
                source_type="skill",
                source_name=skill["name"],
                target_type="script",
                target_name=display_path,
                exists=resolved.exists(),
            ))
    return refs


def check_eval_config_references(
    configs: list[dict],
    known_skill_names: set[str],
) -> list[Reference]:
    refs: list[Reference] = []
    seen: set[tuple[str, str]] = set()
    for cfg in configs:
        skill_name = cfg["skill"]
        if skill_name in _PLACEHOLDER_NAMES:
            continue
        if cfg["runner_type"] != "claude-code":
            # For opaque runners (cli, opencode, ...) execution.skill is just a
            # label substituted into the command template, not a skill to resolve.
            continue
        # Dedup on the reference itself, not the file, so sibling configs that
        # target the same skill collapse into one finding.
        key = (cfg["name"], skill_name)
        if key in seen:
            continue
        seen.add(key)
        refs.append(Reference(
            source_type="eval_config",
            source_name=cfg["name"],
            target_type="skill",
            target_name=skill_name,
            exists=_resolves(skill_name, known_skill_names),
        ))
    return refs


def find_orphan_skills(
    skills: list[dict],
    all_refs: list[Reference],
    configs: list[dict],
) -> list[str]:
    if len(skills) <= 3:
        return []

    referenced_names: set[str] = set()
    for ref in all_refs:
        referenced_names.add(ref.target_name)
    for cfg in configs:
        if cfg["runner_type"] == "claude-code":
            # Credit both spellings — the skill dir may be `rfe.create` itself
            # or `create` inside plugin `rfe`.
            referenced_names.add(cfg["skill"])
            for sep in (".", ":"):
                if sep in cfg["skill"]:
                    referenced_names.add(cfg["skill"].rsplit(sep, 1)[-1])

    orphans = []
    for skill in skills:
        if skill["name"] in referenced_names:
            continue
        fm = skill.get("frontmatter") or {}
        if str(fm.get("user-invocable", "")).lower() == "true":
            continue
        orphans.append(skill["name"])
    return orphans


def analyze(root: Path) -> ReferenceReport:
    skills = find_skills(root)
    commands = find_commands(root)
    configs = find_eval_configs(root)

    known_skill_names = {s["name"] for s in skills}
    known_command_names = {c["name"] for c in commands}
    known_names = known_skill_names | known_command_names

    skill_refs = check_skill_references(skills, commands, known_names)
    script_refs = check_script_references(skills, root)
    config_refs = check_eval_config_references(configs, known_skill_names)

    all_refs = skill_refs + config_refs
    unresolvable = all_refs + script_refs
    broken = [r for r in all_refs if not r.exists and r.confidence == "strong"]
    unresolved = [r for r in unresolvable if not r.exists and r.confidence == "weak"]
    missing_scripts = [
        r for r in script_refs if not r.exists and r.confidence == "strong"]
    orphans = find_orphan_skills(skills, all_refs, configs)

    return ReferenceReport(
        references=all_refs + script_refs,
        broken_refs=broken,
        unresolved_refs=unresolved,
        missing_scripts=missing_scripts,
        orphan_skills=orphans,
        eval_configs=[{"name": c["name"], "path": c["path"], "skill": c["skill"]} for c in configs],
    )


def format_text(report: ReferenceReport) -> str:
    lines = [
        "=== Reference Check ===",
        "",
        f"References found: {len(report.references)}",
        f"Broken references: {len(report.broken_refs)}",
        f"Unresolved references: {len(report.unresolved_refs)}",
        f"Missing scripts: {len(report.missing_scripts)}",
        f"Orphan skills: {len(report.orphan_skills)}",
        f"Eval configs: {len(report.eval_configs)}",
    ]

    if report.broken_refs:
        lines.append("")
        lines.append("Broken references:")
        for ref in report.broken_refs:
            lines.append(f"  {ref.source_type}/{ref.source_name} -> {ref.target_type}/{ref.target_name} (NOT FOUND)")

    if report.unresolved_refs:
        lines.append("")
        lines.append("Unresolved references (informational — may be built-in "
                     "commands, other plugins, or paths outside this project):")
        for ref in report.unresolved_refs:
            target = (f"/{ref.target_name}" if ref.target_type == "skill"
                      else ref.target_name)
            lines.append(f"  {ref.source_type}/{ref.source_name} -> {target}")

    if report.missing_scripts:
        lines.append("")
        lines.append("Missing scripts:")
        for ref in report.missing_scripts:
            lines.append(f"  {ref.source_name}/{ref.target_name} (NOT FOUND)")

    if report.orphan_skills:
        lines.append("")
        lines.append("Orphan skills (not referenced by any component):")
        for name in report.orphan_skills:
            lines.append(f"  {name}")

    if report.eval_configs:
        lines.append("")
        lines.append("Eval configs:")
        for cfg in report.eval_configs:
            lines.append(f"  {cfg['path']} (skill={cfg['skill']})")

    if not report.broken_refs and not report.missing_scripts:
        lines.append("")
        if report.unresolved_refs or report.orphan_skills:
            lines.append("No broken references or missing scripts. "
                         "See the informational findings above.")
        else:
            lines.append("All references resolve. No issues found.")

    return "\n".join(lines)


def format_yaml(report: ReferenceReport) -> str:
    output = {
        "reference_check": True,
        "total_references": len(report.references),
        "broken_references": [r.to_dict() for r in report.broken_refs],
        "unresolved_references": [r.to_dict() for r in report.unresolved_refs],
        "missing_scripts": [r.to_dict() for r in report.missing_scripts],
        "orphan_skills": report.orphan_skills,
        "eval_configs": report.eval_configs,
        "all_references": [r.to_dict() for r in report.references],
    }
    try:
        import yaml
        return yaml.dump(output, default_flow_style=False, sort_keys=False)
    except ImportError:
        return json.dumps(output, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--format", choices=["text", "yaml"], default="text")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    report = analyze(root)

    if args.format == "yaml":
        print(format_yaml(report))
    else:
        print(format_text(report))

    return 1 if report.broken_refs or report.missing_scripts else 0


if __name__ == "__main__":
    sys.exit(main())
