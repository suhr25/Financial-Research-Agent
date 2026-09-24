"""Claim Extractor: pulls numeric and qualitative claims out of retrieved
Source documents into the canonical Claim schema (PRD section 10).

Hard rule enforced in both paths: a claim's evidence_span must point at text
that verbatim exists in the source's document_text. The LLM path asks the
model to quote evidence exactly and discards any claim whose quote cannot be
located in the source (never trusts an LLM-invented span). The mock path
never has this problem by construction - it extracts directly from regex
match spans in the real (mock-labelled) document_text.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from app.llm import NOT_GIVEN, LLMProvider, get_llm_provider
from app.rag import retrieve_relevant_text
from app.retrieval.base import make_evidence
from app.schemas import Basis, Claim, ClaimType, Evidence, ResearchPlan, Source

logger = logging.getLogger("financial_research_agent.extraction.claim_extractor")

# Token-budget tuning. Under a free-tier per-minute token cap these three
# constants are what actually determine how long a real run takes, so they
# are deliberately conservative: financial figures and risk statements
# cluster near the top of filings/articles, so 3500 chars captures the
# useful part of almost every source we retrieve.
MAX_SOURCE_CHARS_FOR_LLM = 3500
SOURCE_BATCH_SIZE = 3
# Must leave room for a full batch's worth of JSON claims. Too low and a
# response gets truncated mid-JSON and is discarded entirely (observed as
# "Expecting value: line 1 column 1" with an empty completion), which
# wastes the whole call's token spend - the opposite of the goal.
MAX_EXTRACTION_COMPLETION_TOKENS = 2600


def _build_rag_query(plan: ResearchPlan) -> str:
    """Turns a ResearchPlan into the text query used for RAG chunk
    retrieval - what the LLM extractor should actually be looking for in a
    long source, rather than "whatever appears first". Falls back to a
    generic financial-metrics query when the planner didn't extract
    anything more specific (e.g. the deterministic mock planner)."""
    parts = [
        *plan.requested_metrics,
        *plan.financial_questions,
        *plan.qualitative_questions,
        *plan.risk_questions,
    ]
    if not parts:
        parts = ["revenue", "net income", "operating margin", "EBITDA", "risks", "earnings"]
    return " ".join(parts)


class _ExtractedClaimDraft(BaseModel):
    source_number: int = Field(default=1, description="Which numbered SOURCE block this claim's evidence came from")
    claim_type: ClaimType
    entity: str
    metric: str
    value: str
    unit: str | None = None
    period: str | None = None
    basis: Basis = Basis.UNKNOWN
    statement: str
    quoted_evidence: str = Field(description="Exact verbatim substring copied from the source text")


class _ExtractionOutput(BaseModel):
    claims: list[_ExtractedClaimDraft] = Field(default_factory=list)


EXTRACTOR_SYSTEM_PROMPT = """You are the Claim Extractor of a financial research agent.
Given the text of ONE source document, extract every distinct factual claim about the
company/companies of interest that is EXPLICITLY stated in the text - do not infer or
invent anything not present.

Extract two kinds of claims:
- numeric claims: revenue, net income, EPS, operating margin, EBITDA, growth rates, debt,
  cash, and similar financial metrics, with their value, unit, period and basis (GAAP /
  non-GAAP / adjusted) if stated or implied by nearby text.
- qualitative claims: major risks, strategic changes, management commentary, business
  developments.

IMPORTANT on `period`: the user's originally requested period (given below) is background
context for what the user is interested in - it is NOT necessarily what this specific source
document is reporting on. A search result can be about a different fiscal period than the one
requested (e.g. an older or newer quarter/year). Always set each claim's `period` to the period
ACTUALLY STATED in or around the evidence text itself (e.g. "fiscal 2026 third quarter",
"quarter ended June 27, 2026", "Q3 2025") - never default it to the requested period. If the
source text does not state a period for a given figure, leave `period` null rather than
guessing the requested one.

