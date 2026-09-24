"""Offline ``/generation`` backfill for a finished run (spec 014).

``python3 -m agent_eval.providers.openrouter.backfill <run_dir>`` re-queries
every transcript id of the run that has no priced ledger row — a generation
that was still materialising at run end, or a run interrupted before its
run-end pass — and re-reconciles the run's ``run_result.json`` files so the
readers see the completed cost. The inference key is read from the
environment (``OPENROUTER_API_KEY`` unless ``--key-env`` says otherwise); the
base URL comes from the run's own ``provider`` block.
"""

from __future__ import annotations

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv before 3p imports
import argparse
import json
import os
import sys
from pathlib import Path

from agent_eval.providers.ledger import Ledger
from agent_eval.providers.openrouter.generation import Backfill, is_generation_id
from agent_eval.providers.openrouter.http import BASE_URL
from agent_eval.providers.reconcile import reconcile_run_dir


def _load(path: Path):
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def collect_sightings(run_dir: Path) -> list:
    """``{gen_id, case_id}`` for every transcript id of the run: the per-case
    files carry their case, the run-level file covers batch mode."""
    seen: dict = {}
    for case_file in sorted(run_dir.glob("cases/*/run_result.json")):
        payload = _load(case_file) or {}
        for gen_id in payload.get("message_ids") or []:
            if is_generation_id(gen_id):
                seen.setdefault(gen_id, {"gen_id": gen_id, "case_id": case_file.parent.name})
    run = _load(run_dir / "run_result.json") or {}
    for gen_id in run.get("message_ids") or []:
        if is_generation_id(gen_id):
            seen.setdefault(gen_id, {"gen_id": gen_id, "case_id": None})
    return list(seen.values())


def run_backfill(run_dir: Path, *, key: str, base_url: str = BASE_URL, give_up_s: float = 30.0,
                 fetch=None) -> dict:
    ledger = Ledger.for_run(run_dir)
    priced = {r.get("gen_id") for r in ledger.read()
              if r.get("source") == "generation" and r.get("status") == "ok"}
    todo = [s for s in collect_sightings(run_dir) if s["gen_id"] not in priced]
    run = _load(run_dir / "run_result.json") or {}
    backfill = Backfill(ledger, key=key, run_id=run_dir.name, base_url=base_url, fetch=fetch,
                        first_poll_s=0.0, give_up_s=give_up_s)
    backfill.sight_many(todo)
    backfill.drain(timeout_s=give_up_s + 5)
    stats = backfill.close(retry=False)
    reconcile_run_dir(run_dir, ledger=ledger)
    return {"requeried": len(todo), "priced": stats.ok, "failed": stats.failed,
            "aborted": stats.aborted, "run_id": run.get("eval_params", {}).get("run_id") or run_dir.name}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY",
                        help="environment variable holding the inference key (value never echoed)")
    parser.add_argument("--give-up-s", type=float, default=30.0)
    args = parser.parse_args(argv)
    key = os.environ.get(args.key_env)
    if not key:
        print(f"ERROR: set {args.key_env} — the backfill authenticates with the run's inference key",
              file=sys.stderr)
        return 2
    run = _load(args.run_dir / "run_result.json")
    if run is None:
        print(f"ERROR: no run_result.json under {args.run_dir}", file=sys.stderr)
        return 2
    base_url = ((run.get("provider") or {}).get("base_url")) or BASE_URL
    summary = run_backfill(args.run_dir, key=key, base_url=base_url, give_up_s=args.give_up_s)
    print(f"backfill: re-queried {summary['requeried']} id(s), priced {summary['priced']}, "
          f"failed {summary['failed']}" + (f", aborted: {summary['aborted']}" if summary["aborted"] else ""))
    return 1 if summary["failed"] or summary["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
