#!/usr/bin/env python3
"""Security scanner for Claude Code agent setups.

Scans skills, commands, hooks, and CLAUDE.md for security issues using
deterministic regex and AST-based rules. No LLM required.

Ported from harness-eval-lab (https://github.com/Benkapner/harness-eval-lab).
"""

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_SIZE = 1_000_000

# ---------------------------------------------------------------------------
# Prompt injection patterns (17)
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore previous instructions", re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I)),
    ("disregard prior", re.compile(r"disregard\s+(all\s+)?(prior|previous|above)", re.I)),
    ("you are now", re.compile(r"you\s+are\s+now\s+(?:a|an|the)\s+", re.I)),
    ("system prompt override", re.compile(r"system\s*prompt\s*(override|injection|change)", re.I)),
    ("override instructions", re.compile(r"override\s+(all\s+)?(instructions|rules|guidelines)", re.I)),
    ("new instructions", re.compile(r"new\s+instructions?\s*:", re.I)),
    ("jailbreak attempt", re.compile(r"(\bDAN\b|do\s+anything\s+now|developer\s+mode)", re.I)),
    ("prompt leak", re.compile(r"(reveal|show|print|output)\s+(your|the)\s+(system\s+)?prompt", re.I)),
    ("role hijack", re.compile(r"forget\s+(everything|all|your)\s+(you|instructions|rules)", re.I)),
    ("hidden instruction", re.compile(r"<\s*(?:system|instruction|hidden)\s*>", re.I)),
    ("role play", re.compile(r"pretend\s+(?:to\s+be|you\s+are)\s+(?:a|an|the)\s+", re.I)),
    ("encoding evasion", re.compile(r"(?:in\s+base64|encode\s+(?:as|in|to)\s+base64|base64\s+encod)", re.I)),
    ("repeat after me", re.compile(r"repeat\s+after\s+me", re.I)),
    ("bypass safety", re.compile(r"(?:ignore\s+safety|bypass\s+(?:filter|safety|restriction))", re.I)),
    ("output control", re.compile(r"output\s+the\s+following\s+exactly", re.I)),
    ("markdown image exfiltration", re.compile(r"!\[.*?\]\(https?://", re.I)),
    ("translate evasion", re.compile(r"translate\s+(?:this|the\s+following)\s+(?:to|into)\s+", re.I)),
]

# ---------------------------------------------------------------------------
# Credential access patterns
# ---------------------------------------------------------------------------
_SENSITIVE_PATHS: list[re.Pattern[str]] = [
    re.compile(r"~/\.ssh/", re.I),
    re.compile(r"~/\.aws/credentials", re.I),
    re.compile(r"~/\.config/gcloud", re.I),
    re.compile(r"~/\.kube/config", re.I),
    re.compile(r"/etc/shadow", re.I),
    re.compile(r"~/\.netrc", re.I),
    re.compile(r"~/\.env\b"),
    re.compile(r"~/\.docker/config\.json", re.I),
    re.compile(r"~/\.npmrc\b"),
    re.compile(r"~/\.pypirc\b"),
]

_SENSITIVE_ENV_VARS: list[re.Pattern[str]] = [
    re.compile(r"\$(?:ANTHROPIC|OPENAI|GEMINI|GOOGLE)_API_KEY"),
    re.compile(r"\$(?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)"),
    re.compile(r"\$(?:DATABASE_URL|DB_PASSWORD)"),
    re.compile(r"\$(?:GITHUB_TOKEN|GH_TOKEN)"),
    re.compile(r"\$(?:SECRET_KEY|PRIVATE_KEY)"),
    re.compile(r"\$SLACK_TOKEN"),
    re.compile(r"\$STRIPE_SECRET_KEY"),
    re.compile(r"\$JWT_SECRET"),
    re.compile(r"\$ENCRYPTION_KEY"),
]

_DANGEROUS_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsudo\s+"), "sudo"),
    (re.compile(r"\bchmod\s+777\b"), "chmod 777"),
    (re.compile(r"\bchown\s+root\b"), "chown root"),
]

