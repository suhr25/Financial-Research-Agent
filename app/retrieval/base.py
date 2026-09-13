"""Retrieval abstractions.

SearchProvider and FinancialDataProvider are the two adapter families the
PRD's architecture diagram shows feeding the Multi-Source Retriever. Every
concrete adapter (real or mock) returns fully-formed Source objects with
document_text preserved verbatim - no adapter is allowed to hand back a
summary in place of the source text.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from app.config import get_settings
from app.schemas import CompanyEntity, Evidence, Source

logger = logging.getLogger("financial_research_agent.retrieval")


def mock_fallback_allowed(provider_name: str, reason: str) -> bool:
    """Whether a provider may substitute mock data after a live call fails.

    Allowed only in DEMO_MODE, where synthetic sources are the entire
    point and are clearly labelled as such. In a live run it is NOT
    allowed: the mock fixtures carry generic illustrative figures, so
    injecting one after (say) a yfinance failure put Apple's revenue into
    a Microsoft research run under a company-named title. A live run is
    better off with one fewer source than with a fabricated one - the
    report, conflict detection and confidence scoring all then reflect
    what was genuinely retrievable.
    """
    if get_settings().effective_demo_mode:
        return True
    logger.warning(
        "%s unavailable in live mode (%s); returning no sources rather than substituting mock data",
        provider_name, reason,
    )
    return False


class SearchProvider(ABC):
    name: str = "base_search"

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[Source]: ...


class FinancialDataProvider(ABC):
    name: str = "base_financial_data"

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def fetch(self, company: CompanyEntity, period: str | None) -> list[Source]: ...


def make_evidence(source: Source, snippet: str, occurrence: int = 0) -> Evidence | None:
    """Locate `snippet` verbatim inside source.document_text and return an
    Evidence span for it. Returns None if the snippet cannot be found -
    callers must treat that as "no evidence", never fabricate a span.
    """
    text = source.document_text
    start = -1
    idx = -1
    for i in range(occurrence + 1):
        idx = text.find(snippet, idx + 1)
        if idx == -1:
            return None
        start = idx
    end = start + len(snippet)
    return Evidence(source_id=source.source_id, start_char=start, end_char=end, evidence_text=snippet)


def full_document_evidence(source: Source) -> Evidence:
    return Evidence(
        source_id=source.source_id,
        start_char=0,
        end_char=len(source.document_text),
        evidence_text=source.document_text,
    )
