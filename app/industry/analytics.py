"""Deterministic industry analytics over official NSE data: per-company
metrics from exchange filings and NSE prices, internal consistency
checks, aggregates, market-cap concentration, and return-based
diversification. Pure functions - no network, no LLM, no estimates - so
every number is reproducible from the filings it links to.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean, median
from typing import Any

import numpy as np
import pandas as pd

from app.industry.nse import CorporateAction, Filing
from app.schemas.industry import (
    CompanyMetrics,
    Concentration,
    CrossCheck,
    Diversification,
    IndustryCompanyRef,
    Insight,
    MetricAggregate,
    PairCorrelation,
    PortfolioStats,
    QuarterPoint,
)

TRADING_DAYS = 252
# Minimum daily observations before a company's returns are trusted for
# correlation/volatility (~6 months of trading).
MIN_RETURN_OBSERVATIONS = 120

# Metrics aggregated across the peer set, with whether higher is better
# (drives leader/laggard). None = no natural direction (e.g. size).
AGGREGATED_METRICS: dict[str, bool | None] = {
    "market_cap": None,
    "revenue_ttm": None,
    "net_income_ttm": None,
    "revenue_growth_yoy": True,
    "earnings_growth_yoy": True,
    "operating_margin": True,
    "profit_margin": True,
    "pe_trailing": False,
    "dividend_yield": True,
    "return_1y": True,
    "volatility_1y": False,
    "max_drawdown_1y": True,
}
# Ratios averaged by market-cap weight as well as plainly - a sector's
# "typical" margin is better described by its large constituents.
WEIGHTED_METRICS = {"revenue_growth_yoy", "earnings_growth_yoy", "operating_margin", "profit_margin", "dividend_yield"}


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


def split_factor_after(actions: list[CorporateAction], when: date, until: date | None = None) -> float:
    """Product of split/bonus factors with an ex-date after `when` (and on
    or before `until`). Dividing a pre-split price or per-share figure by
    this puts it on today's share basis."""
    factor = 1.0
    for a in actions:
        if a.split_factor and a.ex_date > when and (until is None or a.ex_date <= until):
            factor *= a.split_factor
    return factor


def adjusted_closes(closes: list[tuple[date, float]], actions: list[CorporateAction]) -> pd.Series:
    """NSE closes are as-traded; a bonus or split would otherwise show as a
    fake crash. Earlier prices are divided by every later split factor."""
    if not closes:
        return pd.Series(dtype=float)
    return pd.Series(
        [price / split_factor_after(actions, day) for day, price in closes],
        index=pd.DatetimeIndex([pd.Timestamp(day) for day, _ in closes]),
        dtype=float,
    )


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


def build_company_metrics(
    ref: IndustryCompanyRef,
    filings: list[Filing],
    closes: list[tuple[date, float]],
    actions: list[CorporateAction],
    bhavcopy_close: float | None,
) -> CompanyMetrics:
    """One company's metrics, computed only from its NSE filings (newest
    first), its NSE daily closes and its NSE corporate actions."""
    metrics = CompanyMetrics(name=ref.name, short_name=ref.short_name, symbol=ref.symbol, nse=ref.nse, tier=ref.tier)
    if not filings:
        metrics.available = False
        metrics.error = "No consolidated results filings found for this company."
        return metrics

    latest = filings[0]
    metrics.latest_quarter = latest.period_end
    metrics.quarters = [
        QuarterPoint(
            period_end=f.period_end, revenue=f.revenue, net_income=f.net_income, operating_profit=f.operating_profit,
            eps_diluted=f.eps_diluted, filed_at=f.filed_at, audited=f.audited, filing_url=f.url, notes=f.notes,
        )
        for f in reversed(filings)
    ]

    # ---- Price & size: NSE close x shares in issue from the latest filing ----
    price_day = closes[-1][0] if closes else None
    if closes:
        metrics.price = closes[-1][1]
        metrics.price_date = price_day.isoformat()
        if len(closes) > 1:
            metrics.previous_close = closes[-2][1] / split_factor_after(actions, closes[-2][0], price_day)
    if latest.shares:
        # Paid-up capital / face value is the share count at quarter end;
        # a later bonus or split changes it, so carry those forward.
        metrics.shares_outstanding = latest.shares * split_factor_after(actions, date.fromisoformat(latest.period_end), price_day)
    if metrics.price and metrics.shares_outstanding:
        metrics.market_cap = metrics.price * metrics.shares_outstanding

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
        eps = [None if f.eps_diluted is None
               else f.eps_diluted / split_factor_after(actions, date.fromisoformat(f.period_end), price_day) for f in ttm]
        metrics.eps_ttm = _sum(eps)
        if metrics.price and metrics.eps_ttm and metrics.eps_ttm > 0:
            metrics.pe_trailing = metrics.price / metrics.eps_ttm

    # ---- Growth: latest quarter vs. the same quarter a year earlier ----
    prior = _year_earlier(filings, latest.period_end)
    if prior:
        metrics.revenue_growth_yoy = _growth(latest.revenue, prior.revenue)
        metrics.earnings_growth_yoy = _growth(latest.net_income, prior.net_income)

    # ---- Dividends with an ex-date in the 12 months to the price date ----
    if price_day and metrics.price:
        window_start = price_day - timedelta(days=365)
        paid = [a.dividend / split_factor_after(actions, a.ex_date, price_day)
                for a in actions if a.dividend and window_start < a.ex_date <= price_day]
        metrics.dividends_ttm = sum(paid)
        metrics.dividend_yield = metrics.dividends_ttm / metrics.price

    # ---- Consistency checks between independently reported official figures ----
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
        _check("price", "Closing price matches the official end-of-day file",
               metrics.price, bhavcopy_close, 0.05,
               f"The {metrics.price_date} close agrees with the official end-of-day file (within {{diff}}%).",
               f"The {metrics.price_date} close differs from the official end-of-day file by {{diff}}%.",
               "The official end-of-day file for this date wasn't available to compare."),
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


