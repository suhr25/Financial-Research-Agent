"""LLM Entailment Checker (PRD section 12).

Given ONLY a claim and its raw evidence text - never the generated report,
never other claims, never a summary - determines whether the evidence
supports, contradicts, or is insufficient for the claim. This isolation is
what prevents the "verifier grading its own homework" failure mode the PRD
explicitly calls out (section 5.1.4): the checker cannot see what the
Report Generator wrote, only the same primary evidence the claim itself was
extracted from.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.llm import NOT_GIVEN, LLMProvider, get_llm_provider
from app.schemas import Claim, ClaimType, EntailmentResult, VerificationVerdict

logger = logging.getLogger("financial_research_agent.verification.entailment_checker")

ENTAILMENT_SYSTEM_PROMPT = """You are the Entailment Checker of a financial research agent's verification engine.

You will be given a CLAIM and a piece of SOURCE EVIDENCE - the verbatim, original text the claim
was supposedly extracted from. You have NO OTHER CONTEXT about this company or research task.

Determine whether the SOURCE EVIDENCE:
- SUPPORTED: clearly confirms the claim as stated (value, period and basis all consistent)
- CONTRADICTED: clearly conflicts with the claim (e.g. a different number, period, or basis is stated)
- INSUFFICIENT: does not contain enough information to confirm or deny the claim

Be strict: a claim is only SUPPORTED if the evidence text actually says what the claim says, not
merely something related. Do not use outside/world knowledge about the company - judge only from
the evidence text given.
"""

# One research run can produce 20-30+ claims. Checking each with its own LLM
# call is what exhausts a free-tier per-minute token budget. Batching several
# independent (claim, evidence) pairs into one call cuts call count by this
# factor while keeping each pair's verdict judged only against its own
# evidence text - the isolation the PRD requires (see module docstring) is
# about never seeing the generated report, not about one call per claim.
ENTAILMENT_BATCH_SIZE = 6

BATCH_ENTAILMENT_SYSTEM_PROMPT = """You are the Entailment Checker of a financial research agent's verification engine.

You will be given several independent CLAIM/EVIDENCE pairs, each numbered. For EACH pair,
using ONLY that pair's own evidence text (never information from a different pair, never
outside/world knowledge), determine whether the evidence:
- SUPPORTED: clearly confirms the claim as stated (value, period and basis all consistent)
- CONTRADICTED: clearly conflicts with the claim (e.g. a different number, period, or basis)
- INSUFFICIENT: does not contain enough information to confirm or deny the claim

