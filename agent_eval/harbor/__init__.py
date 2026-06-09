"""Harbor integration for agent-eval-harness.

Generates self-contained Harbor task packages that any Harbor agent (Claude Code,
OpenCode, Codex, etc.) can run directly — no custom agent wrapper needed. All
setup (inputs, tool interception hooks, project resources) lives in the task's
``environment/`` dir, which Harbor auto-uploads to the agent workspace.

Modules:
- tasks: generate Harbor task packages from eval.yaml + dataset
- reward: judge -> Harbor ``reward.json`` bridge (runs inside the container)
- run: ``/eval-run --runner harbor`` orchestration (generate → run → map → report)
- results: parse Harbor job dirs into per-case results
- podman: native Podman BaseEnvironment (local)
- kubernetes: Kubernetes BaseEnvironment (OpenShift, Python client)
"""
