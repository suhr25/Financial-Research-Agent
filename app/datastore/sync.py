"""Keeping the database current: the API is only asked for what's missing.

sync_company() lists a company's filings at the exchange and downloads only
the ones the database doesn't already hold (matched by checksum), so a
routine re-sync costs one listing request per company and zero downloads.

Offline (DEMO_MODE) the exchange is never contacted: the database is filled
from sample_data/filings_seed/, which holds real parsed filings captured
from the exchange.
"""
from __future__ import annotations

import json
import logging
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import BASE_DIR, get_settings
from app.datastore import store
from app.industry import nse
from app.industry.nse import Filing
from app.schemas.industry import IndustryCompanyRef
from app.storage.database import get_session
from app.storage.models import CompanyORM, DocumentORM

logger = logging.getLogger("financial_research_agent.datastore.sync")

SEED_DIR = BASE_DIR / "sample_data" / "filings_seed"
_FILING_FIELDS = {f.name for f in fields(Filing)}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sync_company(ref: IndustryCompanyRef, industry_id: str | None) -> dict:
    """Fetches any filings for this company that the database doesn't have
    yet. Returns stats; raises if the source API can't be reached (the
    ingestion log records the failure either way)."""
    db = get_session()
    run = store.start_run(db, "company_sync", ref.nse)
    try:
        company = store.upsert_company(db, ref, industry_id)
        db.commit()
        rows = nse.list_filings(ref.nse)
        known = store.known_checksums(db, company.company_id)
        added = 0
        for row in rows:
            if store.checksum(row["xbrl"]) in known:
                continue
            filing = nse.download_filing(ref.nse, row)
            if store.ingest_filing(db, company.company_id, filing):
                added += 1
            db.commit()
        store.mark_synced(db, company.company_id)
        db.commit()
        stats = {"listed": len(rows), "added": added}
        store.finish_run(db, run, "success", stats)
        logger.info("Synced %s: %d listed, %d new", ref.nse, len(rows), added)
        return stats
    except Exception as exc:
        db.rollback()
        store.finish_run(db, run, "failed", {}, str(exc))
        raise
    finally:
        db.close()


def seed_company(ref: IndustryCompanyRef, industry_id: str | None, seed_dir: Path = SEED_DIR) -> dict:
    """Loads a company's filings from the bundled seed (offline mode)."""
    db = get_session()
    run = store.start_run(db, "seed", ref.nse)
    try:
        company = store.upsert_company(db, ref, industry_id)
        added = 0
        for path in sorted(seed_dir.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("symbol") != ref.nse:
                continue
            filing = Filing(**{k: v for k, v in raw.items() if k in _FILING_FIELDS})
            added += store.ingest_filing(db, company.company_id, filing, source="seed")
        store.mark_synced(db, company.company_id)
        db.commit()
        stats = {"added": added}
        store.finish_run(db, run, "success", stats)
        return stats
    except Exception as exc:
        db.rollback()
        store.finish_run(db, run, "failed", {}, str(exc))
        raise
    finally:
        db.close()


def freshness(refs: list[IndustryCompanyRef]) -> tuple[list[IndustryCompanyRef], list[IndustryCompanyRef]]:
    """Splits companies into (missing, stale): missing ones have no stored
    filings at all, so a request must wait for them; stale ones are served
    from the database while a background sync runs."""
    ttl = timedelta(hours=get_settings().filings_sync_hours)
    ids = [store.company_id_for(ref.nse) for ref in refs]
    db = get_session()
    try:
        synced = dict(db.execute(select(CompanyORM.company_id, CompanyORM.last_synced_at).where(CompanyORM.company_id.in_(ids))).all())
        with_docs = set(db.scalars(select(DocumentORM.company_id).where(DocumentORM.company_id.in_(ids)).distinct()))
    finally:
        db.close()
    missing, stale = [], []
    for ref, cid in zip(refs, ids):
        if cid not in with_docs:
            missing.append(ref)
        elif synced.get(cid) is None or _now() - synced[cid] > ttl:
            stale.append(ref)
    return missing, stale


def refresh(refs: list[IndustryCompanyRef], industry_id: str | None) -> list[str]:
    """Brings the given companies up to date - from the exchange in live
    mode, from the seed offline. Returns human-readable warnings for any
    company that couldn't be refreshed (its stored data is still served)."""
    warnings = []
    offline = get_settings().effective_demo_mode
    for ref in refs:
        try:
            (seed_company if offline else sync_company)(ref, industry_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Refresh failed for %s (%s)", ref.nse, exc)
            warnings.append(f"{ref.name}: couldn't reach the filings source ({exc}); showing stored data.")
    return warnings
