<!-- Sync Impact Report
Version change: 1.0.0 → 1.1.0 → 1.1.1 (PATCH: refined VII and VIII per reviewer feedback)
Modified principles: none renamed
Added sections:
  - VII. Backward Compatibility by Default
  - VIII. Infer State, Don't Persist It
  - Generalized language from "skills" to "eval targets" throughout
Removed sections: none
Templates requiring updates:
  - .specify/templates/plan-template.md: ✅ no constitution references
  - .specify/templates/spec-template.md: ✅ no constitution references
  - .specify/templates/tasks-template.md: ✅ no constitution references
Follow-up TODOs: none
-->

# Agent Eval Harness Constitution

## Core Principles

### I. Schema-Driven Design
Dataset and output structures are described in natural language in eval.yaml. Agents and judges interpret these descriptions. Scripts operate on file paths from eval.yaml directly, with no extraction spec and no hardcoded field names.

### II. Agent-Agnostic Runner
The `EvalRunner` ABC supports multiple agent backends via the `--agent` flag. Claude Code is included as the default. The runner interface is extensible to OpenCode, Agent SDK, and other backends without changing the scoring or reporting pipeline.

### III. Jinja2 for All Templating
All template rendering uses Jinja2 consistently. No manual `str.replace()` or custom template engines. This applies to LLM judge prompts (builtin and inline), prompt files, and any future template-based features. Template variables (`outputs`, `arguments`, `annotations`, `conversation`) are available uniformly across all template contexts.

### IV. Trusted Configuration
eval.yaml is a repository-controlled, trusted configuration file. Security boundaries (restricted `eval()`, Jinja2 environment) are designed for defense-in-depth, not for sandboxing untrusted input. eval.yaml must not be accepted from untrusted sources.

### V. Extend, Don't Replace
New features extend existing dataclasses and functions rather than replacing them. `JudgeConfig`, `load_judges()`, and the scoring pipeline grow incrementally. Existing eval.yaml configurations must continue to work without modification.

### VI. MLflow as Optional Integration
MLflow handles dataset sync, result logging, and trace feedback via a separate skill (`/eval-mlflow`). The core eval pipeline (analyze, dataset, run, review, optimize) works without MLflow. No implicit experiment creation on shared tracking servers.

### VII. Backward Compatibility by Default
Existing configurations must keep working when new features are added. Root-level `eval.yaml` remains a first-class location for single-eval projects with no deprecation warnings. New organizational features (directory layouts, discovery) adapt to project complexity. When migration is needed, the harness should automate it (offer to reorganize, move files, update paths) rather than leaving manual steps to the user.

### VIII. Infer State, Don't Persist It
Prefer inferring system state from existing file structure over creating persistence files. Discovery patterns, layout detection, and convention resolution should derive from what's on disk rather than maintaining separate metadata files. This eliminates artifacts to manage, gitignore entries, and error handling for corrupted state. Exception: persist computed state when it is expensive to recompute (e.g., `eval.md` from skill deep analysis).

## Technology Stack

- **Language**: Python 3.11+
- **Dependencies**: PyYAML, Jinja2 (core); MLflow, Anthropic SDK (optional)
- **Testing**: pytest, with E2E tests gated behind markers
- **Package management**: uv (not pip)
- **Virtual environments**: `~/.venvs/agent-eval/` (not project-local .venv)

## Development Workflow

- Conventional commits for semantic versioning
- Feature branches with spec-driven development (spec -> plan -> tasks -> implement)
- Brainstorm documents in `brainstorm/` are local working files and must never be committed to git

## Governance

Constitution supersedes default practices. Amendments require documentation and review.

**Version**: 1.1.1 | **Ratified**: 2026-05-29 | **Last Amended**: 2026-05-31
