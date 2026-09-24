"""SEC EDGAR adapter - primary filing source (SourceTier.PRIMARY_FILING).

Real implementation pulls company facts (XBRL) from SEC EDGAR's public
`companyfacts` API - no key required, only a descriptive User-Agent. Only
works for companies with a resolved CIK (i.e. SEC registrants).

Mock implementation is used in DEMO_MODE or when a company has no CIK
(e.g. a non-US company like Reliance Industries) or the live call fails.
Mock sources are always source_tier=MOCK, never PRIMARY_FILING, so they can
never be mistaken for a real SEC filing downstream.
"""
from __future__ import annotations

import logging

import re

import httpx

from app.config import get_settings
from app.retrieval.base import FinancialDataProvider, mock_fallback_allowed
from app.schemas import CompanyEntity, Source, SourceTier, SourceType

logger = logging.getLogger("financial_research_agent.retrieval.sec_edgar")

# Entries kept per XBRL concept. Small, because the document is a
# compact evidence digest for the LLM extractor, not an archive.
ENTRIES_PER_CONCEPT = 3

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# XBRL us-gaap concept tags we pull, mapped to a human metric name.
METRIC_TAGS = {
    "Revenues": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "NetIncomeLoss": "net_income",
    "OperatingIncomeLoss": "operating_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
    "LongTermDebtNoncurrent": "long_term_debt",
}


class SECEdgarProvider(FinancialDataProvider):
    name = "sec_edgar"

    def __init__(self):
        self.settings = get_settings()

    def is_available(self) -> bool:
        return not self.settings.effective_demo_mode

    def fetch(self, company: CompanyEntity, period: str | None) -> list[Source]:
        if not self.is_available() or not company.cik:
            # A company with no CIK simply isn't an SEC registrant (e.g. a
            # non-US listing) - in a live run that's a real absence of
            # evidence, not something to paper over with a fixture.
            reason = "company has no SEC CIK" if not company.cik else "provider disabled"
            if not mock_fallback_allowed("SEC EDGAR", reason):
                return []
            return self._mock_fetch(company, period)
        try:
            return self._live_fetch(company, period)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SEC EDGAR live fetch failed for %s (%s)", company.name, exc)
            if not mock_fallback_allowed("SEC EDGAR", str(exc)):
                return []
            return self._mock_fetch(company, period)

    def _live_fetch(self, company: CompanyEntity, period: str | None) -> list[Source]:
        resp = httpx.get(
            COMPANY_FACTS_URL.format(cik=company.cik),
            headers={"User-Agent": self.settings.sec_edgar_user_agent},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        facts = data.get("facts", {}).get("us-gaap", {})

        lines: list[str] = [f"SEC EDGAR XBRL company facts for {company.name} (CIK {company.cik})."]
        wanted = _parse_requested_period(period)
        for tag, metric_name in METRIC_TAGS.items():
            tag_data = facts.get(tag)
            if not tag_data:
                continue
            units = tag_data.get("units", {})
            for unit_name, entries in units.items():
                # Prefer entries matching the requested fiscal period. The
                # full companyfacts payload spans many years of every
                # concept; emitting all of it and letting the extractor
                # truncate meant the requested quarter could be cut off
                # entirely, leaving only unrelated annual figures. Ranking
                # by relevance keeps the asked-for period in the document.
                ranked = sorted(
                    entries,
                    key=lambda e: (0 if _matches_period(e, wanted) else 1, _neg_end(e)),
                )[:ENTRIES_PER_CONCEPT]
                for entry in ranked:
                    fp = entry.get("fp", "")
                    fy = entry.get("fy", "")
                    form = entry.get("form", "")
                    end = entry.get("end", "")
                    val = entry.get("val")
                    period_label = f"{fp} {fy}".strip() if fp and fy else end
                    lines.append(
                        f"{metric_name} ({tag}) = {val} {unit_name} for period ending {end} "
                        f"(fiscal period {period_label}, form {form})."
                    )

        document_text = "\n".join(lines)
        source = Source(
            title=f"SEC EDGAR XBRL Company Facts - {company.name}",
            url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company.cik}",
            source_type=SourceType.SEC_FILING,
            source_tier=SourceTier.PRIMARY_FILING,
            publisher="U.S. Securities and Exchange Commission (EDGAR)",
            document_text=document_text,
            metadata={"cik": company.cik, "ticker": company.ticker, "requested_period": period},
        )
        return [source]

    def _mock_fetch(self, company: CompanyEntity, period: str | None) -> list[Source]:
        period_label = period or "the most recent reported quarter"
        # Deliberately long and multi-section (Overview / MD&A / Segment
        # results / Risk Factors / Outlook), not just the four headline
        # figures - a real 10-Q reads like this, and it gives the RAG
        # chunk-retrieval layer (app/rag/indexer.py) genuine material to
        # select from in demo mode too, rather than only ever exercising
        # that code path against live filings. The headline paragraph is
        # left first and unchanged so the existing regex-based mock
        # extractor (used when no LLM is configured) still finds it.
        document_text = (
            f"[MOCK SEC FILING DATA - DEMO MODE, NOT A REAL SEC FILING]\n\n"
            f"{company.name} periodic filing excerpt for {period_label}.\n\n"
            f"Revenue was $85.8 billion for {period_label}, compared to $81.8 billion in the prior-year period, "
            f"an increase of approximately 5%.\n"
            f"Net income was $21.4 billion for {period_label}, representing a diluted earnings per share of $1.40.\n"
            f"Operating margin was approximately 29.6% on a GAAP basis for {period_label}.\n"
            f"Cash and cash equivalents totaled $28.4 billion as of the end of {period_label}.\n\n"
            f"Management's Discussion and Analysis: Revenue growth for {period_label} was driven primarily by "
            f"continued strength in the Services segment and steady demand for flagship hardware products in "
            f"developed markets, partially offset by softer demand in certain emerging markets and unfavorable "
            f"foreign currency movements relative to the prior-year period. Gross margin expanded modestly due "
            f"to a favorable product mix and disciplined cost management across the supply chain.\n\n"
            f"Segment Results: The Americas segment reported net sales growth of approximately 4% year over "
            f"year, while Europe grew approximately 3%. Greater China net sales declined slightly amid "
            f"macroeconomic headwinds and intensifying local competition. The Services segment, which includes "
            f"subscription and platform revenue, grew approximately 12% year over year and carries a "
            f"significantly higher gross margin than the Products segment.\n\n"
            f"Liquidity and Capital Resources: The Company believes its existing cash, cash equivalents, "
            f"marketable securities, and cash generated from operations will be sufficient to satisfy its "
            f"working capital, capital expenditure, and debt service requirements for at least the next twelve "
            f"months. The Company returned capital to shareholders during {period_label} through share "
            f"repurchases and dividend payments.\n\n"
            f"Risk Factors: The Company's business, reputation, and results of operations could be materially "
            f"adversely affected by global and regional economic conditions, including inflation, changes in "
            f"interest rates, and currency fluctuations. The Company also faces substantial competition in all "
            f"of the markets in which it operates, and this competition could result in reduced margins and "
            f"loss of market share. The Company's business is subject to risks associated with its complex, "
            f"multi-tiered global supply chain, including component shortages, single-source suppliers, and "
            f"geopolitical disruptions that could affect the Company's ability to manufacture and deliver "
            f"products on time. The Company is also subject to increasing regulatory scrutiny and evolving "
            f"data-privacy, antitrust, and digital-markets legislation in multiple jurisdictions, compliance "
            f"with which could increase operating costs or restrict elements of the Company's business model. "
            f"Cybersecurity incidents affecting the Company or its business partners could result in the loss "
            f"of confidential information, disruption to operations, and reputational harm.\n\n"
            f"Outlook: Management expects continued investment in research and development and in new product "
            f"categories, and anticipates that macroeconomic uncertainty, currency volatility, and competitive "
            f"pressure will remain the primary factors affecting results in upcoming periods.\n"
        )
        source = Source(
            title=f"[MOCK] {company.name} Periodic Filing Excerpt - {period_label}",
            url=None,
            source_type=SourceType.MOCK,
            source_tier=SourceTier.MOCK,
            publisher="Demo Mode - Mock SEC Data",
            document_text=document_text,
            metadata={"mock": True, "requested_period": period},
        )
        return [source]


