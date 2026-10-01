"""Exchange filings client - the automated source of company financials.

Company financials come from the XBRL of each company's quarterly results as
filed with the exchange ("Integrated Filing - Financials", consolidated):
the audited/reviewed numbers the company published, not an aggregator's
re-keyed copy. Parsed filings are stored in the database (app/datastore);
this module only lists and parses them.

The exchange's JSON API needs a browser-like client with session cookies, so
a Chrome-impersonating curl_cffi session is warmed on the homepage first.
One session per thread - curl_cffi sessions aren't safe to share.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger("financial_research_agent.industry.nse")

BASE = "https://www.nseindia.com"
FILINGS_URL = BASE + "/api/integrated-filing-results?index=equities&symbol={symbol}&type=Integrated%20Filing-%20Financials"

TIMEOUT = 20


class NSEError(RuntimeError):
    pass


_local = threading.local()


def _session():
    from curl_cffi import requests

    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session(impersonate="chrome")
        sess.get(BASE + "/", timeout=TIMEOUT)  # sets the cookies the API requires
        _local.session = sess
    return sess


def _get(url: str, referer: str = BASE + "/"):
    for attempt in (1, 2):
        resp = _session().get(url, timeout=TIMEOUT, headers={"Referer": referer})
        if resp.status_code in (401, 403) and attempt == 1:
            _local.session = None  # cookies expired - re-warm once
            continue
        if resp.status_code != 200:
            raise NSEError(f"NSE returned HTTP {resp.status_code} for {url.split('?')[0]}")
        return resp
    raise NSEError(f"NSE refused {url.split('?')[0]}")


# ---- Filings -----------------------------------------------------------------


@dataclass
class Filing:
    """One quarterly result, parsed from the company's XBRL filing."""

    symbol: str
    period_start: str
    period_end: str
    filed_at: str
    audited: str | None
    revision: str | None
    url: str
    revenue: float | None = None
    net_income: float | None = None           # attributable to owners of the parent
    profit_before_exceptional_and_tax: float | None = None
    profit_before_tax: float | None = None
    finance_costs: float | None = None
    other_income: float | None = None
    eps_basic: float | None = None
    eps_diluted: float | None = None
    paid_up_capital: float | None = None
    face_value: float | None = None
    # Twelve-month figures, present in the March (fiscal year-end) filing.
    annual_start: str | None = None
    annual_revenue: float | None = None
    annual_net_income: float | None = None
    # Corrections applied to the company's own tagging, shown to the user.
    notes: list[str] = field(default_factory=list)

    @property
    def shares(self) -> float | None:
        if self.paid_up_capital and self.face_value:
            return self.paid_up_capital / self.face_value
        return None

    @property
    def operating_profit(self) -> float | None:
        """Operating profit = profit before exceptional items and tax,
        plus finance costs, less other income - i.e. earnings from the
        business itself, excluding treasury income and one-offs."""
        base = self.profit_before_exceptional_and_tax if self.profit_before_exceptional_and_tax is not None else self.profit_before_tax
        if base is None or self.finance_costs is None or self.other_income is None:
            return None
        return base + self.finance_costs - self.other_income


