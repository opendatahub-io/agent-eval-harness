"""Create Kubernetes resources (ConfigMaps, Secrets) for eval runs.

Shared utility for both the Harbor and EvalHub paths — creates the K8s resources
that trial/job pods mount. Uses the Kubernetes Python client (same as
``kubernetes.py``), so it works from a laptop (kubeconfig) and in-cluster
(ServiceAccount token).

The resources created:
- **Project ConfigMap** — skills, scripts, .context, CLAUDE.md (mounted into
  trial pods so the agent finds its helpers)
- **Eval ConfigMap** — eval.yaml + tool_handlers.yaml (mounted so the verifier
  and EvalHub adapter find the config)
- **Credentials Secret** — GCP service-account key or API keys (mounted
  read-only for model auth)

All resources are labeled ``app.kubernetes.io/managed-by: agent-eval-harness``
for easy cleanup.
"""

import base64
import logging
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException
    _K8S_AVAILABLE = True
except ImportError:
    _K8S_AVAILABLE = False
    ApiException = Exception  # type: ignore[assignment,misc]

_LABELS = {"app.kubernetes.io/managed-by": "agent-eval-harness"}
_MAX_CONFIGMAP_SIZE = 1_000_000  # 1 MB etcd limit


def _ensure_client() -> k8s_client.CoreV1Api:
    if not _K8S_AVAILABLE:
        raise RuntimeError(
            "The 'kubernetes' package is required. "
            "pip install 'agent-eval-harness[harbor]' or pip install kubernetes")
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return k8s_client.CoreV1Api()


def _collect_files(root: Path, extensions: set[str] | None = None) -> dict[str, str]:
    """Recursively collect text files under root into a flat {key: content} dict.

    Keys use ``--`` as a path separator (ConfigMap keys can't contain ``/``).
    Only text files with the given extensions are included. Symlinks, binary
    files, and files larger than 500 KB are skipped.
    """
    files: dict[str, str] = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if extensions and path.suffix not in extensions:
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if len(content) > 500_000:
            log.warning("Skipping large file %s (%d bytes)", path, len(content))
            continue
        key = str(path.relative_to(root)).replace("/", "--")
        files[key] = content
    return files


def _apply_configmap(
    core: k8s_client.CoreV1Api, name: str, namespace: str,
    data: dict[str, str], labels: dict | None = None,
) -> None:
    """Create or update a ConfigMap."""
    total = sum(len(v) for v in data.values())
    if total > _MAX_CONFIGMAP_SIZE:
        raise ValueError(
            f"ConfigMap '{name}' would be {total:,} bytes, exceeding the "
            f"{_MAX_CONFIGMAP_SIZE:,} byte etcd limit. Reduce the content or "
            f"split across multiple ConfigMaps.")

    merged_labels = {**_LABELS, **(labels or {})}
    cm = k8s_client.V1ConfigMap(
        metadata=k8s_client.V1ObjectMeta(
            name=name, namespace=namespace, labels=merged_labels),
        data=data,
    )
    try:
        core.create_namespaced_config_map(namespace, cm)
        log.info("Created ConfigMap %s/%s (%d keys, %d bytes)",
                 namespace, name, len(data), total)
    except ApiException as exc:
        if exc.status == 409:
            core.replace_namespaced_config_map(name, namespace, cm)
            log.info("Updated ConfigMap %s/%s (%d keys, %d bytes)",
                     namespace, name, len(data), total)
        else:
            raise


def _apply_secret(
    core: k8s_client.CoreV1Api, name: str, namespace: str,
    data: dict[str, bytes], labels: dict | None = None,
) -> None:
    """Create or update an opaque Secret."""
    merged_labels = {**_LABELS, **(labels or {})}
    secret = k8s_client.V1Secret(
        metadata=k8s_client.V1ObjectMeta(
            name=name, namespace=namespace, labels=merged_labels),
        data={k: base64.b64encode(v).decode() for k, v in data.items()},
        type="Opaque",
    )
    try:
        core.create_namespaced_secret(namespace, secret)
        log.info("Created Secret %s/%s (%d keys)", namespace, name, len(data))
    except ApiException as exc:
        if exc.status == 409:
            core.replace_namespaced_secret(name, namespace, secret)
            log.info("Updated Secret %s/%s (%d keys)", namespace, name, len(data))
        else:
            raise


