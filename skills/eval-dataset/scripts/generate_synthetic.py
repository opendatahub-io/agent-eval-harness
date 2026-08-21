#!/usr/bin/env python3
"""Generate synthetic test cases from generation prompts using an LLM.

This script reads the ``generation`` block from eval.yaml, resolves each seed's
generation prompt (builtin / prompt_file / inline), and uses the Claude API to
generate test cases following the prompt instructions.
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.config import EvalConfig, GenerationSeed
from agent_eval.dataset_audit import ANNOTATION_FIELDS, MANIFEST_FILENAME
from agent_eval.prompts import resolve_seed_prompt

#: Sampling temperature for generation calls — higher for variety. Recorded
#: in manifest.yaml so regeneration provenance is explicit.
GENERATION_TEMPERATURE = 1.0


def _seed_source(seed: GenerationSeed) -> str:
    """Human-readable description of a seed's prompt source (for logs/metadata)."""
    return seed.builtin or seed.prompt_file or ("inline" if seed.prompt else "?")


def _existing_generated_dirs(output_dir: Path) -> list:
    """Previously generated ``case-NNN`` dirs under *output_dir*.

    Only exact ``case-\\d{3}`` names match — hand-authored dirs like
    ``case-001-simple-basic-input`` never match and are never deleted.
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    return sorted(d for d in output_dir.iterdir()
                  if d.is_dir() and re.fullmatch(r"case-\d{3}", d.name))


def generate_synthetic(
    config: EvalConfig,
    output_dir: Path,
    model: str = "claude-opus-4-6",
    api_key: Optional[str] = None,
    force: bool = False,
    now=None,
) -> list[dict]:
    """Generate test cases from the generation seeds in ``config``.

    Regeneration REPLACES the whole ``case-NNN`` set: seed counts are
    declarative totals and the case counter always restarts at 1, so the
    documented resize flow (increase a seed's ``count``, re-run) rewrites
    ``case-001..N``. Existing ``case-NNN`` dirs therefore refuse to be
    overwritten unless ``force`` is set (raised BEFORE any API call);
    with ``force`` they are removed and regenerated, and ``manifest.yaml``
    is fully replaced to describe the new set.

    Args:
        config: EvalConfig with a ``generation`` block (seeds + context)
        output_dir: Where to write generated test cases
        model: Claude model to use for generation
        api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY or uses ANTHROPIC_VERTEX_PROJECT_ID)
        force: Replace pre-existing ``case-NNN`` dirs (see above)
        now: ``generated_at`` timestamp for manifest.yaml (datetime or ISO
            string; default: system clock) — injectable for tests

    Returns:
        List of generated case metadata

    Raises:
        ValueError: If no generation seeds defined, or existing ``case-NNN``
            dirs would be overwritten without ``force``
        ImportError: If anthropic package not installed
    """
    if not config.generation.seeds:
        raise ValueError(
            "No generation seeds defined in config. "
            "Synthetic generation requires generation.seeds.")

    # Overwrite guard — BEFORE any API call (and before client auth).
    existing = _existing_generated_dirs(Path(output_dir))
    if existing and not force:
        names = ", ".join(d.name for d in existing[:5])
        if len(existing) > 5:
            names += f", … ({len(existing)} total)"
        raise ValueError(
            f"Refusing to overwrite existing generated case dir(s) in "
            f"{output_dir}: {names}. Synthetic regeneration REPLACES the "
            "whole case-NNN set (seed counts are declarative totals and "
            "numbering restarts at case-001), and hand-edits such as TODO_ "
            "replacements would be lost. Re-run with --force to replace "
            "them — see skills/eval-dataset/references/"
            "synthetic-generation.md (resize flow).")

    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package required for synthetic generation. "
            "Install with: pip install anthropic")

    # Support both direct API and Vertex AI authentication
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")

    if api_key:
        client = anthropic.Anthropic(api_key=api_key)
    elif vertex_project:
        # Use Vertex AI authentication
        client = anthropic.AnthropicVertex(
            project_id=vertex_project,
            region=os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5"),
        )
    else:
        raise ValueError(
            "Authentication required: set either ANTHROPIC_API_KEY or "
            "ANTHROPIC_VERTEX_PROJECT_ID environment variable.")

    # --force: replace the pre-existing generated set (after auth checks so
    # an auth failure can never delete the dataset). Hand-authored
    # case-001-slug style dirs never match and survive.
    if existing and force:
        for stale_dir in existing:
            shutil.rmtree(stale_dir)

    all_cases = []
    failed_categories = []
    seed_records = []
    case_counter = 1

    for seed in config.generation.seeds:
        print(f"Generating {seed.count} test cases for category: {seed.category}")

        # Resolve the generation prompt via the seed's discriminator
        # (builtin / prompt_file / prompt — validated by EvalConfig.from_yaml)
        try:
            generation_prompt = resolve_seed_prompt(seed, config.config_dir)
        except (ValueError, FileNotFoundError) as e:
            print(
                f"ERROR: Failed to resolve generation prompt for category '{seed.category}': {e}",
                file=sys.stderr,
            )
            raise

        # Per-seed manifest record — realized counts are tracked durably:
        # `returned` is what the LLM produced (null when the category
        # failed), `written` is what per-case validation actually kept.
        seed_record = {
            "category": seed.category,
            "source": _seed_source(seed),
            "prompt_sha256": hashlib.sha256(
                generation_prompt.encode("utf-8")).hexdigest(),
            "requested": seed.count,
            "returned": None,
            "written": 0,
            "failed": False,
        }
        seed_records.append(seed_record)

        # Generate cases for this category
        try:
            cases = _generate_category_cases(
                client=client,
                generation_prompt=generation_prompt,
                seed=seed,
                context=config.generation.context,
                count=seed.count,
                model=model,
            )
        except ValueError as e:
            print(
                f"ERROR: Failed to generate cases for category '{seed.category}': {e}\n"
                f"Continuing with other categories...",
                file=sys.stderr,
            )
            seed_record["failed"] = True
            failed_categories.append(seed.category)
            # Continue with next category instead of failing entirely
            continue
        except Exception as e:
            print(f"ERROR: Unexpected error for category '{seed.category}': {e}", file=sys.stderr)
            seed_record["failed"] = True
            failed_categories.append(seed.category)
            continue

        seed_record["returned"] = len(cases)

        # Write cases to disk
        for case in cases:
            # Validate case structure
            if not isinstance(case, dict):
                print(
                    f"  WARNING: Skipping invalid case (not a dict): {case}",
                    file=sys.stderr,
                )
                continue
            if "input" not in case:
                print(
                    f"  WARNING: Skipping case without 'input' field: {case}",
                    file=sys.stderr,
                )
                continue

            # Validate input is a dict (not a string, list, etc.)
            if not isinstance(case["input"], dict):
                print(
                    f"  WARNING: Skipping case with invalid 'input' (not a dict): {case['input']!r}",
                    file=sys.stderr,
                )
                continue

            # Build annotations dict - always include category for judge filtering
            annotations = {}
            if "annotations" in case:
                if not isinstance(case["annotations"], dict):
                    print(
                        f"  WARNING: Skipping case with invalid 'annotations' (not a dict): {case['annotations']!r}",
                        file=sys.stderr,
                    )
                    continue
                annotations = case["annotations"].copy()

            # CRITICAL: Always set category to match the seed's category
            # Judges use `if: "annotations.get('category') == 'navigation'"` for filtering.
            # This is also how the category list is derived — never declared separately.
            annotations["category"] = seed.category

            case_id = f"case-{case_counter:03d}"
            case_dir = output_dir / case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            # Write input.yaml
            (case_dir / "input.yaml").write_text(
                yaml.dump(case["input"], sort_keys=False, allow_unicode=True)
            )

            # Always write annotations.yaml with guaranteed category field
            (case_dir / "annotations.yaml").write_text(
                yaml.dump(annotations, sort_keys=False, allow_unicode=True)
            )

            # Track metadata
            all_cases.append({
                "case_id": case_id,
                "category": seed.category,
                "source": _seed_source(seed),
            })

            seed_record["written"] += 1
            case_counter += 1

    if not all_cases:
        raise RuntimeError(
            "Failed to generate any synthetic cases"
            + (f": {', '.join(failed_categories)}" if failed_categories else "")
        )

    # Persist generation provenance at the dataset root — only on success,
    # after the empty-dataset check, so the manifest always describes a
    # dataset that exists. Fully replaced on --force regeneration.
    manifest = _build_manifest(
        config=config,
        model=model,
        seed_records=seed_records,
        failed_categories=failed_categories,
        cases=all_cases,
        now=now,
    )
    _write_manifest(Path(output_dir), manifest)

    return all_cases


def _context_sha256(context) -> str:
    """sha256 of the canonical YAML dump of ``generation.context``."""
    return hashlib.sha256(
        yaml.dump(context, sort_keys=True).encode("utf-8")).hexdigest()


def _build_manifest(config, model, seed_records, failed_categories, cases,
                    now=None) -> dict:
    """Build the manifest.yaml content (generation provenance)."""
    if now is None:
        generated_at = datetime.now(timezone.utc).isoformat()
    elif isinstance(now, str):
        generated_at = now
    else:
        generated_at = now.isoformat()
    return {
        "manifest_version": 1,
        "generated_at": generated_at,
        "model": model,
        "temperature": GENERATION_TEMPERATURE,
        "context_sha256": _context_sha256(config.generation.context),
        "seeds": seed_records,
        "failed_categories": failed_categories,
        "cases": cases,
    }


def _write_manifest(output_dir: Path, manifest: dict) -> Path:
    """Write manifest.yaml at the dataset root.

    A root-level FILE — invisible to dir-only case discovery (workspace.py,
    collect.py, harbor task generation all iterate directories only).
    """
    path = Path(output_dir) / MANIFEST_FILENAME
    path.write_text(
        yaml.dump(manifest, sort_keys=False, allow_unicode=True))
    return path


def _generate_category_cases(
    client,
    generation_prompt: str,
    seed: GenerationSeed,
    context,
    count: int,
    model: str,
) -> list[dict]:
    """Use an LLM to generate cases from a resolved generation prompt.

    Args:
        client: Anthropic client
        generation_prompt: Resolved generation-prompt markdown content
        seed: GenerationSeed config
        context: Repository knowledge (dict or str) injected into the prompt
        count: Number of cases to generate
        model: Claude model to use

    Returns:
        List of test case dicts with 'input' and optional 'annotations' keys
    """
    # Build generation prompt
    context_yaml = (
        yaml.dump(context, sort_keys=False, allow_unicode=True) if context else "None"
    )

    prompt = f"""You are generating test cases for an agent evaluation harness.

