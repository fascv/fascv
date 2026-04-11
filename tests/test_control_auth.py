import os
import unittest

from trading.processes.control import (
    control_effective_token,
    control_write_auth_decision,
    is_loopback_host,
)


class TestControlAuth(unittest.TestCase):
    def test_loopback_host(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("1.2.3.4"))

    def test_token_from_config(self):
        cfg = {"control": {"token": "abc"}}
        tok = control_effective_token(cfg, {})
        self.assertEqual(tok, "abc")

        allowed, code, _ = control_write_auth_decision(host="0.0.0.0", token=tok, header_token=None)
        self.assertFalse(allowed)
        self.assertEqual(code, 401)

        allowed, code, _ = control_write_auth_decision(host="0.0.0.0", token=tok, header_token="wrong")
        self.assertFalse(allowed)
        self.assertEqual(code, 401)

        allowed, code, _ = control_write_auth_decision(host="0.0.0.0", token=tok, header_token="abc")
        self.assertTrue(allowed)
        self.assertEqual(code, 200)

    def test_token_from_env(self):
        cfg = {"control": {}}
        tok = control_effective_token(cfg, {"CONTROL_TOKEN": "envtok"})
        self.assertEqual(tok, "envtok")

        allowed, code, _ = control_write_auth_decision(host="0.0.0.0", token=tok, header_token="envtok")
        self.assertTrue(allowed)
        self.assertEqual(code, 200)

    def test_no_token_loopback_allows(self):
        allowed, code, _ = control_write_auth_decision(host="127.0.0.1", token=None, header_token=None)
        self.assertTrue(allowed)
        self.assertEqual(code, 200)

    def test_no_token_non_loopback_blocks(self):
        allowed, code, _ = control_write_auth_decision(host="0.0.0.0", token=None, header_token=None)
        self.assertFalse(allowed)
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