# ---- Price-based statistics ------------------------------------------------


def daily_returns(closes: pd.DataFrame) -> pd.DataFrame:
    """Simple daily returns per symbol, dropping symbols with too little
    history to say anything reliable about their risk."""
    if closes is None or closes.empty:
        return pd.DataFrame()
    returns = closes.sort_index().pct_change(fill_method=None).iloc[1:]
    keep = [c for c in returns.columns if returns[c].count() >= MIN_RETURN_OBSERVATIONS]
    return returns[keep]


def apply_price_stats(companies: list[CompanyMetrics], closes: pd.DataFrame) -> None:
    if closes is None or closes.empty:
        return
    closes = closes.sort_index()
    for company in companies:
        if company.symbol not in closes.columns:
            continue
        series = closes[company.symbol].dropna()
        if len(series) < MIN_RETURN_OBSERVATIONS:
            continue
        rets = series.pct_change().dropna()
        company.return_1y = num(series.iloc[-1] / series.iloc[0] - 1)
        company.volatility_1y = num(rets.std() * math.sqrt(TRADING_DAYS))
        company.max_drawdown_1y = num((series / series.cummax() - 1).min())


def _portfolio(label: str, weights: np.ndarray, mu: np.ndarray, cov: np.ndarray) -> PortfolioStats:
    vols = np.sqrt(np.diag(cov))
    port_vol = float(np.sqrt(weights @ cov @ weights))
    avg_vol = float(weights @ vols)
    return PortfolioStats(
        label=label,
        expected_return=num(weights @ mu),
        volatility=num(port_vol),
        diversification_ratio=num(avg_vol / port_vol) if port_vol else None,
        volatility_reduction=num(1 - port_vol / avg_vol) if avg_vol else None,
    )


def compute_diversification(companies: list[CompanyMetrics], closes: pd.DataFrame) -> Diversification:
    """Return-correlation view of the peer set: how much holding several
    of these companies actually diversifies versus holding one.

    Correlations/covariances come from overlapping daily returns only
    (rows where every included symbol traded), annualised over 252
    trading days. The covariance matrix is exposed so the frontend's
    basket builder can recompute portfolio volatility for any selection
    without another request."""
    returns = daily_returns(closes).dropna(how="any")
    if returns.shape[1] < 2 or len(returns) < MIN_RETURN_OBSERVATIONS:
        return Diversification()

    by_symbol = {c.symbol: c for c in companies}
    symbols = [c.symbol for c in companies if c.symbol in returns.columns]
    returns = returns[symbols]
    corr = returns.corr().to_numpy()
    cov = returns.cov().to_numpy() * TRADING_DAYS
    mu = returns.mean().to_numpy() * TRADING_DAYS

    n = len(symbols)
    iu = np.triu_indices(n, k=1)
    pairs = sorted(
        (PairCorrelation(a=symbols[i], b=symbols[j], correlation=round(float(corr[i, j]), 4)) for i, j in zip(*iu)),
        key=lambda p: p.correlation,
    )

    caps = np.array([by_symbol[s].market_cap or 0.0 for s in symbols])
    cap_weights = caps / caps.sum() if caps.sum() > 0 else None

    def matrix(m: np.ndarray) -> list[list[float | None]]:
        return [[num(round(float(v), 6)) for v in row] for row in m]

    return Diversification(
        symbols=symbols,
        lookback_days=len(returns),
        correlation=matrix(corr),
        covariance=matrix(cov),
        annual_returns=[num(round(float(v), 6)) for v in mu],
        average_pairwise_correlation=num(corr[iu].mean()),
        equal_weight=_portfolio("Equal-weight basket", np.full(n, 1 / n), mu, cov),
        market_cap_weight=_portfolio("Market-cap-weighted basket", cap_weights, mu, cov) if cap_weights is not None else None,
        least_correlated=pairs[:3],
        most_correlated=list(reversed(pairs[-3:])),
    )