_CONTEXT_RE = re.compile(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', re.S)


def _fact_values(xml: str, tag: str) -> list[tuple[str, str]]:
    """(contextRef, value) for every occurrence of an in-bse-fin fact."""
    out = []
    for attrs, value in re.findall(rf"<in-[a-z-]+:{tag}\b([^>]*)>([^<]*)<", xml):
        ctx = re.search(r'contextRef="([^"]+)"', attrs)
        if ctx:
            out.append((ctx.group(1), value.strip()))
    return out


def parse_filing_xbrl(xml: str, meta: dict) -> Filing:
    """Extracts the figures we use from one results filing.

    A results filing contains several reporting contexts (this quarter,
    year-to-date, sometimes segments). Only dimension-free contexts are
    used, and the quarter vs. twelve-month figure is chosen by the
    context's own start/end dates - never by position in the file."""
    contexts: dict[str, tuple[str, str]] = {}
    for cid, body in _CONTEXT_RE.findall(xml):
        if "explicitMember" in body or "typedMember" in body:
            continue
        start = re.search(r"startDate>([^<]+)<", body)
        end = re.search(r"endDate>([^<]+)<", body)
        if start and end:
            contexts[cid] = (start.group(1).strip(), end.group(1).strip())

    period_end = datetime.strptime(meta["qe_Date"], "%d-%b-%Y").date().isoformat()

    def span(cid: str) -> int | None:
        if cid not in contexts:
            return None
        s, e = contexts[cid]
        if e != period_end:
            return None
        return (date.fromisoformat(e) - date.fromisoformat(s)).days

    def pick(tag: str, lo: int, hi: int) -> tuple[float | None, str | None]:
        for cid, raw in _fact_values(xml, tag):
            days = span(cid)
            if days is not None and lo <= days <= hi:
                try:
                    return float(raw), contexts[cid][0]
                except ValueError:
                    continue
        return None, None

    def quarter(*tags: str) -> float | None:
        for tag in tags:
            v, _ = pick(tag, 80, 100)
            if v is not None:
                return v
        return None

    revenue, quarter_start = pick("RevenueFromOperations", 80, 100)
    annual_revenue, annual_start = pick("RevenueFromOperations", 355, 370)
    notes: list[str] = []

    def owners_profit(lo: int, hi: int, label: str) -> float | None:
        """Profit attributable to owners of the parent. Companies sometimes
        mis-tag this fact (measured: Tech Mahindra Dec-2025 filed Rs 198.7 Cr
        against total profit of Rs 1,118.6 Cr; Persistent Jun-2026 filed 0).
        The accounting identity owners = total profit - minority interest
        must hold, so when the tagged value breaks it the identity is used
        and the correction is recorded for display."""
        tagged, _ = pick("ProfitOrLossAttributableToOwnersOfParent", lo, hi)
        total, _ = pick("ProfitLossForPeriod", lo, hi)
        nci, _ = pick("ProfitOrLossAttributableToNonControllingInterests", lo, hi)
        derived = total - (nci or 0.0) if total is not None else None
        if tagged is None:
            return derived
        if derived is not None and abs(derived) > 0 and abs(tagged - derived) / abs(derived) > 0.02:
            notes.append(
                f"{label}: the filing tags profit attributable to owners as Rs {tagged / 1e7:,.1f} Cr, but its own "
                f"total profit (Rs {total / 1e7:,.1f} Cr) less minority interest (Rs {(nci or 0) / 1e7:,.1f} Cr) is "
                f"Rs {derived / 1e7:,.1f} Cr. The arithmetic figure is used."
            )
            return derived
        return tagged

    quarter_ni = owners_profit(80, 100, "Quarter")
    annual_ni = owners_profit(355, 370, "Full year")
    if revenue is None:
        notes.append("The filing doesn't tag a three-month revenue figure for this quarter, so it is shown as unavailable.")

    return Filing(
        symbol=meta["symbol"],
        period_start=quarter_start or "",
        period_end=period_end,
        filed_at=meta.get("broadcast_Date") or "",
        audited=meta.get("audited"),
        revision=meta.get("type_Sub"),
        url=meta["xbrl"],
        revenue=revenue,
        net_income=quarter_ni,
        profit_before_exceptional_and_tax=quarter("ProfitBeforeExceptionalItemsAndTax"),
        profit_before_tax=quarter("ProfitBeforeTax"),
        finance_costs=quarter("FinanceCosts"),
        other_income=quarter("OtherIncome"),
        eps_basic=quarter("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "BasicEarningsLossPerShareFromContinuingOperations"),
        eps_diluted=quarter("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "DilutedEarningsLossPerShareFromContinuingOperations"),
        paid_up_capital=quarter("PaidUpValueOfEquityShareCapital"),
        face_value=quarter("FaceValueOfEquityShareCapital"),
        annual_start=annual_start,
        annual_revenue=annual_revenue,
        annual_net_income=annual_ni,
        notes=notes,
    )


def _filed(row: dict) -> datetime:
    try:
        return datetime.strptime(row.get("broadcast_Date") or "", "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return datetime.min


def list_filings(symbol: str, max_quarters: int = 6) -> list[dict]:
    """The company's latest consolidated quarterly filings (metadata only),
    newest first. When a quarter was filed more than once (a revision), the
    most recently broadcast filing wins."""
    rows = _get(FILINGS_URL.format(symbol=symbol), BASE + "/companies-listing/corporate-integrated-filing").json().get("data", [])
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("consolidated") != "Consolidated" or not row.get("xbrl") or not row.get("qe_Date"):
            continue
        prev = latest.get(row["qe_Date"])
        if prev is None or _filed(row) > _filed(prev):
            latest[row["qe_Date"]] = row
    return sorted(latest.values(), key=lambda r: datetime.strptime(r["qe_Date"], "%d-%b-%Y"), reverse=True)[:max_quarters]


def download_filing(symbol: str, row: dict) -> Filing:
    """Downloads and parses one filing listed by list_filings."""
    return parse_filing_xbrl(_get(row["xbrl"]).text, {**row, "symbol": symbol})
