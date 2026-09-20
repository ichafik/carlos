"""Unit tests for the multi-provider BYO-key layer used by Carlos.

Runs via `python -m unittest discover -s tests -p "*.py"` (see pr-review.yml's
`tests` job), same as the other scripts in this folder. Imports llm_providers.py
from .github/scripts without needing any provider SDK actually installed: the
SDKs are imported lazily inside each provider's generate(), so dispatch and
key-validation can be tested with zero network access, and generate() itself
is tested against fake stand-ins for the SDK modules.
"""

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".github", "scripts"))

import llm_providers  # noqa: E402


class GetProviderDispatchTests(unittest.TestCase):
    def setUp(self):
        # Isolate from any real secrets the runner's environment might have.
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_missing_key_raises_actionable_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            llm_providers.get_provider("gemini")
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))

    def test_unknown_provider_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            llm_providers.get_provider("copilot")
        self.assertIn("Unknown PROVIDER", str(ctx.exception))

    def test_gemini_dispatch(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.assertIsInstance(llm_providers.get_provider("gemini"), llm_providers.GeminiProvider)

    def test_openai_and_chatgpt_alias_dispatch(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        self.assertIsInstance(llm_providers.get_provider("openai"), llm_providers.OpenAIProvider)
        self.assertIsInstance(llm_providers.get_provider("chatgpt"), llm_providers.OpenAIProvider)

    def test_claude_and_anthropic_alias_dispatch(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.assertIsInstance(llm_providers.get_provider("claude"), llm_providers.ClaudeProvider)
        self.assertIsInstance(llm_providers.get_provider("anthropic"), llm_providers.ClaudeProvider)

    def test_provider_name_is_case_and_whitespace_insensitive(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.assertIsInstance(llm_providers.get_provider(" Gemini \n"), llm_providers.GeminiProvider)

    def test_empty_provider_defaults_to_gemini(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.assertIsInstance(llm_providers.get_provider(""), llm_providers.GeminiProvider)

    def test_canonical_provider_name_resolves_aliases_to_dispatched_class(self):
        # Regression test: canonical_provider_name() must agree with
        # get_provider()'s dispatch table, so a MODEL default keyed off
        # DEFAULT_MODELS[canonical_provider_name(PROVIDER)] always matches
        # the model the actually-instantiated provider uses.
        cases = {
            "chatgpt": llm_providers.OpenAIProvider,
            "openai": llm_providers.OpenAIProvider,
            "anthropic": llm_providers.ClaudeProvider,
            "claude": llm_providers.ClaudeProvider,
            "gemini": llm_providers.GeminiProvider,
        }
        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        os.environ["GEMINI_API_KEY"] = "k"
        for alias, expected_cls in cases.items():
            canonical = llm_providers.canonical_provider_name(alias)
            expected_model = llm_providers.DEFAULT_MODELS[canonical]
            actual_provider = llm_providers.get_provider(alias)
            self.assertIsInstance(actual_provider, expected_cls)
            self.assertEqual(
                expected_model, llm_providers.DEFAULT_MODELS[actual_provider.name]
            )


class GenerateGlueTests(unittest.TestCase):
    """Fakes each SDK module so generate()'s request/response wiring is
    exercised without a real API call or the real package installed."""

    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "k"
        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        self._orig_modules = dict(sys.modules)

    def tearDown(self):
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL"):
            os.environ.pop(k, None)
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_openai_generate_reports_truncation(self):
        fake_openai = types.ModuleType("openai")

        class FakeChoice:
            def __init__(self):
                self.message = types.SimpleNamespace(content='{"summary": "ok"}')
                self.finish_reason = "length"

        class FakeResp:
            def __init__(self):
                self.choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                self.last_kwargs = kwargs
                return FakeResp()

        class FakeChat:
            def __init__(self):
                self.completions = FakeCompletions()

        class FakeOpenAI:
            def __init__(self, api_key=None, timeout=None):
                self.api_key = api_key
                self.timeout = timeout
                self.chat = FakeChat()

        fake_openai.OpenAI = FakeOpenAI
        sys.modules["openai"] = fake_openai

        provider = llm_providers.get_provider("openai")
        text, truncated = provider.generate(
            prompt="diff here", system_prompt="sys", seed=42, temperature=0, max_output_tokens=100
        )
        self.assertEqual(text, '{"summary": "ok"}')
        self.assertTrue(truncated)

    def test_gemini_generate_reports_truncation(self):
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_genai_types = types.ModuleType("google.genai.types")

        class FakeHttpOptions:
            def __init__(self, timeout=None):
                self.timeout = timeout

        class FakeConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeCandidate:
            def __init__(self):
                self.finish_reason = "MAX_TOKENS"

        class FakeResp:
            def __init__(self):
                self.candidates = [FakeCandidate()]
                self.text = '{"summary": "ok"}'

        class FakeModels:
            def generate_content(self, **kwargs):
                self.last_kwargs = kwargs
                return FakeResp()

        class FakeClient:
            def __init__(self, api_key=None, http_options=None):
                self.api_key = api_key
                self.http_options = http_options
                self.models = FakeModels()

        fake_genai_types.HttpOptions = FakeHttpOptions
        fake_genai_types.GenerateContentConfig = FakeConfig
        fake_genai.types = fake_genai_types
        fake_genai.Client = FakeClient
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_genai_types

        provider = llm_providers.get_provider("gemini")
        text, truncated = provider.generate(
            prompt="diff here", system_prompt="sys", seed=42, temperature=0, max_output_tokens=100
        )
        self.assertEqual(text, '{"summary": "ok"}')
        self.assertTrue(truncated)

    def test_claude_generate_reports_truncation_and_warns_once(self):
        fake_anthropic = types.ModuleType("anthropic")

        class FakeBlock:
            def __init__(self, text):
                self.type = "text"
                self.text = text

        class FakeResp:
            def __init__(self):
                self.content = [FakeBlock('{"summary": "ok"}')]
                self.stop_reason = "max_tokens"

        class FakeMessages:
            def create(self, **kwargs):
                return FakeResp()

        class FakeAnthropic:
            def __init__(self, api_key=None, timeout=None):
                self.api_key = api_key
                self.timeout = timeout
                self.messages = FakeMessages()

        fake_anthropic.Anthropic = FakeAnthropic
        sys.modules["anthropic"] = fake_anthropic

        provider = llm_providers.get_provider("claude")
        with mock.patch("builtins.print") as fake_print:
            text, truncated = provider.generate(
                prompt="diff here", system_prompt="sys", seed=42, temperature=0, max_output_tokens=100
            )
        self.assertEqual(text, '{"summary": "ok"}')
        self.assertTrue(truncated)
        self.assertTrue(any("ignores SEED" in str(c) for c in fake_print.call_args_list))


if __name__ == "__main__":
    unittest.main()
