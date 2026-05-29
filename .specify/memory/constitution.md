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

## Technology Stack

- **Language**: Python 3.11+
- **Dependencies**: PyYAML, Jinja2 (core); MLflow, Anthropic SDK (optional)
- **Testing**: pytest, with E2E tests gated behind markers
- **Package management**: uv (not pip)
- **Virtual environments**: `~/.venvs/agent-eval/` (not project-local .venv)

## Development Workflow

- Conventional commits for semantic versioning
- Feature branches with spec-driven development (spec -> plan -> tasks -> implement)
- Brainstorm documents in `brainstorm/` for exploratory ideas (not reflected in code)
- Implemented brainstorms moved to `brainstorm/attic/`

## Governance

Constitution supersedes default practices. Amendments require documentation and review.

**Version**: 1.0.0 | **Ratified**: 2026-05-29