# Generation Prompt

{generation_prompt}

# Generation Context

The repository-specific context that should inform test case generation:

```yaml
{context_yaml}
```

# Generation Task

Generate exactly {count} test case(s) following the generation prompt above.

**IMPORTANT**: Return ONLY a valid JSON array with no additional text, markdown formatting, or explanation.

Each test case MUST be a JSON object with exactly two top-level keys:
- "input": dict with fields the agent receives (typically just "prompt" — the question or task).
  Do NOT put evaluation metadata here.
- "annotations": dict with evaluation metadata used by judges to score the response.
  This includes: category, difficulty, expected_files, expected_mentions,
  expected_rejection, expected_guidance, severity, constraint_type, topic, and
  any other scoring criteria.

Example format:
```json
[
  {{
    "input": {{
      "prompt": "User question or task for the agent"
    }},
    "annotations": {{
      "category": "{seed.category}",
      "difficulty": "medium",
      "expected_files": ["path/to/relevant-doc.md"],
      "expected_mentions": ["keyword1", "keyword2"]
    }}
  }}
]
```

CRITICAL RULES:
- "input" contains ONLY what the agent sees (typically just "prompt")
- "annotations" contains ALL evaluation metadata (expected_files, expected_mentions,
  expected_rejection, expected_guidance, category, difficulty, etc.)
