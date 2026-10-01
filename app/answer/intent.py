"""Understanding a question well enough to answer it from the database.

Deterministic - no LLM - so it costs nothing and runs in microseconds:
which companies are mentioned, whether it's a comparison, which figures
are asked about, which period, and whether it also asks something the
financial tables can't answer (risks, news, strategy...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.industry.universe import find_universe_mentions
from app.schemas.industry import IndustryCompanyRef

# Words -> the figures they ask about. Order matters only for display.
METRIC_WORDS: list[tuple[str, list[str]]] = [
    (r"\b(revenue|revenues|sales|top[\s-]?line|turnover|income from operations)\b", ["revenue_ttm", "revenue_growth_yoy"]),
    (r"\b(net profit|profit|profits|net income|earnings|bottom[\s-]?line|pat)\b", ["net_income_ttm", "earnings_growth_yoy"]),
    (r"\b(margin|margins|profitability|profitable)\b", ["operating_margin", "profit_margin"]),
    (r"\b(growth|grow|grew|growing)\b", ["revenue_growth_yoy", "earnings_growth_yoy"]),
    (r"\b(eps|per share)\b", ["eps_ttm"]),
    (r"\b(share of|market share|biggest|largest|size)\b", ["revenue_ttm", "revenue_share"]),
]
ALL_METRICS = ["revenue_ttm", "revenue_growth_yoy", "net_income_ttm", "earnings_growth_yoy",
               "operating_margin", "profit_margin", "eps_ttm", "revenue_share"]

# Things the financial tables can't answer - need documents or research.
QUALITATIVE = re.compile(
    r"\b(risk|risks|news|strategy|outlook|guidance|management|ceo|deal|deals|acquisition|acquisitions|"
    r"attrition|headcount|employees|clients|customers|ai|lawsuit|litigation|why|explain|future|plans?)\b",
    re.IGNORECASE,
)
COMPARE = re.compile(r"\b(compare|comparison|vs\.?|versus|against|better|between)\b", re.IGNORECASE)

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTH_END = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


@dataclass
class PeriodRequest:
    kind: str  # "quarter" | "annual"
    period_end: date
    label: str


@dataclass
class Intent:
    query: str
    companies: list[IndustryCompanyRef]
    comparison: bool
    metrics: list[str]
    metrics_explicit: bool
    period: PeriodRequest | None
    wants_qualitative: bool
    qualitative_terms: list[str] = field(default_factory=list)


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def _quarter_end_fy(q: int, fy: int) -> date:
    """Indian fiscal year FYn runs Apr (n-1) - Mar n: Q1 ends Jun (n-1)."""
    return {1: date(fy - 1, 6, 30), 2: date(fy - 1, 9, 30), 3: date(fy - 1, 12, 31), 4: date(fy, 3, 31)}[q]


def parse_period(text: str) -> PeriodRequest | None:
    t = text.lower()
    if m := re.search(r"\bq([1-4])\s*[-/ ]?\s*fy\s*'?(\d{4}|\d{2})\b", t):
        q, fy = int(m.group(1)), _year(m.group(2))
        end = _quarter_end_fy(q, fy)
        return PeriodRequest("quarter", end, f"Q{q} FY{str(fy)[-2:]} (quarter ended {end:%d %b %Y})")
    if m := re.search(r"\bfy\s*'?(\d{4}|\d{2})\b", t):
        fy = _year(m.group(1))
        return PeriodRequest("annual", date(fy, 3, 31), f"FY{str(fy)[-2:]} (April {fy - 1} - March {fy})")
    if m := re.search(r"\bq([1-4])\s*(?:of\s*)?(\d{4})\b", t):
        # "Q3 2024" is read as a calendar quarter (Jul-Sep 2024).
        q, y = int(m.group(1)), int(m.group(2))
        month = q * 3
        end = date(y, month, MONTH_END[month])
        return PeriodRequest("quarter", end, f"Q{q} {y} (quarter ended {end:%d %b %Y})")
    if m := re.search(r"\bquarter\s+end(?:ed|ing)?\s+(?:\d{1,2}\s+)?([a-z]{3})[a-z]*\.?\s+(\d{4})\b", t):
        month = MONTHS.get(m.group(1))
        if month:
            y = int(m.group(2))
            end = date(y, month, MONTH_END[month])
            return PeriodRequest("quarter", end, f"quarter ended {end:%d %b %Y}")
    return None


def parse(query: str) -> Intent:
    companies = find_universe_mentions(query)
    metrics: list[str] = []
    for pattern, keys in METRIC_WORDS:
        if re.search(pattern, query, re.IGNORECASE):
            metrics += [k for k in keys if k not in metrics]
    explicit = bool(metrics)
    qualitative = sorted({m.group(0).lower() for m in QUALITATIVE.finditer(query)})
    return Intent(
        query=query,
        companies=companies,
        comparison=len(companies) >= 2 or (bool(COMPARE.search(query)) and len(companies) >= 2),
        metrics=metrics if explicit else list(ALL_METRICS),
        metrics_explicit=explicit,
        period=parse_period(query),
        wants_qualitative=bool(qualitative),
        qualitative_terms=qualitative,
    )
