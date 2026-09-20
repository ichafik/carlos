"""Unit tests for app/review_engine.py's orchestration: does it call the
right GitHubClient methods in the right order, and does it correctly report
"no key configured yet" instead of crashing with a raw exception.

Fakes GitHubClient entirely (no real HTTP) and fakes cryptography/jwt so the
transitive imports (key_store, github_auth) succeed without those packages
installed. The LLM call itself is faked too — this test is about the glue,
not the model or the network.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)


def _install_fakes():
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
    sys.modules["cryptography"] = fake_crypto
    sys.modules["cryptography.fernet"] = fake_fernet_mod

    fake_jwt = types.ModuleType("jwt")
    fake_jwt.encode = lambda payload, key, algorithm=None: "fake-jwt"
    sys.modules["jwt"] = fake_jwt


_install_fakes()

import config  # noqa: E402
import key_store  # noqa: E402
import review_engine  # noqa: E402


class FakeGitHubClient:
    """Records every call so tests can assert on call order/content
    without touching the network."""

    def __init__(self, *a, **kw):
        self.calls = []
        self.sha = "deadbeef"
        self._pr = {"title": "Add feature", "body": "why", "user": {"login": "author"},
                    "head": {"sha": "deadbeef"}}
        self._comment = None

    def set_status(self, context, state, description):
        self.calls.append(("set_status", state, description))

    def get_pr(self):
        self.calls.append(("get_pr",))
        return self._pr

    def get_diff(self):
        self.calls.append(("get_diff",))
        return "diff --git a/x.py b/x.py\n+pass\n"

    def get_changed_files(self):
        self.calls.append(("get_changed_files",))
        return ["x.py"]

    def count_approvals(self):
        self.calls.append(("count_approvals",))
        return 0

    def upsert_comment(self, marker, body):
        self.calls.append(("upsert_comment", marker))
        self._comment = body

    def set_labels(self, prefix, label):
        self.calls.append(("set_labels", label))

    def reply(self, text):
        self.calls.append(("reply", text))

    def get_file_contents(self, path, ref="main", repo=None):
        self.calls.append(("get_file_contents", path))
        return None  # no whitebook configured, keeps this test focused


class FakeProvider:
    def __init__(self, text, truncated=False):
        self._text = text
        self._truncated = truncated

    def generate(self, **kwargs):
        return self._text, self._truncated


PERFECT_REVIEW_JSON = (
    '{"summary": "adds a feature", "issues": [], "edge_cases": [], '
    '"policy_violations": [], "potential_bugs": [], '
    '"tests": {"estimated_changed_line_coverage_pct": 100}, '
    '"scores": {"correctness": 40, "tests": 25, "security": 20, "style": 15}, '
    '"confidence": "high", "score_justification": "clean diff"}'
)


class RunReviewTests(unittest.TestCase):
    def setUp(self):
        config._settings = None
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["GITHUB_APP_ID"] = "1"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "pem"
        os.environ["GITHUB_WEBHOOK_SECRET"] = "secret"
        os.environ["KEY_ENCRYPTION_SECRET"] = "k"
        os.environ["CARLOS_DB_PATH"] = os.path.join(self._tmpdir.name, "carlos.db")

    def tearDown(self):
        config._settings = None
        self._tmpdir.cleanup()

    def test_run_review_without_configured_key_reports_and_raises(self):
        fake_client = FakeGitHubClient()
        with mock.patch("review_engine._client", return_value=fake_client):
            with self.assertRaises(review_engine.NotConfigured):
                review_engine.run_review(installation_id=1, repo_full_name="acme/widgets", pr_number=5)
        states = [c for c in fake_client.calls if c[0] == "set_status"]
        self.assertEqual(states[-1][1], "error")
        self.assertTrue(any(c[0] == "reply" for c in fake_client.calls))

    def test_run_review_happy_path_posts_comment_and_success_status(self):
        key_store.set_key(1, "claude", "sk-test")
        fake_client = FakeGitHubClient()
        with mock.patch("review_engine._client", return_value=fake_client), \
             mock.patch("review_engine.get_provider", return_value=FakeProvider(PERFECT_REVIEW_JSON)):
            policy = review_engine.run_review(installation_id=1, repo_full_name="acme/widgets", pr_number=5)

        self.assertEqual(policy["score"], 100)
        self.assertTrue(policy["auto_merge"])
        call_names = [c[0] for c in fake_client.calls]
        self.assertEqual(call_names[0], "set_status")
        self.assertIn("upsert_comment", call_names)
        self.assertIn("set_labels", call_names)
        # pending, then a final success/failure status
        statuses = [c[1] for c in fake_client.calls if c[0] == "set_status"]
        self.assertEqual(statuses[0], "pending")
        self.assertEqual(statuses[-1], "success")

    def test_run_review_retries_on_truncation_then_succeeds(self):
        key_store.set_key(2, "openai", "sk-test")
        fake_client = FakeGitHubClient()
        truncated_provider = FakeProvider("{not json", truncated=True)
        good_provider = FakeProvider(PERFECT_REVIEW_JSON)
        calls = {"n": 0}

        def fake_generate(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return truncated_provider.generate(**kwargs)
            return good_provider.generate(**kwargs)

        provider = mock.Mock()
        provider.generate.side_effect = fake_generate
        with mock.patch("review_engine._client", return_value=fake_client), \
             mock.patch("review_engine.get_provider", return_value=provider):
            policy = review_engine.run_review(installation_id=2, repo_full_name="acme/widgets", pr_number=9)
        self.assertEqual(policy["score"], 100)
        self.assertEqual(calls["n"], 2)


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        config._settings = None
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["GITHUB_APP_ID"] = "1"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "pem"
        os.environ["GITHUB_WEBHOOK_SECRET"] = "secret"
        os.environ["KEY_ENCRYPTION_SECRET"] = "k"
        os.environ["CARLOS_DB_PATH"] = os.path.join(self._tmpdir.name, "carlos.db")

    def tearDown(self):
        config._settings = None
        self._tmpdir.cleanup()

    def test_non_bot_prefixed_comment_is_ignored(self):
        fake_client = FakeGitHubClient()
        with mock.patch("review_engine._client", return_value=fake_client):
            review_engine.run_command(1, "acme/widgets", 5, "just chatting", "someone", 111)
        self.assertEqual(fake_client.calls, [])

    def test_unauthorized_user_gets_rejected(self):
        fake_client = FakeGitHubClient()
        fake_client.user_can_write = lambda login: False
        fake_client.react = lambda comment_id, content: fake_client.calls.append(("react", content))
        with mock.patch("review_engine._client", return_value=fake_client):
            review_engine.run_command(1, "acme/widgets", 5, "carlos review", "outsider", 111)
        self.assertIn(("react", "confused"), fake_client.calls)
        self.assertTrue(any(c[0] == "reply" and "write access" in c[1] for c in fake_client.calls))


if __name__ == "__main__":
    unittest.main()
