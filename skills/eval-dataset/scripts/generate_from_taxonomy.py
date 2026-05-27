#!/usr/bin/env python3
"""Generate test cases from taxonomy-based templates using LLM.

This script reads test_categories from eval.yaml, resolves templates,
and uses Claude API to generate test cases following template instructions.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import yaml

# Add agent_eval to path
sys.path.insert(0, str(Path(__file__).parent))
from agent_eval.config import EvalConfig, TestCategory
from resolve_template import resolve_template


def generate_from_taxonomy(
    config: EvalConfig,
    output_dir: Path,
    model: str = "claude-opus-4-6",
    api_key: Optional[str] = None,
) -> list[dict]:
    """Generate test cases using category templates.

    Args:
        config: EvalConfig with test_categories and domain
        output_dir: Where to write generated test cases
        model: Claude model to use for generation
        api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY or uses ANTHROPIC_VERTEX_PROJECT_ID)

    Returns:
        List of generated case metadata

    Raises:
        ValueError: If no test_categories defined
        ImportError: If anthropic package not installed
    """
    if not config.test_categories:
        raise ValueError(
            "No test_categories defined in config. "
            "Taxonomy-based generation requires test_categories.")

    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package required for taxonomy-based generation. "
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
    all_cases = []
    failed_categories = []
    case_counter = 1

    for category in config.test_categories:
        print(f"Generating {category.count} test cases for category: {category.name}")

        # Resolve template (required field, validated by EvalConfig.from_yaml)
        try:
            template_path = resolve_template(category.template)
        except (ValueError, FileNotFoundError) as e:
            # Provide helpful error message
            print(
                f"ERROR: Failed to resolve template for category '{category.name}': {e}",
                file=sys.stderr,
            )
            raise
        template_content = template_path.read_text()

        # Generate cases for this category
        try:
            cases = _generate_category_cases(
                client=client,
                template=template_content,
                category=category,
                domain=config.domain,
                count=category.count,
                model=model,
            )
        except ValueError as e:
            print(
                f"ERROR: Failed to generate cases for category '{category.name}': {e}\n"
                f"Continuing with other categories...",
                file=sys.stderr,
            )
            failed_categories.append(category.name)
            # Continue with next category instead of failing entirely
            continue
        except Exception as e:
            print(f"ERROR: Unexpected error for category '{category.name}': {e}", file=sys.stderr)
            failed_categories.append(category.name)
            continue

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

            # Validate annotations is a dict if present
            if "annotations" in case and not isinstance(case["annotations"], dict):
                print(
                    f"  WARNING: Skipping case with invalid 'annotations' (not a dict): {case['annotations']!r}",
                    file=sys.stderr,
                )
                continue

            case_id = f"case-{case_counter:03d}"
            case_dir = output_dir / case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            # Write input.yaml
            (case_dir / "input.yaml").write_text(
                yaml.dump(case["input"], sort_keys=False, allow_unicode=True)
            )

            # Write annotations.yaml if present
            if "annotations" in case:
                (case_dir / "annotations.yaml").write_text(
                    yaml.dump(case["annotations"], sort_keys=False, allow_unicode=True)
                )

            # Track metadata
            all_cases.append({
                "case_id": case_id,
                "category": category.name,
                "template": category.template,
            })

            case_counter += 1

    if not all_cases:
        raise RuntimeError(
            "Failed to generate any taxonomy cases"
            + (f": {', '.join(failed_categories)}" if failed_categories else "")
        )
    return all_cases


def _generate_category_cases(
    client,
    template: str,
    category: TestCategory,
    domain: dict,
    count: int,
    model: str,
) -> list[dict]:
    """Use LLM to generate cases from template.

    Args:
        client: Anthropic client
        template: Template markdown content
        category: TestCategory config
        domain: Domain knowledge dict
        count: Number of cases to generate
        model: Claude model to use

    Returns:
        List of test case dicts with 'input' and optional 'annotations' keys
    """
    # Build generation prompt
    domain_yaml = yaml.dump(domain, sort_keys=False, allow_unicode=True) if domain else "None"

    prompt = f"""You are generating test cases for an agent evaluation harness.

# Template

{template}

# Domain Context

The repository-specific context that should inform test case generation:

```yaml
{domain_yaml}
```

# Generation Task

Generate exactly {count} test case(s) following the template instructions above.

**IMPORTANT**: Return ONLY a valid JSON array with no additional text, markdown formatting, or explanation.

Each test case should be a JSON object with:
- "input": dict matching the template's input schema
- "annotations": optional dict with metadata (category, difficulty, etc.)

Example format:
```json
[
  {{
    "input": {{
      "prompt": "User question here",
      "expected_files": ["path/to/doc.md"]
    }},
    "annotations": {{
      "category": "navigation",
      "difficulty": "easy"
    }}
  }}
]
```

Generate realistic, varied test cases that:
1. Use actual paths/topics from the domain context (if available)
2. Cover different difficulty levels
3. Test different aspects of the capability
4. Are specific and verifiable

Return the JSON array now:"""

    # Call Claude API
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        temperature=1.0,  # Higher temperature for variety
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
            f"for category {category.name}",
            file=sys.stderr,
        )

    return cases


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
    # Remove comments (// style)
    cleaned = re.sub(r'//[^\n]*\n', '\n', cleaned)

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
    """CLI for testing taxonomy-based generation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate test cases from taxonomy templates")
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

    args = parser.parse_args()

    # Load config
    config = EvalConfig.from_yaml(args.config)

    if not config.test_categories:
        print(
            "ERROR: No test_categories defined in config. "
            "Taxonomy-based generation requires test_categories.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = Path(args.output)

    if args.dry_run:
        print("Would generate test cases:")
        for category in config.test_categories:
            print(f"  - {category.name}: {category.count} cases from {category.template}")
        total = sum(c.count for c in config.test_categories)
        print(f"\nTotal: {total} test cases")
        print(f"Output: {output_dir}")
        return

    # Generate cases
    try:
        cases = generate_from_taxonomy(
            config=config,
            output_dir=output_dir,
            model=args.model,
        )

        print(f"\nGenerated {len(cases)} test cases:")
        for case in cases:
            print(f"  {case['case_id']}: {case['category']}")

        print(f"\nOutput written to: {output_dir}")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
