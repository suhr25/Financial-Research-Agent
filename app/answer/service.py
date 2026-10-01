"""Answering questions from the database - the fast path for research.

For companies VeriFi holds, a question is answered from stored, verified
filings in milliseconds: a company brief or a side-by-side comparison, the
exact period asked about if one was named, and plain-English sentences
built from the numbers (templated, not generated). A company that isn't
stored yet is fetched from the source first and stored, so every later
question about it is instant.
"""
from __future__ import annotations

import time
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.answer.intent import Intent, PeriodRequest
from app.datastore import store, sync
from app.industry import analytics
from app.industry.nse import Filing
from app.industry.universe import industry_for_company
from app.schemas.answer import ComparisonRow, DatabaseAnswer, NotAnswered, PeriodFigures
from app.schemas.industry import CompanyMetrics
from app.storage.models import CompanyORM

ROWS: dict[str, tuple[str, str, bool | None]] = {
    # metric: (label, kind, higher_is_better)
    "revenue_ttm": ("Revenue (last 12 months)", "inr", None),
    "revenue_growth_yoy": ("Revenue growth (YoY, latest quarter)", "pct_signed", True),
    "net_income_ttm": ("Net profit (last 12 months)", "inr", None),
    "earnings_growth_yoy": ("Profit growth (YoY, latest quarter)", "pct_signed", True),
    "operating_margin": ("Operating margin", "pct", True),
    "profit_margin": ("Net margin", "pct", True),
    "eps_ttm": ("EPS (last 12 months, diluted)", "rupees", None),
    "revenue_share": ("Share of IT industry revenue", "pct", None),
}


# ---- Formatting for sentences (mirrors the frontend's Indian notation) ----------


