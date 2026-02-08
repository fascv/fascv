from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Callable, Dict, List


@dataclass
class GridSearchResult:
    params: Dict[str, Any]
    metrics: Dict[str, Any]


def grid_search(param_grid: Dict[str, List[Any]], runner: Callable[[Dict[str, Any]], Dict[str, Any]]) -> List[GridSearchResult]:
    keys = list(param_grid.keys())
    results: List[GridSearchResult] = []
    for values in product(*(param_grid[k] for k in keys)):
        params = dict(zip(keys, values))
        metrics = runner(params)
        results.append(GridSearchResult(params=params, metrics=metrics))
    return results
