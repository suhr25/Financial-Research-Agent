"""Verifies the LLM-skip optimization in VerificationEngine.verify_all:
a claim the deterministic numeric matcher already resolved conclusively
must never reach the LLM at all (not just "use a cheap mock path" - never
even attempt a call), since that's the whole point of the optimization
under a rate-limited real provider. Qualitative/inconclusive claims must
still go through the LLM.
"""
import json

from app.analysis.normalizer import ClaimNormalizer
from app.llm.base import LLMProvider
from app.schemas import Basis, Claim, ClaimType, Evidence, VerificationVerdict
from app.verification.verification_engine import VerificationEngine


class _ExplodingLLM(LLMProvider):
    """Fails the test if the verification engine ever calls it - used to
    prove a conclusive numeric claim is skipped entirely, not just cheaply
    mocked."""

    def _raw_complete(self, system, user, max_tokens, temperature):
        raise AssertionError("LLM should not have been called for a conclusive numeric match")


class _FakeLLM(LLMProvider):
    def __init__(self, response_json: dict):
        self._response = json.dumps(response_json)

    def _raw_complete(self, system, user, max_tokens, temperature):
        return self._response


def _numeric_claim(value: str, unit: str, evidence_text: str) -> Claim:
    claim = Claim(
        research_run_id="run_test",
        claim_type=ClaimType.NUMERIC,
        entity="Apple Inc.",
        metric="revenue",
        value=value,
        unit=unit,
        period="Q3 2024",
        basis=Basis.UNKNOWN,
        source_id="src_test",
        evidence_span=Evidence(source_id="src_test", start_char=0, end_char=len(evidence_text), evidence_text=evidence_text),
        statement=f"revenue was {value} {unit}",
    )
    ClaimNormalizer().normalize([claim])
    return claim


def _qualitative_claim(text: str) -> Claim:
    return Claim(
        research_run_id="run_test",
        claim_type=ClaimType.QUALITATIVE,
        entity="Apple Inc.",
        metric="risk_factor",
        value=text,
        basis=Basis.UNKNOWN,
        source_id="src_test",
        evidence_span=Evidence(source_id="src_test", start_char=0, end_char=len(text), evidence_text=text),
        statement=text,
    )


def test_conclusive_numeric_match_never_calls_the_llm():
    claim = _numeric_claim("85.8", "billion", "revenue was $85.8 billion in Q3 2024")
    engine = VerificationEngine(llm=_ExplodingLLM())
    results = engine.verify_all([claim])
    assert len(results) == 1
    assert results[0].final_verdict == VerificationVerdict.SUPPORTED
    assert "not sent to the llm" in results[0].entailment_result.reason.lower()


def test_conclusive_numeric_match_with_no_period_in_evidence_is_insufficient_without_llm():
    """period_match is also deterministic - a numeric match whose evidence
    never states the claimed period is still resolved without the LLM."""
    claim = _numeric_claim("85.8", "billion", "revenue was $85.8 billion")
    engine = VerificationEngine(llm=_ExplodingLLM())
    results = engine.verify_all([claim])
    assert results[0].final_verdict == VerificationVerdict.INSUFFICIENT


def test_conclusive_numeric_mismatch_never_calls_the_llm():
    claim = _numeric_claim("99.9", "billion", "revenue was $50.0 billion")
    engine = VerificationEngine(llm=_ExplodingLLM())
    results = engine.verify_all([claim])
    assert results[0].final_verdict == VerificationVerdict.CONTRADICTED
    assert "not sent to the llm" in results[0].entailment_result.reason.lower()


def test_qualitative_claim_still_uses_the_llm():
    claim = _qualitative_claim("The company faces significant regulatory risk.")
    fake = _FakeLLM({"results": [{"claim_number": 1, "verdict": "contradicted", "reason": "llm said so", "confidence": 0.77}]})
    engine = VerificationEngine(llm=fake)
    results = engine.verify_all([claim])
    assert results[0].entailment_result.reason == "llm said so"
    assert results[0].final_verdict == VerificationVerdict.CONTRADICTED


def test_mixed_batch_only_sends_inconclusive_claims_to_the_llm():
    conclusive = _numeric_claim("85.8", "billion", "revenue was $85.8 billion")
    needs_llm = _qualitative_claim("The company faces significant regulatory risk.")
    fake = _FakeLLM({"results": [{"claim_number": 1, "verdict": "supported", "reason": "from llm", "confidence": 0.8}]})
    engine = VerificationEngine(llm=fake)
    results = {r.claim_id: r for r in engine.verify_all([conclusive, needs_llm])}
    assert "not sent to the llm" in results[conclusive.claim_id].entailment_result.reason.lower()
    assert results[needs_llm.claim_id].entailment_result.reason == "from llm"