- NEVER put expected_files, expected_mentions, or any expected_* fields in "input"

Generate realistic, varied test cases that:
1. Use actual paths/topics from the generation context (if available)
2. Cover different difficulty levels
3. Test different aspects of the capability
4. Are specific and verifiable

Return the JSON array now:"""

    # Call Claude API
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        temperature=GENERATION_TEMPERATURE,  # Higher temperature for variety
    )

    # Parse response (validate content exists and has text attribute - CWE-20)
    if not response.content:
        raise ValueError("Anthropic API returned empty content")

    first_block = response.content[0]
    if not hasattr(first_block, 'text'):
        raise ValueError(
            f"Expected text content from Anthropic API, got type: {type(first_block).__name__}")

    response_text = first_block.text.strip()

    # Extract JSON from response (may be wrapped in markdown)
    cases = _extract_json_from_response(response_text)

    if not isinstance(cases, list):
        raise ValueError(
            f"Expected JSON array from LLM, got: {type(cases).__name__}")

    if len(cases) != count:
        print(
            f"WARNING: Requested {count} cases, got {len(cases)} "
            f"for category {seed.category}",
            file=sys.stderr,
        )

    # Move annotation fields that the LLM misplaced into input
    _fix_misplaced_annotation_fields(cases)

    return cases


# Back-compat alias — the canonical set lives in agent_eval.dataset_audit
# so the audit and the generator cannot diverge.
_ANNOTATION_FIELDS = ANNOTATION_FIELDS


def _fix_misplaced_annotation_fields(cases: list[dict]) -> None:
    """Move known annotation fields from input to annotations if the LLM misplaced them."""
    for case in cases:
        if not isinstance(case.get("input"), dict):
            continue
        misplaced = _ANNOTATION_FIELDS & set(case["input"].keys())
        if misplaced:
            if "annotations" not in case:
                case["annotations"] = {}
            for field in misplaced:
                case["annotations"][field] = case["input"].pop(field)
            print(
                f"  WARNING: Moved {misplaced} from input to annotations",
                file=sys.stderr,
            )


def _extract_json_from_response(text: str) -> list:
    """Extract JSON array from LLM response.

    Handles responses that may be wrapped in markdown code blocks.

    Args:
        text: Response text from LLM

    Returns:
        Parsed JSON array

    Raises:
        ValueError: If JSON cannot be parsed
    """
    original_text = text

    # Try parsing directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    if "```" in text:
        # Find content between ``` markers
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            # Skip language identifiers
            if part.startswith("json") or part.startswith("JSON"):
                part = part[4:].strip()

            # Try parsing if it looks like JSON
            if part.startswith("[") or part.startswith("{"):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue

    # Try finding JSON array with regex (handles text before/after)
    array_pattern = r'\[(?:[^\[\]]|\[[^\[\]]*\])*\]'
    matches = list(re.finditer(array_pattern, text, re.DOTALL))

    # Try matches from longest to shortest
    for match in sorted(matches, key=lambda m: len(m.group(0)), reverse=True):
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue

    # Try cleaning common issues
    # Remove trailing commas before ] or }
    cleaned = re.sub(r',(\s*[\]}])', r'\1', text)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Final attempt: find the largest bracket pair
    start = text.find('[')
    if start >= 0:
        end = text.rfind(']')
        if end > start:
            candidate = text[start:end+1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Give up with detailed error
    raise ValueError(
        f"Could not extract valid JSON from response.\n"
        f"Response preview: {original_text[:500]}...\n"
        f"Response length: {len(original_text)} chars"
    )


def main():
    """CLI for testing synthetic generation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate synthetic test cases from generation prompts")
    parser.add_argument(
        "--config", required=True,
        help="Path to eval.yaml")
    parser.add_argument(
        "--output", required=True,
        help="Output directory for generated test cases")
    parser.add_argument(
        "--model", default="claude-opus-4-6",
        help="Claude model to use (default: claude-opus-4-6)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be generated without calling API")
    parser.add_argument(
        "--force", action="store_true",
        help="Replace existing case-NNN dirs (regeneration rewrites the "
             "whole set — seed counts are declarative totals and numbering "
             "restarts at case-001; hand-edits are lost)")

    args = parser.parse_args()

    # Load config
    config = EvalConfig.from_yaml(args.config)

    if not config.generation.seeds:
        print(
            "ERROR: No generation seeds defined in config. "
            "Synthetic generation requires generation.seeds.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = Path(args.output)

    if args.dry_run:
        print("Would generate test cases:")
        for seed in config.generation.seeds:
            print(f"  - {seed.category}: {seed.count} cases from {_seed_source(seed)}")
        total = sum(s.count for s in config.generation.seeds)
        print(f"\nTotal: {total} test cases")
        print(f"Output: {output_dir}")
        return

    # Generate cases
    try:
        cases = generate_synthetic(
            config=config,
            output_dir=output_dir,
            model=args.model,
            force=args.force,
        )

        print(f"\nGenerated {len(cases)} test cases:")
        for case in cases:
            print(f"  {case['case_id']}: {case['category']}")

        print(f"\nOutput written to: {output_dir}")
        print(f"Manifest: {output_dir / MANIFEST_FILENAME}")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
