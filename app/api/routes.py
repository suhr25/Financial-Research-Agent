from __future__ import annotations

import logging

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.answer import intent as answer_intent
from app.answer import service as answer_service
from app.auth.routes import require_session
from app.auth.service import Principal
from app.config import get_settings

from app.industry.service import IndustryService
from app.industry.universe import list_industries
from app.schemas import Claim, Conflict, ResearchRun, Source
from app.schemas.answer import DatabaseAnswer, NotAnswered
from app.schemas.industry import IndustrySnapshot, IndustrySummary
from app.storage import repositories as repo
from app.storage.database import get_session
from app.storage.models import ResearchRunORM, SearchLogORM

logger = logging.getLogger("financial_research_agent.api")

router = APIRouter()


def db_session():
    db = get_session()
    try:
        yield db
    finally:
        db.close()


class ResearchRequest(BaseModel):
    query: str
    # True = run fresh research even if the same question was answered recently.
    fresh: bool = False


def normalize_query(query: str) -> str:
    return " ".join(query.lower().split()).strip(" ?.!")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log_search(db: Session, query: str, answered_from: str, principal: Principal | None, *,
               companies: list[str] | None = None, run_id: str | None = None, elapsed_ms: int | None = None) -> None:
    db.add(SearchLogORM(query=query, normalized_query=normalize_query(query), answered_from=answered_from,
                        companies=companies or [], research_run_id=run_id, elapsed_ms=elapsed_ms,
                        user_kind=principal.kind if principal else None, created_at=_now()))
    db.commit()


@router.post("/answer", response_model=DatabaseAnswer | NotAnswered)
def answer_question(req: ResearchRequest, db: Session = Depends(db_session), principal: Principal = Depends(require_session)):
    """Database first: answers questions about companies VeriFi holds
    straight from stored, verified filings (milliseconds). Returns
    answered=false when the database can't answer - the client then starts
    full research (POST /research)."""
    started = time.perf_counter()
    intent = answer_intent.parse(req.query)
    result = answer_service.answer(db, intent)
    elapsed = round((time.perf_counter() - started) * 1000)
    log_search(db, req.query, "database" if result.answered else "none", principal,
               companies=[c.nse for c in intent.companies], elapsed_ms=elapsed)
    return result


@router.get("/industries", response_model=list[IndustrySummary])
def get_industries():
    return list_industries()


@router.get("/industries/{industry_id}", response_model=IndustrySnapshot)
def get_industry_snapshot(industry_id: str, refresh: bool = False):
    """Peer metrics, consistency checks and revenue share for one industry,
    computed from the database. Companies with nothing stored are fetched
    from the source first; pass refresh=true to re-check every company's
    filings against the source now."""
    try:
        snapshot = IndustryService().get_snapshot(industry_id, force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Industry snapshot failed for %s", industry_id)
        raise HTTPException(status_code=502, detail=f"Could not load industry data: {exc}") from exc
    if snapshot is None:
        raise HTTPException(status_code=404, detail="industry not found")
    return snapshot


@router.post("/research", response_model=ResearchRun, status_code=202)
def start_research(req: ResearchRequest, db: Session = Depends(db_session), principal: Principal = Depends(require_session)):
    """Returns immediately with status=PENDING and a research_run_id - the
    actual pipeline runs in a background thread (see
    ResearchOrchestrator.start_async). A fully real run with paced LLM
    calls can take minutes; blocking the HTTP response on that would hang
    the browser with no feedback and risk a silent timeout. Poll
    GET /research/{id} for live status until it reaches complete/failed."""
    from app.agents.research_orchestrator import PIPELINE_VERSION, ResearchOrchestrator

    # Reuse: the same question researched recently is answered from the
    # stored run instead of re-running the whole pipeline.
    if not req.fresh:
        since = _now() - timedelta(hours=get_settings().research_reuse_hours)
        wanted = normalize_query(req.query)
        for row in db.scalars(select(ResearchRunORM).where(ResearchRunORM.status == "complete", ResearchRunORM.updated_at >= since)
                              .order_by(ResearchRunORM.updated_at.desc())):
            if normalize_query(row.query) != wanted:
                continue
            run = repo.get_research_run(db, row.research_run_id)
            if run is None or run.pipeline_version != PIPELINE_VERSION:
                continue  # produced by an older pipeline - research it again
            log_search(db, req.query, "cache", principal, run_id=row.research_run_id, elapsed_ms=0)
            return run

    orchestrator = ResearchOrchestrator(db)
    try:
        run = orchestrator.start_async(req.query)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to queue research run for query=%r", req.query)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    log_search(db, req.query, "research", principal, run_id=run.research_run_id)
    return run


@router.get("/research/{research_id}", response_model=ResearchRun)
def get_research(research_id: str, db: Session = Depends(db_session)):
    run = repo.get_research_run(db, research_id)
    if not run:
        raise HTTPException(status_code=404, detail="research run not found")
    return run


@router.get("/research/{research_id}/claims", response_model=list[Claim])
def get_research_claims(research_id: str, db: Session = Depends(db_session)):
    return repo.get_claims_for_run(db, research_id)


@router.get("/research/{research_id}/sources", response_model=list[Source])
def get_research_sources(research_id: str, db: Session = Depends(db_session)):
    return repo.get_sources_for_run(db, research_id)


@router.get("/research/{research_id}/conflicts", response_model=list[Conflict])
def get_research_conflicts(research_id: str, db: Session = Depends(db_session)):
    return repo.get_conflicts_for_run(db, research_id)


@router.get("/research/{research_id}/report")
def get_research_report(research_id: str, db: Session = Depends(db_session)):
    report = repo.get_report_for_run(db, research_id)
    if not report:
        raise HTTPException(status_code=404, detail="report not found")
    return report
