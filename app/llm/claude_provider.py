from __future__ import annotations

from app.config import Settings
from app.llm.base import LLMProvider
from app.llm.rate_limiter import TokenRateLimiter


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, settings: Settings):
        import anthropic

        # See app/llm/openai_provider.py: fail fast on rate limits rather
        # than stacking the SDK's default retry backoff across the many
        # LLM calls one research run makes.
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0, timeout=20.0)
        self._model = settings.anthropic_model
        if settings.llm_tpm_limit:
            self._rate_limiter = TokenRateLimiter(settings.llm_tpm_limit)

    def _raw_complete(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._last_usage_tokens = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
        else:
            self._last_usage_tokens = None
        return "".join(block.text for block in response.content if block.type == "text")
