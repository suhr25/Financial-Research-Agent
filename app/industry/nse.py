"""NSE India client - the official source for the industry dashboard.

Everything the dashboard shows comes from the exchange itself:
  - Company financials: the XBRL of each company's quarterly results as
    filed with NSE ("Integrated Filing - Financials", consolidated). These
    are the audited/reviewed numbers the company published, not an
    aggregator's re-keyed copy.
  - Prices: NSE's security-wise daily price history (EQ series), with the
    latest close cross-checked against NSE's end-of-day bhavcopy file.
  - Corporate actions: splits/bonuses (to adjust historical prices) and
    dividends (for dividend yield).

NSE's JSON APIs need a browser-like client with session cookies, so a
Chrome-impersonating curl_cffi session is warmed on the homepage first.
One session per thread - curl_cffi sessions aren't safe to share.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("financial_research_agent.industry.nse")

BASE = "https://www.nseindia.com"
FILINGS_URL = BASE + "/api/integrated-filing-results?index=equities&symbol={symbol}&type=Integrated%20Filing-%20Financials"
HISTORY_URL = (BASE + "/api/historicalOR/generateSecurityWiseHistoricalData"
               "?from={start}&to={end}&symbol={symbol}&type=priceVolumeDeliverable&series=ALL")
ACTIONS_URL = BASE + "/api/corporates-corporateActions?index=equities&symbol={symbol}"
BHAVCOPY_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{day}_F_0000.csv.zip"

TIMEOUT = 20
# NSE returns at most ~3 months of daily rows per history request.
HISTORY_CHUNK_DAYS = 85


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


def fetch_filings(symbol: str, cache_dir: Path, max_quarters: int = 6) -> list[Filing]:
    """The company's latest consolidated quarterly filings, newest first.

    When a quarter was filed more than once (a revision), the most recently
    broadcast filing wins. Parsed filings are cached on disk keyed by their
    archive URL - an exchange filing never changes once published, so each
    XBRL is downloaded exactly once."""
    rows = _get(FILINGS_URL.format(symbol=symbol), BASE + "/companies-listing/corporate-integrated-filing").json().get("data", [])
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("consolidated") != "Consolidated" or not row.get("xbrl") or not row.get("qe_Date"):
            continue
        prev = latest.get(row["qe_Date"])
        if prev is None or _filed(row) > _filed(prev):
            latest[row["qe_Date"]] = row
    chosen = sorted(latest.values(), key=lambda r: datetime.strptime(r["qe_Date"], "%d-%b-%Y"), reverse=True)[:max_quarters]

    cache_dir.mkdir(parents=True, exist_ok=True)
    filings = []
    for row in chosen:
        path = cache_dir / (Path(row["xbrl"]).stem + ".json")
        if path.exists():
            try:
                filings.append(Filing(**json.loads(path.read_text(encoding="utf-8"))))
                continue
            except (TypeError, ValueError):
                pass
        xml = _get(row["xbrl"]).text
        filing = parse_filing_xbrl(xml, {**row, "symbol": symbol})
        path.write_text(json.dumps(asdict(filing)), encoding="utf-8")
        filings.append(filing)
    return filings


# ---- Prices & corporate actions ------------------------------------------------------


def fetch_price_history(symbol: str, days: int = 366, today: date | None = None) -> list[tuple[date, float]]:
    """Daily EQ-series closes, oldest first, unadjusted (as traded)."""
    end = today or date.today()
    start = end - timedelta(days=days)
    closes: dict[date, float] = {}
    chunk_end = end
    while chunk_end > start:
        chunk_start = max(start, chunk_end - timedelta(days=HISTORY_CHUNK_DAYS))
        url = HISTORY_URL.format(symbol=symbol, start=chunk_start.strftime("%d-%m-%Y"), end=chunk_end.strftime("%d-%m-%Y"))
        for row in _get(url, BASE + f"/get-quotes/equity?symbol={symbol}").json().get("data", []):
            if row.get("CH_SERIES") != "EQ" or row.get("CH_CLOSING_PRICE") is None:
                continue
            closes[datetime.strptime(row["mTIMESTAMP"], "%d-%b-%Y").date()] = float(row["CH_CLOSING_PRICE"])
        chunk_end = chunk_start - timedelta(days=1)
    return sorted(closes.items())


@dataclass
class CorporateAction:
    ex_date: date
    subject: str
    split_factor: float | None = None   # shares multiply by this on ex_date
    dividend: float | None = None       # rupees per share


_RUPEES = re.compile(r"\bR[se]\.?\s*([\d]+(?:\.\d+)?)", re.IGNORECASE)


def parse_action(ex_date: date, subject: str) -> CorporateAction:
    action = CorporateAction(ex_date=ex_date, subject=subject)
    bonus = re.search(r"bonus\s*(\d+)\s*:\s*(\d+)", subject, re.IGNORECASE)
    split = re.search(r"split.*?from\s*r[se]\.?\s*([\d.]+).*?to\s*r[se]\.?\s*([\d.]+)", subject, re.IGNORECASE)
    if bonus:  # "Bonus a:b" = a new shares for every b held
        a, b = float(bonus.group(1)), float(bonus.group(2))
        action.split_factor = (a + b) / b
    elif split:
        old, new = float(split.group(1)), float(split.group(2))
        if new:
            action.split_factor = old / new
    elif "dividend" in subject.lower():
        action.dividend = sum(float(v) for v in _RUPEES.findall(subject)) or None
    return action


def fetch_corporate_actions(symbol: str) -> list[CorporateAction]:
    rows = _get(ACTIONS_URL.format(symbol=symbol), BASE + f"/get-quotes/equity?symbol={symbol}").json()
    actions = []
    for row in rows if isinstance(rows, list) else []:
        try:
            ex = datetime.strptime(row["exDate"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        actions.append(parse_action(ex, row.get("subject") or ""))
    return actions


def fetch_bhavcopy_closes(day: date) -> dict[str, float]:
    """NSE's official end-of-day file for one trading day: EQ closes by symbol."""
    resp = _get(BHAVCOPY_URL.format(day=day.strftime("%Y%m%d")))
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
            return {r["TckrSymb"]: float(r["ClsPric"]) for r in reader if r.get("SctySrs") == "EQ" and r.get("ClsPric")}
