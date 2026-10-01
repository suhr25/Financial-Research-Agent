"""Financial data store: writes filings into the database and reads them back.

A filing becomes one `documents` row plus one `financial_facts` row per
number (revenue, net profit, EPS, ...), each pointing back to its document.
Writes are idempotent - a document is keyed by the checksum of its source,
so syncing the same filing twice changes nothing.

Reading reverses the mapping: stored facts are reassembled into `Filing`
objects, so the analytics layer computes the same numbers whether the data
arrived a second ago from the API or was ingested months back.
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.industry.nse import Filing
from app.schemas.industry import IndustryCompanyRef
from app.storage.models import CompanyORM, DocumentORM, FinancialFactORM, IngestionRunORM

# Filing attribute -> (metric name, unit). Quarter figures.
QUARTER_METRICS: dict[str, tuple[str, str]] = {
    "revenue": ("revenue", "INR"),
    "net_income": ("net_income", "INR"),
    "profit_before_exceptional_and_tax": ("profit_before_exceptional_and_tax", "INR"),
    "profit_before_tax": ("profit_before_tax", "INR"),
    "finance_costs": ("finance_costs", "INR"),
    "other_income": ("other_income", "INR"),
    "eps_basic": ("eps_basic", "INR_per_share"),
    "eps_diluted": ("eps_diluted", "INR_per_share"),
    "paid_up_capital": ("paid_up_capital", "INR"),
    "face_value": ("face_value", "INR_per_share"),
}
# Twelve-month figures carried by fiscal-year-end filings.
ANNUAL_METRICS: dict[str, str] = {"annual_revenue": "revenue", "annual_net_income": "net_income"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def company_id_for(symbol: str) -> str:
    return f"cmp_{symbol.lower()}"


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _filed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# ---- Companies ---------------------------------------------------------------------


def upsert_company(db: Session, ref: IndustryCompanyRef, industry_id: str | None) -> CompanyORM:
    company = db.get(CompanyORM, company_id_for(ref.nse))
    now = _now()
    if company is None:
        company = CompanyORM(company_id=company_id_for(ref.nse), symbol=ref.nse, created_at=now,
                             name=ref.name, short_name=ref.short_name, updated_at=now)
        db.add(company)
    company.name, company.short_name, company.tier, company.industry_id = ref.name, ref.short_name, ref.tier, industry_id
    company.updated_at = now
    db.flush()
    return company


def mark_synced(db: Session, company_id: str) -> None:
    company = db.get(CompanyORM, company_id)
    if company:
        company.last_synced_at = _now()


# ---- Writing filings ------------------------------------------------------------------


def known_checksums(db: Session, company_id: str) -> set[str]:
    return set(db.scalars(select(DocumentORM.checksum).where(DocumentORM.company_id == company_id)))


def ingest_filing(db: Session, company_id: str, filing: Filing, source: str = "exchange_api") -> bool:
    """Stores one quarterly filing and its numbers. Returns False (and
    changes nothing) if this exact filing is already stored."""
    digest = checksum(filing.url)
    if db.scalar(select(DocumentORM.document_id).where(DocumentORM.checksum == digest)):
        return False

    now = _now()
    doc = DocumentORM(
        document_id=f"doc_{uuid.uuid4().hex[:16]}", company_id=company_id, doc_type="quarterly_result",
        title=f"Quarterly results - quarter ended {filing.period_end}",
        period_start=_date(filing.period_start), period_end=_date(filing.period_end), consolidated=True,
        source=source, source_url=filing.url, checksum=digest, filed_at=_filed_at(filing.filed_at),
        audited=filing.audited, revision=filing.revision, status="verified", notes=list(filing.notes), ingested_at=now,
    )
    db.add(doc)
    db.flush()

    corrected_quarter = any(n.startswith("Quarter:") for n in filing.notes)
    corrected_year = any(n.startswith("Full year:") for n in filing.notes)
    period_end = _date(filing.period_end)
    for attr, (metric, unit) in QUARTER_METRICS.items():
        value = getattr(filing, attr)
        if value is None or period_end is None:
            continue
        corrected = metric == "net_income" and corrected_quarter
        db.add(FinancialFactORM(
            company_id=company_id, document_id=doc.document_id, metric=metric, period_type="quarter",
            period_start=_date(filing.period_start), period_end=period_end, consolidated=True,
            value=Decimal(str(value)), unit=unit, origin="xbrl",
            verification_status="corrected" if corrected else "verified",
            note=next((n for n in filing.notes if n.startswith("Quarter:")), None) if corrected else None,
            created_at=now,
        ))
    for attr, metric in ANNUAL_METRICS.items():
        value = getattr(filing, attr)
        if value is None or period_end is None:
            continue
        corrected = metric == "net_income" and corrected_year
        db.add(FinancialFactORM(
            company_id=company_id, document_id=doc.document_id, metric=metric, period_type="annual",
            period_start=_date(filing.annual_start), period_end=period_end, consolidated=True,
            value=Decimal(str(value)), unit="INR", origin="xbrl",
            verification_status="corrected" if corrected else "verified",
            note=next((n for n in filing.notes if n.startswith("Full year:")), None) if corrected else None,
            created_at=now,
        ))
    return True


# ---- Reading filings back ---------------------------------------------------------------


def load_filings(db: Session, company_id: str, limit: int = 8) -> list[Filing]:
    """The company's stored quarterly filings, newest first - one per
    quarter (the latest-filed revision wins), rebuilt from their facts."""
    return load_filings_many(db, [company_id], limit)[company_id]


def load_filings_many(db: Session, company_ids: list[str], limit: int = 8) -> dict[str, list[Filing]]:
    """load_filings for several companies in three queries total (companies,
    documents, facts) rather than a few per company - the whole industry
    view is read in one round of queries."""
    companies = {c.company_id: c for c in db.scalars(select(CompanyORM).where(CompanyORM.company_id.in_(company_ids)))}
    docs = db.scalars(
        select(DocumentORM)
        .where(DocumentORM.company_id.in_(company_ids), DocumentORM.doc_type == "quarterly_result",
               DocumentORM.status == "verified")
        .order_by(DocumentORM.company_id, DocumentORM.period_end.desc(), DocumentORM.filed_at.desc())
    ).all()
    chosen_by_company: dict[str, list[DocumentORM]] = defaultdict(list)
    seen: set[tuple[str, date]] = set()
    for doc in docs:
        key = (doc.company_id, doc.period_end)
        if key in seen or len(chosen_by_company[doc.company_id]) >= limit:
            continue
        seen.add(key)
        chosen_by_company[doc.company_id].append(doc)

    doc_ids = [d.document_id for chosen in chosen_by_company.values() for d in chosen]
    facts_by_doc: dict[str, list[FinancialFactORM]] = defaultdict(list)
    if doc_ids:
        for fact in db.scalars(select(FinancialFactORM).where(FinancialFactORM.document_id.in_(doc_ids))):
            facts_by_doc[fact.document_id].append(fact)

    return {cid: _rebuild(chosen_by_company.get(cid, []), facts_by_doc, companies.get(cid)) for cid in company_ids}


def _rebuild(chosen: list[DocumentORM], facts_by_doc: dict[str, list[FinancialFactORM]], company: CompanyORM | None) -> list[Filing]:
    filings = []
    for doc in chosen:
        values: dict[str, float] = {}
        annual_start = None
        for fact in facts_by_doc[doc.document_id]:
            if fact.period_type == "quarter":
                values[fact.metric] = float(fact.value)
            elif fact.period_type == "annual":
                values[f"annual_{fact.metric}"] = float(fact.value)
                annual_start = fact.period_start.isoformat() if fact.period_start else annual_start
        filings.append(Filing(
            symbol=company.symbol if company else "",
            period_start=doc.period_start.isoformat() if doc.period_start else "",
            period_end=doc.period_end.isoformat(),
            filed_at=doc.filed_at.strftime("%d-%b-%Y %H:%M:%S") if doc.filed_at else "",
            audited=doc.audited, revision=doc.revision, url=doc.source_url or "",
            revenue=values.get("revenue"), net_income=values.get("net_income"),
            profit_before_exceptional_and_tax=values.get("profit_before_exceptional_and_tax"),
            profit_before_tax=values.get("profit_before_tax"), finance_costs=values.get("finance_costs"),
            other_income=values.get("other_income"), eps_basic=values.get("eps_basic"),
            eps_diluted=values.get("eps_diluted"), paid_up_capital=values.get("paid_up_capital"),
            face_value=values.get("face_value"),
            annual_start=annual_start, annual_revenue=values.get("annual_revenue"),
            annual_net_income=values.get("annual_net_income"),
            notes=list(doc.notes or []),
        ))
    return filings


# ---- Ingestion log -------------------------------------------------------------------------


def start_run(db: Session, kind: str, target: str | None) -> IngestionRunORM:
    run = IngestionRunORM(run_id=f"ing_{uuid.uuid4().hex[:16]}", kind=kind, target=target, status="running",
                          stats={}, started_at=_now())
    db.add(run)
    db.commit()
    return run


def finish_run(db: Session, run: IngestionRunORM, status: str, stats: dict, error: str | None = None) -> None:
    run.status, run.stats, run.error, run.finished_at = status, stats, error, _now()
    db.commit()
