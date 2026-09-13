"""Follow-Up Research Loop decision module (PRD section 16).

Answers two questions the PRD explicitly wants an LLM for when available:
"is the evidence gathered so far sufficient to answer the research
question?" and "if not, what should we search for next?". The actual
iteration loop (bounded by max_followup_iterations / max_research_queries)
lives in ResearchOrchestrator - this module only makes the sufficiency
judgement and proposes targeted sub-queries for one round.

Mock path is a deterministic rule: any claim whose metric was explicitly
requested (or is a core financial metric) and came back INSUFFICIENT
triggers a follow-up query for that exact metric/entity/period.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.llm import NOT_GIVEN, LLMProvider, get_llm_provider
from app.schemas import Claim, ResearchPlan, SourceType, SubQuery, VerificationVerdict

logger = logging.getLogger("financial_research_agent.agents.followup_research")

CORE_METRICS = {"revenue", "net_income", "operating_margin", "operating_income", "risk_factor"}


class _FollowupQueryDraft(BaseModel):
    text: str
    purpose: str


class _SufficiencyOutput(BaseModel):
    sufficient: bool
    reasoning: str
    followup_queries: list[_FollowupQueryDraft] = Field(default_factory=list)


SUFFICIENCY_SYSTEM_PROMPT = """You are the evidence-sufficiency judge in an agentic financial research pipeline.
You will be given the original research plan/question and a coverage summary of the evidence gathered
so far - one line per entity/metric showing the BEST verification verdict reached for it
(SUPPORTED / CONTRADICTED / INSUFFICIENT) and how many claims exist for it.

Decide: is there sufficient SUPPORTED evidence to answer the user's research question? If any explicitly
requested metric or question has no SUPPORTED claim backing it (i.e. it is missing or only INSUFFICIENT/
CONTRADICTED claims exist for it), mark sufficient=false and propose up to 3 short, targeted follow-up
search queries (each with a purpose label) that would plausibly find that missing evidence. Do not propose
follow-ups for things that already have solid SUPPORTED evidence.
"""


class FollowupResearch:
    def __init__(self, llm: LLMProvider | None = NOT_GIVEN):
        self.llm = get_llm_provider() if llm is NOT_GIVEN else llm

    def assess(self, plan: ResearchPlan, claims: list[Claim]) -> tuple[bool, list[SubQuery], str]:
        """Returns (sufficient, followup_sub_queries, reasoning)."""
        if self.llm is not None:
            try:
                return self._llm_assess(plan, claims)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM sufficiency check failed (%s); using rule-based fallback", exc)
        return self._rule_based_assess(plan, claims)

    def _llm_assess(self, plan: ResearchPlan, claims: list[Claim]) -> tuple[bool, list[SubQuery], str]:
        user_prompt = (
            f"RESEARCH QUERY: {plan.raw_query}\n"
            f"Requested metrics: {plan.requested_metrics}\n"
            f"Financial questions: {plan.financial_questions}\n"
            f"Risk questions: {plan.risk_questions}\n\n"
            f"EVIDENCE COVERAGE SO FAR:\n{_coverage_summary(claims)}"
        )
        output = self.llm.complete_json(
            system=SUFFICIENCY_SYSTEM_PROMPT, user=user_prompt, schema_model=_SufficiencyOutput, max_tokens=800
        )
        sub_queries = [
            SubQuery(text=q.text, purpose=q.purpose, target_source_types=[SourceType.WEB_ARTICLE, SourceType.FINANCIAL_API], is_followup=True)
            for q in output.followup_queries
        ]
        if not output.sufficient and not sub_queries:
            # Safety net: LLM said insufficient but gave nothing to search for - fall back to rules.
            _, sub_queries, _ = self._rule_based_assess(plan, claims)
        return output.sufficient, sub_queries, output.reasoning

    def _rule_based_assess(self, plan: ResearchPlan, claims: list[Claim]) -> tuple[bool, list[SubQuery], str]:
        watched_metrics = CORE_METRICS | set(plan.requested_metrics)
        gaps: dict[tuple[str, str], Claim] = {}
        for c in claims:
            if c.metric not in watched_metrics:
                continue
            key = (c.entity, c.metric)
            if c.verification_status == VerificationVerdict.SUPPORTED:
                gaps.pop(key, None)
            elif key not in gaps:
                gaps[key] = c

        if not gaps:
            return True, [], "All watched metrics have at least one SUPPORTED claim."

        sub_queries = []
        for (entity, metric), c in gaps.items():
            period_part = f" {c.period}" if c.period else ""
            sub_queries.append(
                SubQuery(
                    text=f"{entity}{period_part} {metric.replace('_', ' ')}",
                    purpose=metric,
                    target_source_types=[SourceType.WEB_ARTICLE, SourceType.FINANCIAL_API],
                    is_followup=True,
                )
            )
        return False, sub_queries, f"{len(gaps)} watched metric(s) lack SUPPORTED evidence: {[m for _, m in gaps]}."


def _coverage_summary(claims: list[Claim]) -> str:
    """Collapses the full claim list into one line per (entity, metric)
    stating the best verdict reached and how many claims support it.

    The sufficiency question is only ever "which metrics still lack solid
    evidence?", so sending every individual claim was both needlessly
    verbose and a worse prompt. Measured: the full-claim version produced
    a ~6,800-token request - nearly an entire minute's token budget on a
    free tier - for a question answerable from a summary a fraction of
    that size.
    """
    if not claims:
        return "(no claims extracted yet)"

    _RANK = {
        VerificationVerdict.SUPPORTED: 3,
        VerificationVerdict.CONTRADICTED: 2,
        VerificationVerdict.INSUFFICIENT: 1,
    }
    best: dict[tuple[str, str], tuple[int, str, int]] = {}
    for c in claims:
        key = (c.entity, c.metric)
        rank = _RANK.get(c.verification_status, 0)
        verdict = c.verification_status.value if c.verification_status else "unknown"
        prev = best.get(key)
        if prev is None:
            best[key] = (rank, verdict, 1)
        else:
            count = prev[2] + 1
            best[key] = (prev[0], prev[1], count) if prev[0] >= rank else (rank, verdict, count)

    return "\n".join(
        f"- {entity} / {metric}: best_verdict={verdict} ({count} claim(s))"
        for (entity, metric), (_rank, verdict, count) in sorted(best.items())
    )
