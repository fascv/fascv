from __future__ import annotations

import os
from pathlib import Path


def _default_user_secrets_path() -> Path:
    raw = str(os.getenv("CODEX_TRADING_SECRETS_ENV", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "codex" / "trading-secrets.env"


def _load_env_file(path: str | Path) -> None:
    path = Path(path).expanduser()
    if not path.exists():
        return
    st = path.stat()
    if (st.st_mode & 0o777) != 0o600:
        raise PermissionError(f"{path} must have chmod 600")
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def load_env(path: str = ".env") -> None:
    primary = Path(path).expanduser()
    _load_env_file(primary)

    secrets_path = _default_user_secrets_path()
    if secrets_path != primary:
        _load_env_file(secrets_path)
