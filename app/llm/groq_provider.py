from __future__ import annotations

from app.config import Settings
from app.llm.openai_provider import OpenAIProvider


class GroqProvider(OpenAIProvider):
    """Groq's chat-completions API is OpenAI-compatible, so this just points
    the OpenAI SDK at Groq's base URL with a Groq key/model instead."""

    name = "groq"

    # Groq's gpt-oss models are reasoning models; "low" keeps reasoning
    # token spend minimal (measured: ~8 reasoning tokens vs hundreds
    # unbounded), which both prevents truncated/empty JSON responses and
    # conserves the per-minute token budget. Harmless on Groq models that
    # ignore it.
    _reasoning_effort = "low"

    def __init__(self, settings: Settings):
        super().__init__(
            settings,
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            base_url="https://api.groq.com/openai/v1",
        )
