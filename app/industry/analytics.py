"""Deterministic industry analytics over companies' reported financials.

Every figure is computed from quarterly results as filed by the companies
(read from the database): trailing-twelve-month totals, growth, margins,
EPS, each company's share of industry revenue, and consistency checks
between independently reported figures. Pure functions - no network, no
LLM, no estimates, and no market data.
"""
from __future__ import annotations

import math
from datetime import date
from statistics import mean, median
from typing import Any

from app.industry.nse import Filing
from app.schemas.industry import (
    CompanyMetrics,
    Concentration,
    CrossCheck,
    IndustryCompanyRef,
    Insight,
    MetricAggregate,
    QuarterPoint,
)

# Metrics aggregated across the peer set, with whether higher is better
# (drives leader/laggard). None = no natural direction (e.g. size).
AGGREGATED_METRICS: dict[str, bool | None] = {
    "revenue_ttm": None,
    "net_income_ttm": None,
    "revenue_growth_yoy": True,
    "earnings_growth_yoy": True,
    "operating_margin": True,
    "profit_margin": True,
    "eps_ttm": None,
}
# Ratios also averaged weighted by revenue - equivalent to the margin of the
# industry taken as a whole.
WEIGHTED_METRICS = {"revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "profit_margin"}


def num(value: Any) -> float | None:
    """Coerces a value to a finite float, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---- Per-company metrics -------------------------------------------------


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def trailing_four(filings: list[Filing], ending: str | None = None) -> list[Filing] | None:
    """Four consecutive quarterly filings (newest first) ending at `ending`
    (default: the latest), or None if any quarter in between is missing."""
    start = 0 if ending is None else next((i for i, f in enumerate(filings) if f.period_end == ending), None)
    if start is None:
        return None
    window = filings[start:start + 4]
    if len(window) < 4:
        return None
    for newer, older in zip(window, window[1:]):
        if not 80 <= _days(newer.period_end, older.period_end) <= 100:
            return None
    return window


def _sum(values: list[float | None]) -> float | None:
    return sum(values) if values and all(v is not None for v in values) else None


def _year_earlier(filings: list[Filing], period_end: str) -> Filing | None:
    return next((f for f in filings if f.period_end < period_end and 350 <= _days(period_end, f.period_end) <= 380), None)


def _growth(now: float | None, before: float | None) -> float | None:
    return now / before - 1 if now is not None and before is not None and before > 0 else None


def _check(metric: str, label: str, reported: float | None, recomputed: float | None, tolerance_pct: float,
           ok_text: str, bad_text: str, missing_text: str) -> CrossCheck:
    if reported is None or recomputed is None or recomputed == 0:
        return CrossCheck(metric=metric, label=label, reported=reported, recomputed=recomputed,
                          tolerance_pct=tolerance_pct, status="unavailable", detail=missing_text)
    diff = abs(reported - recomputed) / abs(recomputed) * 100
    ok = diff <= tolerance_pct
    return CrossCheck(
        metric=metric, label=label, reported=reported, recomputed=recomputed, difference_pct=round(diff, 3),
        tolerance_pct=tolerance_pct, status="verified" if ok else "mismatch",
        detail=(ok_text if ok else bad_text).replace("{diff}", f"{diff:.2f}"),
    )


def build_company_metrics(ref: IndustryCompanyRef, filings: list[Filing]) -> CompanyMetrics:
    """One company's metrics, computed only from its stored quarterly
    filings (newest first)."""
    metrics = CompanyMetrics(name=ref.name, short_name=ref.short_name, symbol=ref.symbol, nse=ref.nse, tier=ref.tier)
    if not filings:
        metrics.available = False
        metrics.error = "No reported results stored for this company yet."
        return metrics

    latest = filings[0]
    metrics.latest_quarter = latest.period_end
    metrics.shares_outstanding = latest.shares
    metrics.quarters = [
        QuarterPoint(
            period_end=f.period_end, revenue=f.revenue, net_income=f.net_income, operating_profit=f.operating_profit,
            eps_diluted=f.eps_diluted, filed_at=f.filed_at, audited=f.audited, filing_url=f.url, notes=f.notes,
        )
        for f in reversed(filings)
    ]

    # ---- Trailing twelve months: the four latest consecutive filings ----
    ttm = trailing_four(filings)
    if ttm:
        metrics.revenue_ttm = _sum([f.revenue for f in ttm])
        metrics.net_income_ttm = _sum([f.net_income for f in ttm])
        op = _sum([f.operating_profit for f in ttm])
        if op is not None and metrics.revenue_ttm:
            metrics.operating_margin = op / metrics.revenue_ttm
        if metrics.net_income_ttm is not None and metrics.revenue_ttm:
            metrics.profit_margin = metrics.net_income_ttm / metrics.revenue_ttm
        metrics.eps_ttm = _sum([f.eps_diluted for f in ttm])

    # ---- Growth: latest quarter vs. the same quarter a year earlier ----
    prior = _year_earlier(filings, latest.period_end)
    if prior:
        metrics.revenue_growth_yoy = _growth(latest.revenue, prior.revenue)
        metrics.earnings_growth_yoy = _growth(latest.net_income, prior.net_income)

    # ---- Consistency checks between independently reported figures ----
    fy = next((f for f in filings if f.annual_revenue is not None), None)
    fy_quarters = trailing_four(filings, fy.period_end) if fy else None
    fy_label = f"FY ending {fy.period_end}" if fy else "the latest fiscal year"
    missing_fy = "The four quarters of a full fiscal year aren't all available to reconcile yet."
    # Basic EPS divides by the WEIGHTED-average share count, which lies
    # between the counts at the start and end of the quarter (a buyback or
    # share issue mid-quarter moves it). So the EPS-implied share count is
    # compared with whichever quarter-end count it's closer to.
    implied_shares = latest.net_income / latest.eps_basic if latest.net_income and latest.eps_basic else None
    prev_shares = filings[1].shares if len(filings) > 1 else None
    counts = [c for c in (latest.shares, prev_shares) if c]
    nearest = min(counts, key=lambda c: abs(c - implied_shares)) if implied_shares and counts else None
    if implied_shares and len(counts) == 2 and min(counts) <= implied_shares <= max(counts):
        nearest = implied_shares  # inside the range: consistent by construction
    checks = [
        _check("revenue_ttm", "Quarterly filings add up to the annual report (revenue)",
               fy.annual_revenue if fy else None, _sum([f.revenue for f in fy_quarters]) if fy_quarters else None, 1.5,
               f"The four quarterly revenue figures for {fy_label} sum to the full-year figure in the annual results (within {{diff}}%).",
               f"The four quarterly revenue figures for {fy_label} differ from the full-year figure by {{diff}}% - more than a later restatement of an earlier quarter would explain.",
               missing_fy),
        _check("net_income_ttm", "Quarterly filings add up to the annual report (net profit)",
               fy.annual_net_income if fy else None, _sum([f.net_income for f in fy_quarters]) if fy_quarters else None, 1.5,
               f"The four quarterly net profit figures for {fy_label} sum to the full-year figure (within {{diff}}%).",
               f"The four quarterly net profit figures for {fy_label} differ from the full-year figure by {{diff}}%.",
               missing_fy),
        _check("eps", "Reported EPS is consistent with net profit and shares in issue",
               implied_shares, nearest, 3.0,
               "Net profit ÷ reported basic EPS gives a share count consistent with the shares in issue during the quarter (within {diff}%).",
               "Net profit ÷ reported basic EPS implies a share count {diff}% away from the shares in issue - usually treasury shares held by an employee trust.",
               "The filing doesn't report enough to recompute EPS."),
    ]
    for f in filings:
        for note in f.notes:
            checks.append(CrossCheck(
                metric="filing_correction", label=f"Filing note - quarter ending {f.period_end}", tolerance_pct=0,
                status="unavailable", informational=True, detail=note,
            ))
    metrics.checks = checks
    statuses = {c.status for c in checks if not c.informational}
    metrics.verification_status = "mismatch" if "mismatch" in statuses else "verified" if "verified" in statuses else "unavailable"
    return metrics


# ---- Industry aggregates -----------------------------------------------------


def compute_revenue_share(companies: list[CompanyMetrics]) -> Concentration:
    """How the industry's revenue is split: each company's share, the
    Herfindahl-Hirschman index, and its reciprocal - the "effective number
    of companies" the industry's revenue is really spread across."""
    with_rev = [c for c in companies if c.revenue_ttm]
    total = sum(c.revenue_ttm for c in with_rev)
    if not total:
        return Concentration()
    for c in with_rev:
        c.revenue_share = c.revenue_ttm / total
    shares = sorted((c.revenue_share for c in with_rev), reverse=True)
    hhi = sum(s * s for s in shares)
    largest = max(with_rev, key=lambda c: c.revenue_ttm)
    return Concentration(
        total_revenue=total,
        hhi=hhi,
        effective_companies=1 / hhi if hhi else None,
        top3_share=sum(shares[:3]),
        largest=largest.short_name,
        largest_share=largest.revenue_share,
    )


def compute_aggregates(companies: list[CompanyMetrics]) -> dict[str, MetricAggregate]:
    result: dict[str, MetricAggregate] = {}
    for metric, higher_is_better in AGGREGATED_METRICS.items():
        pts = [(c, getattr(c, metric)) for c in companies if c.available and getattr(c, metric) is not None]
        if not pts:
            result[metric] = MetricAggregate(metric=metric)
            continue
        values = [v for _, v in pts]
        weighted = None
        if metric in WEIGHTED_METRICS:
            wpts = [(c.revenue_ttm, v) for c, v in pts if c.revenue_ttm]
            wsum = sum(w for w, _ in wpts)
            weighted = sum(w * v for w, v in wpts) / wsum if wsum else None
        top = max(pts, key=lambda p: p[1])[0].short_name
        bottom = min(pts, key=lambda p: p[1])[0].short_name
        result[metric] = MetricAggregate(
            metric=metric, median=median(values), mean=mean(values), weighted_mean=weighted,
            min=min(values), max=max(values), count=len(values),
            leader=None if higher_is_better is None else (top if higher_is_better else bottom),
            laggard=None if higher_is_better is None else (bottom if higher_is_better else top),
        )
    return result


def _pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def build_insights(
    companies: list[CompanyMetrics],
    aggregates: dict[str, MetricAggregate],
    concentration: Concentration,
) -> list[Insight]:
    """Plain-language takeaways, each derived from a specific computed
    figure - templated, not generated, so every sentence is traceable to
    the numbers on the page."""
    by_short = {c.short_name: c for c in companies}
    out: list[Insight] = []

    if concentration.total_revenue:
        n = sum(1 for c in companies if c.revenue_ttm)
        out.append(Insight(
            kind="concentration",
            title=f"{concentration.largest} earns {_pct(concentration.largest_share, 0)} of the industry's revenue",
            detail=(f"The three largest companies account for {_pct(concentration.top3_share, 0)} of combined revenue. "
                    f"Revenue is spread as if across {concentration.effective_companies:.1f} equal-sized companies, not {n}."),
        ))

    growth = aggregates.get("revenue_growth_yoy")
    if growth and growth.count >= 2:
        lead, lag = by_short[growth.leader], by_short[growth.laggard]
        out.append(Insight(
            kind="growth",
            title=f"{lead.short_name} is growing fastest at {_pct(lead.revenue_growth_yoy)} YoY",
            detail=(f"Median revenue growth across the group is {_pct(growth.median)}; "
                    f"{lag.short_name} is slowest at {_pct(lag.revenue_growth_yoy)}."),
        ))

    margin = aggregates.get("operating_margin")
    if margin and margin.count >= 2:
        lead = by_short[margin.leader]
        out.append(Insight(
            kind="profitability",
            title=f"{lead.short_name} has the widest operating margin ({_pct(lead.operating_margin)})",
            detail=(f"Industry median is {_pct(margin.median)}"
                    + (f", or {_pct(margin.weighted_mean)} for the industry as a whole." if margin.weighted_mean is not None else ".")),
        ))

    both = [c for c in companies if c.revenue_growth_yoy is not None and c.earnings_growth_yoy is not None]
    if both:
        ahead = [c.short_name for c in both if c.earnings_growth_yoy > c.revenue_growth_yoy]
        out.append(Insight(
            kind="earnings",
            title=f"Profit grew faster than revenue at {len(ahead)} of {len(both)} companies",
            detail=("Latest quarter vs. the same quarter a year earlier. "
                    + (f"Profit outpaced revenue at {', '.join(ahead)} - margins widened." if ahead
                       else "Nowhere did profit outpace revenue - margins narrowed across the group.")),
        ))

    net = aggregates.get("profit_margin")
    if net and net.count >= 2:
        lead, lag = by_short[net.leader], by_short[net.laggard]
        out.append(Insight(
            kind="profitability",
            title=f"{lead.short_name} keeps {_pct(lead.profit_margin)} of revenue as net profit",
            detail=f"The median net margin is {_pct(net.median)}; {lag.short_name} is lowest at {_pct(lag.profit_margin)}.",
        ))

    verified = sum(1 for c in companies if c.verification_status == "verified")
    mismatched = [c.short_name for c in companies if c.verification_status == "mismatch"]
    out.append(Insight(
        kind="data",
        title=f"{verified} of {len(companies)} companies pass every consistency check",
        detail=("Quarterly filings were reconciled with each annual report, and reported EPS with net profit and shares in issue. "
                + (f"Review: {', '.join(mismatched)} - at least one figure disagrees beyond tolerance." if mismatched
                   else "No figure disagreed beyond tolerance.")),
    ))
    return out