def _indian_group(n: int) -> str:
    s = str(abs(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(v: float | None) -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 1e12:
        return f"₹{v / 1e12:.2f} L Cr"
    return f"₹{_indian_group(round(v / 1e7))} Cr"


def pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{'+' if signed and v > 0 else ''}{v * 100:.1f}%"


def _d(iso: str) -> str:
    return date.fromisoformat(iso[:10]).strftime("%d %b %Y").lstrip("0")


# ---- Building the answer -------------------------------------------------------------


def _period_figures(filings: list[Filing], req: PeriodRequest, coverage: str) -> PeriodFigures:
    end = req.period_end.isoformat()
    if req.kind == "quarter":
        f = next((f for f in filings if f.period_end == end), None)
        if f:
            return PeriodFigures(found=True, label=req.label, period_end=end, revenue=f.revenue, net_income=f.net_income,
                                 operating_profit=f.operating_profit, eps_diluted=f.eps_diluted, filing_url=f.url, filed_at=f.filed_at)
    else:
        f = next((f for f in filings if f.period_end == end and f.annual_revenue is not None), None)
        if f:
            return PeriodFigures(found=True, label=req.label, period_end=end, revenue=f.annual_revenue,
                                 net_income=f.annual_net_income, filing_url=f.url, filed_at=f.filed_at)
    return PeriodFigures(found=False, label=req.label, period_end=end,
                         message=f"{req.label} isn't in the database. Stored results cover {coverage}.")


def _rows(companies: list[CompanyMetrics], metrics: list[str]) -> list[ComparisonRow]:
    rows = []
    for key in metrics:
        label, kind, better = ROWS[key]
        values = {c.nse: getattr(c, key) for c in companies}
        present = {k: v for k, v in values.items() if v is not None}
        leader = None
        if better is not None and len(present) >= 2:
            leader = (max if better else min)(present, key=present.get)
        rows.append(ComparisonRow(metric=key, label=label, kind=kind, values=values, leader=leader))
    return rows


def _company_summary(c: CompanyMetrics) -> list[tuple[str, str]]:
    """(topic, sentence) pairs, so the answer can lead with what was asked."""
    out = []
    if c.revenue_ttm is not None:
        s = f"{c.short_name} earned {inr(c.revenue_ttm)} in revenue over the last four reported quarters"
        if c.revenue_growth_yoy is not None:
            s += f"; the latest quarter (ended {_d(c.latest_quarter)}) was {pct(c.revenue_growth_yoy, True)} on the same quarter a year earlier"
        out.append(("revenue", s + "."))
    if c.operating_margin is not None or c.profit_margin is not None:
        bits = []
        if c.operating_margin is not None:
            bits.append(f"operating margin was {pct(c.operating_margin)}")
        if c.profit_margin is not None:
            bits.append(f"{'its ' if bits else ''}net margin {'was ' if not bits else ''}{pct(c.profit_margin)}")
        out.append(("margin", f"{c.short_name}'s {' and '.join(bits)} over the last four quarters."))
    if c.net_income_ttm is not None:
        s = f"{c.short_name}'s net profit over the last four reported quarters was {inr(c.net_income_ttm)}"
        if c.earnings_growth_yoy is not None:
            s += f"; the latest quarter's profit was {pct(c.earnings_growth_yoy, True)} on a year earlier"
        out.append(("profit", s + "."))
    if c.eps_ttm is not None:
        out.append(("eps", f"Diluted EPS over the last four quarters: ₹{c.eps_ttm:.2f}."))
    return out


def _comparison_summary(cs: list[CompanyMetrics]) -> list[tuple[str, str]]:
    many = len(cs) > 2
    out = []
    by_rev = sorted([c for c in cs if c.revenue_ttm], key=lambda c: c.revenue_ttm, reverse=True)
    if len(by_rev) >= 2:
        a, b = by_rev[0], by_rev[-1]
        out.append(("revenue", f"{a.short_name} is the {'largest' if many else 'larger'} business: {inr(a.revenue_ttm)} of revenue over the last 12 months, "
                               f"{a.revenue_ttm / b.revenue_ttm:.1f}× {b.short_name}'s {inr(b.revenue_ttm)}."))
    grow = sorted([c for c in cs if c.revenue_growth_yoy is not None], key=lambda c: c.revenue_growth_yoy, reverse=True)
    if len(grow) >= 2:
        out.append(("growth", f"{grow[0].short_name} is growing {'fastest' if many else 'faster'}: revenue "
                              + ", ".join(f"{c.short_name} {pct(c.revenue_growth_yoy, True)}" for c in grow) + " YoY (latest quarter)."))
    marg = sorted([c for c in cs if c.operating_margin is not None], key=lambda c: c.operating_margin, reverse=True)
    if len(marg) >= 2:
        s = (f"{marg[0].short_name} is {'the most' if many else 'more'} profitable: operating margin "
             + ", ".join(f"{c.short_name} {pct(c.operating_margin)}" for c in marg))
        if all(c.profit_margin is not None for c in marg):
            s += "; net margin " + ", ".join(f"{c.short_name} {pct(c.profit_margin)}" for c in marg)
        out.append(("margin", s + "."))
    prof = sorted([c for c in cs if c.earnings_growth_yoy is not None], key=lambda c: c.earnings_growth_yoy, reverse=True)
    if len(prof) >= 2:
        out.append(("profit", "Profit growth (latest quarter, YoY): " + ", ".join(f"{c.short_name} {pct(c.earnings_growth_yoy, True)}" for c in prof) + "."))
    return out


TOPIC_OF = {"revenue_ttm": "revenue", "revenue_share": "revenue", "revenue_growth_yoy": "growth", "net_income_ttm": "profit",
            "earnings_growth_yoy": "profit", "operating_margin": "margin", "profit_margin": "margin", "eps_ttm": "eps"}


def _ordered(pairs: list[tuple[str, str]], intent: Intent) -> list[str]:
    """Lead with the topics the question asked about."""
    if not intent.metrics_explicit:
        return [s for _, s in pairs]
    wanted = []
    for m in intent.metrics:
        t = TOPIC_OF.get(m)
        if t and t not in wanted:
            wanted.append(t)
    rank = {t: i for i, t in enumerate(wanted)}
    return [s for _, s in sorted(pairs, key=lambda p: rank.get(p[0], len(rank)))]


def _verification_line(cs: list[CompanyMetrics]) -> str:
    bad = [c.short_name for c in cs if c.verification_status == "mismatch"]
    if bad:
        return f"Check the flags on {', '.join(bad)}: at least one reported figure disagrees with the company's own annual report."
    who = "Both companies'" if len(cs) == 2 else ("All companies'" if len(cs) > 2 else f"{cs[0].short_name}'s")
    return f"{who} figures pass every consistency check against their own filings."


def answer(db: Session, intent: Intent) -> DatabaseAnswer | NotAnswered:
    started = time.perf_counter()
    if not intent.companies:
        return NotAnswered(reason="No company that VeriFi holds was mentioned.")

    # Database first. A company with nothing stored is fetched from the
    # source once and stored - every later question about it is instant.
    missing, _ = sync.freshness(intent.companies)
    warnings = sync.refresh(missing, None) if missing else []

    # One batched read: the companies asked about plus their industry peers
    # (peers are needed for each company's share of industry revenue).
    ids = [store.company_id_for(r.nse) for r in intent.companies]
    industry = industry_for_company(intent.companies[0].nse)
    peer_refs = [r for r in (industry.companies if industry else []) if r.nse not in {c.nse for c in intent.companies}]
    all_refs = [*intent.companies, *peer_refs]
    filings = store.load_filings_many(db, [store.company_id_for(r.nse) for r in all_refs], limit=12)
    metrics = {r.nse: analytics.build_company_metrics(r, filings[store.company_id_for(r.nse)]) for r in all_refs}
    analytics.compute_revenue_share([m for m in metrics.values() if m.available])

    companies = [metrics[r.nse] for r in intent.companies]
    peers = [c for c in companies if c.available]
    if not peers:
        return NotAnswered(reason="No stored results for these companies yet.")

    ends = sorted({f.period_end for cid in ids for f in filings[cid]})
    coverage = f"quarters ended {_d(ends[0])} to {_d(ends[-1])}" if ends else "no quarters yet"

    period_figures = {}
    if intent.period:
        for ref, cid in zip(intent.companies, ids):
            period_figures[ref.nse] = _period_figures(filings[cid], intent.period, coverage)

    summary = _ordered(_comparison_summary(peers) if len(peers) >= 2 else _company_summary(peers[0]), intent)
    if intent.period:
        lead = []
        for ref in intent.companies:
            pf = period_figures[ref.nse]
            if pf.found:
                bits = [f"revenue {inr(pf.revenue)}"] + ([f"net profit {inr(pf.net_income)}"] if pf.net_income is not None else [])
                lead.append(f"{ref.short_name}, {pf.label}: " + ", ".join(bits) + ".")
            else:
                lead.append(f"{ref.short_name}: {pf.message} The latest figures are shown instead.")
        summary = lead + summary
    summary.append(_verification_line(peers))

    synced = [s for s in db.scalars(select(CompanyORM.last_synced_at).where(CompanyORM.company_id.in_(ids))) if s]
    return DatabaseAnswer(
        query=intent.query,
        intent="comparison" if len(peers) >= 2 else "company",
        companies=companies,
        focus_metrics=intent.metrics,
        period_label=intent.period.label if intent.period else None,
        period_figures=period_figures,
        comparison=_rows(companies, list(ROWS)),
        summary=summary,
        coverage=coverage,
        wants_qualitative=intent.wants_qualitative,
        qualitative_terms=intent.qualitative_terms,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
        synced_at=min(synced).isoformat() if synced else None,
        warnings=warnings,
    )
