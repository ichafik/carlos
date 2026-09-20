"""
LLM provider abstraction for Carlos — "bring your own key" across ChatGPT, Gemini
and Claude.

Each repo picks ONE provider via the `PROVIDER` env var (default: gemini) and
supplies that provider's own API key as a repo secret. carlos_review.py never
talks to an SDK directly; it calls get_provider(name).generate(...) and gets
back plain text plus a truncation flag, so the rest of the script (JSON
parsing, scoring, rendering) stays provider-agnostic.

Adding a fourth provider later means: one new class implementing generate(),
one line in get_provider()'s dispatch table, one new secret. Everything else
in carlos_review.py is unaffected.
"""

import os
from abc import ABC, abstractmethod

# Sensible defaults per provider; override any of them with the MODEL env var.
DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "openai": "gpt-4.1",
    "claude": "claude-sonnet-4-5",
}


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


def _require_key(env_var: str, provider_name: str) -> str:
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

    def __init__(self):
        self._api_key = _require_key("GEMINI_API_KEY", self.name)

    def generate(self, prompt, system_prompt, seed, temperature, max_output_tokens):
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
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

    def __init__(self):
        self._api_key = _require_key("OPENAI_API_KEY", self.name)

    def generate(self, prompt, system_prompt, seed, temperature, max_output_tokens):
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key)
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

    def __init__(self):
        self._api_key = _require_key("ANTHROPIC_API_KEY", self.name)

    def generate(self, prompt, system_prompt, seed, temperature, max_output_tokens):
        import anthropic

        if not ClaudeProvider._warned_no_seed:
            print("[carlos] note: PROVIDER=claude ignores SEED (Anthropic API has no "
                  "seed parameter); reproducibility across runs is best-effort only.")
            ClaudeProvider._warned_no_seed = True

        client = anthropic.Anthropic(api_key=self._api_key)
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


def get_provider(name: str) -> LLMProvider:
    """Instantiate the provider named by PROVIDER. Raises RuntimeError with a
    clear, actionable message if the name is unknown or its key is missing."""
    key = (name or "gemini").strip().lower()
    cls = _DISPATCH.get(key)
    if cls is None:
        raise RuntimeError(
            f"Unknown PROVIDER '{name}'. Supported values: "
            f"gemini, openai (chatgpt), claude (anthropic)."
        )
    return cls()
