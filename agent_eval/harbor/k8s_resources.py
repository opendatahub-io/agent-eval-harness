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
- **Per-run OpenRouter Secret** — at ``enforcement: key-guardrail`` (spec 014),
  ``agent-eval-<run_id>-openrouter`` holding only the per-run inference key,
  created before ``harbor run`` and deleted with the key revoke

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
    total = sum(len(v.encode("utf-8")) for v in data.values())
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

    Mounted read-only into the pod with ``GOOGLE_APPLICATION_CREDENTIALS``
    pointing at the key file.
    """
    core = _ensure_client()
    data = {key: Path(creds_file).read_bytes()}
    _apply_secret(core, name, namespace, data)


def create_env_secret(
    env_vars: dict[str, str], name: str, namespace: str,
) -> None:
    """Create a Secret from key-value pairs (e.g. ANTHROPIC_API_KEY).

    Injected into the pod via ``envFrom`` (``AGENT_EVAL_K8S_CREDENTIALS_SECRET``).
    """
    core = _ensure_client()
    data = {k: v.encode() for k, v in env_vars.items()}
    _apply_secret(core, name, namespace, data)


OWNER_LABEL = "agent-eval.opendatahub.io/owner"


def create_openrouter_secret(api_key: str, name: str, namespace: str,
                             key: str = "OPENROUTER_API_KEY", owner: str | None = None) -> str:
    """Create the per-run Secret holding only the OpenRouter inference key
    (spec 014, ``key-guardrail`` on Kubernetes). The pod maps it to
    ``ANTHROPIC_AUTH_TOKEN`` through ``valueFrom.secretKeyRef``; the value is
    never logged. **Create only**: a name clash (409) is an error, never a
    replacement of a Secret another run may still be using. ``owner`` (a
    per-invocation token) is stamped as a label so the matching delete can
    prove the Secret is this run's. Returns the Secret's UID."""
    core = _ensure_client()
    labels = {**_LABELS, **({OWNER_LABEL: owner} if owner else {})}
    secret = k8s_client.V1Secret(
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        data={key: base64.b64encode(api_key.encode()).decode()},
        type="Opaque",
    )
    try:
        created = core.create_namespaced_secret(namespace, secret)
    except ApiException as exc:
        if getattr(exc, "status", None) == 409:
            raise RuntimeError(
                f"Secret {namespace}/{name} already exists — a previous run with the same run id "
                "did not clean up (or is still running). Delete it, or use another --output "
                "name, before retrying") from None
        raise
    log.info("Created Secret %s/%s (per-run OpenRouter key)", namespace, name)
    uid = getattr(getattr(created, "metadata", None), "uid", None)
    return uid or ""


def delete_openrouter_secret(name: str, namespace: str, *, owner: str | None = None,
                             uid: str | None = None) -> bool:
    """Delete the per-run Secret — only when it is this run's. With ``owner``
    the Secret is read first and deleted only if its owner label matches; the
    delete carries the observed UID as a precondition, so a Secret re-created
    under the same name by another run is never removed. A missing Secret
    (404) or a failed precondition (409/412) is not an error — the delete runs
    from the session's cleanups on every exit path, including a startup that
    failed before the Secret existed. Returns True when deleted."""
    core = _ensure_client()
    try:
        if owner is not None or uid is None:
            current = core.read_namespaced_secret(name, namespace)
            labels = getattr(getattr(current, "metadata", None), "labels", None) or {}
            if owner is not None and labels.get(OWNER_LABEL) != owner:
                log.warning("Secret %s/%s is not this run's (owner label mismatch); left in place",
                            namespace, name)
                return False
            uid = getattr(getattr(current, "metadata", None), "uid", None) or uid
        body = k8s_client.V1DeleteOptions(preconditions=k8s_client.V1Preconditions(uid=uid)) if uid else None
        if body is not None:
            core.delete_namespaced_secret(name, namespace, body=body)
        else:
            core.delete_namespaced_secret(name, namespace)
    except ApiException as exc:
        if getattr(exc, "status", None) in (404, 409, 412):
            return False
        raise
    log.info("Deleted Secret %s/%s", namespace, name)
    return True


def default_namespace() -> str:
    """The namespace the Harbor environment will use: ``AGENT_EVAL_K8S_NAMESPACE``,
    else the in-cluster service-account namespace, else the active kubeconfig
    context, else ``default`` (mirrors ``kubernetes._default_namespace``)."""
    import os

    ns = os.environ.get("AGENT_EVAL_K8S_NAMESPACE")
    if ns:
        return ns
    sa_ns = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if sa_ns.is_file():
        try:
            return sa_ns.read_text().strip() or "default"
        except OSError:
            pass
    if _K8S_AVAILABLE:
        try:
            _, active = k8s_config.list_kube_config_contexts()
            ns = (active or {}).get("context", {}).get("namespace")
            if ns:
                return ns
        except Exception:
            pass
    return "default"


def cleanup(namespace: str, name_prefix: str | None = None) -> int:
    """Delete ConfigMaps and Secrets created by agent-eval-harness.

    If ``name_prefix`` is given, only delete resources whose name starts with
    that prefix (scoped cleanup for a specific run/project). Otherwise deletes
    all harness-managed resources in the namespace.
    """
    core = _ensure_client()
    selector = "app.kubernetes.io/managed-by=agent-eval-harness"
    deleted = 0
    for cm in core.list_namespaced_config_map(
            namespace, label_selector=selector).items:
        if name_prefix and not cm.metadata.name.startswith(name_prefix):
            continue
        core.delete_namespaced_config_map(cm.metadata.name, namespace)
        deleted += 1
    for secret in core.list_namespaced_secret(
            namespace, label_selector=selector).items:
        if name_prefix and not secret.metadata.name.startswith(name_prefix):
            continue
        core.delete_namespaced_secret(secret.metadata.name, namespace)
        deleted += 1
    if deleted:
        log.info("Deleted %d resource(s) in %s", deleted, namespace)
    return deleted
