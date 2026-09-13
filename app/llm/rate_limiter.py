"""Sliding-window token-per-minute rate limiter for LLM calls.

Motivation (measured, not assumed): a free-tier key can have a hard
tokens-per-minute ceiling far below what one research run needs (Groq's free
tier measured at 8,000-12,000 TPM depending on model, against a pipeline
that can need 20,000-40,000+ tokens per run). Firing every call as soon as
it's ready races past that ceiling and most calls get rejected with 429,
falling back to the mock heuristic. Pacing calls to fit the budget trades
latency for reliability - a run takes longer, but every call gets a real
chance to succeed instead of being rejected outright.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger("financial_research_agent.llm.rate_limiter")


class TokenRateLimiter:
    def __init__(self, tokens_per_minute: int, window_seconds: float = 60.0):
        if tokens_per_minute <= 0:
            raise ValueError("tokens_per_minute must be positive")
        self.budget = tokens_per_minute
        self.window = window_seconds
        self._usage: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _prune_and_sum(self, now: float) -> int:
        while self._usage and now - self._usage[0][0] > self.window:
            self._usage.popleft()
        return sum(tokens for _, tokens in self._usage)

    def acquire(self, estimated_tokens: int) -> object:
        """Blocks until making a call for `estimated_tokens` would keep the
        trailing window's total under budget, then reserves that capacity.
        A single call's estimate is capped at the whole budget - otherwise a
        big call could never proceed under this limiter.

        Returns an opaque reservation handle; pass it to reconcile() once
        the provider reports the call's ACTUAL token usage.
        """
        estimated_tokens = min(max(estimated_tokens, 1), self.budget)
        while True:
            with self._lock:
                now = time.monotonic()
                used = self._prune_and_sum(now)
                if used + estimated_tokens <= self.budget:
                    entry = [now, estimated_tokens]
                    self._usage.append(entry)
                    return entry
                oldest_ts = self._usage[0][0]
                wait_for = max(self.window - (now - oldest_ts) + 0.05, 0.1)
            sleep_for = min(wait_for, 5.0)
            logger.info(
                "LLM rate limiter: pacing call (%d est. tokens, %d/%d used in trailing %.0fs) - waiting %.1fs",
                estimated_tokens, used, self.budget, self.window, sleep_for,
            )
            time.sleep(sleep_for)

    def reconcile(self, reservation: object, actual_tokens: int) -> None:
        """Replaces a reservation's estimate with the provider-reported
        actual usage.

        Without this, the window stays inflated by however much the
        estimate over-reserved. That matters a lot: a call reserves its
        full `max_tokens` output allowance up front (it cannot know the
        real completion length in advance), but real completions are
        typically a fraction of that - so the limiter would throttle to
        roughly half the throughput the quota actually allows.
        """
        if not isinstance(reservation, list) or actual_tokens <= 0:
            return
        with self._lock:
            reservation[1] = min(actual_tokens, self.budget)


def estimate_tokens(*texts: str, completion_budget: int = 0) -> int:
    """Rough pre-call estimate (~4 characters/token for English text).

    `completion_budget` should be the caller's max_tokens allowance: before
    the call there is no way to know the true completion length, so we
    reserve the worst case and then correct it via
    TokenRateLimiter.reconcile() once the response reports real usage.
    """
    chars = sum(len(t) for t in texts if t)
    return chars // 4 + completion_budget
