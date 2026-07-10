"""Built-in generation prompts registry with auto-discovery from category subdirectories.

Generation prompts are LLM instruction files that ``/eval-dataset`` uses to generate
test cases (synthetic generation). They mirror the builtin *judges* model
(``agent_eval/judges/``): builtins live here in category subdirectories and are
referenced from a generation seed via ``builtin: <category>/<name>`` (e.g.
``builtin: docs/navigation``). Projects that need something bespoke supply their own
via a seed's ``prompt_file:`` (a path relative to the eval config) or an inline
``prompt:`` string.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass
class BuiltinPromptEntry:
    category: str
    path: Path


class BuiltinPromptRegistry:
    """Discovers builtin generation prompts (``.md`` files) under category subdirs."""

    def __init__(self):
        self._prompts: dict[str, BuiltinPromptEntry] = {}

    def discover(self) -> None:
        package_dir = Path(__file__).parent
        for category_dir in sorted(package_dir.iterdir()):
            if not category_dir.is_dir() or category_dir.name.startswith("_"):
                continue
            category = category_dir.name
            for file_path in sorted(category_dir.iterdir()):
                if file_path.name.startswith("_") or file_path.suffix != ".md":
                    continue
                if file_path.name == "README.md":
                    continue
                name = file_path.stem
                if name in self._prompts:
                    existing = self._prompts[name]
                    raise ValueError(
                        f"Builtin prompt name collision: '{name}' found in "
                        f"both {existing.category}/ and {category}/")
                self._prompts[name] = BuiltinPromptEntry(
                    category=category, path=file_path)

    def get(self, name: str) -> BuiltinPromptEntry:
        if "/" in name:
            _, flat_name = name.rsplit("/", 1)
        else:
            flat_name = name
        entry = self._prompts.get(flat_name)
        if entry is None:
            available = ", ".join(self.list_names())
            raise ValueError(
                f"Unknown builtin prompt '{name}'. Available: {available}")
        if "/" in name:
            expected_category = name.rsplit("/", 1)[0]
            if entry.category != expected_category:
                raise ValueError(
                    f"Unknown builtin prompt '{name}'. "
                    f"'{flat_name}' is in category '{entry.category}', "
                    f"not '{expected_category}'. "
                    f"Available: {', '.join(self.list_names())}")
        return entry

    def list_names(self) -> list[str]:
        return sorted(
            f"{entry.category}/{name}" for name, entry in self._prompts.items())


def _seed_field(seed, name: str):
    """Read a field from a GenerationSeed dataclass or a plain dict."""
    if isinstance(seed, dict):
        return seed.get(name)
    return getattr(seed, name, None)


def resolve_seed_prompt(seed, config_dir: Union[str, Path]) -> str:
    """Resolve a generation seed's prompt text via its discriminator.

    Exactly one of ``builtin`` / ``prompt_file`` / ``prompt`` is expected (enforced
    at config load). Resolution order mirrors judges: inline first, then file, then
    builtin registry.

    Args:
        seed: A ``GenerationSeed`` (or dict) with one discriminator set.
        config_dir: Directory of the eval config; ``prompt_file`` paths resolve
            relative to it.

    Returns:
        The prompt markdown/text to hand to the generating LLM.
    """
    inline = _seed_field(seed, "prompt")
    if inline:
        return inline

    prompt_file = _seed_field(seed, "prompt_file")
    if prompt_file:
        path = Path(prompt_file)
        if not path.is_absolute():
            path = Path(config_dir) / path
        if not path.exists():
            raise FileNotFoundError(
                f"Generation prompt file not found: {path}")
        return path.read_text()

    builtin = _seed_field(seed, "builtin")
    if builtin:
        registry = BuiltinPromptRegistry()
        registry.discover()
        return registry.get(builtin).path.read_text()

    category = _seed_field(seed, "category") or "?"
    raise ValueError(
        f"Generation seed '{category}' must set exactly one of "
        f"builtin / prompt_file / prompt")
