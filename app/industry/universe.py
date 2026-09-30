"""Industry universes: which companies make up each industry the dashboard
covers. Defined as data in sample_data/industries.json, so adding an
industry (or swapping a constituent after an index rebalance) is a JSON
edit, not a code change.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from app.config import BASE_DIR
from app.schemas.industry import IndustryCompanyRef, IndustryDefinition, IndustrySummary

INDUSTRIES_PATH = BASE_DIR / "sample_data" / "industries.json"


@lru_cache
def _load() -> tuple[IndustryDefinition, ...]:
    with open(INDUSTRIES_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return tuple(IndustryDefinition.model_validate(item) for item in raw["industries"])


def list_industries() -> list[IndustrySummary]:
    return [summarize(ind) for ind in _load()]


def get_industry(industry_id: str) -> IndustryDefinition | None:
    return next((ind for ind in _load() if ind.id == industry_id), None)


def summarize(industry: IndustryDefinition) -> IndustrySummary:
    return IndustrySummary(
        id=industry.id,
        name=industry.name,
        short_name=industry.short_name,
        universe=industry.universe,
        description=industry.description,
        company_count=len(industry.companies),
    )


def find_universe_company(text: str) -> IndustryCompanyRef | None:
    """Matches a company mention against every industry universe by NSE
    symbol, Yahoo symbol, or alias (whole words, case-insensitive). The
    universe is checked BEFORE the SEC ticker directory by CompanyResolver:
    "TCS" on SEC EDGAR is The Container Store, not Tata Consultancy."""
    needle = text.strip().lower()
    if not needle:
        return None
    for industry in _load():
        for company in industry.companies:
            keys = {company.nse.lower(), company.symbol.lower(), company.name.lower(), *company.aliases}
            if needle in keys:
                return company
    return None


def find_universe_mentions(text: str) -> list[IndustryCompanyRef]:
    """All universe companies mentioned anywhere in free text, in order of
    first appearance. Longer aliases are matched first so "hcl
    technologies" wins over a bare "hcl"."""
    hits: list[tuple[int, IndustryCompanyRef]] = []
    for industry in _load():
        for company in industry.companies:
            keys = sorted({company.name.lower(), company.nse.lower(), *company.aliases}, key=len, reverse=True)
            for key in keys:
                match = re.search(rf"\b{re.escape(key)}\b", text, re.IGNORECASE)
                if match:
                    hits.append((match.start(), company))
                    break
    hits.sort(key=lambda item: item[0])
    seen: set[str] = set()
    ordered = []
    for _, company in hits:
        if company.symbol not in seen:
            seen.add(company.symbol)
            ordered.append(company)
    return ordered