def _parse_requested_period(period: str | None) -> tuple[str | None, str | None]:
    """Parses a period label like "Q3 2024" or "FY2024" into an
    (fp, fy) pair matching SEC XBRL's own fields - fp is "Q1".."Q4" or
    "FY", fy is the 4-digit fiscal year. Returns (None, None) when no
    period was requested or it isn't recognisable."""
    if not period:
        return None, None
    text = period.upper().replace("-", " ")
    year_match = re.search(r"(20\d{2})", text)
    fy = year_match.group(1) if year_match else None
    quarter_match = re.search(r"\bQ([1-4])\b", text)
    if quarter_match:
        return f"Q{quarter_match.group(1)}", fy
    if "FY" in text or "ANNUAL" in text or "FULL YEAR" in text:
        return "FY", fy
    return None, fy


def _matches_period(entry: dict, wanted: tuple[str | None, str | None]) -> bool:
    fp_wanted, fy_wanted = wanted
    if not fp_wanted and not fy_wanted:
        return False
    if fy_wanted and str(entry.get("fy", "")) != fy_wanted:
        return False
    if fp_wanted and str(entry.get("fp", "")).upper() != fp_wanted:
        return False
    return True


def _neg_end(entry: dict) -> str:
    """Sort key that orders `end` dates newest-first among equally
    relevant entries (inverting a string date via its complement keeps the
    comparison purely lexicographic)."""
    end = entry.get("end", "")
    return "".join(chr(255 - ord(ch)) for ch in end)