# ---------------------------------------------------------------------------
# Data exfiltration patterns (8)
# ---------------------------------------------------------------------------
_EXFIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("curl post file contents", re.compile(r"curl\s+.*-d\s+\"\$\(cat\b", re.I)),
    ("curl with command substitution", re.compile(r"curl\s+.*--data.*\$\(", re.I)),
    ("wget post data", re.compile(r"wget\s+--post-data", re.I)),
    ("dns tunneling dig", re.compile(r"\bdig\s+.*\bTXT\b", re.I)),
    ("dns tunneling nslookup", re.compile(r"\bnslookup\s+.*-type=TXT", re.I)),
    ("webhook exfiltration", re.compile(r"(?:curl|wget|fetch)\s+.*(?:webhook|hooks\.|pipedream|requestbin|ngrok)", re.I)),
    ("base64 pipe to network", re.compile(r"base64\s+.*\|\s*(?:curl|wget|nc)\b", re.I)),
    ("archive pipe to network", re.compile(r"tar\s+.*\|\s*(?:curl|wget|nc|ssh)\b", re.I)),
]

# ---------------------------------------------------------------------------
# Reverse shell patterns (10)
# ---------------------------------------------------------------------------
_REVERSE_SHELL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bash reverse shell", re.compile(r"bash\s+-i\s+>&\s*/dev/tcp/", re.I)),
    ("netcat exec", re.compile(r"\bnc\s+.*-e\s+/bin/", re.I)),
    ("ncat exec", re.compile(r"\bncat\s+.*--exec", re.I)),
    ("python socket shell", re.compile(r"python[23]?\s+-c\s+.*(?:socket|subprocess)", re.I)),
    ("perl socket shell", re.compile(r"perl\s+-e\s+.*(?:socket|Socket)", re.I)),
    ("ruby socket shell", re.compile(r"ruby\s+-rsocket\s+-e", re.I)),
    ("php socket shell", re.compile(r"php\s+-r\s+.*fsockopen", re.I)),
    ("socat exec", re.compile(r"\bsocat\s+.*exec:", re.I)),
    ("named pipe shell", re.compile(r"\bmknod\s+.*\bp\b.*(?:/bin/sh|bash)", re.I)),
    ("powershell reverse shell", re.compile(r"\bpowershell\s+.*(?:Net\.Sockets|TCPClient)", re.I)),
]

# ---------------------------------------------------------------------------
# Obfuscation patterns (8)
# ---------------------------------------------------------------------------
_OBFUSCATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("eval with decode", re.compile(r"eval\s*\(\s*(?:atob|Buffer\.from|base64\.b64decode)\s*\(", re.I)),
    ("char code construction", re.compile(r"String\.fromCharCode\s*\(", re.I)),
    ("hex escape sequence", re.compile(r"(?:\\x[0-9a-fA-F]{2}){4,}")),
    ("unicode escape sequence", re.compile(r"(?:\\u[0-9a-fA-F]{4}){4,}")),
    ("zero-width characters", re.compile(r"[​-‏﻿]")),
    ("tag characters", re.compile(r"[\U000e0000-\U000e007f]")),
    ("python dynamic exec", re.compile(r"exec\s*\(\s*(?:compile|__import__)\s*\(", re.I)),
    ("char code round-trip", re.compile(r"charCodeAt\b.*\bfromCharCode\b", re.I)),
]

# ---------------------------------------------------------------------------
# Hidden instruction / tool poisoning patterns
# ---------------------------------------------------------------------------
_HIDDEN_INSTRUCTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("HTML comment with instruction", re.compile(r"<!--\s*(?:system|instruction|ignore|override|you\s+are)", re.I)),
    ("markdown comment", re.compile(r"\[//\]:\s*#\s*\(.*(?:ignore|override|instruction)", re.I)),
    ("base64 blob in text", re.compile(r"(?:data:text/[^;]+;base64,|[A-Za-z0-9+/]{40,}={0,2})")),
    ("data URI with script", re.compile(r"data:\s*(?:text/javascript|application/javascript|text/html)", re.I)),
]

_ZERO_WIDTH_CHARS: dict[str, str] = {
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "⁠": "word joiner",
    "﻿": "BOM / zero-width no-break space",
    "­": "soft hyphen",
}