# ---- Industry aggregates -----------------------------------------------------


def compute_concentration(companies: list[CompanyMetrics]) -> Concentration:
    """Market-cap concentration: each company's weight, the
    Herfindahl-Hirschman index, and its reciprocal - the "effective number
    of companies" the sector's value is really spread across."""
    capped = [c for c in companies if c.market_cap]
    total = sum(c.market_cap for c in capped)
    if not total:
        return Concentration()
    for c in capped:
        c.market_cap_weight = c.market_cap / total
    weights = sorted((c.market_cap_weight for c in capped), reverse=True)
    hhi = sum(w * w for w in weights)
    largest = max(capped, key=lambda c: c.market_cap)
    return Concentration(
        total_market_cap=total,
        hhi=hhi,
        effective_companies=1 / hhi if hhi else None,
        top3_share=sum(weights[:3]),
        largest=largest.short_name,
        largest_share=largest.market_cap_weight,
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
            wpts = [(c.market_cap, v) for c, v in pts if c.market_cap]
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
    diversification: Diversification,
) -> list[Insight]:
    """Plain-language takeaways, each derived from a specific computed
    figure above - templated, not generated, so every sentence is
    traceable to the numbers on the page."""
    names = {c.symbol: c.short_name for c in companies}
    by_short = {c.short_name: c for c in companies}
    out: list[Insight] = []

    if concentration.total_market_cap:
        out.append(Insight(
            kind="concentration",
            title=f"{concentration.largest} alone is {_pct(concentration.largest_share, 0)} of the sector",
            detail=(f"The top three companies hold {_pct(concentration.top3_share, 0)} of combined market value. "
                    f"Value behaves as if spread across {concentration.effective_companies:.1f} equal-sized companies, "
                    f"not {sum(1 for c in companies if c.market_cap)} - a cap-weighted IT index is a concentrated bet."),
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
                    + (f", or {_pct(margin.weighted_mean)} weighted by market cap." if margin.weighted_mean is not None else ".")),
        ))

    pe = aggregates.get("pe_trailing")
    if pe and pe.count >= 2 and pe.median:
        rich = by_short[pe.laggard]
        cheap = by_short[pe.leader]
        out.append(Insight(
            kind="valuation",
            title=f"{rich.short_name} trades at {rich.pe_trailing:.1f}× earnings vs. a {pe.median:.1f}× median",
            detail=f"The cheapest on trailing earnings is {cheap.short_name} at {cheap.pe_trailing:.1f}×.",
        ))

    ew = diversification.equal_weight
    if ew and ew.volatility_reduction is not None and diversification.average_pairwise_correlation is not None:
        low = diversification.least_correlated[0] if diversification.least_correlated else None
        out.append(Insight(
            kind="diversification",
            title=f"An equal-weight basket cuts volatility by {_pct(ew.volatility_reduction, 0)}",
            detail=(f"Average pairwise return correlation is {diversification.average_pairwise_correlation:.2f}"
                    + (f"; the least correlated pair is {names.get(low.a, low.a)} and {names.get(low.b, low.b)} ({low.correlation:.2f})." if low else ".")
                    + " Stocks in one industry share the same demand cycle, so diversification within it has a ceiling."),
        ))

    vol = aggregates.get("volatility_1y")
    if vol and vol.count >= 2:
        risky = by_short[vol.laggard]
        out.append(Insight(
            kind="risk",
            title=f"{risky.short_name} is the most volatile ({_pct(risky.volatility_1y, 0)} annualised)",
            detail=(f"Its deepest 1-year drawdown was {_pct(risky.max_drawdown_1y, 0)}; "
                    f"the industry median volatility is {_pct(vol.median, 0)}."),
        ))

    verified = sum(1 for c in companies if c.verification_status == "verified")
    mismatched = [c.short_name for c in companies if c.verification_status == "mismatch"]
    out.append(Insight(
        kind="data",
        title=f"{verified} of {len(companies)} companies pass every cross-check",
        detail=("Quarterly filings were reconciled with each annual report, EPS with profit ÷ shares, and prices with the official end-of-day file. "
                + (f"Review: {', '.join(mismatched)} - at least one figure disagrees beyond tolerance." if mismatched
                   else "No figure disagreed beyond tolerance.")),
    ))
    return out
