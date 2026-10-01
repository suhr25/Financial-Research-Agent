"""Database-first answering: questions about companies VeriFi holds are
answered from stored filings; research runs are reused; every search is
logged."""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.answer.intent import parse
from app.main import app
from app.schemas import CompanyEntity, ResearchRun, ResearchStatus
from app.storage import repositories as repo
from app.storage.models import SearchLogORM


@pytest.fixture()
def client():
    c = TestClient(app)
    c.post("/api/auth/demo")
    return c


# ---- Understanding the question -------------------------------------------------


def test_comparison_of_two_held_companies():
    it = parse("Compare TCS and Wipro")
    assert [c.nse for c in it.companies] == ["TCS", "WIPRO"]
    assert it.comparison and not it.metrics_explicit and not it.wants_qualitative


def test_metrics_and_qualitative_terms():
    it = parse("Analyze Infosys revenue and risks")
    assert [c.nse for c in it.companies] == ["INFY"]
    assert it.metrics[:2] == ["revenue_ttm", "revenue_growth_yoy"]
    assert it.wants_qualitative and it.qualitative_terms == ["risks"]


@pytest.mark.parametrize("text, kind, end", [
    ("Wipro Q1 FY27 profit", "quarter", date(2026, 6, 30)),      # Indian FY: Q1 FY27 = Apr-Jun 2026
    ("TCS Q4 FY26 revenue", "quarter", date(2026, 3, 31)),
    ("TCS FY26 revenue", "annual", date(2026, 3, 31)),           # FY26 = Apr 2025 - Mar 2026
    ("Infosys revenue Q3 2024", "quarter", date(2024, 9, 30)),   # calendar quarter
    ("HCL revenue for the quarter ended June 2026", "quarter", date(2026, 6, 30)),
])
def test_period_parsing(text, kind, end):
    p = parse(text).period
    assert p is not None and p.kind == kind and p.period_end == end


def test_unknown_company_is_not_matched():
    assert parse("Analyze Apple revenue").companies == []


# ---- Answering from the database ----------------------------------------------------


def test_comparison_answered_from_database(client):
    body = client.post("/api/answer", json={"query": "Compare TCS and Wipro"}).json()
    assert body["answered"] and body["answered_from"] == "database" and body["intent"] == "comparison"
    assert [c["nse"] for c in body["companies"]] == ["TCS", "WIPRO"]
    revenue = next(r for r in body["comparison"] if r["metric"] == "revenue_ttm")
    assert revenue["values"]["TCS"] > revenue["values"]["WIPRO"] > 0
    margin = next(r for r in body["comparison"] if r["metric"] == "operating_margin")
    assert margin["leader"] in ("TCS", "WIPRO")
    assert all(c["revenue_share"] and 0 < c["revenue_share"] < 1 for c in body["companies"])
    assert "TCS" in body["summary"][0] and "Wipro" in body["summary"][0]


def test_requested_period_is_answered_or_honestly_missing(client):
    found = client.post("/api/answer", json={"query": "Wipro Q1 FY27 revenue"}).json()
    pf = found["period_figures"]["WIPRO"]
    assert pf["found"] and pf["revenue"] > 0 and pf["filing_url"]

    missing = client.post("/api/answer", json={"query": "Wipro Q1 FY20 revenue"}).json()
    pf = missing["period_figures"]["WIPRO"]
    assert not pf["found"] and "isn't in the database" in pf["message"]


def test_question_about_unheld_company_is_not_answered(client):
    body = client.post("/api/answer", json={"query": "Analyze Apple revenue"}).json()
    assert body["answered"] is False and body["reason"]


def test_every_search_is_logged(client, db_session):
    q = "compare infosys and hcltech margins"
    client.post("/api/answer", json={"query": q})
    row = db_session.scalar(select(SearchLogORM).where(SearchLogORM.normalized_query == q))
    assert row.answered_from == "database" and row.companies == ["INFY", "HCLTECH"] and row.elapsed_ms is not None


# ---- Reusing research, and the database as a research source ------------------------


def test_repeated_research_question_reuses_the_stored_run(client, db_session):
    from app.agents.research_orchestrator import PIPELINE_VERSION

    old = ResearchRun(query="What is Zeta Corp's revenue outlook?", status=ResearchStatus.COMPLETE)  # pre-database pipeline
    repo.save_research_run(db_session, old)
    run = ResearchRun(query="What is Zeta Corp's revenue outlook?", status=ResearchStatus.COMPLETE, pipeline_version=PIPELINE_VERSION)
    repo.save_research_run(db_session, run)
    resp = client.post("/api/research", json={"query": "  what is zeta corp's revenue outlook  "})
    assert resp.status_code == 202 and resp.json()["research_run_id"] == run.research_run_id
    logged = db_session.scalar(select(SearchLogORM).where(SearchLogORM.research_run_id == run.research_run_id))
    assert logged.answered_from == "cache"


def test_research_pipeline_uses_stored_figures_for_held_companies():
    from app.datastore.research_source import database_financials
    from app.industry.service import IndustryService

    IndustryService().get_snapshot("information-technology")  # make sure the store is filled
    src = database_financials(CompanyEntity(name="Wipro", ticker="WIPRO.NS", resolved=True))
    assert src is not None and src.source_tier == "primary_filing"
    assert "Wipro revenue from operations for the four quarters ended" in src.document_text
    assert database_financials(CompanyEntity(name="Apple Inc.", ticker="AAPL", resolved=True)) is None