For every single claim, `quoted_evidence` MUST be an exact, verbatim, character-for-character
substring copied from the SOURCE TEXT below (not paraphrased, not summarized) that supports the
claim - this will be programmatically located in the source text, so it must match exactly.
If the source contains nothing relevant, return an empty claims list.
"""


class ClaimExtractor:
    def __init__(self, llm: LLMProvider | None = NOT_GIVEN):
        self.llm = get_llm_provider() if llm is NOT_GIVEN else llm

    def extract(self, research_run_id: str, sources: list[Source], plan: ResearchPlan) -> list[Claim]:
        """Extracts claims from every source. With an LLM configured, sources
        are processed in batches (SOURCE_BATCH_SIZE per call) rather than one
        call per source - measured: one call per source meant ~11 calls x
        ~3900 tokens for a single run, which under a free-tier per-minute
        token budget serialised into 6+ minutes of pacing. Batching cuts
        call count and, more importantly, pays the fixed per-call overhead
        (system prompt + JSON schema) once per batch instead of once per
        source."""
        if self.llm is None:
            claims: list[Claim] = []
            for source in sources:
                claims.extend(self._mock_extract(research_run_id, source, plan))
            return claims

        claims = []
        for start in range(0, len(sources), SOURCE_BATCH_SIZE):
            batch = sources[start : start + SOURCE_BATCH_SIZE]
            try:
                claims.extend(self._llm_extract_batch(research_run_id, batch, plan))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM claim extraction failed for %d source(s) (%s); using mock extractor for this batch",
                    len(batch), exc,
                )
                for source in batch:
                    claims.extend(self._mock_extract(research_run_id, source, plan))
        return claims

    # ---- Real path -----------------------------------------------------

    def _llm_extract_batch(self, research_run_id: str, sources: list[Source], plan: ResearchPlan) -> list[Claim]:
        entities = ", ".join(c.name for c in plan.companies) or "the company/companies mentioned"
        rag_query = _build_rag_query(plan)
        blocks = []
        for i, source in enumerate(sources, start=1):
            company_hint = source.metadata.get("company_name")
            # RAG: for a source longer than a plain prefix would sensibly
            # cover, retrieve the chunks most relevant to what this plan
            # actually asks about (LangChain RecursiveCharacterTextSplitter
            # + FAISS + local embeddings - see app/rag/indexer.py) instead
            # of blindly cutting at MAX_SOURCE_CHARS_FOR_LLM and losing
            # whatever came after that character.
            text = retrieve_relevant_text(source.document_text, rag_query, MAX_SOURCE_CHARS_FOR_LLM)
            blocks.append(
                f"--- SOURCE {i} ---\n"
                f"TITLE: {source.title}\n"
                f"PUBLISHER: {source.publisher}\n"
                + (f"RETRIEVED FOR: {company_hint}\n" if company_hint else "")
                + f"TEXT:\n{text}\n"
            )
        user_prompt = (
            f"Companies of interest: {entities}\n"
            f"User's originally requested period (context only, do NOT default claims to this - "
            f"see period instructions above): {plan.period or 'not specified'}\n\n"
            + "\n".join(blocks)
            + f"\n\nExtract claims from all {len(sources)} source(s) above. Set `source_number` on every "
            f"claim to the number of the source its evidence came from, and quote `quoted_evidence` "
            f"verbatim from THAT source's text only."
        )
        output = self.llm.complete_json(
            system=EXTRACTOR_SYSTEM_PROMPT,
            user=user_prompt,
            schema_model=_ExtractionOutput,
            max_tokens=MAX_EXTRACTION_COMPLETION_TOKENS,
        )

        claims: list[Claim] = []
        for draft in output.claims:
            idx = (draft.source_number or 1) - 1
            if not (0 <= idx < len(sources)):
                logger.warning("Discarding claim with out-of-range source_number=%s", draft.source_number)
                continue
            source = sources[idx]
            evidence = make_evidence(source, draft.quoted_evidence)
            if evidence is None:
                logger.warning(
                    "Discarding LLM-extracted claim with unlocatable evidence quote in source=%s: %r",
                    source.source_id, draft.quoted_evidence[:120],
                )
                continue
            claims.append(_build_claim(research_run_id, source, draft, evidence))
        return claims

    # ---- Mock path -------------------------------------------------------

    def _mock_extract(self, research_run_id: str, source: Source, plan: ResearchPlan) -> list[Claim]:
        entity = _resolve_entity_for_source(source, plan)
        claims: list[Claim] = []
        seen_metrics: set[str] = set()

        for metric, matched_text, num, unit, start, end, basis in _extract_numeric_matches(source.document_text):
            if metric in seen_metrics:
                continue
            seen_metrics.add(metric)
            evidence = Evidence(source_id=source.source_id, start_char=start, end_char=end, evidence_text=matched_text)
            claims.append(
                Claim(
                    research_run_id=research_run_id,
                    claim_type=ClaimType.NUMERIC,
                    entity=entity,
                    metric=metric,
                    value=num,
                    unit=unit or None,
                    period=plan.period,
                    basis=basis,
                    source_id=source.source_id,
                    evidence_span=evidence,
                    statement=matched_text.strip().rstrip("."),
                )
            )

        for sentence, start, end in _extract_risk_sentences(source.document_text):
            evidence = Evidence(source_id=source.source_id, start_char=start, end_char=end, evidence_text=sentence)
            claims.append(
                Claim(
                    research_run_id=research_run_id,
                    claim_type=ClaimType.QUALITATIVE,
                    entity=entity,
                    metric="risk_factor",
                    value=sentence.strip(),
                    unit=None,
                    period=plan.period,
                    basis=Basis.UNKNOWN,
                    source_id=source.source_id,
                    evidence_span=evidence,
                    statement=sentence.strip(),
                )
            )

        return claims


def _build_claim(research_run_id: str, source: Source, draft: _ExtractedClaimDraft, evidence: Evidence) -> Claim:
    return Claim(
        research_run_id=research_run_id,
        claim_type=draft.claim_type,
        entity=draft.entity,
        metric=_slugify_metric(draft.metric),
        value=draft.value,
        unit=draft.unit,
        period=draft.period,
        basis=draft.basis,
        source_id=source.source_id,
        evidence_span=evidence,
        statement=draft.statement,
    )


def _slugify_metric(metric: str) -> str:
    return re.sub(r"\s+", "_", metric.strip().lower())


def _resolve_entity_for_source(source: Source, plan: ResearchPlan) -> str:
    """Determines which company a source is about, for the mock extractor.
    Prefers the explicit company_name tag SourceRetriever stamps onto every
    source's metadata; falls back to the single company in a single-company
    plan, then to a substring match against the source title/text, and only
    then to the first company as a last resort."""
    tagged = source.metadata.get("company_name")
    if tagged:
        return tagged
    if len(plan.companies) == 1:
        return plan.companies[0].name
    for company in plan.companies:
        if company.name.lower() in source.title.lower() or company.name.lower() in source.document_text.lower():
            return company.name
    return plan.companies[0].name if plan.companies else "Unknown Entity"


# ---- Mock-mode deterministic extraction helpers ----------------------------

METRIC_ALIASES = {
    "revenue": "revenue", "revenue_ttm": "revenue", "revenues": "revenue", "total revenue": "revenue",
    "net income": "net_income", "net_income_ttm": "net_income",
    "operating margin": "operating_margin", "operating_margin_ttm": "operating_margin",
    "operating income": "operating_income", "operating_income": "operating_income",
    "ebitda": "ebitda", "adjusted ebitda": "ebitda",
    "cash and cash equivalents": "cash_and_equivalents", "cash_and_equivalents": "cash_and_equivalents",
    "total debt": "total_debt", "total_debt": "total_debt",
    "diluted earnings per share": "eps_diluted", "earnings per share": "eps_diluted",
    "eps_diluted": "eps_diluted", "eps_trailing": "eps_diluted",
    "profit margin": "profit_margin", "profit_margin_ttm": "profit_margin",
}

_PATTERN_KV = re.compile(r"(?P<label>[a-z_]+)\s*=\s*(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>USD|%)?")
_PATTERN_DOLLAR = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z &]{2,45}?)\s+(?:was|is|totaled|totalled)\s+(?:approximately\s+)?"
    r"\$\s?(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<scale>billion|million|thousand)?",
    re.IGNORECASE,
)
_PATTERN_PERCENT = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z &]{2,45}?)\s+(?:was|is)\s+(?:approximately\s+)?(?P<num>[\d]+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_PATTERN_EPS = re.compile(r"(?:diluted\s+)?earnings per share(?:\s+of)?\s+\$?(?P<num>[\d.]+)", re.IGNORECASE)

RISK_KEYWORDS = ("risk", "faces", "warns", "challenge", "competition", "adversely affected", "headwind")


def _infer_basis(text: str, start: int) -> Basis:
    window = text[max(0, start - 60):start].lower()
    if "non-gaap" in window:
        return Basis.NON_GAAP
    if "adjusted" in window:
        return Basis.ADJUSTED
    if "gaap" in window:
        return Basis.GAAP
    return Basis.UNKNOWN


def _extract_numeric_matches(text: str):
    """Yields (metric, matched_text, num, unit, start, end, basis) tuples
    with offsets guaranteed correct (computed directly from regex match
    spans against the real document_text, never LLM-provided)."""
    for m in _PATTERN_KV.finditer(text):
        metric = METRIC_ALIASES.get(m.group("label").lower())
        if not metric:
            continue
        yield metric, m.group(0), m.group("num"), m.group("unit") or "", m.start(), m.end(), _infer_basis(text, m.start())

    for m in _PATTERN_DOLLAR.finditer(text):
        metric = METRIC_ALIASES.get(m.group("label").strip().lower())
        if not metric:
            continue
        yield metric, m.group(0), m.group("num"), m.group("scale") or "", m.start(), m.end(), _infer_basis(text, m.start())

    for m in _PATTERN_PERCENT.finditer(text):
        metric = METRIC_ALIASES.get(m.group("label").strip().lower())
        if not metric:
            continue
        yield metric, m.group(0), m.group("num"), "%", m.start(), m.end(), _infer_basis(text, m.start())

    for m in _PATTERN_EPS.finditer(text):
        yield "eps_diluted", m.group(0), m.group("num"), "USD", m.start(), m.end(), _infer_basis(text, m.start())


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]")


def _extract_risk_sentences(text: str):
    for m in _SENTENCE_RE.finditer(text):
        raw = m.group(0)
        # Trim leading/trailing whitespace while keeping start/end tightly
        # aligned to the trimmed text, so evidence_text always exactly
        # equals source.document_text[start:end].
        lstrip_len = len(raw) - len(raw.lstrip())
        rstrip_len = len(raw) - len(raw.rstrip())
        start, end = m.start() + lstrip_len, m.end() - rstrip_len
        sentence = text[start:end]
        if len(sentence) < 15:
            continue
        if any(k in sentence.lower() for k in RISK_KEYWORDS):
            yield sentence, start, end
