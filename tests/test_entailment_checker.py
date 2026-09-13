import json

from app.llm.base import LLMProvider
from app.schemas import Basis, Claim, ClaimType, Evidence, VerificationVerdict
from app.verification.entailment_checker import EntailmentChecker


class _FakeLLM(LLMProvider):
    """Returns a canned response regardless of prompt, so batching/mapping
    logic can be tested without a real network call."""

    def __init__(self, response_json: dict):
        self._response = json.dumps(response_json)

    def _raw_complete(self, system, user, max_tokens, temperature):
        return self._response


class _FailingLLM(LLMProvider):
    def _raw_complete(self, system, user, max_tokens, temperature):
        raise RuntimeError("simulated LLM failure")


def _qualitative_claim(value: str, evidence_text: str) -> Claim:
    return Claim(
        research_run_id="run_test",
        claim_type=ClaimType.QUALITATIVE,
        entity="Apple Inc.",
        metric="risk_factor",
        value=value,
        basis=Basis.UNKNOWN,
        source_id="src_test",
        evidence_span=Evidence(source_id="src_test", start_char=0, end_char=len(evidence_text), evidence_text=evidence_text),
        statement=value,
    )


def test_qualitative_claim_directly_present_in_evidence_is_supported():
    evidence = "The company faces substantial competition in all of its markets."
    claim = _qualitative_claim(evidence, evidence)
    result = EntailmentChecker(llm=None).check(claim)
    assert result.verdict == VerificationVerdict.SUPPORTED
    assert 0.0 <= result.confidence <= 1.0


def test_qualitative_claim_unrelated_to_evidence_is_insufficient():
    evidence = "The weather was sunny with light winds across the region."
    claim = _qualitative_claim("The company faces significant regulatory risk in Europe.", evidence)
    result = EntailmentChecker(llm=None).check(claim)
    assert result.verdict == VerificationVerdict.INSUFFICIENT


def test_result_is_a_valid_structured_entailment_result():
    """PRD: entailment output must be a structured Pydantic result, not free text."""
    evidence = "Net income was $21.4 billion for the quarter."
    claim = _qualitative_claim(evidence, evidence)
    result = EntailmentChecker(llm=None).check(claim)
    assert result.verdict in (VerificationVerdict.SUPPORTED, VerificationVerdict.CONTRADICTED, VerificationVerdict.INSUFFICIENT)
    assert isinstance(result.reason, str) and result.reason


def test_check_batch_with_no_llm_uses_mock_for_every_claim():
    claims = [_qualitative_claim(f"claim {i}", f"claim {i}") for i in range(3)]
    results = EntailmentChecker(llm=None).check_batch(claims)
    assert set(results.keys()) == {c.claim_id for c in claims}
    for c in claims:
        assert results[c.claim_id].verdict == VerificationVerdict.SUPPORTED


def test_check_batch_maps_results_back_by_claim_number_not_list_order():
    """The fake LLM deliberately returns results out of order - verifies
    mapping is done by claim_number, not by position in the response list."""
    claims = [_qualitative_claim(f"claim-{i}", f"evidence-{i}") for i in range(1, 4)]
    fake = _FakeLLM(
        {
            "results": [
                {"claim_number": 3, "verdict": "contradicted", "reason": "r3", "confidence": 0.9},
                {"claim_number": 1, "verdict": "supported", "reason": "r1", "confidence": 0.8},
                {"claim_number": 2, "verdict": "insufficient", "reason": "r2", "confidence": 0.5},
            ]
        }
    )
    results = EntailmentChecker(llm=fake).check_batch(claims)
    assert results[claims[0].claim_id].verdict == VerificationVerdict.SUPPORTED
    assert results[claims[1].claim_id].verdict == VerificationVerdict.INSUFFICIENT
    assert results[claims[2].claim_id].verdict == VerificationVerdict.CONTRADICTED


def test_check_batch_falls_back_to_mock_per_chunk_on_llm_failure():
    """A failing LLM must not crash the whole run - each affected chunk
    falls back to the deterministic mock checker."""
    claims = [_qualitative_claim(f"claim {i}", f"claim {i}") for i in range(2)]
    results = EntailmentChecker(llm=_FailingLLM()).check_batch(claims)
    assert set(results.keys()) == {c.claim_id for c in claims}
    for c in claims:
        assert results[c.claim_id].verdict == VerificationVerdict.SUPPORTED


def test_check_batch_chunks_large_claim_lists():
    """More claims than ENTAILMENT_BATCH_SIZE must still all get a result -
    exercises the multi-chunk path."""
    from app.verification.entailment_checker import ENTAILMENT_BATCH_SIZE

    n = ENTAILMENT_BATCH_SIZE * 2 + 1
    claims = [_qualitative_claim(f"claim {i}", f"claim {i}") for i in range(n)]
    results = EntailmentChecker(llm=None).check_batch(claims)
    assert len(results) == n
