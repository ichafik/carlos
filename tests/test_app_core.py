"""Unit tests for app/config.py, app/key_store.py, and app/github_auth.py.

These three modules import third-party packages (cryptography, PyJWT) that
aren't necessarily installed wherever tests run, so this file fakes them via
sys.modules the same way tests/test_llm_providers.py fakes the LLM SDKs —
testing our glue code's behavior, not the third-party library itself.
"""

import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _install_fake_cryptography():
    """A reversible, obviously-not-secure stand-in for Fernet: good enough to
    exercise key_store.py's encrypt/decrypt/round-trip and failure paths."""
    fake_crypto = types.ModuleType("cryptography")
    fake_fernet_mod = types.ModuleType("cryptography.fernet")

    class InvalidToken(Exception):
        pass

    class FakeFernet:
        def __init__(self, key):
            self.key = key

        def encrypt(self, data: bytes) -> bytes:
            return b"ENC:" + self.key + b":" + data

        def decrypt(self, token: bytes) -> bytes:
            prefix = b"ENC:" + self.key + b":"
            if not token.startswith(prefix):
                raise InvalidToken("bad token or wrong key")
            return token[len(prefix):]

        @staticmethod
        def generate_key() -> bytes:
            return b"fake-generated-key-0123456789abcdef"

    fake_fernet_mod.Fernet = FakeFernet
    fake_fernet_mod.InvalidToken = InvalidToken
    fake_crypto.fernet = fake_fernet_mod
    sys.modules["cryptography"] = fake_crypto
    sys.modules["cryptography.fernet"] = fake_fernet_mod


def _install_fake_jwt():
    fake_jwt = types.ModuleType("jwt")

    def encode(payload, key, algorithm=None):
        # Not a real JWT — just enough structure for github_auth's caller to
        # inspect claims and for the HTTP call downstream to be mockable.
        return f"fake-jwt:{payload['iss']}:{payload['iat']}:{payload['exp']}"

    fake_jwt.encode = encode
    sys.modules["jwt"] = fake_jwt


_install_fake_cryptography()
_install_fake_jwt()

from app import config, github_auth, key_store  # noqa: E402


def _set_required_env(db_path: str):
    os.environ["GITHUB_APP_ID"] = "12345"
    os.environ["GITHUB_APP_PRIVATE_KEY"] = "fake-pem-contents"
    os.environ["GITHUB_WEBHOOK_SECRET"] = "webhook-secret"
    os.environ["KEY_ENCRYPTION_SECRET"] = "fake-generated-key-0123456789abcdef"
    os.environ["CARLOS_DB_PATH"] = db_path


class SettingsTests(unittest.TestCase):
    def setUp(self):
        config._settings = None
        self._tmpdir = tempfile.TemporaryDirectory()
        _set_required_env(os.path.join(self._tmpdir.name, "carlos.db"))

    def tearDown(self):
        config._settings = None
        self._tmpdir.cleanup()

    def test_missing_required_var_raises(self):
        del os.environ["GITHUB_APP_ID"]
        config._settings = None
        with self.assertRaises(RuntimeError):
            config.get_settings()

    def test_private_key_newline_escaping_is_normalized(self):
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "line1\\nline2"
        config._settings = None
        settings = config.get_settings()
        self.assertEqual(settings.private_key, "line1\nline2")

    def test_protected_paths_parsed_from_csv(self):
        os.environ["PROTECTED_PATHS"] = "auth/, payments/ ,,infra/"
        config._settings = None
        settings = config.get_settings()
        self.assertEqual(settings.protected_paths, ["auth/", "payments/", "infra/"])

    def test_settings_is_a_cached_singleton(self):
        self.assertIs(config.get_settings(), config.get_settings())


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        config._settings = None
        self._tmpdir = tempfile.TemporaryDirectory()
        _set_required_env(os.path.join(self._tmpdir.name, "carlos.db"))

    def tearDown(self):
        config._settings = None
        self._tmpdir.cleanup()

    def test_round_trip_encrypt_decrypt(self):
        key_store.set_key(42, "Claude", "sk-abc123")
        result = key_store.get_key(42)
        self.assertEqual(result.provider, "claude")  # normalized to lowercase
        self.assertEqual(result.api_key, "sk-abc123")

    def test_missing_installation_returns_none(self):
        self.assertIsNone(key_store.get_key(999))

    def test_set_key_overwrites_existing(self):
        key_store.set_key(1, "gemini", "old-key")
        key_store.set_key(1, "openai", "new-key")
        result = key_store.get_key(1)
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.api_key, "new-key")

    def test_delete_key_removes_it(self):
        key_store.set_key(7, "claude", "sk-xyz")
        key_store.delete_key(7)
        self.assertIsNone(key_store.get_key(7))

    def test_wrong_encryption_key_returns_none_not_raise(self):
        key_store.set_key(5, "claude", "sk-secret")
        # Simulate a KEY_ENCRYPTION_SECRET rotation: new key can't decrypt old data.
        os.environ["KEY_ENCRYPTION_SECRET"] = "a-completely-different-key-value"
        config._settings = None
        self.assertIsNone(key_store.get_key(5))


class GithubAuthTests(unittest.TestCase):
    def setUp(self):
        config._settings = None
        self._tmpdir = tempfile.TemporaryDirectory()
        _set_required_env(os.path.join(self._tmpdir.name, "carlos.db"))
        github_auth._token_cache.clear()

    def tearDown(self):
        config._settings = None
        self._tmpdir.cleanup()

    def test_build_app_jwt_uses_configured_app_id(self):
        token = github_auth._build_app_jwt()
        self.assertIn(":12345:", token)

    def test_get_installation_token_caches_until_near_expiry(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        fake_response = mock.Mock()
        fake_response.json.return_value = {"token": "ghs_abc123", "expires_at": future}
        fake_response.raise_for_status.return_value = None
        with mock.patch("app.github_auth.requests.post", return_value=fake_response) as post:
            t1 = github_auth.get_installation_token(99)
            t2 = github_auth.get_installation_token(99)  # should hit cache, not POST again
        self.assertEqual(t1, "ghs_abc123")
        self.assertEqual(t2, "ghs_abc123")
        post.assert_called_once()

    def test_force_refresh_bypasses_cache(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        fake_response = mock.Mock()
        fake_response.json.return_value = {"token": "ghs_new", "expires_at": future}
        fake_response.raise_for_status.return_value = None
        with mock.patch("app.github_auth.requests.post", return_value=fake_response) as post:
            github_auth.get_installation_token(100)
            github_auth.get_installation_token(100, force_refresh=True)
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
