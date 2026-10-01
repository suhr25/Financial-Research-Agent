"""The database as a research source.

When the research pipeline investigates a company VeriFi holds, its
financial figures come from the stored, verified filings - not from a
third-party aggregator. The figures are written as one plain sentence per
number, with the exact period and filing, so the claim extractor quotes
them verbatim and every claim traces back to a filing.
"""
from __future__ import annotations

from datetime import date

from app.datastore import store
from app.industry import analytics
from app.industry.universe import find_universe_company
from app.schemas import CompanyEntity, Source, SourceTier, SourceType
from app.storage.database import get_session


def _cr(v: float) -> str:
    return f"{v / 1e7:,.2f} crore rupees"


def database_financials(company: CompanyEntity) -> Source | None:
    """A source document built from stored filings, or None if VeriFi
    doesn't hold this company."""
    ref = find_universe_company(company.name) or (find_universe_company(company.ticker) if company.ticker else None)
    if ref is None:
        return None
    db = get_session()
    try:
        filings = store.load_filings(db, store.company_id_for(ref.nse))
    finally:
        db.close()
    if not filings:
        return None

    m = analytics.build_company_metrics(ref, filings)
    latest = date.fromisoformat(filings[0].period_end).strftime("%d %B %Y")
    lines = [f"{ref.name} - consolidated financial results as reported by the company in its quarterly filings (INR)."]
    if m.revenue_ttm is not None:
        lines.append(f"{ref.name} revenue from operations for the four quarters ended {latest} was {_cr(m.revenue_ttm)}.")
    if m.net_income_ttm is not None:
        lines.append(f"{ref.name} net profit attributable to shareholders for the four quarters ended {latest} was {_cr(m.net_income_ttm)}.")
    if m.operating_margin is not None:
        lines.append(f"{ref.name} operating margin for the four quarters ended {latest} was {m.operating_margin * 100:.1f}%.")
    if m.profit_margin is not None:
        lines.append(f"{ref.name} net profit margin for the four quarters ended {latest} was {m.profit_margin * 100:.1f}%.")
    if m.revenue_growth_yoy is not None:
        lines.append(f"{ref.name} revenue for the quarter ended {latest} grew {m.revenue_growth_yoy * 100:.1f}% year on year.")
    if m.earnings_growth_yoy is not None:
        lines.append(f"{ref.name} net profit for the quarter ended {latest} changed {m.earnings_growth_yoy * 100:+.1f}% year on year.")
    for f in filings[:4]:
        end = date.fromisoformat(f.period_end).strftime("%d %B %Y")
        if f.revenue is not None:
            lines.append(f"{ref.name} revenue from operations for the quarter ended {end} was {_cr(f.revenue)}.")
        if f.net_income is not None:
            lines.append(f"{ref.name} net profit for the quarter ended {end} was {_cr(f.net_income)}.")

    return Source(
        title=f"{ref.name} - reported quarterly results (VeriFi database)",
        url=filings[0].url or None,
        source_type=SourceType.SEC_FILING,
        source_tier=SourceTier.PRIMARY_FILING,
        publisher="Company filings, stored in the VeriFi database",
        document_text="\n".join(lines),
        metadata={"company_name": ref.name, "from_database": True, "filings": [f.url for f in filings[:4]]},
    )
