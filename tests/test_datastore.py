"""Financial data store: filings in, facts stored, filings back out -
database first, source API only for what's missing."""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.datastore import store, sync
from app.industry import analytics
from app.industry.nse import Filing
from app.schemas.industry import IndustryCompanyRef
from app.storage.models import CompanyORM, FinancialFactORM, IngestionRunORM, UserORM

CR = 1e7


def _ref() -> IndustryCompanyRef:
    sym = f"T{uuid.uuid4().hex[:6].upper()}"
    return IndustryCompanyRef(name=f"Test {sym}", short_name=sym, symbol=f"{sym}.NS", nse=sym, tier="Mid cap")


def _filing(sym: str, end: str, revenue: float, ni: float, filed: str = "10-Jul-2026 18:00:00", **kw) -> Filing:
    base = dict(
        symbol=sym, period_start="", period_end=end, filed_at=filed, audited="Audited", revision="Original",
        url=f"https://example.test/{sym}/{end}/{filed.replace(' ', '_')}.xml", revenue=revenue * CR, net_income=ni * CR,
        profit_before_exceptional_and_tax=ni * 1.3 * CR, finance_costs=1 * CR, other_income=2 * CR,
        eps_basic=ni / 100, eps_diluted=ni / 100, paid_up_capital=100 * CR, face_value=1.0,
    )
    base.update(kw)
    return Filing(**base)


def _quarters(sym: str) -> list[Filing]:
    return [
        _filing(sym, "2026-06-30", 130, 13.0),
        _filing(sym, "2026-03-31", 120, 12.0, annual_start="2025-04-01", annual_revenue=445 * CR, annual_net_income=44.5 * CR),
        _filing(sym, "2025-12-31", 115, 11.5),
        _filing(sym, "2025-09-30", 110, 11.0),
        _filing(sym, "2025-06-30", 100, 10.0),
    ]


def _seed_company(db, ref, filings):
    company = store.upsert_company(db, ref, "information-technology")
    for f in filings:
        store.ingest_filing(db, company.company_id, f)
    db.commit()
    return company


def test_ingest_is_idempotent_and_stores_one_fact_per_number(db_session):
    ref = _ref()
    company = store.upsert_company(db_session, ref, None)
    filing = _quarters(ref.nse)[1]
    assert store.ingest_filing(db_session, company.company_id, filing) is True
    assert store.ingest_filing(db_session, company.company_id, filing) is False
    db_session.commit()
    facts = db_session.scalars(select(FinancialFactORM).where(FinancialFactORM.company_id == company.company_id)).all()
    # One fact per reported quarter figure (this filing leaves profit before
    # tax empty, so 9 of the 10) + 2 annual figures from the March filing.
    assert len(facts) == 9 + 2
    assert {f.period_type for f in facts} == {"quarter", "annual"}
    assert all(f.origin == "xbrl" and f.document_id for f in facts)


def test_round_trip_reproduces_the_same_metrics(db_session):
    ref = _ref()
    filings = _quarters(ref.nse)
    company = _seed_company(db_session, ref, filings)
    loaded = store.load_filings(db_session, company.company_id)
    assert [f.period_end for f in loaded] == [f.period_end for f in filings]
    direct = analytics.build_company_metrics(ref, filings)
    from_db = analytics.build_company_metrics(ref, loaded)
    for field in ("revenue_ttm", "net_income_ttm", "eps_ttm", "operating_margin", "revenue_growth_yoy", "verification_status"):
        assert getattr(from_db, field) == pytest.approx(getattr(direct, field)) if isinstance(getattr(direct, field), float) \
            else getattr(from_db, field) == getattr(direct, field)


def test_latest_revision_of_a_quarter_wins(db_session):
    ref = _ref()
    original = _filing(ref.nse, "2026-03-31", 120, 12.0, filed="21-Apr-2026 19:00:00")
    revised = _filing(ref.nse, "2026-03-31", 121, 12.1, filed="05-May-2026 10:00:00", revision="Revised")
    company = _seed_company(db_session, ref, [original, revised])
    [loaded] = store.load_filings(db_session, company.company_id)
    assert loaded.revenue == pytest.approx(121 * CR) and loaded.revision == "Revised"


