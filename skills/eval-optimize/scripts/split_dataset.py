#!/usr/bin/env python3
"""Split a dataset into train/selection/test partitions for optimization.

Deterministic splitting with a fixed seed ensures reproducibility across
iterations. The split ratio is specified as train:selection:test (e.g., 40:20:40).

Usage:
    python3 split_dataset.py --dataset <path> --ratio 40:20:40 --output tmp/splits.yaml
"""

import argparse
import random
import sys
from pathlib import Path

import yaml


def split_cases(case_ids, ratio_str, seed=42):
    """Split case IDs into train/selection/test according to ratio.

    For small datasets (< 10 cases), train and selection overlap to ensure
    the validation gate has enough signal.
    """
    parts = [int(x) for x in ratio_str.split(":")]
    if len(parts) != 3:
        print(f"ERROR: ratio must be train:sel:test (got {ratio_str})",
              file=sys.stderr)
        sys.exit(1)

    total_ratio = sum(parts)
    n = len(case_ids)

    rng = random.Random(seed)
    shuffled = list(case_ids)
    rng.shuffle(shuffled)

    if n < 3:
        # Tiny dataset: use all cases in all splits
        train = list(shuffled)
        selection = list(shuffled)
        test = list(shuffled)
    elif n < 10:
        # Small dataset: overlap train and selection, ensure non-empty
        n_test = max(1, round(n * parts[2] / total_ratio))
        n_train_sel = max(1, n - n_test)
        train = shuffled[:n_train_sel]
        selection = list(train)  # overlap
        test = shuffled[n_train_sel:]
    else:
        n_train = max(1, round(n * parts[0] / total_ratio))
        n_sel = max(1, round(n * parts[1] / total_ratio))
        train = shuffled[:n_train]
        selection = shuffled[n_train:n_train + n_sel]
        test = shuffled[n_train + n_sel:]

    return {
        "train": sorted(train),
        "selection": sorted(selection),
        "test": sorted(test),
        "ratio": ratio_str,
        "seed": seed,
        "total_cases": n,
    }


def main():
    parser = argparse.ArgumentParser(description="Split dataset for optimization")
    parser.add_argument("--dataset", required=True, help="Path to dataset/cases dir")
    parser.add_argument("--ratio", default="40:20:40", help="train:sel:test ratio")
    parser.add_argument("--output", default="tmp/splits.yaml", help="Output file")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.is_dir():
        print(f"ERROR: dataset path does not exist: {dataset_path}",
              file=sys.stderr)
        sys.exit(1)

    case_ids = sorted(
        d.name for d in dataset_path.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )

    if not case_ids:
        print(f"ERROR: no case directories found in {dataset_path}",
              file=sys.stderr)
        sys.exit(1)

    splits = split_cases(case_ids, args.ratio, args.seed)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(splits, f, default_flow_style=False)

    print(f"Split {splits['total_cases']} cases ({args.ratio}):")
    print(f"  train ({len(splits['train'])}): {', '.join(splits['train'])}")
    print(f"  selection ({len(splits['selection'])}): {', '.join(splits['selection'])}")
    print(f"  test ({len(splits['test'])}): {', '.join(splits['test'])}")


if __name__ == "__main__":
    main()
