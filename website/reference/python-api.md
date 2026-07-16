# Python API

Reference for the `agent_eval` package, generated from the source docstrings and type
hints. Most users drive the harness through the [slash commands](cli.md) and
[`eval.yaml`](eval-yaml.md) — this page is for extending it (for example, writing a
custom [runner](../concepts/runners.md)) or embedding it in your own tooling.

!!! info "Autodoc"
    These entries are rendered by
    [`mkdocstrings`](https://mkdocstrings.github.io/). The documentation build runs
    `pip install -e .` so the package is importable; add more `:::` directives to this
    page to surface additional modules.

## Configuration

The entire `eval.yaml` surface is parsed into `EvalConfig`. See the
[eval.yaml reference](eval-yaml.md) for the YAML-level documentation of every field.

::: agent_eval.config.EvalConfig

## Runners

A runner adapts a generic evaluation call to a specific agent runtime and returns a
normalized [`RunResult`](#agent_eval.agent.base.RunResult). To support a new agent,
subclass `EvalRunner`, implement the three abstract members below, and register it in
the `RUNNERS` registry — see [Runners](../concepts/runners.md).

```python title="agent_eval/agent/base.py"
class EvalRunner(ABC):
    """Abstract runner -- one implementation per agent platform."""

    @classmethod
    @abstractmethod
    def from_config(cls, config, *, log_prefix=None, **overrides):
        """Construct a runner from an EvalConfig."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this runner (e.g. 'claude-code')."""

    @abstractmethod
    def execute(self, target, args, workspace, model, ...) -> RunResult:
        """Run one invocation and return a normalized RunResult."""
```

Every runner returns the same normalized result, so scoring and reporting are
runner-agnostic:

::: agent_eval.agent.base.RunResult
