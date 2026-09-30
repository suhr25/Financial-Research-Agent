"""Industry snapshot service: builds, caches and serves IndustrySnapshots.

Serving strategy (stale-while-revalidate), so the dashboard answers in
milliseconds rather than waiting on the network:
  1. A fresh snapshot in memory is returned as-is.
  2. Otherwise the last snapshot persisted to disk is returned immediately,
     flagged `stale` + `refreshing`, and a single background refresh starts.
  3. Only when nothing has ever been fetched does a request wait on a
     live fetch from NSE.

In DEMO_MODE the bundled, clearly-labelled fixture snapshot is served
instead. In live mode a company whose fetch fails is shown as unavailable
- never back-filled with fixture data.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.config import BASE_DIR, get_settings
from app.industry import analytics, nse
from app.industry.universe import get_industry, summarize
from app.schemas.industry import CompanyMetrics, IndustryCompanyRef, IndustryDefinition, IndustrySnapshot

logger = logging.getLogger("financial_research_agent.industry.service")

FIXTURE_DIR = BASE_DIR / "sample_data" / "industry_snapshots"


def _fetch_company(ref: IndustryCompanyRef, filings_cache: Path):
    """All official data for one company: results filings, a year of
    daily closes and corporate actions. Returns an error string instead of
    raising so one failing company doesn't sink the whole industry."""
    try:
        filings = nse.fetch_filings(ref.nse, filings_cache)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSE filings failed for %s (%s)", ref.nse, exc)
        return ref, [], [], [], f"Results filings unavailable right now: {exc}"
    try:
        closes = nse.fetch_price_history(ref.nse)
        actions = nse.fetch_corporate_actions(ref.nse)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSE prices failed for %s (%s)", ref.nse, exc)
        return ref, filings, [], [], f"Prices unavailable right now: {exc}"
    return ref, filings, closes, actions, None


