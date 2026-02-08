from __future__ import annotations

import os
from typing import Optional


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    st = os.stat(path)
    if (st.st_mode & 0o777) != 0o600:
        raise PermissionError(f"{path} must have chmod 600")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())
