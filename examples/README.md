# Example Evaluation Recipes

This directory contains domain-specific evaluation recipe examples that users can reference or adapt for their own projects.

## Available Examples

### openshift-agentic-docs.md

**Domain**: OpenShift/Kubernetes operator and platform documentation

**Description**: A specialized documentation analysis recipe for OpenShift ecosystem projects. Uses domain-specific terminology (CRDs, Operators, status conditions, webhooks, MachineConfig, etc.) and includes examples relevant to OpenShift operator repositories.

**Usage**:
```bash
/eval-analyze --prompt examples/openshift-agentic-docs.md
```

**When to use**:
- Evaluating OpenShift Operator documentation
- Testing OpenShift platform component docs (machine-config-operator, etc.)
- Projects using CustomResourceDefinitions (CRDs)
- Repos with operator-pattern documentation

**Domain-specific elements**:
- Repository type examples include "Operator repos"
- Domain topics include "operator-patterns", "status-conditions", "webhooks"
- API examples reference MachineConfig, KubeletConfig, etc.
- Schema examples use YAML (common in OpenShift/K8s ecosystem)

---

## Creating Your Own Recipe

Adapt existing recipes for your domain:

1. **Start from an example**: Copy `openshift-agentic-docs.md` as a template
2. **Adapt terminology**: Replace domain-specific terms with your ecosystem's language
3. **Update examples**: Change repository types, API names, and topics to match your domain
4. **Save locally**: Store in your project's `eval/` or `examples/` directory
5. **Reference it**: Use `--prompt path/to/your-recipe.md`

### What to customize:

- **Repository Type examples** (Step 2): "Operator repos" → "Service repos", "Plugin repos", etc.
- **Domain topics** (Step 3.1): "operator-patterns" → your domain's patterns
- **API terminology** (Step 3.3): "CRDs" → your data models/schemas
- **Example types**: yaml, json, protobuf, etc.

### What to keep:

- The overall methodology (discover → classify → extract → categories → schema → judges → config)
- Steps 1-7 structure
- Pointers to `list_templates.py`, `list_builtins.py`, and `eval-yaml-template.md`

---

## When to Customize

**Use an example as-is** when:
- Your domain closely matches an existing example (e.g., OpenShift/K8s operators)
- Getting started quickly
- Prototyping evaluation setup

**Create a custom recipe** when:
- Your domain has specialized terminology that agents should learn
- Test categories should reflect domain-specific capabilities
- Examples and schemas benefit from domain context
- Generic patterns don't capture your documentation structure