_RTL_OVERRIDE_CHARS: dict[str, str] = {
    "‪": "LRE",
    "‫": "RLE",
    "‬": "PDF",
    "‭": "LRO",
    "‮": "RLO",
    "⁦": "LRI",
    "⁧": "RLI",
    "⁨": "FSI",
    "⁩": "PDI",
}

_HOMOGLYPH_MAP: dict[str, str] = {
    "А": "A (Cyrillic)", "В": "B (Cyrillic)", "С": "C (Cyrillic)",
    "Е": "E (Cyrillic)", "Н": "H (Cyrillic)", "К": "K (Cyrillic)",
    "М": "M (Cyrillic)", "О": "O (Cyrillic)", "Р": "P (Cyrillic)",
    "Т": "T (Cyrillic)", "Х": "X (Cyrillic)",
    "а": "a (Cyrillic)", "е": "e (Cyrillic)", "о": "o (Cyrillic)",
    "р": "p (Cyrillic)", "с": "c (Cyrillic)", "у": "y (Cyrillic)",
    "х": "x (Cyrillic)",
    "Α": "A (Greek)", "Β": "B (Greek)", "Ε": "E (Greek)",
    "Η": "H (Greek)", "Κ": "K (Greek)", "Μ": "M (Greek)",
    "Ο": "O (Greek)", "Ρ": "P (Greek)", "Τ": "T (Greek)",
    "Χ": "X (Greek)", "ο": "o (Greek)",
}

# ---------------------------------------------------------------------------
# AST behavioral analysis constants
# ---------------------------------------------------------------------------
_DANGEROUS_BUILTINS = {"exec", "eval", "compile", "__import__"}

_SUBPROCESS_CALLS = {
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_output", "subprocess.check_call",
    "subprocess.getoutput", "subprocess.getstatusoutput",
}

_OS_EXEC_CALLS = {
    "os.system", "os.popen", "os.exec", "os.execl", "os.execle",
    "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp",
    "os.execvpe", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
}

# ---------------------------------------------------------------------------
# Taint tracking constants
# ---------------------------------------------------------------------------
_CREDENTIAL_SOURCES = {"os.environ.get", "os.environ", "os.getenv", "dotenv.dotenv_values"}
_FILE_READ_SOURCES = {"open", "pathlib.Path.read_text", "pathlib.Path.read_bytes"}
_NETWORK_INPUT_SOURCES = {"requests.get", "requests.post", "httpx.get", "httpx.post", "urllib.request.urlopen", "input"}

_NETWORK_SINKS = {
    "requests.post", "requests.put", "requests.patch",
    "httpx.post", "httpx.put", "httpx.patch",
    "urllib.request.urlopen", "urllib.request.Request",
    "smtplib.SMTP.sendmail", "socket.socket.send", "socket.socket.sendall",
}

_EXEC_SINKS = {
    "exec", "eval", "compile",
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_output", "os.system", "os.popen",
}

_ALL_TAINT_SOURCES = _CREDENTIAL_SOURCES | _FILE_READ_SOURCES | _NETWORK_INPUT_SOURCES
_ALL_TAINT_SINKS = _NETWORK_SINKS | _EXEC_SINKS

# ---------------------------------------------------------------------------
# MCP least privilege constants
# ---------------------------------------------------------------------------
_CAPABILITY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "shell": [
        re.compile(r"\bsubprocess\b", re.I), re.compile(r"\bos\.system\b", re.I),
        re.compile(r"\bos\.popen\b", re.I), re.compile(r"\bos\.exec", re.I),
        re.compile(r"\bshutil\b", re.I),
    ],
    "network": [
        re.compile(r"\brequests\.", re.I), re.compile(r"\bhttpx\.", re.I),
        re.compile(r"\burllib\.", re.I), re.compile(r"\bsocket\.", re.I),
        re.compile(r"\baiohttp\.", re.I),
    ],
    "file_write": [
        re.compile(r"\.write\(", re.I), re.compile(r"open\(.+['\"]w", re.I),
        re.compile(r"\.write_text\(", re.I), re.compile(r"\.write_bytes\(", re.I),
    ],
    "file_read": [
        re.compile(r"open\(.+['\"]r", re.I), re.compile(r"\.read_text\(", re.I),
        re.compile(r"\.read_bytes\(", re.I), re.compile(r"\.read\(", re.I),
    ],
    "env": [
        re.compile(r"\bos\.environ\b", re.I), re.compile(r"\bos\.getenv\b", re.I),
        re.compile(r"\bdotenv\b", re.I),
    ],
}

