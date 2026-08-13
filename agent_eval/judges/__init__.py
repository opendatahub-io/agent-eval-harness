"""Built-in judges registry with auto-discovery from category subdirectories."""

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Optional


@dataclass
class BuiltinJudgeEntry:
    kind: str  # "python" or "llm"
    category: str
    module: Optional[ModuleType] = None
    function_name: str = ""
    prompt_path: Optional[Path] = None


class BuiltinJudgeRegistry:

    def __init__(self):
        self._judges: dict[str, BuiltinJudgeEntry] = {}

    def discover(self) -> None:
        package_dir = Path(__file__).parent
        for category_dir in sorted(package_dir.iterdir()):
            if not category_dir.is_dir() or category_dir.name.startswith("_"):
                continue
            category = category_dir.name
            for file_path in sorted(category_dir.iterdir()):
                if file_path.name.startswith("_"):
                    continue
                if file_path.suffix == ".py":
                    name = file_path.stem
                    if name in self._judges:
                        existing = self._judges[name]
                        raise ValueError(
                            f"Builtin judge name collision: '{name}' found in "
                            f"both {existing.category}/ and {category}/")
                    spec = importlib.util.spec_from_file_location(
                        f"agent_eval.judges.{category}.{name}", file_path)
                    if spec is None or spec.loader is None:
                        raise ValueError(
                            f"Failed to load builtin judge module: {file_path}")
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if not callable(getattr(mod, "judge", None)):
                        raise ValueError(
                            f"Builtin judge '{name}' in {category}/ "
                            f"must define a callable 'judge' function")
                    self._judges[name] = BuiltinJudgeEntry(
                        kind="python",
                        category=category,
                        module=mod,
                        function_name="judge",
                    )
                elif file_path.suffix == ".md":
                    name = file_path.stem
                    if name in self._judges:
                        existing = self._judges[name]
                        raise ValueError(
                            f"Builtin judge name collision: '{name}' found in "
                            f"both {existing.category}/ and {category}/")
                    self._judges[name] = BuiltinJudgeEntry(
                        kind="llm",
                        category=category,
                        prompt_path=file_path,
                    )

    def get(self, name: str) -> BuiltinJudgeEntry:
        if "/" in name:
            _, flat_name = name.rsplit("/", 1)
        else:
            flat_name = name
        entry = self._judges.get(flat_name)
        if entry is None:
            available = ", ".join(self.list_names())
            raise ValueError(
                f"Unknown builtin judge '{name}'. Available: {available}")
        if "/" in name:
            expected_category = name.rsplit("/", 1)[0]
            if entry.category != expected_category:
                raise ValueError(
                    f"Unknown builtin judge '{name}'. "
                    f"'{flat_name}' is in category '{entry.category}', "
                    f"not '{expected_category}'. "
                    f"Available: {', '.join(self.list_names())}")
        return entry

    def list_names(self) -> list[str]:
        return sorted(self._judges.keys())


def _scan_builtin_judges() -> dict:
    """Map "category/name" -> kind ("python" or "llm"), read off the filesystem.

    Config load needs to resolve `builtin:` names but must not pay for
    `BuiltinJudgeRegistry.discover()`, which execs every Python judge module.
    Names are matched against this mapping rather than used to build a path,
    so a `builtin:` value cannot reach outside the package.
    """
    package_dir = Path(__file__).parent
    found = {}
    for category_dir in sorted(package_dir.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith((".", "_")):
            continue
        for file_path in sorted(category_dir.iterdir()):
            if file_path.name.startswith((".", "_")):
                continue
            kind = {".py": "python", ".md": "llm"}.get(file_path.suffix)
            if kind:
                found[f"{category_dir.name}/{file_path.stem}"] = kind
    return found


def builtin_judge_names() -> list[str]:
    """Every builtin judge as "category/name", without importing any of them."""
    return sorted(_scan_builtin_judges())


def builtin_judge_kind(name: str) -> Optional[str]:
    """Resolve a builtin judge's kind, by flat name or "category/name".

    Returns None when the name does not resolve to anything.
    """
    found = _scan_builtin_judges()
    if "/" in name:
        return found.get(name)
    matches = [k for fqn, k in found.items() if fqn.rsplit("/", 1)[1] == name]
    return matches[0] if matches else None
