#!/bin/bash
# Generate ConfigMap YAML from provider.yaml
# Usage: bash deploy/harbor/generate-configmap.sh > deploy/harbor/configmap-template.yaml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVIDER_YAML="${SCRIPT_DIR}/provider.yaml"

if [ ! -f "$PROVIDER_YAML" ]; then
    echo "Error: provider.yaml not found at $PROVIDER_YAML" >&2
    exit 1
fi

cat <<'HEADER'
# AUTO-GENERATED — do not edit directly.
# Regenerate: bash deploy/harbor/generate-configmap.sh > deploy/harbor/configmap-template.yaml
#
# Source: deploy/harbor/provider.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: evalhub-provider-harbor-bench
  namespace: redhat-ods-applications
  labels:
    app.kubernetes.io/part-of: trustyai
    app.opendatahub.io/trustyai: "true"
    trustyai.opendatahub.io/evalhub-provider-type: system
    trustyai.opendatahub.io/evalhub-provider-name: harbor-bench
    opendatahub.io/managed: "true"
data:
  provider.yaml: |
HEADER

sed 's/^/    /' "$PROVIDER_YAML"
