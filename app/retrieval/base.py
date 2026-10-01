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
            return _locate_tolerant(source, snippet) if occurrence == 0 else None
        start = idx
    end = start + len(snippet)
    return Evidence(source_id=source.source_id, start_char=start, end_char=end, evidence_text=snippet)


_QUOTE_CLASSES = {"'": "['\u2018\u2019]", "\u2018": "['\u2018\u2019]", "\u2019": "['\u2018\u2019]",
                  '"': '["\u201c\u201d]', "\u201c": '["\u201c\u201d]', "\u201d": '["\u201c\u201d]',
                  "-": "[-\u2010\u2011\u2012\u2013\u2014]", "\u2013": "[-\u2013\u2014]", "\u2014": "[-\u2013\u2014]"}


def _locate_tolerant(source: Source, snippet: str) -> Evidence | None:
    """Finds a quote that differs from the source only in whitespace (line
    breaks, repeated spaces) or straight-vs-curly quotes and dashes - common
    when a model re-types text extracted from a web page. The evidence span
    is still the source's own text, character for character: nothing is
    paraphrased or invented, only the formatting of the quote is forgiven."""
    import re

    words = snippet.split()
    if len(words) < 4:
        return None
    pattern = r"\s+".join("".join(_QUOTE_CLASSES.get(ch, re.escape(ch)) for ch in w) for w in words)
    m = re.search(pattern, source.document_text)
    if not m:
        return None
    return Evidence(source_id=source.source_id, start_char=m.start(), end_char=m.end(), evidence_text=m.group(0))


def full_document_evidence(source: Source) -> Evidence:
    return Evidence(
        source_id=source.source_id,
        start_char=0,
        end_char=len(source.document_text),
        evidence_text=source.document_text,
    )
