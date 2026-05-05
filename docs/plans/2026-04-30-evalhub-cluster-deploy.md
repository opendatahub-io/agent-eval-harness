# Plan: Deploy agent-eval-harness EvalHub Provider to jeder-evalhub Cluster

## Context

Live RHOAI 3.4 cluster (`jeder-evalhub`) with EvalHub + MLflow running. The `feat/evalhub-port` branch has a FrameworkAdapter provider. Goal: containerize, deploy, register, and run an end-to-end eval job.

## Status: E2E Pipeline Working

Pod phase `Succeeded` on 2026-04-30. Adapter ran 1 case, exit_code=0, 0.9s. Judge scoring skipped (score.py not in container). Remaining gaps tracked in beads.

## Execution Log

### Step 1: Enable internal image registry route

Registry was already `Managed` on ROSA HCP. Needed to enable the default route:

```bash
oc patch configs.imageregistry.operator.openshift.io cluster --type merge -p '{"spec":{"defaultRoute":true}}'
```

**Registry route**: `default-route-openshift-image-registry.apps.rosa.jeder-evalhub.uqi3.p3.openshiftapps.com`

### Step 2: Create ImageStream (required before push)

**Critical**: OpenShift internal registry requires an ImageStream to exist BEFORE pushing. Without it, layers push successfully but the manifest push fails with HTTP 500.

```bash
oc create imagestream agent-eval-provider -n evalhub
```

### Step 3: Build and push provider container

**Must use `--platform linux/amd64`** on Mac (ARM). Without it, the manifest has no amd64 entry and pods fail with `no image found in image index for architecture amd64`.

```bash
REGISTRY=default-route-openshift-image-registry.apps.rosa.jeder-evalhub.uqi3.p3.openshiftapps.com

docker build --platform linux/amd64 -f provider/Containerfile \
  -t ${REGISTRY}/evalhub/agent-eval-provider:latest .
oc whoami -t | docker login -u $(oc whoami) --password-stdin ${REGISTRY}
docker push ${REGISTRY}/evalhub/agent-eval-provider:latest
```

Internal image ref for pod specs: `image-registry.openshift-image-registry.svc:5000/evalhub/agent-eval-provider:latest`

### Step 4: Test data (baked into container)

EvalHub operator mounts an emptyDir at `/data` in adapter pods, overriding any files at that path. Test data must be at a different path.

**Solution**: Bake smoke test config at `/app/eval-config/` in the Containerfile:
```dockerfile
COPY provider/smoke-test/ /app/eval-config/
```

The `eval.yaml` uses relative `dataset.path: cases` (resolved from `/app/eval-config/`). Entrypoint defaults to `EVAL_CONFIG_PATH=/app/eval-config/eval.yaml`.

PVC approach was also tested (works for data loading with `oc cp`) but is not needed when data is baked in. PVC + data-loader pod kept around for future use.

### Step 5: Vertex AI credentials

```bash
oc create secret generic vertex-ai-credentials \
  --from-file=credentials.json=~/.config/gcloud/application_default_credentials.json \
  -n evalhub
```

**Note**: Credentials are NOT yet mounted into the adapter pod. The smoke test ran using `--print` mode which doesn't require real API calls. Full eval will need env vars: `CLAUDE_CODE_USE_VERTEX=1`, `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, `GOOGLE_APPLICATION_CREDENTIALS`.

### Step 6: Register provider via ConfigMap

**Critical learnings**:
1. ConfigMap must be in `redhat-ods-applications` namespace (NOT evalhub)
2. Must include all TrustyAI labels: `app.kubernetes.io/part-of: trustyai`, `app.opendatahub.io/trustyai: "true"`, `platform.opendatahub.io/part-of: trustyai`
3. YAML format must match built-in providers (bare string metrics, `runtime.k8s.entrypoint` array, `runtime.local.command` field)
4. EvalHub CR `spec.providers[]` must list ALL desired providers (built-in + custom)
5. Pod restart needed after ConfigMap changes
6. Resource requests must be low (100m CPU, 256Mi memory) to fit on a 2-node m5.2xlarge cluster with RHOAI already consuming 95%+ CPU requests

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: evalhub-provider-agent-eval
  namespace: redhat-ods-applications
  labels:
    app.kubernetes.io/part-of: trustyai
    app.opendatahub.io/trustyai: "true"
    platform.opendatahub.io/part-of: trustyai
    trustyai.opendatahub.io/evalhub-provider-type: system
    trustyai.opendatahub.io/evalhub-provider-name: agent-eval
data:
  agent-eval.yaml: |
    id: agent-eval
    name: agent-eval
    title: Agent Skill Evaluation
    description: Evaluate AI coding agent skills against test case datasets with configurable judges.
    runtime:
      k8s:
        image: image-registry.openshift-image-registry.svc:5000/evalhub/agent-eval-provider:latest
        entrypoint:
        - python3
        - entrypoint.py
        cpu_request: 100m
        memory_request: 256Mi
        cpu_limit: 2000m
        memory_limit: 4Gi
      local:
        command: 'true'
    benchmarks:
    - id: skill-eval
      name: Skill Evaluation
      description: Generic agent skill evaluation benchmark
      category: agent-evaluation
      metrics:
      - exit_code
      - duration_seconds
      - cost_usd
      - num_turns
      - num_examples_evaluated
      tags:
      - agents
      - skills
      primary_score:
        metric: exit_code
        lower_is_better: true
