from __future__ import annotations

from app.config import Settings
from app.llm.base import LLMProvider
from app.llm.rate_limiter import TokenRateLimiter


class OpenAIProvider(LLMProvider):
    """Also the base for any OpenAI-compatible chat-completions API (e.g.
    Groq - see GroqProvider below) via the optional `base_url` override."""

    name = "openai"

    def __init__(self, settings: Settings, api_key: str | None = None, model: str | None = None, base_url: str | None = None):
        from openai import OpenAI

        # A single research run can trigger dozens of LLM calls (one per
        # source for extraction, one per claim-batch for entailment). On a
        # free-tier rate limit, the SDK's default retry-with-backoff would
        # stack multi-second delays across every one of those calls and
        # blow up total request latency. Fail fast instead - a single
        # 429/5xx is caught by the caller and falls back to the
        # already-tested deterministic mock heuristic for that one item,
        # which is far better than the whole run stalling for minutes.
        # If llm_tpm_limit is configured, calls are paced (see rate_limiter)
        # to fit under it instead, so they succeed for real rather than
        # racing the limit and failing.
        self._client = OpenAI(api_key=api_key or settings.openai_api_key, base_url=base_url, max_retries=0, timeout=45.0)
        self._model = model or settings.openai_model
        if settings.llm_tpm_limit:
            self._rate_limiter = TokenRateLimiter(settings.llm_tpm_limit)

    # Reasoning models (e.g. Groq's openai/gpt-oss-*) spend completion
    # tokens on internal reasoning BEFORE emitting any answer. Left
    # unbounded that silently truncated our JSON responses to empty
    # strings, and it burns the per-minute token budget for no benefit on
    # what are structured-extraction tasks, not puzzles. Subclasses set
    # this to "low" where the API supports it; None omits the parameter so
    # providers that don't know it aren't sent an unexpected field.
    _reasoning_effort: str | None = None

    def _raw_complete(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        kwargs = {}
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        usage = getattr(response, "usage", None)
        self._last_usage_tokens = getattr(usage, "total_tokens", None) if usage else None
        return response.choices[0].message.content or ""
