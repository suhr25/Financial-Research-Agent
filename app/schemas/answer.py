"""A question answered straight from the VeriFi database."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.industry import CompanyMetrics


class ComparisonRow(BaseModel):
    """One figure across the companies asked about."""

    metric: str
    label: str
    kind: Literal["inr", "pct", "pct_signed", "rupees"]
    values: dict[str, float | None]  # keyed by company symbol (NSE)
    leader: str | None = None        # symbol with the best value, if "better" has a direction
    note: str | None = None


class PeriodFigures(BaseModel):
    """A company's figures for the specific period the question asked about."""

    found: bool
    label: str
    period_end: str
    revenue: float | None = None
    net_income: float | None = None
    operating_profit: float | None = None
    eps_diluted: float | None = None
    filing_url: str | None = None
    filed_at: str | None = None
    message: str | None = None


class DatabaseAnswer(BaseModel):
    answered: bool = True
    answered_from: Literal["database"] = "database"
    query: str
    intent: Literal["company", "comparison"]
    companies: list[CompanyMetrics]
    focus_metrics: list[str]
    period_label: str | None = None
    period_figures: dict[str, PeriodFigures] = Field(default_factory=dict)
    comparison: list[ComparisonRow] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    coverage: str = ""
    wants_qualitative: bool = False
    qualitative_terms: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    synced_at: str | None = None
    warnings: list[str] = Field(default_factory=list)


class NotAnswered(BaseModel):
    answered: bool = False
    reason: str
