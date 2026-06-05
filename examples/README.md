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

**Extends**: The core `builtin:docs` methodology with OpenShift-specific:
- Type B categorization includes "Operator repos"
- Domain topics include "operator-patterns", "status-conditions", "webhooks"
- API examples reference MachineConfig, KubeletConfig, etc.
- Schema examples use YAML (common in OpenShift/K8s ecosystem)

---

## Creating Your Own Recipe

Domain-specific recipes extend the core `builtin:docs` methodology for your ecosystem:

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

## Generic vs Domain-Specific

**Use `builtin:docs`** (generic) when:
- First time evaluating any documentation
- No strong domain-specific patterns
- Want neutral, adaptable output

**Use a domain recipe** when:
- Your domain has specialized terminology that agents should learn
- Test categories should reflect domain-specific capabilities
- Examples and schemas benefit from domain context
