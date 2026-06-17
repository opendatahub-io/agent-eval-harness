#!/usr/bin/env python3
"""Validate eval.yaml and eval.md.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py config [eval.yaml]
    python3 ${CLAUDE_SKILL_DIR}/scripts/validate_eval.py memory [eval.md]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import hashlib
import sys
from pathlib import Path

import yaml

# Import skill lookup from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from find_skills import find_skill


def _extract_template_variables(template_text):
    """Extract variable names from Jinja2 template (e.g., {{ variable }})."""
    import re
    # Match {{ variable }}, {{ variable.field }}, {{ variable['key'] }}, etc.
    # Capture the root variable name only
    pattern = r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)'
    matches = re.findall(pattern, template_text)
    return set(matches)


def _validate_template_variables(judges, outputs, dataset_schema, errors, warnings):
    """Validate that all template variables referenced in judges exist in outputs or are standard."""
    # Build set of available output names
    output_names = {o.get("name", "") for o in outputs if o.get("name")}

    # Standard variables that are always available (loaded by scoring infrastructure)
    standard_vars = {
        "input",        # Loaded from dataset input.yaml
        "annotations",  # Loaded from dataset annotations.yaml
        "conversation", # Common alias for stdout
        "events",       # Tool call log
    }

    had_undefined_vars = False

    # Check each judge's templates
    for j in judges:
        name = j.get("name", "unnamed")
        template_text = ""

        # Collect all template text from this judge
        if j.get("prompt"):
            template_text += j["prompt"]
        if j.get("prompt_file"):
            # Note: prompt_file content isn't available yet, skip for now
            pass
        if j.get("llm_rubric"):
            # llm_rubric might have template variables in criteria
            template_text += str(j["llm_rubric"])

        if not template_text:
            continue

        # Extract variables used in templates
        vars_used = _extract_template_variables(template_text)

        # Check for undefined variables
        available = output_names | standard_vars
        undefined = vars_used - available

        if undefined:
            had_undefined_vars = True
            errors.append(
                f"judges.{name} references undefined template variable(s): {', '.join(sorted(undefined))}. "
                f"Available: {', '.join(sorted(available))}"
            )

        # Check for common mistakes with input/annotations
        if "input" in vars_used:
            # Warn if input.yaml expected fields aren't documented
            if not dataset_schema or "input.yaml" not in dataset_schema:
                warnings.append(
                    f"judges.{name} uses {{{{ input }}}} but dataset.schema doesn't document input.yaml structure. "
                    f"Document expected fields (prompt, expected_api, expected_documentation, etc.)"
                )

        if "annotations" in vars_used:
            # Warn if annotations.yaml expected fields aren't documented
            if not dataset_schema or "annotations.yaml" not in dataset_schema:
                warnings.append(
                    f"judges.{name} uses {{{{ annotations }}}} but dataset.schema doesn't document annotations.yaml structure. "
                    f"Document expected fields (category, expected_files, complexity, etc.)"
                )

    # Add helpful guidance if undefined variables were found
    if had_undefined_vars:
        errors.append(
            "\n💡 Template variable errors detected. Common fixes:\n"
            "   • Ensure all {{ variable }} references match output names or standard variables\n"
            "   • Standard variables (always available): input, annotations, conversation, events\n"
            "   • For custom variables, add them to outputs section in eval.yaml\n"
            "   • Check dataset.schema documents expected structure of input.yaml and annotations.yaml"
        )


def _test_render_judge_templates(judges, outputs, errors, warnings):
    """Test-render judge templates with mock data to catch Jinja2 errors."""
    try:
        from jinja2 import Template, UndefinedError, TemplateSyntaxError
    except ImportError:
        # Jinja2 not available, skip test rendering
        return

    # Build mock outputs dict
    output_names = {o.get("name", "") for o in outputs if o.get("name")}
    mock_data = {
        "conversation": "Mock conversation",
        "events": [],
        "input": {"prompt": "Mock prompt", "expected_documentation": []},
        "annotations": {"category": "test", "expected_files": []},
    }
    # Add all declared outputs
    for name in output_names:
        if name not in mock_data:
            mock_data[name] = "mock_value"

    had_template_error = False
    for j in judges:
        name = j.get("name", "unnamed")
        prompt_text = j.get("prompt", "")

        if not prompt_text:
            continue

        try:
            template = Template(prompt_text)
            # Try to render with mock data
            template.render(**mock_data)
        except UndefinedError as e:
            # This catches variables that aren't in mock_data
            had_template_error = True
            errors.append(
                f"judges.{name} template has undefined variable: {e}. "
                f"This will fail at runtime during scoring."
            )
        except TemplateSyntaxError as e:
            had_template_error = True
            errors.append(
                f"judges.{name} template has syntax error: {e}"
            )
        except Exception as e:
            # Other template errors
            warnings.append(
                f"judges.{name} template validation warning: {e}"
            )

    # Add helpful guidance if template errors were found
    if had_template_error:
        errors.append(
            "\nTemplate variable errors detected. Common fixes:\n"
            "  1. Ensure all {{ variable }} references match output names\n"
            "  2. For dataset files (input.yaml, annotations.yaml), verify they're loaded by scoring\n"
            "  3. Check dataset.schema documents the expected structure\n"
            "  4. Standard variables: input, annotations, conversation, events"
        )


def _validate_builtin_arguments(builtin_name, judge_name, arguments, errors, warnings):
    """Validate that builtin judges only use documented arguments."""
    # Known builtin judges and their valid arguments
    BUILTIN_ARGS = {
        "consulted_docs": {"min_coverage", "match"},
        "cost_budget": {"max_cost_usd"},
        "no_harmful_content": set(),  # No custom arguments
        "output_completeness": set(),  # No custom arguments
        "tool_call_validation": {"required_tools", "forbidden_tools"},
    }

    valid_args = BUILTIN_ARGS.get(builtin_name)
    if valid_args is None:
        # Unknown builtin - this might be a new one we haven't documented
        warnings.append(
            f"judges.{judge_name} uses unknown builtin '{builtin_name}'. "
            f"Known builtins: {', '.join(sorted(BUILTIN_ARGS.keys()))}"
        )
        return

    invalid_args = set(arguments.keys()) - valid_args
    if invalid_args:
        errors.append(
            f"judges.{judge_name} (builtin:{builtin_name}) has invalid argument(s): {', '.join(sorted(invalid_args))}. "
            f"Valid arguments for {builtin_name}: {', '.join(sorted(valid_args)) if valid_args else 'none'}"
        )

    # Specific validation for consulted_docs
    if builtin_name == "consulted_docs":
        # Common mistake: using required_paths instead of relying on annotations.expected_files
        if "required_paths" in arguments:
            errors.append(
                f"judges.{judge_name} uses 'required_paths' argument, which doesn't exist for consulted_docs. "
                f"Remove this argument - consulted_docs reads from annotations.expected_files in the dataset."
            )


def _validate_field_consistency(config, judges, dataset_path, config_dir, errors, warnings):
    """Check for field name consistency between judges and dataset annotations."""
    # Check if consulted_docs is used
    has_consulted_docs = any(j.get("builtin") == "consulted_docs" for j in judges)

    # Check if any LLM judges reference expected_paths
    llm_judges_with_expected_paths = []
    for j in judges:
        name = j.get("name", "unnamed")
        prompt = j.get("prompt", "") + j.get("prompt_file", "")
        if "expected_paths" in prompt:
            llm_judges_with_expected_paths.append(name)

    if has_consulted_docs and llm_judges_with_expected_paths:
        warnings.append(
            f"Inconsistent field names: consulted_docs expects 'annotations.expected_files' "
            f"but these judges reference 'expected_paths': {', '.join(llm_judges_with_expected_paths)}. "
            f"Use 'expected_files' consistently for documentation path annotations."
        )

    # Sample dataset cases to check what fields they actually use
    if dataset_path:
        dp = Path(dataset_path) if Path(dataset_path).is_absolute() else config_dir / dataset_path
        if dp.exists() and dp.is_dir():
            sample_cases = [d for d in dp.iterdir() if d.is_dir() and not d.name.startswith(".")][:3]
            uses_expected_paths = False
            uses_expected_files = False

            for case_dir in sample_cases:
                ann_file = case_dir / "annotations.yaml"
                if ann_file.exists():
                    try:
                        with open(ann_file) as f:
                            ann = yaml.safe_load(f) or {}
                            if "expected_paths" in ann:
                                uses_expected_paths = True
                            if "expected_files" in ann:
                                uses_expected_files = True
                    except (yaml.YAMLError, OSError):
                        pass

            if has_consulted_docs and uses_expected_paths and not uses_expected_files:
                errors.append(
                    f"Dataset cases use 'expected_paths' but consulted_docs judge expects 'expected_files'. "
                    f"Rename the field in all {dataset_path}/*/annotations.yaml files."
                )

            if uses_expected_paths and llm_judges_with_expected_paths:
                # Both dataset and judges use expected_paths, but should use expected_files
                warnings.append(
                    f"Dataset and judges use non-standard field 'expected_paths'. "
                    f"Consider renaming to 'expected_files' (the standard field for doc paths)."
                )


def _validate_prompt_mode_config(config, dataset, test_categories, config_dir, errors, warnings):
    """Validate prompt-mode specific configuration.

    This includes taxonomy-based generation and domain-specific validation
    (e.g., documentation_structure for documentation templates).
    """
    # --- Taxonomy-based generation checks ---
    if test_categories:
        # Check for extra fields that will be silently dropped by TestCategory dataclass
        known_fields = {"name", "template", "count", "description"}
        for i, tc in enumerate(test_categories):
            if not isinstance(tc, dict):
                continue
            extra_fields = set(tc.keys()) - known_fields
            if extra_fields:
                warnings.append(
                    f"test_categories[{i}] ({tc.get('name', 'unnamed')}) has extra fields that will be ignored during generation: {', '.join(sorted(extra_fields))}"
                )
                warnings.append(
                    f"  Hint: Move domain-specific metadata (test_prompts, apis, constraints, etc.) to dataset.domain section"
                )

        # Check if dataset.domain is populated for taxonomy-based generation
        domain = dataset.get("domain", {})
        if not domain or (isinstance(domain, dict) and not domain):
            warnings.append(
                "Taxonomy-based generation detected (test_categories present) but dataset.domain is empty. "
                "LLM will generate generic test cases without repository-specific context."
            )
            warnings.append(
                "  Hint: Add dataset.domain section with constraints, apis, components, or documentation_structure"
            )

        # --- Documentation-specific validation (conditional) ---
        # Only validate documentation_structure when documentation templates are actually used
        uses_doc_templates = any(
            isinstance(tc, dict) and
            tc.get("template", "").startswith(("documentation/", "builtin:"))
            for tc in test_categories
        )

        if uses_doc_templates and isinstance(domain, dict):
            doc_structure = domain.get("documentation_structure", {})
            entry_point = doc_structure.get("entry_point", "")
            if entry_point:
                ep = Path(entry_point)
                if not ep.exists():
                    errors.append(
                        f"dataset.domain.documentation_structure.entry_point '{entry_point}' does not exist"
                    )
            elif not doc_structure:
                warnings.append(
                    "Documentation templates detected but dataset.domain.documentation_structure is not defined. "
                    "Test generation may produce generic documentation test cases without repository-specific structure."
                )


def validate_config(path="eval.yaml"):
    """Validate eval.yaml — structure, completeness, and file references."""
    p = Path(path)
    if not p.exists():
        print(f"NOT_FOUND: {path}")
        sys.exit(1)

    # First, check YAML syntax with safe_load
    with open(p) as f:
        try:
            config = yaml.safe_load(f) or {}
        except yaml.scanner.ScannerError as e:
            print(f"YAML_SYNTAX_ERROR: {path}")
            print(f"  {e}")
            print("\nTip: Look for unquoted strings in lists, especially with '()', '-', or ':'")
            print("  Bad:  - field (type)")
            print("  Good: - \"field (type)\"")
            sys.exit(1)

    config_dir = p.resolve().parent

    errors = []
    warnings = []

    # Second, validate schema with EvalConfig
    try:
        from agent_eval.config import EvalConfig
        config_obj = EvalConfig.from_yaml(str(p))
        # Schema validation passed - use the validated config
        # (EvalConfig doesn't have to_dict(), so we keep using the yaml.safe_load config)
    except ValueError as e:
        # EvalConfig validation failed - this is a critical schema error
        error_msg = str(e)
        # Make the error message more user-friendly
        if "test_categories" in error_msg and "template" in error_msg:
            errors.append(f"Schema validation failed: {error_msg}")
            errors.append("Hint: Each test_categories entry needs a 'template' field (e.g., 'documentation/navigation')")
        else:
            errors.append(f"Schema validation failed: {error_msg}")
    except ImportError as e:
        # EvalConfig not available - skip schema validation (shouldn't happen with agent_eval._bootstrap)
        warnings.append(f"Could not import EvalConfig for schema validation: {e}")

    # --- Structure checks ---
    if not config.get("name"):
        errors.append("Missing 'name' field")

    # Either execution.skill or execution.prompt must be set (with top-level skill as fallback)
    execution = config.get("execution", {})
    has_exec_skill = bool(execution.get("skill", "").strip())
    has_exec_prompt = bool(execution.get("prompt", "").strip())
    has_top_level_skill = bool(config.get("skill", "").strip())

    # This check is now redundant with the ExecutionConfig validation but kept for backward compat
    if not has_exec_skill and not has_exec_prompt and not has_top_level_skill:
        errors.append("Either execution.skill or execution.prompt must be set (or top-level 'skill' for backward compat)")

    dataset = config.get("dataset", {})
    outputs = config.get("outputs", [])
    judges = config.get("judges", [])

    if not dataset.get("path"):
        warnings.append("No dataset.path — eval-run won't find test cases")
    if not dataset.get("schema"):
        warnings.append("No dataset.schema — agents won't understand case structure")
    if not outputs:
        warnings.append("No outputs — collect step won't know where to find artifacts")
    if not judges:
        warnings.append("No judges — scoring step will have nothing to run")

    # --- Prompt-mode validation (taxonomy-based generation, documentation templates) ---
    test_categories = dataset.get("test_categories", [])
    _validate_prompt_mode_config(config, dataset, test_categories, config_dir, errors, warnings)

    # --- Skill reference check ---
    # Check execution.skill first, then fall back to top-level skill
    skill_name = execution.get("skill", "") or config.get("skill", "")
    if skill_name and not find_skill(skill_name):
        warnings.append(f"skill '{skill_name}' not found in project")

    # --- File reference checks (resolve relative to config file location) ---
    dataset_path = dataset.get("path", "")
    if dataset_path:
        dp = Path(dataset_path) if Path(dataset_path).is_absolute() else config_dir / dataset_path
        if not dp.exists():
            warnings.append(f"dataset.path '{dataset_path}' does not exist (run /eval-dataset to generate)")
        elif not any(p for p in dp.iterdir() if not p.name.startswith(".")):
            warnings.append(f"dataset.path '{dataset_path}' is empty (run /eval-dataset to generate cases)")

    for i, o in enumerate(outputs):
        out_path = o.get("path", "")
        out_name = o.get("name", f"outputs[{i}]")
        out_from = o.get("from", "")

        # Check for 'from: dataset' usage (advanced feature)
        if out_from == "dataset":
            warnings.append(
                f"outputs.{out_name} uses 'from: dataset' - ensure scoring infrastructure supports this. "
                f"Dataset files (input.yaml, annotations.yaml) may be loaded automatically without explicit output declaration."
            )
        elif out_from and out_from != "dataset":
            errors.append(
                f"outputs.{out_name}.from has unknown value '{out_from}'. Valid values: 'dataset'"
            )

        if out_path:
            op = Path(out_path)
            if op.is_absolute():
                errors.append(f"outputs[{i}].path must be relative: {out_path}")
            elif ".." in op.parts:
                errors.append(f"outputs[{i}].path must not traverse parent: {out_path}")

    # Valid judge fields
    valid_judge_fields = {
        "name", "description", "builtin", "check", "prompt", "prompt_file",
        "module", "function", "arguments", "context", "model", "if", "llm_rubric"
    }

    for j in judges:
        name = j.get("name", "unnamed")

        # Check for unknown fields (common mistake: scoring, validation)
        unknown_fields = set(j.keys()) - valid_judge_fields
        if unknown_fields:
            errors.append(
                f"judges.{name} has unknown field(s): {', '.join(sorted(unknown_fields))}. "
                f"Valid fields: builtin, check, prompt, prompt_file, module+function, llm_rubric"
            )

        # Check that exactly one implementation type is specified
        impl_types = []
        if j.get("builtin"):
            impl_types.append("builtin")
        if j.get("check"):
            impl_types.append("check")
        if j.get("prompt"):
            impl_types.append("prompt")
        if j.get("prompt_file"):
            impl_types.append("prompt_file")
        if j.get("llm_rubric"):
            impl_types.append("llm_rubric")
        if j.get("module"):
            impl_types.append("module")

        if len(impl_types) == 0:
            errors.append(
                f"judges.{name} missing implementation. "
                f"Must have one of: builtin, check, prompt, prompt_file, llm_rubric, or module+function"
            )
        elif len(impl_types) > 1:
            errors.append(
                f"judges.{name} has multiple implementations: {', '.join(impl_types)}. "
                f"Choose exactly one."
            )

        # Module judges must also have function
        if j.get("module") and not j.get("function"):
            errors.append(f"judges.{name} has 'module' but missing 'function'")

        # Check for common mistakes in inline check judges
        check_code = j.get("check", "")
        if check_code:
            import re
            # Flag bare usage of annotations/conversation (should be outputs.get("annotations"/"conversation"))
            bare_annotations = re.search(r'\bannotations\s*\.', check_code)
            bare_conversation = re.search(r'\bconversation\b(?!\s*=)', check_code)

            if bare_annotations:
                errors.append(
                    f"judges.{name}.check uses bare 'annotations' — "
                    f"must use outputs.get(\"annotations\", {{}}) instead. "
                    f"See eval-yaml-template.md for correct pattern."
                )
            if bare_conversation:
                errors.append(
                    f"judges.{name}.check uses bare 'conversation' — "
                    f"must use outputs.get(\"conversation\", \"\") instead. "
                    f"See eval-yaml-template.md for correct pattern."
                )

        # File reference checks
        prompt_file = j.get("prompt_file", "")
        if prompt_file:
            pf = Path(prompt_file) if Path(prompt_file).is_absolute() else config_dir / prompt_file
            if not pf.exists():
                errors.append(f"judges.{name}.prompt_file '{prompt_file}' not found")
        for ctx_file in j.get("context", []):
            cf = Path(ctx_file) if Path(ctx_file).is_absolute() else config_dir / ctx_file
            if not cf.exists():
                warnings.append(f"judges.{name}.context '{ctx_file}' not found")
        module = j.get("module", "")
        if module:
            try:
                import importlib
                importlib.import_module(module)
            except ImportError:
                errors.append(f"judges.{name}.module '{module}' not importable")

        # Validate builtin judge arguments
        builtin_name = j.get("builtin", "")
        if builtin_name:
            arguments = j.get("arguments", {})
            _validate_builtin_arguments(builtin_name, name, arguments, errors, warnings)

    # --- Execution config ---
    execution = config.get("execution", {})
    exec_mode = execution.get("mode", "case")
    if exec_mode not in ("case", "batch"):
        errors.append(f"execution.mode must be 'case' or 'batch', got '{exec_mode}'")

    # Check mutual exclusivity of skill and prompt
    has_skill = bool(execution.get("skill", "").strip())
    has_prompt = bool(execution.get("prompt", "").strip())
    top_level_skill = bool(config.get("skill", "").strip())

    if has_skill and has_prompt:
        errors.append(
            "execution.skill and execution.prompt are mutually exclusive. "
            "Use execution.skill for '/skill-name' invocations or execution.prompt for direct prompts."
        )

    # At least one of skill/prompt must be set (with top-level skill as fallback)
    if not has_skill and not has_prompt and not top_level_skill:
        errors.append(
            "Either execution.skill or execution.prompt must be set. "
            "Use execution.skill for skill invocations, execution.prompt for direct prompt mode."
        )

    # Warn if arguments missing for skill mode
    if (has_skill or top_level_skill) and not has_prompt and not execution.get("arguments"):
        warnings.append("No execution.arguments — skill will be invoked with no arguments")

    # For prompt mode, the prompt template should be valid
    if has_prompt:
        prompt = execution.get("prompt", "")
        if not prompt.strip():
            errors.append("execution.prompt is set but empty")

    # --- Inputs (tool interception) ---
    for t in (config.get("inputs", {}).get("tools") or []):
        if not t.get("match"):
            warnings.append("inputs.tools entry missing 'match' field")
        if not t.get("prompt") and not t.get("prompt_file"):
            warnings.append(f"inputs.tools entry '{t.get('match', '?')[:30]}' has no prompt")
        prompt_file = t.get("prompt_file", "")
        if prompt_file:
            pf = Path(prompt_file) if Path(prompt_file).is_absolute() else config_dir / prompt_file
            if not pf.exists():
                errors.append(f"inputs.tools prompt_file '{prompt_file}' not found")

    runner = config.get("runner") or {}
    settings = runner.get("settings")
    if isinstance(settings, str) and settings:
        sp = Path(settings) if Path(settings).is_absolute() else config_dir / settings
        if not sp.exists():
            errors.append(f"runner.settings '{settings}' not found")

    # --- Models ---
    models = config.get("models") or {}
    if not models.get("skill"):
        warnings.append("No models.skill — eval-run will require --model on every invocation")
    if not models.get("judge"):
        warnings.append("No models.judge — LLM/pairwise judges will need EVAL_JUDGE_MODEL or per-judge 'model:'")

    # --- Thresholds ---
    thresholds = config.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        errors.append("thresholds must be a mapping of <judge_name>: <threshold_config>")
        thresholds = {}
    judge_names = {j.get("name", "") for j in judges if isinstance(j, dict) and j.get("name")}
    for thresh_name in thresholds:
        if thresh_name not in judge_names:
            warnings.append(
                f"thresholds.{thresh_name} references non-existent judge "
                f"(available: {', '.join(sorted(judge_names)) or 'none'})")

    # --- Field name consistency checks ---
    _validate_field_consistency(config, judges, dataset_path if dataset_path else None,
                                config_dir, errors, warnings)

    # --- Template variable validation ---
    dataset_schema = dataset.get("schema", "")
    _validate_template_variables(judges, outputs, dataset_schema, errors, warnings)

    # --- Test-render judge templates ---
    _test_render_judge_templates(judges, outputs, errors, warnings)

    # --- Report ---
    if errors:
        for e in errors:
            print(f"ERROR: {e}")

    status = "VALID"
    if errors:
        status = "INVALID"
    elif warnings:
        status = "INCOMPLETE"

    mlflow = config.get("mlflow") or {}
    skill_display = config.get("skill") or f"mode={exec_mode}"
    print(f"{status}: {config.get('name')} (skill={skill_display})")
    print(f"  execution: mode={exec_mode}, arguments={'yes' if execution.get('arguments') else 'no'}")
    print(f"  runner: {runner.get('type', 'claude-code')}")
    print(f"  models: skill={models.get('skill', 'unset')}, judge={models.get('judge', 'unset')}")
    print(f"  mlflow: experiment={mlflow.get('experiment') or config.get('name', 'unset')}")
    print(f"  dataset: {dataset.get('path', 'not set')}")
    print(f"  schema: {'yes' if dataset.get('schema') else 'no'}")
    print(f"  outputs: {len(outputs)} directories")
    print(f"  judges: {len(judges)}")

    for w in warnings:
        print(f"  WARNING: {w}")

    if errors:
        sys.exit(1)


def validate_memory(path="eval.md"):
    """Check if eval.md is fresh (skill or documentation hasn't changed)."""
    p = Path(path)
    if not p.exists():
        print("STALE: eval.md does not exist")
        sys.exit(1)

    content = p.read_text()
    if not content.startswith("---"):
        print("STALE: no frontmatter")
        sys.exit(1)

    parts = content.split("---", 2)
    if len(parts) < 3:
        print("STALE: invalid frontmatter")
        sys.exit(1)

    fm = yaml.safe_load(parts[1]) or {}
    eval_type = fm.get("type", "")

    # Handle documentation-based evals
    if eval_type == "documentation-eval":
        stored_hash = fm.get("documentation_hash", "")
        if not stored_hash:
            print("STALE: missing documentation_hash in frontmatter")
            sys.exit(1)

        # Check CLAUDE.md or AGENTS.md (prefer CLAUDE.md)
        doc_path = Path("CLAUDE.md") if Path("CLAUDE.md").exists() else Path("AGENTS.md")
        if not doc_path.exists():
            print("STALE: no CLAUDE.md or AGENTS.md found")
            sys.exit(1)

        current_hash = hashlib.sha256(doc_path.read_bytes()).hexdigest()[:12]
        if current_hash == stored_hash:
            print(f"FRESH: documentation-eval (hash={stored_hash})")
        else:
            print(f"STALE: documentation changed ({stored_hash} -> {current_hash})")
            sys.exit(1)
        return

    # Handle skill-based evals
    skill_name = fm.get("skill", "")
    stored_hash = fm.get("skill_hash", "")

    if not skill_name or not stored_hash:
        print("STALE: missing skill or hash in frontmatter")
        sys.exit(1)

    skill_path = find_skill(skill_name)
    if not skill_path:
        print(f"STALE: skill '{skill_name}' not found")
        sys.exit(1)

    current_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest()[:12]
    if current_hash == stored_hash:
        print(f"FRESH: {skill_name} (hash={stored_hash})")
    else:
        print(f"STALE: skill changed ({stored_hash} -> {current_hash})")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_eval.py <config|memory> [path]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "config":
        path = sys.argv[2] if len(sys.argv) > 2 else "eval.yaml"
        validate_config(path)
    elif cmd == "memory":
        path = sys.argv[2] if len(sys.argv) > 2 else "eval.md"
        validate_memory(path)
    else:
        print(f"Unknown: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
