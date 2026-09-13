"""LLM provider abstraction.

This is the ONLY place agent modules should talk to an LLM API. It is
intentionally a "real LLM" abstraction only - Groq and OpenAI - with no
mock branch inside it. Mock/DEMO_MODE behaviour lives at the call-site
(QueryPlanner, ClaimExtractor, EntailmentChecker, ReportGenerator etc. each
implement their own deterministic heuristic fallback) so that "the LLM was
never actually called" is always an explicit, visible code path rather than
a fake LLM pretending to reason. See app/llm/factory.py.

complete_json() asks the model to emit JSON matching a Pydantic schema and
validates the result, retrying once with the validation error appended to
the prompt if parsing/validation fails.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.rate_limiter import TokenRateLimiter, estimate_tokens

logger = logging.getLogger("financial_research_agent.llm")

T = TypeVar("T", bound=BaseModel)

# Fraction of a call's max_tokens allowance to reserve up front as the
# expected completion size. max_tokens is a ceiling, not a prediction -
# real completions here run well under half of it - and reserving the full
# ceiling made the limiter pace as if every call were worst-case, roughly
# halving throughput. Reserving a realistic share is safe because
# _call_provider immediately reconciles the reservation against the
# provider's reported actual usage (in either direction) once the call
# returns.
COMPLETION_ESTIMATE_RATIO = 0.4


class _NotGiven:
    """Sentinel distinguishing "caller didn't pass an llm, look one up from
    settings" from "caller explicitly passed llm=None, force the mock
    heuristic path". Every agent/extraction/verification module that takes
    an optional `llm` constructor argument must default to NOT_GIVEN, never
    to None directly - `llm if llm is not None else get_llm_provider()`
    cannot tell an explicit None apart from an omitted argument, which
    silently defeated the evaluation harness's "no live API calls"
    guarantee whenever real credentials were configured (see git history)."""

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = _NotGiven()


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str = "base"
    _rate_limiter: TokenRateLimiter | None = None

    @abstractmethod
    def _raw_complete(self, system: str, user: str, max_tokens: int, temperature: float) -> str: ...

    # Set by _raw_complete implementations to the provider-reported total
    # token usage of the most recent call, so the rate limiter can replace
    # its pre-call worst-case estimate with reality. None means "provider
    # didn't report usage" and the estimate simply stands.
    _last_usage_tokens: int | None = None

    def _call_provider(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        """Every call site (complete() and complete_json()'s retry loop)
        goes through here so rate-limit pacing (see app/llm/rate_limiter.py)
        applies uniformly - set self._rate_limiter in a subclass's __init__
        to enable it (see GroqProvider)."""
        if self._rate_limiter is None:
            return self._raw_complete(system, user, max_tokens, temperature)

        reservation = self._rate_limiter.acquire(
            estimate_tokens(system, user, completion_budget=int(max_tokens * COMPLETION_ESTIMATE_RATIO))
        )
        self._last_usage_tokens = None
        try:
            return self._raw_complete(system, user, max_tokens, temperature)
        finally:
            # Correct the reservation with real usage where the provider
            # reports it - otherwise the window stays inflated by the
            # unused part of the max_tokens allowance and we throttle far
            # harder than the quota actually requires.
            if self._last_usage_tokens:
                self._rate_limiter.reconcile(reservation, self._last_usage_tokens)

    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.0) -> str:
        return self._call_provider(system, user, max_tokens, temperature)

    def complete_json(
        self,
        system: str,
        user: str,
        schema_model: type[T],
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> T:
        schema_instructions = (
            f"\n\nRespond with ONLY a single valid JSON object matching this JSON schema "
            f"(no markdown fences, no commentary, no extra text before or after):\n"
            f"{json.dumps(schema_model.model_json_schema())}"
        )
        full_system = system + schema_instructions
        last_error: Exception | None = None
        user_msg = user
        for attempt in range(2):
            raw = self._call_provider(full_system, user_msg, max_tokens, temperature)
            cleaned = _strip_code_fences(raw)
            try:
                data = json.loads(cleaned)
                return schema_model.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                logger.warning("complete_json parse failure (attempt %d): %s", attempt + 1, exc)
                user_msg = (
                    f"{user}\n\nYour previous response was invalid: {exc}\n"
                    f"Previous response was:\n{raw}\n"
                    f"Return ONLY corrected valid JSON matching the schema."
                )
        raise LLMError(f"LLM failed to produce valid JSON for schema {schema_model.__name__}: {last_error}")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
