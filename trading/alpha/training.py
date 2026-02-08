from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple


@dataclass
class WalkForwardSplit:
    train_idx: Tuple[int, int]
    val_idx: Tuple[int, int]
    test_idx: Tuple[int, int]


def make_walk_forward_splits(
    n: int,
    train_size: int,
    val_size: int,
    test_size: int,
    purge: int,
) -> List[WalkForwardSplit]:
    splits: List[WalkForwardSplit] = []
    start = 0
    while start + train_size + val_size + test_size <= n:
        train = (start, start + train_size)
        val = (train[1] + purge, train[1] + purge + val_size)
        test = (val[1] + purge, val[1] + purge + test_size)
        splits.append(WalkForwardSplit(train, val, test))
        start = test[0]
    return splits


def early_stop(training_losses: List[float], patience: int = 5) -> bool:
    if len(training_losses) < patience + 1:
        return False
    recent = training_losses[-patience:]
    return min(recent) > min(training_losses[:-patience])


def version_stamp(feature_version: str, model_version: str) -> Dict[str, str]:
    return {"feature_version": feature_version, "model_version": model_version}


def train_walk_forward(
    splits: List[WalkForwardSplit],
    trainer: Callable[[Tuple[int, int], Tuple[int, int]], Dict[str, float]],
) -> List[Dict[str, float]]:
    reports: List[Dict[str, float]] = []
    for split in splits:
        report = trainer(split.train_idx, split.val_idx)
        reports.append(report)
    return reports