Be strict: a claim is only SUPPORTED if its evidence text actually says what the claim says.
Judge every numbered pair completely independently of the others - do not let one pair's
evidence or verdict influence another's. Return one result per numbered pair, using the same
claim_number given.
"""


class _BatchEntailmentItem(BaseModel):
    claim_number: int
    verdict: VerificationVerdict
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class _BatchEntailmentOutput(BaseModel):
    results: list[_BatchEntailmentItem]


class EntailmentChecker:
    def __init__(self, llm: LLMProvider | None = NOT_GIVEN):
        self.llm = get_llm_provider() if llm is NOT_GIVEN else llm

    def check(self, claim: Claim) -> EntailmentResult:
        if self.llm is not None:
            try:
                return self._llm_check(claim)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM entailment check failed for claim=%s (%s); using mock checker", claim.claim_id, exc)
        return self._mock_check(claim)

    def check_batch(self, claims: list[Claim]) -> dict[str, EntailmentResult]:
        """Same semantics as check(), but processes many claims in chunks of
        ENTAILMENT_BATCH_SIZE per LLM call instead of one call per claim.
        Returns a dict keyed by claim_id so callers don't need to track
        ordering. If a chunk's LLM call fails, only that chunk falls back to
        the mock heuristic - other chunks are unaffected."""
        if not claims:
            return {}
        if self.llm is None:
            return {c.claim_id: self._mock_check(c) for c in claims}

        results: dict[str, EntailmentResult] = {}
        for start in range(0, len(claims), ENTAILMENT_BATCH_SIZE):
            chunk = claims[start : start + ENTAILMENT_BATCH_SIZE]
            try:
                results.update(self._llm_check_batch(chunk))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM batch entailment check failed for %d claim(s) (%s); using mock checker for this chunk",
                    len(chunk), exc,
                )
                for c in chunk:
                    results[c.claim_id] = self._mock_check(c)
        return results

    def _llm_check_batch(self, claims: list[Claim]) -> dict[str, EntailmentResult]:
        parts = []
        for i, claim in enumerate(claims, start=1):
            parts.append(
                f"--- PAIR {i} ---\n"
                f"CLAIM {i}:\n"
                f"  Statement: {claim.statement}\n"
                f"  Entity: {claim.entity}\n"
                f"  Metric: {claim.metric}\n"
                f"  Value: {claim.value} {claim.unit or ''}\n"
                f"  Period: {claim.period or 'unspecified'}\n"
                f"  Basis: {claim.basis.value}\n"
                f"EVIDENCE {i} (verbatim, the ONLY information you may use for CLAIM {i}):\n"
                f'"""\n{claim.evidence_span.evidence_text}\n"""\n'
            )
        user_prompt = "\n".join(parts) + f"\n\nReturn exactly {len(claims)} result(s), one per claim_number 1..{len(claims)}."

        output = self.llm.complete_json(
            system=BATCH_ENTAILMENT_SYSTEM_PROMPT,
            user=user_prompt,
            schema_model=_BatchEntailmentOutput,
            max_tokens=250 * len(claims) + 300,
        )

        by_number = {item.claim_number: item for item in output.results}
        results: dict[str, EntailmentResult] = {}
        for i, claim in enumerate(claims, start=1):
            item = by_number.get(i)
            if item is None:
                logger.warning("Batch entailment response missing claim_number=%d; using mock checker for it", i)
                results[claim.claim_id] = self._mock_check(claim)
            else:
                results[claim.claim_id] = EntailmentResult(verdict=item.verdict, reason=item.reason, confidence=item.confidence)
        return results

    def _llm_check(self, claim: Claim) -> EntailmentResult:
        user_prompt = (
            f"CLAIM:\n"
            f"  Statement: {claim.statement}\n"
            f"  Entity: {claim.entity}\n"
            f"  Metric: {claim.metric}\n"
            f"  Value: {claim.value} {claim.unit or ''}\n"
            f"  Period: {claim.period or 'unspecified'}\n"
            f"  Basis: {claim.basis.value}\n\n"
            f"SOURCE EVIDENCE (verbatim, this is the ONLY information you may use to judge the claim):\n"
            f"\"\"\"\n{claim.evidence_span.evidence_text}\n\"\"\"\n"
        )
        return self.llm.complete_json(
            system=ENTAILMENT_SYSTEM_PROMPT, user=user_prompt, schema_model=EntailmentResult, max_tokens=500
        )

    # ---- Mock path -------------------------------------------------------

    def _mock_check(self, claim: Claim) -> EntailmentResult:
        if claim.claim_type == ClaimType.NUMERIC:
            return self._mock_check_numeric(claim)
        return self._mock_check_qualitative(claim)

    def _mock_check_numeric(self, claim: Claim) -> EntailmentResult:
        from app.verification.numeric_matcher import NumericMatcher

        result = NumericMatcher().match(claim)
        if not result.applicable:
            return EntailmentResult(
                verdict=VerificationVerdict.INSUFFICIENT,
                reason="Claim value could not be parsed as a number to compare against evidence.",
                confidence=0.3,
            )
        if result.normalized_match:
            return EntailmentResult(
                verdict=VerificationVerdict.SUPPORTED,
                reason=f"Evidence contains a matching numeric value ({result.notes}).",
                confidence=0.92,
            )
        if result.percent_difference is not None and result.percent_difference > 2.0:
            return EntailmentResult(
                verdict=VerificationVerdict.CONTRADICTED,
                reason=f"Evidence contains a comparable figure that differs by {result.percent_difference:.2f}%, exceeding tolerance.",
                confidence=0.85,
            )
        return EntailmentResult(
            verdict=VerificationVerdict.INSUFFICIENT,
            reason="No comparable numeric value with a matching unit/currency found in evidence text.",
            confidence=0.4,
        )

    def _mock_check_qualitative(self, claim: Claim) -> EntailmentResult:
        evidence_lower = claim.evidence_span.evidence_text.lower()
        value_lower = claim.value.lower().strip()
        if value_lower and (value_lower in evidence_lower or evidence_lower in value_lower):
            return EntailmentResult(
                verdict=VerificationVerdict.SUPPORTED,
                reason="Claim text is directly present in the source evidence.",
                confidence=0.88,
            )
        overlap = _word_overlap_ratio(value_lower, evidence_lower)
        if overlap >= 0.6:
            return EntailmentResult(
                verdict=VerificationVerdict.SUPPORTED,
                reason=f"Claim substantially overlaps with source evidence (word overlap={overlap:.2f}).",
                confidence=0.7,
            )
        return EntailmentResult(
            verdict=VerificationVerdict.INSUFFICIENT,
            reason="Insufficient textual overlap between claim and evidence to confirm support.",
            confidence=0.35,
        )


def _word_overlap_ratio(a: str, b: str) -> float:
    words_a = set(w for w in a.split() if len(w) > 2)
    words_b = set(w for w in b.split() if len(w) > 2)
    if not words_a:
        return 0.0
    return len(words_a & words_b) / len(words_a)
