"""Multi-Source Retriever: fans a ResearchPlan's sub-queries and companies
out to the web search adapter and financial data adapters, deduplicates the
results, and persists every Source via the repository layer so provenance
(including full document_text) is never lost.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable

from sqlalchemy.orm import Session

from app.retrieval.financial_data import get_financial_data_provider
from app.retrieval.sec_edgar import SECEdgarProvider
from app.retrieval.web_search import get_search_provider
from app.schemas import ResearchPlan, Source, SourceType
from app.storage import repositories as repo

logger = logging.getLogger("financial_research_agent.retrieval.source_retriever")

# Upper bound on sources kept per retrieval round. Every source costs an
# LLM extraction call's worth of tokens, which under a per-minute token
# budget translates directly into wall-clock latency - see
# app/llm/rate_limiter.py and ClaimExtractor's batching constants.
MAX_SOURCES_PER_RETRIEVAL = 6

# Concurrency for the independent external fetches in one retrieval round.
# Bounded so we stay a polite client of SEC EDGAR / the search API rather
# than issuing an unbounded burst.
MAX_RETRIEVAL_WORKERS = 6


def _tag_company(sources: list[Source], company_name: str) -> list[Source]:
    """Stamps which company a retrieved source is about into its metadata,
    so the Claim Extractor can attribute claims to the right company in
    multi-company (comparison) research runs instead of guessing."""
    for source in sources:
        source.metadata["company_name"] = company_name
    return sources


class SourceRetriever:
    def __init__(self):
        self.search_provider = get_search_provider()
        self.financial_provider = get_financial_data_provider()
        self.sec_provider = SECEdgarProvider()

    # ---- Individual retrieval tasks (run concurrently by retrieve) ------

    def _fetch_sec(self, company, period) -> list[Source]:
        return _tag_company(self.sec_provider.fetch(company, period), company.name)

    def _fetch_financial(self, company, period) -> list[Source]:
        return _tag_company(self.financial_provider.fetch(company, period), company.name)

    def _fetch_web(self, sub_query) -> list[Source]:
        sources = self.search_provider.search(sub_query.text, max_results=3)
        return _tag_company(sources, sub_query.company) if sub_query.company else sources

    def retrieve(self, db: Session, research_run_id: str, plan: ResearchPlan) -> list[Source]:
        seen_text_hashes: set[int] = set()

        # Every fetch below is an independent network call to a different
        # external service, so they run concurrently. Sequentially, one slow
        # or timing-out provider blocked all the others - measured at 96s of
        # a 229s run, almost entirely spent waiting out consecutive search
        # timeouts. Unlike LLM calls (which share a token budget and must
        # stay paced), these have no shared quota to exhaust.
        tasks: list[Callable[[], list[Source]]] = []

        for company in plan.companies:
            if SourceType.SEC_FILING in plan.required_source_types or not plan.required_source_types:
                tasks.append(partial(self._fetch_sec, company, plan.period))
            if SourceType.FINANCIAL_API in plan.required_source_types or not plan.required_source_types:
                tasks.append(partial(self._fetch_financial, company, plan.period))

        for sub_query in plan.sub_queries:
            if plan.required_source_types and SourceType.WEB_ARTICLE not in plan.required_source_types:
                continue
            if sub_query.target_source_types and SourceType.WEB_ARTICLE not in sub_query.target_source_types:
                continue
            tasks.append(partial(self._fetch_web, sub_query))

        collected: list[Source] = []
        if tasks:
            with ThreadPoolExecutor(max_workers=min(len(tasks), MAX_RETRIEVAL_WORKERS)) as pool:
                # Submit in order and read results in order so retrieval
                # output stays deterministic regardless of completion order.
                for future in [pool.submit(task) for task in tasks]:
                    try:
                        collected.extend(future.result())
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("A retrieval task failed (%s); continuing with other sources", exc)

        # Dedup by URL as well as by exact text. Different sub-queries
        # routinely return the SAME document with slightly different search
        # snippets (e.g. one press release matching "revenue", "net income"
        # and "EPS" queries), so a text-only hash let the same document
        # through several times - wasting an LLM extraction call on each
        # copy and producing duplicate claims that then showed up as
        # spurious "conflicts" between a source and itself.
        deduped: list[Source] = []
        seen_urls: set[str] = set()
        for source in collected:
            url_key = (source.url or "").strip().rstrip("/").lower()
            if url_key and url_key in seen_urls:
                continue
            h = hash(source.document_text)
            if h in seen_text_hashes:
                continue
            if url_key:
                seen_urls.add(url_key)
            seen_text_hashes.add(h)
            deduped.append(source)

        # Bound worst-case work per run: keep the most trustworthy sources
        # first (SourceTier.rank: primary filings > financial APIs > press >
        # aggregators) so the cap drops the weakest evidence, never the
        # strongest.
        if len(deduped) > MAX_SOURCES_PER_RETRIEVAL:
            deduped.sort(key=lambda s: s.source_tier.rank)
            logger.info(
                "Capping %d retrieved sources to the %d highest-tier ones for research_run_id=%s",
                len(deduped), MAX_SOURCES_PER_RETRIEVAL, research_run_id,
            )
            deduped = deduped[:MAX_SOURCES_PER_RETRIEVAL]

        for source in deduped:
            repo.save_source(db, research_run_id, source)

        logger.info(
            "Retrieved %d sources (%d after dedup) for research_run_id=%s",
            len(collected), len(deduped), research_run_id,
        )
        return deduped