def test_corrected_figures_are_flagged_with_their_note(db_session):
    ref = _ref()
    noted = _filing(ref.nse, "2026-06-30", 130, 13.0, notes=["Quarter: tagged owners' profit contradicted the total; identity used."])
    company = _seed_company(db_session, ref, [noted])
    fact = db_session.scalar(select(FinancialFactORM).where(
        FinancialFactORM.company_id == company.company_id, FinancialFactORM.metric == "net_income"))
    assert fact.verification_status == "corrected" and "identity used" in fact.note
    [loaded] = store.load_filings(db_session, company.company_id)
    assert loaded.notes == noted.notes


def test_sync_downloads_only_filings_the_database_lacks(db_session, monkeypatch):
    ref = _ref()
    filings = _quarters(ref.nse)
    rows = [{"xbrl": f.url, "qe_Date": f.period_end} for f in filings]
    downloads = []
    monkeypatch.setattr(sync.nse, "list_filings", lambda symbol: rows)
    monkeypatch.setattr(sync.nse, "download_filing", lambda symbol, row: downloads.append(row["xbrl"]) or next(f for f in filings if f.url == row["xbrl"]))

    assert sync.sync_company(ref, None) == {"listed": 5, "added": 5}
    assert sync.sync_company(ref, None) == {"listed": 5, "added": 0}
    assert len(downloads) == 5  # the second sync downloaded nothing

    runs = db_session.scalars(select(IngestionRunORM).where(IngestionRunORM.target == ref.nse)).all()
    assert [r.status for r in runs] == ["success", "success"]


def test_failed_sync_is_logged_and_stored_data_is_still_served(db_session, monkeypatch):
    ref = _ref()
    _seed_company(db_session, ref, _quarters(ref.nse))

    def down(symbol):
        raise RuntimeError("source unreachable")
    monkeypatch.setattr(sync.nse, "list_filings", down)
    monkeypatch.setattr(sync, "get_settings", lambda: type("S", (), {"effective_demo_mode": False})())

    warnings = sync.refresh([ref], None)
    assert warnings and "showing stored data" in warnings[0]
    run = db_session.scalar(select(IngestionRunORM).where(IngestionRunORM.target == ref.nse, IngestionRunORM.status == "failed"))
    assert run is not None and "unreachable" in run.error
    assert len(store.load_filings(db_session, store.company_id_for(ref.nse))) == 5


def test_freshness_splits_missing_and_stale(db_session):
    fresh, stale, missing = _ref(), _ref(), _ref()
    for ref in (fresh, stale):
        company = _seed_company(db_session, ref, _quarters(ref.nse)[:1])
        store.mark_synced(db_session, company.company_id)
    db_session.get(CompanyORM, store.company_id_for(stale.nse)).last_synced_at = datetime.utcnow() - timedelta(days=3)
    db_session.commit()
    got_missing, got_stale = sync.freshness([fresh, stale, missing])
    assert [r.nse for r in got_missing] == [missing.nse]
    assert [r.nse for r in got_stale] == [stale.nse]


def test_fresh_database_answers_without_calling_the_api(monkeypatch):
    """Database first: once the industry is stored and fresh, a request
    never reaches the source."""
    from app.industry.service import IndustryService

    IndustryService().get_snapshot("information-technology")  # fills from the seed if needed
    calls = []
    monkeypatch.setattr(sync, "refresh", lambda refs, industry_id: calls.append(refs) or [])
    snap = IndustryService().get_snapshot("information-technology")
    assert calls == [] and all(c.available for c in snap.companies)


def test_make_admin_command(db_session):
    from app.auth.service import create_user
    from app.cli import main

    email = f"admin_{uuid.uuid4().hex[:6]}@example.com"
    create_user(db_session, "Admin", email, "longenough")
    main(["make-admin", email])
    db_session.expire_all()
    assert db_session.scalar(select(UserORM.role).where(UserORM.email == email)) == "admin"
    assert db_session.scalar(select(func.count()).select_from(UserORM).where(UserORM.role == "admin")) >= 1
