from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading.utils.env import load_env


class TestEnvLoading(unittest.TestCase):
    def test_load_env_also_reads_user_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            primary = tmp_path / ".env"
            secrets = tmp_path / "trading-secrets.env"

            primary.write_text("BINANCE_BASE_URL=https://api.binance.com\n", encoding="utf-8")
            secrets.write_text(
                "BINANCE_API_KEY=test_key\nBINANCE_API_SECRET=test_secret\n",
                encoding="utf-8",
            )
            primary.chmod(0o600)
            secrets.chmod(0o600)

            with patch.dict(
                os.environ,
                {"CODEX_TRADING_SECRETS_ENV": str(secrets)},
                clear=True,
            ):
                load_env(str(primary))

                self.assertEqual(os.environ["BINANCE_BASE_URL"], "https://api.binance.com")
                self.assertEqual(os.environ["BINANCE_API_KEY"], "test_key")
                self.assertEqual(os.environ["BINANCE_API_SECRET"], "test_secret")


if __name__ == "__main__":
    unittest.main()
