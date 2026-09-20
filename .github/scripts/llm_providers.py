"""
LLM provider abstraction for Carlos — "bring your own key" across ChatGPT, Gemini
and Claude.

Shared by two callers with different key sources:
  - the GitHub Actions script (carlos_review.py): one PROVIDER per repo, key
    comes from a repo secret env var (GEMINI_API_KEY / OPENAI_API_KEY /
    ANTHROPIC_API_KEY).
  - the GitHub App (app/review_engine.py): PROVIDER + key are looked up per
    installation from key_store.py (a repo secret doesn't exist in that
    context — one process serves every installation).

Either caller ends up calling get_provider(name, api_key=...).generate(...)
and gets back plain text plus a truncation flag, so the rest of the review
logic (JSON parsing, scoring, rendering) stays provider-agnostic. When
api_key is omitted, get_provider() falls back to reading the matching env
var directly — that's what keeps the Actions script's call sites unchanged.

Adding a fourth provider later means: one new class implementing generate(),
one line in get_provider()'s dispatch table, one new secret/key source.
Everything else is unaffected.
"""

import os
from abc import ABC, abstractmethod

# Sensible defaults per provider; override any of them with the MODEL env var.
DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "openai": "gpt-4.1",
    "claude": "claude-sonnet-4-5",
}

# WB-REL-03: every network call needs an explicit timeout so a hung LLM API
# doesn't stall a workflow run indefinitely. Override via env if a slower
# model/larger prompt legitimately needs more time.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("LLM_REQUEST_TIMEOUT_SECONDS", "60"))


class LLMProvider(ABC):
    """Common interface every provider must implement.

    generate() takes the fully-built prompt/system prompt and sampling
    knobs, and returns (text, truncated):
      - text: the raw model output (expected to be a JSON object per
        SYSTEM_PROMPT, but this layer does not parse it — that is
        carlos_review.py's job).
      - truncated: True if the model hit its output-token limit before
        finishing, so the caller can retry with a "be more concise" nudge.
    """

    #: Set by subclasses; used only for error messages.
    name = "unknown"

    @abstractmethod
    def generate(self, prompt: str, system_prompt: str, seed: int,
                 temperature: float, max_output_tokens: int) -> tuple[str, bool]:
        raise NotImplementedError


# Which env var each canonical provider reads its key from when no explicit
# api_key is passed to get_provider() (the Actions-script call path).
_ENV_VARS = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}


def _require_env_key(env_var: str, provider_name: str) -> str:
    """Fail fast (WB-REL-05) with a message that names the exact secret to add."""
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise RuntimeError(
            f"PROVIDER={provider_name} but {env_var} is not set. "
            f"Add it as a repository secret (Settings -> Secrets and variables -> "
            f"Actions) and pass it through pr-review.yml."
        )
    return key


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def generate(self, prompt, system_prompt, seed, temperature, max_output_tokens):
        from google import genai
        from google.genai import types

        # HttpOptions.timeout is in milliseconds.
        client = genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_SECONDS * 1000)),
        )
        model = os.environ.get("MODEL") or DEFAULT_MODELS[self.name]
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                temperature=temperature,
                top_p=1.0,
                top_k=1,
                seed=seed,
                max_output_tokens=max_output_tokens,
            ),
        )
        finish = getattr(resp.candidates[0], "finish_reason", None) if resp.candidates else None
        text = resp.text or ""
        truncated = str(finish).endswith("MAX_TOKENS")
        return text, truncated


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def generate(self, prompt, system_prompt, seed, temperature, max_output_tokens):
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        model = os.environ.get("MODEL") or DEFAULT_MODELS[self.name]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            seed=seed,
            response_format={"type": "json_object"},
            max_tokens=max_output_tokens,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        truncated = choice.finish_reason == "length"
        return text, truncated


class ClaudeProvider(LLMProvider):
    name = "claude"
    #: Anthropic's API has no seed parameter (as of this writing); the
    #: SEED env var is silently ignored for this provider. Warn once so a
    #: user who set SEED expecting determinism isn't confused by drift.
    _warned_no_seed = False

    def __init__(self, api_key: str):
        self._api_key = api_key

    def generate(self, prompt, system_prompt, seed, temperature, max_output_tokens):
        import anthropic

        if not ClaudeProvider._warned_no_seed:
            print("[carlos] note: PROVIDER=claude ignores SEED (Anthropic API has no "
                  "seed parameter); reproducibility across runs is best-effort only.")
            ClaudeProvider._warned_no_seed = True

        client = anthropic.Anthropic(api_key=self._api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        model = os.environ.get("MODEL") or DEFAULT_MODELS[self.name]
        resp = client.messages.create(
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_output_tokens,
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        truncated = resp.stop_reason == "max_tokens"
        return text, truncated


_DISPATCH = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "chatgpt": OpenAIProvider,  # alias, since the org refers to it that way
    "claude": ClaudeProvider,
    "anthropic": ClaudeProvider,  # alias
}

# Alias -> canonical name, so anything keying off DEFAULT_MODELS (e.g.
# carlos_review.py's log/error messages) resolves the same model the
# dispatched provider instance actually uses.
_CANONICAL = {
    "gemini": "gemini",
    "openai": "openai",
    "chatgpt": "openai",
    "claude": "claude",
    "anthropic": "claude",
}


def canonical_provider_name(name: str) -> str:
    """Resolve an alias (e.g. 'chatgpt', 'anthropic') to its canonical key in
    DEFAULT_MODELS/_DISPATCH. Unknown names pass through unchanged so
    get_provider() is the single place that raises on a bad PROVIDER value."""
    key = (name or "gemini").strip().lower()
    return _CANONICAL.get(key, key)


def get_provider(name: str, api_key: str | None = None) -> LLMProvider:
    """Instantiate the provider named by PROVIDER.

    api_key is optional: pass it explicitly when the caller has its own key
    source (e.g. the GitHub App reading a per-installation key from
    key_store.py). Omit it to fall back to the matching repo-secret env var
    (the GitHub Actions script's call path).

    Raises RuntimeError with a clear, actionable message if the name is
    unknown or (when api_key is omitted) its env var is missing.
    """
    key = (name or "gemini").strip().lower()
    cls = _DISPATCH.get(key)
    if cls is None:
        raise RuntimeError(
            f"Unknown PROVIDER '{name}'. Supported values: "
            f"gemini, openai (chatgpt), claude (anthropic)."
        )
    if api_key is None:
        canonical = _CANONICAL.get(key, key)
        api_key = _require_env_key(_ENV_VARS[canonical], key)
    return cls(api_key)
