# Agent Eval — EvalHub Provider

Custom EvalHub provider for evaluating AI coding agent skills on Red Hat OpenShift AI.

The adapter runs the eval **in-process** inside the Job pod created by EvalHub's
server — matching EvalHub's architecture where adapter pods are execution-only
(they don't create sub-pods or call Harbor). Uses `ClaudeCodeRunner` directly.
In-process parallelism (`execution.parallelism` in eval.yaml) handles concurrent
cases within the pod.

## Build

```bash
podman build --platform linux/amd64 -f deploy/evalhub/Containerfile -t quay.io/rhoai/agent-eval-hub:latest .
```

## Push to Internal Registry

```bash
# Create ImageStream first (required before pushing)
oc create imagestream agent-eval-hub -n <namespace>

# Tag and push
podman tag quay.io/rhoai/agent-eval-hub:latest \
  image-registry.openshift-image-registry.svc:5000/<namespace>/agent-eval-hub:latest
podman push image-registry.openshift-image-registry.svc:5000/<namespace>/agent-eval-hub:latest
```

## Register Provider

Providers are registered via ConfigMap in the same namespace as the TrustyAI
operator (typically `redhat-ods-applications`), not the EvalHub CR namespace.
EvalHub discovers providers using two labels (`evalhub-provider-type` and
`evalhub-provider-name`); the remaining labels are standard ODH labels:

```bash
oc apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: evalhub-provider-agent-eval
  namespace: redhat-ods-applications
  labels:
    app.kubernetes.io/part-of: trustyai
    app.opendatahub.io/trustyai: "true"
    trustyai.opendatahub.io/evalhub-provider-type: system
    trustyai.opendatahub.io/evalhub-provider-name: agent-eval
    opendatahub.io/managed: "true"
data:
  provider.yaml: |
    $(cat deploy/evalhub/provider.yaml | sed 's/^/    /')
EOF
```

After applying the ConfigMap, add `agent-eval` to the EvalHub CR `spec.providers[]`
list and restart the EvalHub pod.

## Submit Job

```bash
evalhub eval run --config job-config.yaml
```

## Configuration

The provider expects:
- `eval.yaml` baked into the container or mounted at `/app/eval-config/eval.yaml`
- Test cases in S3 (referenced via `s3_bucket` and `s3_prefix` parameters)
  or baked into the container at `/app/eval-config/cases/`
- Claude Code CLI available in the container (inherited from base image)
- `ANTHROPIC_API_KEY` or Vertex AI credentials as environment variables

## Tests

Adapter unit tests are in `tests/evalhub/` (run with the main test suite).
Smoke test fixture is in `tests/fixtures/evalhub-smoke/`.
