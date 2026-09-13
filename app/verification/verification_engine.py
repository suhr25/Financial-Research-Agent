"""Verification Engine: combines the deterministic NumericMatcher and the
LLM EntailmentChecker into one final verdict per claim (PRD section 12,
"dual-path verification").

Combination rule (documented, deterministic, and gives priority to
non-LLM evidence per the project's core instruction "do not use the LLM for
things that can be deterministically verified"):
  - If the NumericMatcher found a conclusive normalized match -> SUPPORTED.
    verify_all() doesn't even call the LLM for these (see below); verify()
    (the single-claim path) still does, and lets a confident LLM
    CONTRADICTED override it - e.g. catching a period/basis mismatch prose
    alone reveals but pure number-matching cannot.
  - If the NumericMatcher found a conclusive numeric mismatch -> CONTRADICTED.
  - Otherwise (numeric check inconclusive, or claim is qualitative) -> defer
    to the LLM entailment verdict.

verify_all() (used by the orchestrator for a whole run) additionally skips
the LLM call entirely for any claim the numeric matcher already resolved
conclusively - see its docstring.
"""
from __future__ import annotations

from app.llm import NOT_GIVEN, LLMProvider
from app.schemas import Claim, ClaimType, EntailmentResult, VerificationResult, VerificationVerdict
from app.verification.entailment_checker import EntailmentChecker
from app.verification.numeric_matcher import NumericMatcher

# Confidence assigned to a verdict reached from the numeric matcher alone,
# without an LLM call - high because it's an exact/near-exact deterministic
# string match, not a guess.
_NUMERIC_ONLY_CONFIDENCE = 0.95


class VerificationEngine:
    def __init__(self, llm: LLMProvider | None = NOT_GIVEN):
        self.numeric_matcher = NumericMatcher()
        self.entailment_checker = EntailmentChecker(llm)

    def verify(self, claim: Claim) -> VerificationResult:
        numeric_result = self.numeric_matcher.match(claim) if claim.claim_type == ClaimType.NUMERIC else None
        entailment_result = self.entailment_checker.check(claim)
        final_verdict, combined_reason = self._combine(numeric_result, entailment_result)
        return VerificationResult(
            claim_id=claim.claim_id,
            numeric_result=numeric_result,
            entailment_result=entailment_result,
            final_verdict=final_verdict,
            combined_reason=combined_reason,
        )

    def verify_all(self, claims: list[Claim]) -> list[VerificationResult]:
        """Batched equivalent of calling verify() per claim, with one more
        optimization on top of batching: a claim is only sent to the LLM at
        all if the deterministic numeric matcher didn't already reach a
        conclusive verdict on its own. This is the project's core rule taken
        literally ("do not use the LLM for things that can be
        deterministically verified") - most extracted numeric claims get a
        clean exact/normalized match, so skipping the LLM for those cuts
        real-mode LLM call volume (and therefore latency under a paced
        rate limit) substantially without weakening verification: a
        deterministic string/number match doesn't become less true for not
        also asking an LLM to confirm it."""
        if not claims:
            return []
        numeric_results = {
            c.claim_id: (self.numeric_matcher.match(c) if c.claim_type == ClaimType.NUMERIC else None) for c in claims
        }

        needs_llm: list[Claim] = []
        conclusive: dict[str, EntailmentResult] = {}
        for claim in claims:
            nr = numeric_results[claim.claim_id]
            if nr is not None and nr.applicable and nr.normalized_match:
                conclusive[claim.claim_id] = EntailmentResult(
                    verdict=VerificationVerdict.SUPPORTED,
                    reason=f"Deterministic numeric match was conclusive ({nr.notes}) - not sent to the LLM.",
                    confidence=_NUMERIC_ONLY_CONFIDENCE,
                )
            elif nr is not None and nr.applicable and nr.percent_difference is not None and nr.percent_difference > 2.0:
                conclusive[claim.claim_id] = EntailmentResult(
                    verdict=VerificationVerdict.CONTRADICTED,
                    reason=f"Deterministic numeric mismatch was conclusive ({nr.notes}) - not sent to the LLM.",
                    confidence=_NUMERIC_ONLY_CONFIDENCE,
                )
            else:
                needs_llm.append(claim)

        entailment_results = {**conclusive, **self.entailment_checker.check_batch(needs_llm)}

        results = []
        for claim in claims:
            numeric_result = numeric_results[claim.claim_id]
            entailment_result = entailment_results[claim.claim_id]
            final_verdict, combined_reason = self._combine(numeric_result, entailment_result)
            results.append(
                VerificationResult(
                    claim_id=claim.claim_id,
                    numeric_result=numeric_result,
                    entailment_result=entailment_result,
                    final_verdict=final_verdict,
                    combined_reason=combined_reason,
                )
            )
        return results

    @staticmethod
    def _combine(numeric_result, entailment_result) -> tuple[VerificationVerdict, str]:
        if numeric_result is not None and numeric_result.applicable:
            if numeric_result.normalized_match:
                if numeric_result.period_match is False:
                    return (
                        VerificationVerdict.INSUFFICIENT,
                        f"Evidence contains a matching numeric value, but does not appear to reference the "
                        f"claimed period ({numeric_result.notes}); cannot confirm the figure applies to the "
                        f"stated period.",
                    )
                if entailment_result.verdict == VerificationVerdict.CONTRADICTED and entailment_result.confidence >= 0.75:
                    return (
                        VerificationVerdict.CONTRADICTED,
                        f"Deterministic numeric matcher found a matching value, but the LLM entailment "
                        f"check independently found a contradiction: {entailment_result.reason}",
                    )
                return (
                    VerificationVerdict.SUPPORTED,
                    f"Deterministic numeric match ({numeric_result.notes}). LLM entailment agreed: {entailment_result.reason}",
                )
            if numeric_result.percent_difference is not None and numeric_result.percent_difference > 2.0:
                return (
                    VerificationVerdict.CONTRADICTED,
                    f"Deterministic numeric mismatch ({numeric_result.notes}).",
                )

        return entailment_result.verdict, f"LLM entailment check: {entailment_result.reason}"
