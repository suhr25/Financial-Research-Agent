"""Industry snapshots, built from the database.

Database first, API as fallback:
  1. The snapshot is always computed from stored filings - a few indexed
     queries, milliseconds.
  2. A company with nothing stored yet is fetched from the source API before
     answering (first visit only), and the result is stored.
  3. A company whose stored data is older than FILINGS_SYNC_HOURS is served
     from the database immediately while a background sync checks the API
     for newer filings.
Offline (DEMO_MODE) the source API is never contacted; missing companies
are loaded from the bundled seed of real filings.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select

from app.config import get_settings
from app.datastore import store, sync
from app.industry import analytics
from app.industry.universe import get_industry, summarize
from app.schemas.industry import IndustryDefinition, IndustrySnapshot
from app.storage.database import get_session
from app.storage.models import CompanyORM

logger = logging.getLogger("financial_research_agent.industry.service")


def build_snapshot(industry: IndustryDefinition, mode: str = "live") -> IndustrySnapshot:
    """Computes the industry view purely from what the database holds."""
    started = time.perf_counter()
    ids = [store.company_id_for(ref.nse) for ref in industry.companies]
    db = get_session()
    try:
        filings = store.load_filings_many(db, ids)
        synced = [s for s in db.scalars(select(CompanyORM.last_synced_at).where(CompanyORM.company_id.in_(ids))) if s]
    finally:
        db.close()
    companies = [analytics.build_company_metrics(ref, filings[cid]) for ref, cid in zip(industry.companies, ids)]

    available = [c for c in companies if c.available]
    concentration = analytics.compute_revenue_share(available)
    aggregates = analytics.compute_aggregates(available)
    return IndustrySnapshot(
        industry=summarize(industry),
        mode=mode,
        currency=industry.currency,
        fetch_seconds=round(time.perf_counter() - started, 3),
        synced_at=min(synced) if synced else None,
        companies=companies,
        aggregates=aggregates,
        concentration=concentration,
        insights=analytics.build_insights(available, aggregates, concentration),
        warnings=[f"{c.name}: {c.error}" for c in companies if not c.available],
    )


class IndustryService:
    """One background refresh per industry at a time (class-level state,
    because the API constructs a service per request)."""

    _refreshing: set[str] = set()
    _guard = threading.Lock()

    def __init__(self):
        self.settings = get_settings()

    @property
    def mode(self) -> str:
        return "demo" if self.settings.effective_demo_mode else "live"

    def get_snapshot(self, industry_id: str, force_refresh: bool = False) -> IndustrySnapshot | None:
        industry = get_industry(industry_id)
        if industry is None:
            return None

        warnings: list[str] = []
        if force_refresh:
            warnings += sync.refresh(industry.companies, industry.id)
            stale = []
        else:
            missing, stale = sync.freshness(industry.companies)
            if missing:
                # Nothing stored yet for these - must fetch before answering.
                warnings += sync.refresh(missing, industry.id)
            if self.settings.effective_demo_mode:
                stale = []  # the offline seed never changes
            if stale:
                self.refresh_in_background(industry_id, stale)

        snap = build_snapshot(industry, mode=self.mode)
        return snap.model_copy(update={
            "stale": bool(stale),
            "refreshing": industry_id in self._refreshing,
            "warnings": [*snap.warnings, *warnings],
        })

    def refresh_in_background(self, industry_id: str, refs=None) -> None:
        industry = get_industry(industry_id)
        if industry is None:
            return
        with self._guard:
            if industry_id in self._refreshing:
                return
            self._refreshing.add(industry_id)

        def _worker():
            try:
                todo = refs
                if todo is None:
                    missing, stale = sync.freshness(industry.companies)
                    todo = missing + ([] if self.settings.effective_demo_mode else stale)
                if todo:
                    sync.refresh(todo, industry.id)
            except Exception:  # noqa: BLE001
                logger.exception("Background refresh failed for industry=%s", industry_id)
            finally:
                with self._guard:
                    self._refreshing.discard(industry_id)

        threading.Thread(target=_worker, daemon=True, name=f"industry-sync-{industry_id}").start()