def test_numbers_for_held_companies_come_only_from_the_database():
    from app.agents.research_orchestrator import prefer_database_numbers
    from app.schemas import Claim, ClaimType, Evidence, Source, SourceTier, SourceType

    db_src = Source(title="db", source_type=SourceType.SEC_FILING, source_tier=SourceTier.PRIMARY_FILING, publisher="VeriFi",
                    document_text="x", metadata={"company_name": "Infosys", "from_database": True})
    web = Source(title="web", source_type=SourceType.WEB_ARTICLE, source_tier=SourceTier.AGGREGATOR, publisher="scribd",
                 document_text="x", metadata={"company_name": "Infosys"})

    def claim(src, kind, entity="Infosys"):
        return Claim(research_run_id="r", claim_type=kind, entity=entity, metric="revenue", value="1", source_id=src.source_id,
                     evidence_span=Evidence(source_id=src.source_id, start_char=0, end_char=1, evidence_text="x"), statement="s")

    claims = [claim(db_src, ClaimType.NUMERIC), claim(web, ClaimType.NUMERIC), claim(web, ClaimType.QUALITATIVE),
              claim(web, ClaimType.NUMERIC, entity="Apple")]
    kept = prefer_database_numbers(claims, [db_src, web])
    assert [(c.source_id == db_src.source_id, c.claim_type, c.entity) for c in kept] == [
        (True, ClaimType.NUMERIC, "Infosys"),       # database number kept
        (False, ClaimType.QUALITATIVE, "Infosys"),  # web risk/news kept
        (False, ClaimType.NUMERIC, "Apple"),        # company not held: web number kept
    ]


def test_searches_for_held_companies_target_what_the_database_lacks():
    from datetime import date

    from app.agents.research_orchestrator import focus_searches_on_what_the_database_lacks
    from app.schemas import ResearchPlan, SubQuery

    plan = ResearchPlan(
        raw_query="Analyze Infosys revenue and risks",
        companies=[CompanyEntity(name="Infosys", ticker="INFY.NS", resolved=True)],
        risk_questions=["regulatory risks"],
        sub_queries=[SubQuery(text="Infosys revenue 2023", purpose="revenue", company="Infosys"),
                     SubQuery(text="Infosys debt 2023", purpose="debt", company="Infosys"),
                     SubQuery(text="Infosys risk factors", purpose="risks", company="Infosys")],
    )
    focused = focus_searches_on_what_the_database_lacks(plan, limit=4)
    texts = [sq.text for sq in focused.sub_queries]
    assert not any("revenue" in t or "debt" in t for t in texts)  # the database has these
    assert "Infosys risk factors" in texts
    assert f"Infosys key business risks and challenges {date.today().year}" in texts


def test_risks_recognised_by_meaning_not_only_by_label():
    from app.generation.report_generator import is_risk_claim
    from app.schemas import Claim, ClaimType, Evidence

    def claim(metric, statement, kind=ClaimType.QUALITATIVE):
        return Claim(research_run_id="r", claim_type=kind, entity="X", metric=metric, value=statement, source_id="s",
                     evidence_span=Evidence(source_id="s", start_char=0, end_char=1, evidence_text="x"), statement=statement)

    assert is_risk_claim(claim("cybersecurity", "Cybersecurity incidents pose a material risk to operations."))
    assert is_risk_claim(claim("risk_factor", "anything"))
    assert not is_risk_claim(claim("strategy", "The company is expanding its AI services practice."))
    assert not is_risk_claim(claim("revenue", "Revenue risk-adjusted", kind=ClaimType.NUMERIC))


def test_fragments_and_boilerplate_never_appear_as_risks():
    from app.generation.report_generator import is_presentable_risk
    from app.schemas import Claim, ClaimType, Evidence

    def claim(statement):
        return Claim(research_run_id="r", claim_type=ClaimType.QUALITATIVE, entity="X", metric="risk_factor", value="v",
                     source_id="s", evidence_span=Evidence(source_id="s", start_char=0, end_char=1, evidence_text="x"), statement=statement)

    assert not is_presentable_risk(claim("Growth has stalled"))                      # fragment
    assert not is_presentable_risk(claim("constant currency growth is ..."))          # truncated snippet
    assert not is_presentable_risk(claim("The risk of not detecting a material misstatement resulting from fraud is higher."))
    assert is_presentable_risk(claim("Slower discretionary spending by US banking clients could weigh on revenue growth."))


def test_evidence_tolerates_whitespace_and_quote_style_but_stays_verbatim():
    from app.retrieval.base import make_evidence
    from app.schemas import Source, SourceTier, SourceType

    text = "Risk factors\nOur ability to maintain   our competitive position\ndepends on clients\u2019 budgets."
    src = Source(title="t", source_type=SourceType.WEB_ARTICLE, source_tier=SourceTier.AGGREGATOR, publisher="p", document_text=text)
    ev = make_evidence(src, "Our ability to maintain our competitive position depends on clients' budgets.")
    assert ev is not None
    assert ev.evidence_text == text[ev.start_char:ev.end_char]  # the source's own characters
    assert make_evidence(src, "Our ability to grow our competitive position depends on clients' budgets.") is None  # wording must match


def test_fragment_quotes_are_widened_to_the_whole_source_sentence():
    from app.extraction.claim_extractor import expand_to_sentence
    from app.retrieval.base import make_evidence
    from app.schemas import Source, SourceTier, SourceType

    text = ("Infosys faces several risks. Clients are moving to automation, which may reduce revenues "
            "from certain traditional services or adversely affect our margins. Other text.")
    src = Source(title="t", source_type=SourceType.WEB_ARTICLE, source_tier=SourceTier.AGGREGATOR, publisher="p", document_text=text)
    ev = expand_to_sentence(src, make_evidence(src, "which may reduce revenues from certain traditional services"))
    assert ev.evidence_text == ("Clients are moving to automation, which may reduce revenues "
                                "from certain traditional services or adversely affect our margins.")
    assert text[ev.start_char:ev.end_char] == ev.evidence_text
