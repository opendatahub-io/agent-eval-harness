"""Export a dataset into self-contained per-case directories for S3/EvalHub.

The EvalHub adapter runs the agent directly in each downloaded case directory
and never runs eval-run's workspace preparation, so ``dataset.workspace.files``
— in particular shared ``{dest, source}`` entries — are not honored in-pod, and
a pod loaded purely from S3 contains no project/plugin files to resolve a
``source`` against. This step materializes those shared files into every case
directory at packaging time (in the project checkout, where the sources are
real), producing a staging tree a publisher can ``aws s3 sync`` to the bucket
the EvalHub benchmark parameters point at. :func:`download_dataset` then restores
each file per case with no adapter change.
"""

import shutil
from pathlib import Path

from agent_eval.evalhub.s3_dataset import DatasetInfo
from agent_eval.workspace_provisioning import materialize_shared_files


def _ignore_symlinks(dirpath, names):
    """copytree ignore callback: drop symlinks (S3 has no symlink concept, and
    the local workspace path already skips them for safety)."""
    base = Path(dirpath)
    return {name for name in names if (base / name).is_symlink()}


def export_dataset(
    config, dest, *, s3_client=None, bucket=None, prefix=None
) -> DatasetInfo:
    """Materialize a self-contained dataset under *dest*, optionally uploading.

    For each case directory under ``dataset.path``:

    - copy the case dir into ``dest/<case_id>`` with symlinks dropped, so
      ``input.yaml``, ``annotations.yaml``, ``answers.yaml`` and per-case string
      ``workspace.files`` come across as real files;
    - materialize shared ``{dest, source}`` ``workspace.files`` into the same
      per-case dir via the shared provisioner (source resolved against the
      project/plugin roots, symlinks followed, containment-checked).

    If *s3_client*, *bucket*, and *prefix* are all given, every materialized file
    is uploaded to ``{prefix}/{case_id}/{rel}`` — the layout
    :func:`download_dataset` reconstructs, so the export round-trips.
    """
    dest = Path(dest)
    cases_dir = config.resolve_path(config.dataset.path)
    if not cases_dir.is_dir():
        raise FileNotFoundError(f"Dataset path not found: {cases_dir}")

    case_ids = []
    for case_dir in sorted(d for d in cases_dir.iterdir() if d.is_dir()):
        case_id = case_dir.name
        out_case = dest / case_id
        if out_case.exists():
            shutil.rmtree(out_case)
        shutil.copytree(case_dir, out_case, ignore=_ignore_symlinks)
        materialize_shared_files(out_case, config)
        case_ids.append(case_id)

    if s3_client is not None and bucket and prefix is not None:
        _upload_tree(s3_client, bucket, prefix, dest, case_ids)

    return DatasetInfo(
        num_cases=len(case_ids), case_ids=sorted(case_ids), dest=dest
    )


def _upload_tree(s3_client, bucket, prefix, dest, case_ids):
    """Upload each exported case's files to ``{prefix}/{case_id}/{rel}``."""
    prefix = prefix.rstrip("/")
    for case_id in case_ids:
        out_case = dest / case_id
        for path in sorted(out_case.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(out_case).as_posix()
            s3_client.upload_file(str(path), bucket, f"{prefix}/{case_id}/{rel}")
