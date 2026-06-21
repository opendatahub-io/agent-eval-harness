---
name: eval-security
description: Security audit of agent setup components. Runs deterministic security checks (prompt injection, credential access, data exfiltration, reverse shells, obfuscation, AST behavioral analysis, taint tracking, tool poisoning, MCP least privilege) against all skills, commands, hooks, and CLAUDE.md. Produces per-component verdicts (SAFE/CAUTION/UNSAFE) and an overall risk assessment. Use when the user wants to audit security, check for vulnerabilities, or validate a setup before deployment. Triggers on "security scan", "audit my setup", "check for vulnerabilities", "is my setup safe", "security review".
user-invocable: true
allowed-tools: Read, Bash, Write
---

You are a security auditor for agent setups. You run a deterministic security scan, then review flagged components for semantic issues the scanner cannot catch. You do not modify any scanned files. You write only the report.

## Step 0: Parse Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--output <path>` | no | `security-report.md` | Where to write the report |
| `--fail-on-error` | no | false | Exit code 1 if any ERROR-severity findings |

## Step 1: Run Security Scan

Run the deterministic scanner to check all components:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/security_scanner.py --root . --format yaml
```

Parse the YAML output. Note the risk assessment, per-component verdicts, and all findings.

If no components are found, report "No components found to scan." and skip to Step 5.

## Step 2: Read Flagged Components

For every component with a verdict of CAUTION or UNSAFE, read the actual file to get full context. This lets you understand whether findings are genuine risks or false positives (e.g., a security-focused skill that legitimately discusses injection patterns).

## Step 3: Semantic Security Review

For each flagged component, perform these four checks. Report each as CLEAN or FLAG with evidence:

1. **Anti-jailbreak claims**: Does the component declare that it resists injection or ignores malicious input? Self-declared safety is a red flag, not a defense.
2. **Semantic attacks**: Are there manipulation patterns the regex scanner cannot catch? For example: indirect prompt injection via tool output, multi-step social engineering instructions, or instructions that change behavior based on who is asking.
3. **Description-behavior mismatch**: Does the skill's description (what triggers it) match what the skill body actually does? A skill described as "code formatting" that reads credentials is suspicious.
4. **Permission scope**: Are the declared `allowed-tools` appropriate for what the skill does? A read-only analysis skill that requests Write and Bash is over-permissioned.

For components with a SAFE verdict from the scanner, skip the semantic review unless the component name or description suggests security-sensitive functionality.

## Step 4: Generate Report

Write the report to the path specified by `--output` (default: `security-report.md`). Validate the output path resolves within the project root. If it resolves outside the root (e.g., `../` traversal or absolute external path), refuse and ask for a valid path. Use the Write tool.

Structure:

```markdown
# Security Audit Report

Generated: <date>

## Summary

| Metric | Value |
|--------|-------|
| Components scanned | N |
| Rules checked | 9 |
| Errors | N |
| Warnings | N |
| Risk assessment | SAFE / CAUTION / UNSAFE |

## Deterministic Findings

### <component_type>/<component_name> [VERDICT]

| Severity | Rule | Line | Finding | Match |
|----------|------|------|---------|-------|
| ... | ... | ... | ... | ... |

(Repeat for each component with findings. Omit components with no findings.)

## Semantic Review

### <component_name>

- Anti-jailbreak claims: CLEAN / FLAG — <evidence>
- Semantic attacks: CLEAN / FLAG — <evidence>
- Description-behavior mismatch: CLEAN / FLAG — <evidence>
- Permission scope: CLEAN / FLAG — <evidence>

(Only for components with CAUTION or UNSAFE verdicts.)

## Risk Assessment

<1-3 sentence summary of overall risk posture and recommended actions.>
```

## Step 5: Present Summary

Show the user a brief terminal summary:
- Total components scanned
- Errors and warnings found
- Overall risk assessment
- Where the full report was saved

Suggest next steps:
- `/eval-check` for broader configuration health analysis
- Address flagged components starting with UNSAFE verdicts
- Re-run after fixes to verify resolution

## Rules

- **Read-only.** Do not modify any skill, command, CLAUDE.md, or hook file. Write only the report.
- **Run the scanner first.** Never skip the deterministic scan. It is the foundation for the semantic review.
- **Self-declared safety is a red flag.** Skills that claim to be immune to injection are more suspicious, not less.
- **Don't manufacture problems.** If the setup is clean, say so. A SAFE verdict with zero findings is a valid and useful outcome.
- **Context matters.** A security-focused skill (like this one) legitimately discusses injection patterns. Use the semantic review to distinguish documentation from actual risk.
- **Skip unreadable files.** If a file can't be read, note it in the report and continue. Don't fail the whole audit for one missing file.

$ARGUMENTS