def build_snapshot(industry: IndustryDefinition, mode: str = "live") -> IndustrySnapshot:
    started = time.perf_counter()
    filings_cache = Path(get_settings().industry_cache_dir) / "filings"
    # Polite concurrency - NSE throttles clients that burst.
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(lambda ref: _fetch_company(ref, filings_cache), industry.companies))

    last_days = [closes[-1][0] for _, _, closes, _, _ in fetched if closes]
    price_day = max(last_days) if last_days else None
    bhavcopy: dict[str, float] = {}
    if price_day:
        try:
            bhavcopy = nse.fetch_bhavcopy_closes(price_day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("NSE bhavcopy for %s unavailable (%s)", price_day, exc)

    warnings: list[str] = []
    companies: list[CompanyMetrics] = []
    adjusted: dict[str, pd.Series] = {}
    for ref, filings, closes, actions, error in fetched:
        if error:
            warnings.append(f"{ref.name}: {error}")
        bhav = bhavcopy.get(ref.nse) if closes and closes[-1][0] == price_day else None
        metrics = analytics.build_company_metrics(ref, filings, closes, actions, bhav)
        if error and not metrics.error:
            metrics.error = error
        companies.append(metrics)
        if closes:
            adjusted[ref.symbol] = analytics.adjusted_closes(closes, actions)

    closes_df = pd.DataFrame(adjusted).sort_index() if adjusted else pd.DataFrame()
    if closes_df.empty:
        warnings.append("Price history unavailable - returns, volatility and diversification are not shown.")
    analytics.apply_price_stats(companies, closes_df)
    available = [c for c in companies if c.available]
    concentration = analytics.compute_concentration(available)
    aggregates = analytics.compute_aggregates(available)
    diversification = analytics.compute_diversification(available, closes_df)
    insights = analytics.build_insights(available, aggregates, concentration, diversification)

    return IndustrySnapshot(
        industry=summarize(industry),
        mode=mode,
        currency=industry.currency,
        fetch_seconds=round(time.perf_counter() - started, 2),
        price_date=price_day.isoformat() if price_day else None,
        companies=companies,
        aggregates=aggregates,
        concentration=concentration,
        diversification=diversification,
        insights=insights,
        warnings=warnings,
    )


class IndustryService:
    """Process-wide cache. Class-level state because the API constructs
    services per request; one refresh per industry at a time."""

    _snapshots: dict[str, IndustrySnapshot] = {}
    _locks: dict[str, threading.Lock] = {}
    _refreshing: set[str] = set()
    _guard = threading.Lock()

    def __init__(self):
        self.settings = get_settings()

    @property
    def ttl_seconds(self) -> int:
        return self.settings.industry_cache_ttl_seconds

    def _cache_path(self, industry_id: str) -> Path:
        return Path(self.settings.industry_cache_dir) / f"{industry_id}.json"

    def _lock(self, industry_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(industry_id, threading.Lock())

    def _age(self, snap: IndustrySnapshot) -> float:
        return (datetime.now(timezone.utc) - snap.fetched_at).total_seconds()

    def get_snapshot(self, industry_id: str, force_refresh: bool = False) -> IndustrySnapshot | None:
        industry = get_industry(industry_id)
        if industry is None:
            return None
        if self.settings.effective_demo_mode:
            return self._demo_snapshot(industry)

        if force_refresh:
            return self._refresh(industry)

        snap = self._snapshots.get(industry_id) or self._load_disk(industry_id)
        if snap is None:
            return self._refresh(industry)
        if self._age(snap) <= self.ttl_seconds:
            self._snapshots[industry_id] = snap
            return snap.model_copy(update={"stale": False, "refreshing": False})
        self.refresh_in_background(industry_id)
        return snap.model_copy(update={"stale": True, "refreshing": True})

    def refresh_in_background(self, industry_id: str) -> None:
        with self._guard:
            if industry_id in self._refreshing:
                return
            self._refreshing.add(industry_id)
        industry = get_industry(industry_id)

        def _worker():
            try:
                self._refresh(industry)
            except Exception:  # noqa: BLE001
                logger.exception("Background refresh failed for industry=%s", industry_id)
            finally:
                with self._guard:
                    self._refreshing.discard(industry_id)

        threading.Thread(target=_worker, daemon=True, name=f"industry-refresh-{industry_id}").start()

    def _refresh(self, industry: IndustryDefinition) -> IndustrySnapshot:
        lock = self._lock(industry.id)
        with lock:
            # Another thread may have refreshed while this one waited.
            current = self._snapshots.get(industry.id)
            if current is not None and self._age(current) < 5:
                return current
            snap = build_snapshot(industry, mode="live")
            if not any(c.available for c in snap.companies):
                previous = current or self._load_disk(industry.id)
                if previous is not None:
                    logger.warning("Live refresh for %s returned no data; keeping previous snapshot", industry.id)
                    return previous.model_copy(update={
                        "stale": True, "refreshing": False,
                        "warnings": [*previous.warnings, "Latest refresh failed - showing the last good snapshot."],
                    })
            self._snapshots[industry.id] = snap
            self._save_disk(snap)
            logger.info("Industry snapshot %s refreshed in %.2fs (%d warnings)", industry.id, snap.fetch_seconds or 0, len(snap.warnings))
            return snap

    def _load_disk(self, industry_id: str) -> IndustrySnapshot | None:
        path = self._cache_path(industry_id)
        if not path.exists():
            return None
        try:
            return IndustrySnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ignoring unreadable industry cache %s (%s)", path, exc)
            return None

    def _save_disk(self, snap: IndustrySnapshot) -> None:
        path = self._cache_path(snap.industry.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(snap.model_dump_json(), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not persist industry cache %s (%s)", path, exc)

    def _demo_snapshot(self, industry: IndustryDefinition) -> IndustrySnapshot | None:
        path = FIXTURE_DIR / f"{industry.id}.json"
        if not path.exists():
            return None
        snap = IndustrySnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        return snap.model_copy(update={"mode": "demo", "stale": False, "refreshing": False})