_TOOL_TO_CAPABILITY: dict[str, set[str]] = {
    "bash": {"shell", "network", "file_write", "file_read", "env"},
    "read": {"file_read"},
    "write": {"file_write"},
    "edit": {"file_write"},
    "webfetch": {"network"},
    "websearch": {"network"},
}


# ===========================================================================
# Data structures
# ===========================================================================

@dataclass
class Finding:
    rule: str
    severity: str  # "error", "warning", "info"
    file: str
    line: int | None
    label: str
    matched: str

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "label": self.label,
            "matched": self.matched[:80],
        }


@dataclass
class ComponentResult:
    name: str
    component_type: str  # "skill", "command", "hook", "claude_md"
    verdict: str = "SAFE"
    findings: list[Finding] = field(default_factory=list)

    def compute_verdict(self) -> None:
        if any(f.severity == "error" for f in self.findings):
            self.verdict = "UNSAFE"
        elif any(f.severity == "warning" for f in self.findings):
            self.verdict = "CAUTION"
        else:
            self.verdict = "SAFE"


# ===========================================================================
# Discovery (adapted from harness_inventory.py in eval-check)
# ===========================================================================

def _read_text_safe(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError, UnicodeDecodeError):
        return ""


def _parse_frontmatter(content: str) -> dict:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        import yaml
        parsed = yaml.safe_load(fm_text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


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

    seen: set[Path] = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for skill_md in search_dir.rglob("SKILL.md"):
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
                "frontmatter": _parse_frontmatter(content),
                "dir": str(skill_dir),
            })
    return skills


def find_commands(root: Path) -> list[dict]:
    commands = []
    search_dirs = [root / ".claude" / "commands", root / "commands"]
    seen: set[str] = set()
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for md_file in search_dir.rglob("*.md"):
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


def find_hooks(root: Path) -> list[dict]:
    hooks = []
    sources = [root / ".claude" / "settings.json", root / ".claude-plugin" / "plugin.json"]
    for source_path in sources:
        if not source_path.exists():
            continue
        content = _read_text_safe(source_path)
        if not content:
            continue
        try:
            data = json.loads(content)
            hooks_map = data.get("hooks", {}) if isinstance(data, dict) else {}
            for hook_type, matchers in hooks_map.items():
                if not isinstance(matchers, list):
                    continue
                for matcher in matchers:
                    if not isinstance(matcher, dict):
                        continue
                    inner_hooks = matcher.get("hooks", [])
                    if isinstance(inner_hooks, list):
                        for hook in inner_hooks:
                            if not isinstance(hook, dict):
                                continue
                            cmd = hook.get("command", "")
                            hooks.append({
                                "type": hook_type,
                                "matcher": matcher.get("matcher", ""),
                                "command": cmd,
                            })
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass
    return hooks


