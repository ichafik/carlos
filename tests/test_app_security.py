"""Unit tests for app/security.py's webhook signature verification.

Pure stdlib (hmac/hashlib) — no third-party deps needed, unlike most of the
app/ package, so this runs even in environments without fastapi/cryptography
installed.
"""

import hashlib
import hmac
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import security  # noqa: E402


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class VerifySignatureTests(unittest.TestCase):
    def test_valid_signature_accepted(self):
        body = b'{"action": "opened"}'
        secret = "test-webhook-secret"
        self.assertTrue(security.verify_signature(body, _sign(body, secret), secret))

    def test_wrong_secret_rejected(self):
        body = b'{"action": "opened"}'
        self.assertFalse(security.verify_signature(body, _sign(body, "right"), "wrong"))

    def test_tampered_body_rejected(self):
        secret = "test-webhook-secret"
        signature = _sign(b'{"action": "opened"}', secret)
        self.assertFalse(security.verify_signature(b'{"action": "closed"}', signature, secret))

    def test_missing_header_rejected(self):
        self.assertFalse(security.verify_signature(b"{}", None, "secret"))

    def test_malformed_header_rejected(self):
        self.assertFalse(security.verify_signature(b"{}", "not-sha256=abc", "secret"))
        self.assertFalse(security.verify_signature(b"{}", "sha256=", "secret"))


if __name__ == "__main__":
    unittest.main()
