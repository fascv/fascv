def run_exec_process(*args, **kwargs):
    from .process import run_exec_process as _run_exec_process

    return _run_exec_process(*args, **kwargs)


def run_exec_gui(*args, **kwargs):
    from .gui import run_exec_gui as _run_exec_gui

    return _run_exec_gui(*args, **kwargs)

__all__ = ["run_exec_process", "run_exec_gui"]