# --- Public API ---------------------------------------------------------------

_TEXT_EXTENSIONS = {
    ".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".sh",
    ".j2", ".jinja", ".jinja2", ".tmpl", ".cfg", ".ini", ".env",
}


def create_project_configmap(
    project_dir: Path, name: str, namespace: str,
) -> None:
    """Create a ConfigMap from project resources (skills, scripts, .context).

    Collects text files from subdirectories that agents typically need:
    ``.claude/skills/``, ``scripts/``, ``.context/``, and ``CLAUDE.md``.
    Keys use ``--`` as path separators (ConfigMap keys can't contain ``/``).
    """
    core = _ensure_client()
    project_dir = Path(project_dir)
    data: dict[str, str] = {}

    for subdir in (".claude/skills", "scripts", ".context"):
        path = project_dir / subdir
        if path.is_dir():
            for key, content in _collect_files(path, _TEXT_EXTENSIONS).items():
                prefixed_key = subdir.replace("/", "--") + "--" + key
                data[prefixed_key] = content

    claude_md = project_dir / "CLAUDE.md"
    if claude_md.is_file():
        data["CLAUDE.md"] = claude_md.read_text()

    if not data:
        log.warning("No project resources found in %s", project_dir)
        return

    _apply_configmap(core, name, namespace, data)


def create_eval_configmap(
    eval_yaml_path: Path, name: str, namespace: str,
) -> None:
    """Create a ConfigMap from eval.yaml + tool_handlers.yaml (if present).

    The ConfigMap is mounted into the pod so the verifier (reward bridge) and
    the EvalHub adapter find the eval config without baking it into the image.
    """
    core = _ensure_client()
    eval_yaml_path = Path(eval_yaml_path)
    data: dict[str, str] = {"eval.yaml": eval_yaml_path.read_text()}

    handlers = eval_yaml_path.parent / "tool_handlers.yaml"
    if handlers.is_file():
        data["tool_handlers.yaml"] = handlers.read_text()

    _apply_configmap(core, name, namespace, data)


def create_creds_secret(
    creds_file: Path, name: str, namespace: str, key: str = "key.json",
) -> None:
    """Create a Secret from a credentials file (e.g. GCP service-account key).

    Mounted into the pod at ``AGENT_EVAL_K8S_CREDS_MOUNT`` with
    ``GOOGLE_APPLICATION_CREDENTIALS`` pointing at ``<mount>/<key>``.
    """
    core = _ensure_client()
    data = {key: Path(creds_file).read_bytes()}
    _apply_secret(core, name, namespace, data)


def create_env_secret(
    env_vars: dict[str, str], name: str, namespace: str,
) -> None:
    """Create a Secret from key-value pairs (e.g. ANTHROPIC_API_KEY).

    Injected into the pod via ``envFrom`` (``AGENT_EVAL_K8S_ENV_SECRET``).
    """
    core = _ensure_client()
    data = {k: v.encode() for k, v in env_vars.items()}
    _apply_secret(core, name, namespace, data)


def cleanup(namespace: str) -> int:
    """Delete all ConfigMaps and Secrets created by agent-eval-harness."""
    core = _ensure_client()
    selector = "app.kubernetes.io/managed-by=agent-eval-harness"
    deleted = 0
    for cm in core.list_namespaced_config_map(
            namespace, label_selector=selector).items:
        core.delete_namespaced_config_map(cm.metadata.name, namespace)
        deleted += 1
    for secret in core.list_namespaced_secret(
            namespace, label_selector=selector).items:
        core.delete_namespaced_secret(secret.metadata.name, namespace)
        deleted += 1
    if deleted:
        log.info("Deleted %d resource(s) in %s", deleted, namespace)
    return deleted
