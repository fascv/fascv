"""
Keep package import light-weight.

`trading_journal.process` imports `trading.*` (repo-root package). When the module is used
standalone via PYTHONPATH=modules/journal/src, that repo-root may not be present.
We therefore use lazy imports so `python3 -m trading_journal.gui` works reliably.
"""

from typing import Any

__all__ = ["run_journal_process", "run_journal_gui"]


def run_journal_process(*args: Any, **kwargs: Any) -> Any:
    from .process import run_journal_process as _impl

    return _impl(*args, **kwargs)


def run_journal_gui(*args: Any, **kwargs: Any) -> Any:
    from .gui import run_journal_gui as _impl

    return _impl(*args, **kwargs)
