"""Integration-style tests for app/main.py's FastAPI endpoints, using
fastapi.testclient.TestClient against the real app object (no mocked
FastAPI internals) — this addresses Carlos's WB-TST-03 finding that
app/main.py had zero test coverage.

Requires fastapi + starlette + httpx (see app/requirements.txt). Locally,
where those may not be installed, this file skips entirely rather than
failing the whole suite — but in CI, where the `tests` job now installs
app/requirements.txt (see the workflow fix), it runs for real.
"""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


def _install_fake_crypto_and_jwt():
    """main.py doesn't use cryptography/jwt directly, but it imports
    key_store and review_engine, which transitively do. Faked the same way
    as the other app/ test files so this doesn't require those packages
    either — only fastapi/starlette/httpx need to be real here."""
    fake_crypto = types.ModuleType("cryptography")
    fake_fernet_mod = types.ModuleType("cryptography.fernet")

    class InvalidToken(Exception):
        pass

    class FakeFernet:
        def __init__(self, key):
            self.key = key

        def encrypt(self, data):
            return b"ENC:" + data

        def decrypt(self, token):
            if not token.startswith(b"ENC:"):
                raise InvalidToken()
            return token[4:]

    fake_fernet_mod.Fernet = FakeFernet
    fake_fernet_mod.InvalidToken = InvalidToken
    fake_crypto.fernet = fake_fernet_mod
    sys.modules.setdefault("cryptography", fake_crypto)
    sys.modules.setdefault("cryptography.fernet", fake_fernet_mod)

    if "jwt" not in sys.modules:
        fake_jwt = types.ModuleType("jwt")
        fake_jwt.encode = lambda payload, key, algorithm=None: "fake-jwt"
        sys.modules["jwt"] = fake_jwt


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi not installed in this environment")
class WebhookEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fake_crypto_and_jwt()
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["GITHUB_APP_ID"] = "1"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "pem"
        os.environ["GITHUB_WEBHOOK_SECRET"] = "test-webhook-secret"
        os.environ["KEY_ENCRYPTION_SECRET"] = "k"
        os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
        os.environ["CARLOS_DB_PATH"] = os.path.join(cls._tmpdir.name, "carlos.db")

        from app import config
        config._settings = None
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _signed_headers(self, body: bytes, event: str) -> dict:
        digest = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        return {
            "X-Hub-Signature-256": f"sha256={digest}",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "delivery-123",
            "Content-Type": "application/json",
        }

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_webhook_rejects_invalid_signature(self):
        body = json.dumps({"installation": {"id": 1}}).encode()
        headers = self._signed_headers(body, "pull_request")
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        resp = self.client.post("/webhook", data=body, headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_webhook_ignored_without_installation_id(self):
        body = json.dumps({"action": "opened"}).encode()
        headers = self._signed_headers(body, "pull_request")
        resp = self.client.post("/webhook", data=body, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ignored")

    def test_webhook_accepts_valid_pull_request_event(self):
        body = json.dumps({
            "action": "opened",
            "installation": {"id": 42},
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {"number": 7, "draft": False},
        }).encode()
        headers = self._signed_headers(body, "pull_request")
        resp = self.client.post("/webhook", data=body, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "accepted")
        self.assertEqual(resp.json()["delivery"], "delivery-123")

    def test_webhook_draft_pr_is_not_queued(self):
        # Can't directly assert "not queued" without inspecting background
        # tasks, but this at least confirms a draft PR doesn't error out.
        body = json.dumps({
            "action": "opened",
            "installation": {"id": 42},
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {"number": 7, "draft": True},
        }).encode()
        headers = self._signed_headers(body, "pull_request")
        resp = self.client.post("/webhook", data=body, headers=headers)
        self.assertEqual(resp.status_code, 200)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi not installed in this environment")
class InstallationKeyEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fake_crypto_and_jwt()
        cls._tmpdir = tempfile.TemporaryDirectory()
        os.environ["GITHUB_APP_ID"] = "1"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "pem"
        os.environ["GITHUB_WEBHOOK_SECRET"] = "test-webhook-secret"
        os.environ["KEY_ENCRYPTION_SECRET"] = "k"
        os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
        os.environ["CARLOS_DB_PATH"] = os.path.join(cls._tmpdir.name, "carlos.db")

        from app import config
        config._settings = None
        from app.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_missing_admin_token_rejected(self):
        resp = self.client.post("/installations/1/key", json={"provider": "claude", "api_key": "sk-x"})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_admin_token_rejected(self):
        resp = self.client.post(
            "/installations/1/key",
            json={"provider": "claude", "api_key": "sk-x"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_registration_succeeds(self):
        resp = self.client.post(
            "/installations/2/key",
            json={"provider": "claude", "api_key": "sk-valid"},
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["provider"], "claude")

    def test_unsupported_provider_rejected(self):
        resp = self.client.post(
            "/installations/3/key",
            json={"provider": "cohere", "api_key": "sk-x"},
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unsupported provider", resp.json()["detail"])

    def test_provider_alias_normalized_to_canonical(self):
        resp = self.client.post(
            "/installations/4/key",
            json={"provider": "anthropic", "api_key": "sk-x"},
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["provider"], "claude")

    def test_oversized_api_key_rejected(self):
        resp = self.client.post(
            "/installations/5/key",
            json={"provider": "claude", "api_key": "x" * 1000},
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_requires_admin_token(self):
        resp = self.client.delete("/installations/2/key")
        self.assertEqual(resp.status_code, 401)

    def test_delete_succeeds_with_admin_token(self):
        resp = self.client.delete(
            "/installations/2/key", headers={"Authorization": "Bearer test-admin-token"}
        )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
