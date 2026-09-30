"""Industry-level schemas: one IndustrySnapshot is everything the industry
dashboard shows for a peer universe (e.g. the ten NIFTY IT companies) -
per-company metrics, deterministic cross-checks, industry aggregates,
market-cap concentration, and return-based diversification analytics.

All monetary values are absolute amounts in the industry's reporting
currency (INR for NIFTY IT); ratios are fractions (0.24 = 24%), never
percent numbers, so the frontend formats them one way.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class IndustryCompanyRef(BaseModel):
    name: str
    short_name: str
    symbol: str
    nse: str
    tier: str
    aliases: list[str] = Field(default_factory=list)


class IndustryDefinition(BaseModel):
    id: str
    name: str
    short_name: str
    universe: str
    description: str
    currency: str
    benchmark_symbol: str | None = None
    companies: list[IndustryCompanyRef]


class IndustrySummary(BaseModel):
    id: str
    name: str
    short_name: str
    universe: str
    description: str
    company_count: int


CheckStatus = Literal["verified", "mismatch", "unavailable"]


class CrossCheck(BaseModel):
    """One deterministic consistency check between two independently
    reported figures (e.g. the provider's TTM revenue vs the sum of the
    last four reported quarters). No LLM is involved."""

    metric: str
    label: str
    reported: float | None = None
    recomputed: float | None = None
    difference_pct: float | None = None
    tolerance_pct: float
    status: CheckStatus
    detail: str
    # True when the provider figure being checked is NOT the one displayed
    # (e.g. we show statement-derived growth instead). Such a check is
    # reported but doesn't affect the company's verification status.
    informational: bool = False


class QuarterPoint(BaseModel):
    """One quarter exactly as filed with the exchange."""

    period_end: str
    revenue: float | None = None
    net_income: float | None = None
    operating_profit: float | None = None
    eps_diluted: float | None = None
    filed_at: str | None = None
    audited: str | None = None
    filing_url: str | None = None
    notes: list[str] = Field(default_factory=list)


class CompanyMetrics(BaseModel):
    """Every figure is either taken from an NSE filing / NSE price data or
    computed from those with a stated formula. Nothing is estimated, and no
    third-party aggregator is used."""

    name: str
    short_name: str
    symbol: str
    nse: str
    tier: str
    available: bool = True
    error: str | None = None

    price: float | None = None
    price_date: str | None = None
    previous_close: float | None = None
    shares_outstanding: float | None = None
    market_cap: float | None = None
    revenue_ttm: float | None = None
    net_income_ttm: float | None = None
    eps_ttm: float | None = None
    revenue_growth_yoy: float | None = None
    earnings_growth_yoy: float | None = None
    operating_margin: float | None = None
    profit_margin: float | None = None
    pe_trailing: float | None = None
    dividends_ttm: float | None = None
    dividend_yield: float | None = None

    return_1y: float | None = None
    volatility_1y: float | None = None
    max_drawdown_1y: float | None = None
    market_cap_weight: float | None = None

    latest_quarter: str | None = None
    quarters: list[QuarterPoint] = Field(default_factory=list)
    checks: list[CrossCheck] = Field(default_factory=list)
    verification_status: CheckStatus = "unavailable"


class MetricAggregate(BaseModel):
    metric: str
    median: float | None = None
    mean: float | None = None
    weighted_mean: float | None = None
    min: float | None = None
    max: float | None = None
    leader: str | None = None
    laggard: str | None = None
    count: int = 0


class Concentration(BaseModel):
    total_market_cap: float | None = None
    hhi: float | None = None
    effective_companies: float | None = None
    top3_share: float | None = None
    largest: str | None = None
    largest_share: float | None = None


class PairCorrelation(BaseModel):
    a: str
    b: str
    correlation: float


class PortfolioStats(BaseModel):
    label: str
    expected_return: float | None = None
    volatility: float | None = None
    diversification_ratio: float | None = None
    volatility_reduction: float | None = None


class Diversification(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    lookback_days: int = 0
    correlation: list[list[float | None]] = Field(default_factory=list)
    covariance: list[list[float | None]] = Field(default_factory=list)
    annual_returns: list[float | None] = Field(default_factory=list)
    average_pairwise_correlation: float | None = None
    equal_weight: PortfolioStats | None = None
    market_cap_weight: PortfolioStats | None = None
    least_correlated: list[PairCorrelation] = Field(default_factory=list)
    most_correlated: list[PairCorrelation] = Field(default_factory=list)


class Insight(BaseModel):
    kind: Literal["concentration", "growth", "profitability", "valuation", "diversification", "risk", "data"]
    title: str
    detail: str


class IndustrySnapshot(BaseModel):
    industry: IndustrySummary
    mode: Literal["live", "demo"]
    currency: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fetch_seconds: float | None = None
    stale: bool = False
    refreshing: bool = False
    price_date: str | None = None
    source: str = "NSE India - company results filings (XBRL) and NSE end-of-day prices"
    companies: list[CompanyMetrics]
    aggregates: dict[str, MetricAggregate] = Field(default_factory=dict)
    concentration: Concentration = Field(default_factory=Concentration)
    diversification: Diversification = Field(default_factory=Diversification)
    insights: list[Insight] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