def find_claude_md(root: Path) -> dict | None:
    for candidate in [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md"]:
        if candidate.exists():
            content = _read_text_safe(candidate)
            if content:
                return {
                    "path": str(candidate.relative_to(root)),
                    "content": content,
                }
    return None


# ===========================================================================
# Regex-based scanners
# ===========================================================================

def _scan_patterns(
    content: str,
    file_path: str,
    patterns: list[tuple[str, re.Pattern[str]]],
    rule: str,
    track_code_fence: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    lines = content.split("\n")
    in_code_fence = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if track_code_fence and stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue

        for label, pattern in patterns:
            m = pattern.search(line)
            if m:
                if in_code_fence:
                    severity = "warning"
                else:
                    is_quoted = stripped.startswith(">") or stripped.startswith('"')
                    is_example = any(w in line.lower() for w in ["for example", "e.g.", "such as", "like:"])
                    severity = "warning" if (is_quoted or is_example) else "error"
                findings.append(Finding(
                    rule=rule,
                    severity=severity,
                    file=file_path,
                    line=i + 1,
                    label=label,
                    matched=m.group(0),
                ))
                break
    return findings


def scan_prompt_injection(content: str, file_path: str) -> list[Finding]:
    return _scan_patterns(content, file_path, _INJECTION_PATTERNS, "no-prompt-injection")


def scan_data_exfiltration(content: str, file_path: str) -> list[Finding]:
    return _scan_patterns(content, file_path, _EXFIL_PATTERNS, "data-exfiltration")


def scan_reverse_shells(content: str, file_path: str) -> list[Finding]:
    return _scan_patterns(content, file_path, _REVERSE_SHELL_PATTERNS, "reverse-shell")


def scan_obfuscation(content: str, file_path: str) -> list[Finding]:
    return _scan_patterns(content, file_path, _OBFUSCATION_PATTERNS, "obfuscation")


def scan_credential_access(content: str, file_path: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = content.split("\n")
    for i, line in enumerate(lines):
        for pattern in _SENSITIVE_PATHS:
            m = pattern.search(line)
            if m:
                findings.append(Finding(
                    rule="no-credential-access", severity="error", file=file_path,
                    line=i + 1, label="sensitive path", matched=m.group(0),
                ))
                break

        for pattern in _SENSITIVE_ENV_VARS:
            m = pattern.search(line)
            if m:
                findings.append(Finding(
                    rule="no-credential-access", severity="warning", file=file_path,
                    line=i + 1, label="sensitive environment variable", matched=m.group(0),
                ))
                break

        for pattern, label in _DANGEROUS_COMMANDS:
            m = pattern.search(line)
            if m:
                findings.append(Finding(
                    rule="no-credential-access", severity="warning", file=file_path,
                    line=i + 1, label=f"dangerous command: {label}", matched=m.group(0),
                ))
                break
    return findings


def scan_tool_poisoning(content: str, file_path: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = content.split("\n")

    for i, line in enumerate(lines):
        for label, pattern in _HIDDEN_INSTRUCTION_PATTERNS:
            m = pattern.search(line)
            if m:
                findings.append(Finding(
                    rule="tool-poisoning", severity="error", file=file_path,
                    line=i + 1, label=label, matched=m.group(0),
                ))
                break

        for char, char_name in _ZERO_WIDTH_CHARS.items():
            if char in line:
                findings.append(Finding(
                    rule="tool-poisoning", severity="error", file=file_path,
                    line=i + 1, label=f"zero-width char: {char_name}",
                    matched=f"U+{ord(char):04X}",
                ))

        for char, char_name in _RTL_OVERRIDE_CHARS.items():
            if char in line:
                findings.append(Finding(
                    rule="tool-poisoning", severity="error", file=file_path,
                    line=i + 1, label=f"RTL override: {char_name}",
                    matched=f"U+{ord(char):04X}",
                ))

        for char, char_name in _HOMOGLYPH_MAP.items():
            if char in line:
                findings.append(Finding(
                    rule="tool-poisoning", severity="warning", file=file_path,
                    line=i + 1, label=f"homoglyph: looks like {char_name}",
                    matched=f"U+{ord(char):04X}",
                ))
    return findings


# ===========================================================================
# AST-based scanners
# ===========================================================================

def _ast_get_call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return f"{node.func.value.id}.{node.func.attr}"
    return None


def _ast_resolve_dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _ast_resolve_dotted(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _is_dynamic_source(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        name = _ast_get_call_name(node)
        if name and any(kw in name.lower() for kw in ["decode", "b64decode", "urlopen", "read", "recv", "get"]):
            return True
    return isinstance(node, ast.Subscript)


def scan_ast_behavioral(skill_dir: Path, base_path: str) -> list[Finding]:
    findings: list[Finding] = []
    if not skill_dir.is_dir():
        return findings

    for py_file in sorted(skill_dir.rglob("*.py")):
        if ".git" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        source = _read_text_safe(py_file)
        if not source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        rel_name = py_file.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _ast_get_call_name(node)
            if call_name is None:
                continue
            line = getattr(node, "lineno", 0)

            if call_name in _DANGEROUS_BUILTINS:
                has_dynamic = any(_is_dynamic_source(arg) for arg in node.args)
                if has_dynamic:
                    findings.append(Finding(
                        rule="ast-behavioral", severity="error", file=base_path,
                        line=line, label=f"exec chain: {call_name} with dynamic source",
                        matched=f"{rel_name}:{line} {call_name}(...)",
                    ))
                else:
                    findings.append(Finding(
                        rule="ast-behavioral", severity="warning", file=base_path,
                        line=line, label=f"dangerous builtin: {call_name}",
                        matched=f"{rel_name}:{line} {call_name}(...)",
                    ))
            elif call_name in _SUBPROCESS_CALLS or call_name in _OS_EXEC_CALLS:
                findings.append(Finding(
                    rule="ast-behavioral", severity="warning", file=base_path,
                    line=line, label=f"subprocess/os call: {call_name}",
                    matched=f"{rel_name}:{line} {call_name}(...)",
                ))
            elif call_name == "getattr" and len(node.args) >= 2:
                if not isinstance(node.args[1], ast.Constant):
                    findings.append(Finding(
                        rule="ast-behavioral", severity="warning", file=base_path,
                        line=line, label="dynamic getattr (non-literal)",
                        matched=f"{rel_name}:{line} getattr(...)",
                    ))
    return findings


def _classify_taint_source(name: str) -> str | None:
    if name in _CREDENTIAL_SOURCES:
        return "credential"
    if name in _FILE_READ_SOURCES:
        return "file_read"
    if name in _NETWORK_INPUT_SOURCES:
        return "network_input"
    return None


def _classify_taint_sink(name: str) -> str | None:
    if name in _NETWORK_SINKS:
        return "network_output"
    if name in _EXEC_SINKS:
        return "code_execution"
    return None


def scan_taint_tracking(skill_dir: Path, base_path: str) -> list[Finding]:
    findings: list[Finding] = []
    if not skill_dir.is_dir():
        return findings

    for py_file in sorted(skill_dir.rglob("*.py")):
        if ".git" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        source = _read_text_safe(py_file)
        if not source:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        rel_name = py_file.name
        tainted: dict[str, tuple[str, int]] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                    call_name = _ast_get_call_name(node.value)
                    if call_name:
                        source_type = _classify_taint_source(call_name)
                        if source_type:
                            tainted[target.id] = (source_type, getattr(node, "lineno", 0))

                if (
                    isinstance(target, ast.Name)
                    and isinstance(node.value, ast.Subscript)
                    and isinstance(node.value.value, ast.Attribute)
                ):
                    attr_name = ""
                    if isinstance(node.value.value.value, ast.Name):
                        attr_name = f"{node.value.value.value.id}.{node.value.value.attr}"
                    if attr_name in _CREDENTIAL_SOURCES:
                        tainted[target.id] = ("credential", getattr(node, "lineno", 0))

                if isinstance(target, ast.Name) and isinstance(node.value, ast.JoinedStr):
                    for val in node.value.values:
                        if (
                            isinstance(val, ast.FormattedValue)
                            and isinstance(val.value, ast.Name)
                            and val.value.id in tainted
                        ):
                            tainted[target.id] = tainted[val.value.id]
                            break

            if isinstance(node, ast.Call):
                call_name_full = _ast_resolve_dotted(node.func) if not _ast_get_call_name(node) else _ast_get_call_name(node)
                if not call_name_full:
                    continue
                sink_type = _classify_taint_sink(call_name_full)
                if not sink_type:
                    continue
                sink_line = getattr(node, "lineno", 0)

                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in tainted:
                        source_type, source_line = tainted[arg.id]
                        if source_type == "credential" and sink_type == "network_output":
                            label = "credential leak"
                        elif sink_type == "code_execution":
                            label = "input to code execution"
                        else:
                            label = f"{source_type} to {sink_type}"
                        findings.append(Finding(
                            rule="taint-flow", severity="error", file=base_path,
                            line=sink_line, label=label,
                            matched=f"{rel_name}: {source_type} (line {source_line}) -> {sink_type} (line {sink_line})",
                        ))
                        break

                for kw in node.keywords:
                    if isinstance(kw.value, ast.Name) and kw.value.id in tainted:
                        source_type, source_line = tainted[kw.value.id]
                        findings.append(Finding(
                            rule="taint-flow", severity="error", file=base_path,
                            line=sink_line, label=f"{source_type} to {sink_type}",
                            matched=f"{rel_name}: {source_type} (line {source_line}) -> {sink_type} (line {sink_line})",
                        ))
                        break
    return findings


# ===========================================================================
# MCP least privilege scanner
# ===========================================================================

def _detect_capabilities(skill_dir: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for py_file in sorted(skill_dir.rglob("*.py")):
        if ".git" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        content = _read_text_safe(py_file)
        if not content:
            continue
        for cap, patterns in _CAPABILITY_PATTERNS.items():
            for pat in patterns:
                if pat.search(content):
                    found.setdefault(cap, []).append(py_file.name)
                    break

    for sh_file in sorted(skill_dir.rglob("*.sh")):
        if ".git" in sh_file.parts:
            continue
        found.setdefault("shell", []).append(sh_file.name)
    return found


def _get_allowed_capabilities(frontmatter: dict) -> set[str]:
    tools_raw = frontmatter.get("allowed-tools", "")
    if isinstance(tools_raw, list):
        tools = tools_raw
    elif isinstance(tools_raw, str):
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    else:
        return set()
    caps: set[str] = set()
    for tool in tools:
        tool_lower = str(tool).lower().strip()
        if tool_lower == "*":
            return {"shell", "network", "file_write", "file_read", "env", "__wildcard__"}
        mapped = _TOOL_TO_CAPABILITY.get(tool_lower, set())
        caps.update(mapped)
    return caps


def scan_mcp_least_privilege(skill_dir: Path, frontmatter: dict, base_path: str) -> list[Finding]:
    findings: list[Finding] = []
    if not skill_dir.is_dir() or not frontmatter:
        return findings

    allowed = _get_allowed_capabilities(frontmatter)
    if "__wildcard__" in allowed:
        findings.append(Finding(
            rule="mcp-least-privilege", severity="warning", file=base_path,
            line=None, label="wildcard allowed-tools",
            matched="allowed-tools contains * (unrestricted access)",
        ))
        return findings

    if not allowed and "allowed-tools" not in frontmatter:
        return findings

    actual = _detect_capabilities(skill_dir)

    for cap, files in actual.items():
        if cap not in allowed:
            unique_files = sorted(set(files))[:3]
            findings.append(Finding(
                rule="mcp-least-privilege", severity="warning", file=base_path,
                line=None, label=f"underdeclared capability: {cap}",
                matched=f"Used in: {', '.join(unique_files)}",
            ))

    if actual:
        for cap in allowed:
            if cap not in actual:
                findings.append(Finding(
                    rule="mcp-least-privilege", severity="info", file=base_path,
                    line=None, label=f"overdeclared capability: {cap}",
                    matched=f"allowed-tools grants {cap} but no code uses it",
                ))
    return findings


# ===========================================================================
# Component orchestrator
# ===========================================================================

def scan_component(
    name: str,
    component_type: str,
    content: str,
    file_path: str,
    skill_dir: Path | None = None,
    frontmatter: dict | None = None,
) -> ComponentResult:
    result = ComponentResult(name=name, component_type=component_type)

    result.findings.extend(scan_prompt_injection(content, file_path))
    result.findings.extend(scan_credential_access(content, file_path))
    result.findings.extend(scan_data_exfiltration(content, file_path))
    result.findings.extend(scan_reverse_shells(content, file_path))
    result.findings.extend(scan_obfuscation(content, file_path))
    result.findings.extend(scan_tool_poisoning(content, file_path))

    if skill_dir and skill_dir.is_dir():
        result.findings.extend(scan_ast_behavioral(skill_dir, file_path))
        result.findings.extend(scan_taint_tracking(skill_dir, file_path))
        if frontmatter:
            result.findings.extend(scan_mcp_least_privilege(skill_dir, frontmatter, file_path))

    result.compute_verdict()
    return result


# ===========================================================================
# Output formatting
# ===========================================================================

def format_text(results: list[ComponentResult], total_rules: int) -> str:
    total_errors = sum(1 for r in results for f in r.findings if f.severity == "error")
    total_warnings = sum(1 for r in results for f in r.findings if f.severity == "warning")
    total_info = sum(1 for r in results for f in r.findings if f.severity == "info")

    worst = "SAFE"
    if any(r.verdict == "UNSAFE" for r in results):
        worst = "UNSAFE"
    elif any(r.verdict == "CAUTION" for r in results):
        worst = "CAUTION"

    lines = [
        "=== Security Scan ===",
        "",
        f"Components scanned: {len(results)}",
        f"Rules: {total_rules}",
        f"Errors: {total_errors}",
        f"Warnings: {total_warnings}",
        f"Info: {total_info}",
        f"Risk assessment: {worst}",
    ]

    for r in results:
        lines.append("")
        lines.append(f"--- {r.component_type}/{r.name} [{r.verdict}] ---")
        if not r.findings:
            lines.append("  No findings.")
        else:
            for f in r.findings:
                line_str = f"line {f.line}" if f.line else ""
                lines.append(f"  [{f.severity.upper()}] {f.rule}: {line_str} {f.label} ({f.matched[:60]})")

    return "\n".join(lines)


def format_yaml(results: list[ComponentResult], total_rules: int) -> str:
    total_errors = sum(1 for r in results for f in r.findings if f.severity == "error")
    total_warnings = sum(1 for r in results for f in r.findings if f.severity == "warning")

    worst = "SAFE"
    if any(r.verdict == "UNSAFE" for r in results):
        worst = "UNSAFE"
    elif any(r.verdict == "CAUTION" for r in results):
        worst = "CAUTION"

    output = {
        "security_scan": True,
        "risk_assessment": worst,
        "components_scanned": len(results),
        "rules_checked": total_rules,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "components": [],
    }

    for r in results:
        comp: dict = {
            "name": r.name,
            "type": r.component_type,
            "verdict": r.verdict,
            "findings": [f.to_dict() for f in r.findings],
        }
        output["components"].append(comp)  # type: ignore[union-attr]

    try:
        import yaml
        return yaml.dump(output, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except ImportError:
        return json.dumps(output, indent=2, ensure_ascii=False)


# ===========================================================================
# Main
# ===========================================================================

TOTAL_RULES = 9


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--format", choices=["text", "yaml"], default="text", help="Output format")
    parser.add_argument("--output", default=None, help="Write output to file instead of stdout")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit code 1 if any errors found")
    args = parser.parse_args()

    root = Path(args.root).resolve()

    if args.output:
        out_path = (root / args.output).resolve()
        if not out_path.is_relative_to(root):
            print(f"ERROR: --output path '{args.output}' resolves outside project root", file=sys.stderr)
            return 1
    results: list[ComponentResult] = []

    skills = find_skills(root)
    commands = find_commands(root)
    hooks = find_hooks(root)
    claude_md = find_claude_md(root)

    for skill in skills:
        result = scan_component(
            name=skill["name"],
            component_type="skill",
            content=skill["content"],
            file_path=skill["path"],
            skill_dir=Path(skill["dir"]),
            frontmatter=skill["frontmatter"],
        )
        results.append(result)

    for cmd in commands:
        result = scan_component(
            name=cmd["name"],
            component_type="command",
            content=cmd["content"],
            file_path=cmd["path"],
        )
        results.append(result)

    if hooks:
        hook_content = "\n".join(h["command"] for h in hooks)
        result = scan_component(
            name="hooks",
            component_type="hook",
            content=hook_content,
            file_path=".claude/settings.json",
        )
        results.append(result)

    if claude_md:
        result = scan_component(
            name="CLAUDE.md",
            component_type="claude_md",
            content=claude_md["content"],
            file_path=claude_md["path"],
        )
        results.append(result)

    if args.format == "yaml":
        output = format_yaml(results, TOTAL_RULES)
    else:
        output = format_text(results, TOTAL_RULES)

    if args.output:
        out_path = (root / args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"Report written to {args.output}")
    else:
        print(output)

    if args.fail_on_error:
        has_errors = any(f.severity == "error" for r in results for f in r.findings)
        return 1 if has_errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
