import time

from app.llm.rate_limiter import TokenRateLimiter, estimate_tokens


def test_acquire_does_not_block_when_under_budget():
    limiter = TokenRateLimiter(tokens_per_minute=1000)
    start = time.monotonic()
    limiter.acquire(100)
    limiter.acquire(100)
    assert time.monotonic() - start < 0.5


def test_acquire_blocks_until_window_frees_capacity():
    """Use a short window so the test doesn't take 60s: budget of 10 tokens
    per 0.3s window - a second call needing 6 tokens right after a 6-token
    call must wait for the window to roll over."""
    limiter = TokenRateLimiter(tokens_per_minute=10, window_seconds=0.3)
    limiter.acquire(6)
    start = time.monotonic()
    limiter.acquire(6)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # had to wait for the first entry to age out


def test_single_call_never_exceeds_whole_budget_estimate():
    limiter = TokenRateLimiter(tokens_per_minute=100)
    # Requesting more than the total budget must be capped, not hang forever.
    start = time.monotonic()
    limiter.acquire(10_000)
    assert time.monotonic() - start < 0.5


def test_estimate_tokens_scales_with_text_length_and_completion_budget():
    short = estimate_tokens("hi", completion_budget=0)
    long = estimate_tokens("a" * 4000, completion_budget=0)
    assert long > short
    assert estimate_tokens("", completion_budget=500) == 500


def test_reconcile_frees_capacity_when_actual_usage_is_below_the_estimate():
    """A call reserves its worst-case max_tokens allowance up front; once
    the provider reports real (much smaller) usage, that capacity must be
    released or the limiter throttles far harder than the quota requires."""
    limiter = TokenRateLimiter(tokens_per_minute=1000, window_seconds=60)
    reservation = limiter.acquire(900)
    limiter.reconcile(reservation, 100)

    # 900 was reserved but only 100 truly used, so a second 800-token call
    # must proceed immediately rather than waiting for the window to roll.
    start = time.monotonic()
    limiter.acquire(800)
    assert time.monotonic() - start < 0.5


def test_reconcile_ignores_invalid_input():
    limiter = TokenRateLimiter(tokens_per_minute=1000)
    reservation = limiter.acquire(100)
    limiter.reconcile(reservation, 0)      # non-positive usage ignored
    limiter.reconcile(None, 500)           # bad handle ignored, no crash