```

Update EvalHub CR to include all providers:

```bash
PROVIDERS=$(oc get configmap -n redhat-ods-applications \
  -l trustyai.opendatahub.io/evalhub-provider-name \
  -o jsonpath='{range .items[*]}{.metadata.labels.trustyai\.opendatahub\.io/evalhub-provider-name}{"\n"}{end}')
PROVIDERS_JSON=$(echo "$PROVIDERS" | jq -R . | jq -s .)
oc patch evalhub evalhub -n evalhub --type merge -p "{\"spec\":{\"providers\":$PROVIDERS_JSON}}"
oc delete pod -n evalhub -l app=eval-hub
```

### Step 7: API authentication

EvalHub requires the `evalhub-service` ServiceAccount token (NOT `default` SA). Admin token also works for job submission.

```bash
SA_TOKEN=$(oc create token evalhub-service -n evalhub --duration=30m)
# -k is required: cluster-internal route uses self-signed TLS cert
curl -sk -H "Authorization: Bearer ${SA_TOKEN}" -H "X-Tenant: evalhub" \
  "${EVALHUB_URL}/api/v1/evaluations/providers"
```

The `X-Tenant: evalhub` header is mandatory — without it, all requests return 401.

### Step 8: Submit test job (DONE)

The job submission API requires a `name` field and uses `id` (not `benchmark_id`) in the benchmarks array:

```bash
EVALHUB_URL="https://$(oc get routes evalhub -n evalhub -o jsonpath='{.spec.host}')"

curl -sk -X POST \
  -H "Authorization: Bearer $(oc whoami -t)" \
  -H "X-Tenant: evalhub" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "agent-eval-e2e",
    "model": {"url": "vertex-ai", "name": "claude-sonnet-4-6"},
    "benchmarks": [{"provider_id": "agent-eval", "id": "skill-eval"}],
    "experiment": {"name": "agent-eval-e2e"}
  }' \
  "${EVALHUB_URL}/api/v1/evaluations/jobs"
```

**Result**: Pod `Succeeded`. Adapter ran 1 case (case-001), exit_code=0, 0.9s.

## Code Changes Made

### `provider/entrypoint.py`
- Job spec path: `/meta/job.json` (not `/config/job.json`)
- `DefaultCallbacks` requires `job_id` and `benchmark_id` args
- `JobSpec` has `benchmark_id` attribute (not `benchmarks` list)
- Added comprehensive debug logging

### `agent_eval/evalhub/adapter.py`
- **Fixed indentation bug**: Steps 3-7 (runner, execution, scoring, mapping) were inside the S3 `else` branch, unreachable with local datasets
- Added local dataset support: resolves relative `dataset_path` from eval.yaml parent directory
- Added `DatasetInfo` import for local path
- Added debug logging at every phase

### `provider/Containerfile`
- Added `--platform linux/amd64` note (build-time flag)
- Bakes smoke test at `/app/eval-config/` (not `/data/` which gets overridden)

### `provider/smoke-test/`
- `eval.yaml`: minimal eval config with relative `cases` path
- `cases/case-001/input.yaml`: simple "2+2" prompt

## Gotchas & Lessons

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| `docker push` 500 error | ImageStream missing | `oc create imagestream <name> -n <ns>` before pushing |
| Image `no architecture amd64` | Built on Mac ARM | `docker build --platform linux/amd64` |
| `/data/eval.yaml` not found | Operator mounts emptyDir at `/data` | Use `/app/eval-config/` instead |
| `oc cp` fails | UBI minimal has no `tar` | Use full `ubi:latest` image |
| PVC permission denied | Root-owned mount, non-root pod | initContainer with `runAsUser: 0` to chmod |
| Provider not found | ConfigMap in wrong namespace | Must be in `redhat-ods-applications` |
| Provider not loading | Missing TrustyAI labels | Add `app.kubernetes.io/part-of: trustyai` etc. |
| YAML format mismatch | Metrics as objects not strings | Match built-in format: bare string metrics |
| API 401 unauthorized | Wrong ServiceAccount | Use `evalhub-service` SA, not `default` |
| API 401 bad request | Missing X-Tenant header | Always include `X-Tenant: evalhub` |
| Only custom provider shows | CR spec lists only custom | List ALL providers in `spec.providers[]` |
| `DefaultCallbacks()` TypeError | Requires job_id, benchmark_id | Pass from `spec.id` and `spec.benchmark_id` |
| `spec.benchmarks` AttributeError | Real SDK uses `benchmark_id` | Each pod runs single benchmark |
| Job validation error | Missing `name` field | Include `name` in job submission |
| Adapter returns None | Steps 3-7 inside else branch | Dedent to run after both local and S3 paths |
| `dataset.path` absolute rejected | config.py validation | Use relative path, resolve from eval.yaml dir |
| CPU insufficient scheduling | 95%+ CPU requests on 2 nodes | Reduce provider to 100m CPU request |
| Parameters{} empty in pod | EvalHub strips custom params | Bake config into image; file RFE |

## EvalHub RFE Gaps

1. **Parameter passthrough**: `parameters{}` in job submission not forwarded to benchmark pods
2. **Volume mounts**: No way to declare volumes in provider spec or EvalHub CR
3. **Env var passthrough**: Provider `env[]` exists but job-level env vars can't be set per-submission

## Remaining Work

Tracked in beads. See `bd list --status=open`.
