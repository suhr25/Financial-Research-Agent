"""Industry dashboard: NSE filing parsing, deterministic analytics and
consistency checks, caching and the API surface. Everything runs offline -
analytics take synthetic filings/prices, and the API serves the bundled
demo fixture."""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.industry import analytics
from app.industry.nse import CorporateAction, Filing, parse_action, parse_filing_xbrl
from app.industry.universe import find_universe_company, find_universe_mentions, get_industry, list_industries
from app.retrieval.company_resolver import CompanyResolver
from app.schemas.industry import CompanyMetrics, IndustryCompanyRef

REF = IndustryCompanyRef(name="Example Tech", short_name="ExT", symbol="EXT.NS", nse="EXT", tier="Large cap")
CR = 1e7  # one crore


def _filing(end: str, revenue: float, ni: float, **kw) -> Filing:
    base = dict(
        symbol="EXT", period_start="", period_end=end, filed_at="", audited="Audited", revision="Original",
        url=f"https://nsearchives.nseindia.com/corporate/xbrl/{end}.xml", revenue=revenue * CR, net_income=ni * CR,
        profit_before_exceptional_and_tax=ni * 1.3 * CR, finance_costs=1 * CR, other_income=2 * CR,
        eps_basic=ni * CR / (100 * CR), eps_diluted=ni * CR / (100 * CR), paid_up_capital=100 * CR, face_value=1.0,
    )
    base.update(kw)
    return Filing(**base)


# Six quarters, newest first, as fetch_filings returns them. FY26 (Apr-25..Mar-26)
# quarters sum to 100+110+115+120 = 445 revenue, 44.5 profit.
FILINGS = [
    _filing("2026-06-30", 130, 13.0),
    _filing("2026-03-31", 120, 12.0, annual_revenue=445 * CR, annual_net_income=44.5 * CR),
    _filing("2025-12-31", 115, 11.5),
    _filing("2025-09-30", 110, 11.0),
    _filing("2025-06-30", 100, 10.0),
    _filing("2025-03-31", 95, 9.5, annual_revenue=380 * CR, annual_net_income=38.0 * CR),
]
TODAY = date(2026, 9, 29)
CLOSES = [(TODAY - timedelta(days=i), 200.0) for i in range(300, -1, -1)]


def _build(filings=FILINGS, closes=CLOSES, actions=(), bhav=200.0) -> CompanyMetrics:
    return analytics.build_company_metrics(REF, list(filings), list(closes), list(actions), bhav)


def test_ttm_and_ratios_come_from_four_consecutive_filings():
    m = _build()
    assert m.revenue_ttm == pytest.approx((130 + 120 + 115 + 110) * CR)
    assert m.net_income_ttm == pytest.approx((13 + 12 + 11.5 + 11) * CR)
    assert m.profit_margin == pytest.approx(0.1)
    assert m.shares_outstanding == pytest.approx(100 * CR)
    assert m.market_cap == pytest.approx(200.0 * 100 * CR)
    assert m.pe_trailing == pytest.approx(200.0 / ((13 + 12 + 11.5 + 11) / 100))
    assert m.revenue_growth_yoy == pytest.approx(0.30)  # Jun-26 vs Jun-25
    assert m.verification_status == "verified"
    assert [q.filing_url for q in m.quarters][-1].endswith("2026-06-30.xml")


def test_missing_quarter_means_no_ttm_rather_than_a_shifted_window():
    gappy = [f for f in FILINGS if f.period_end != "2025-12-31"]
    m = _build(gappy)
    assert m.revenue_ttm is None and m.pe_trailing is None


def test_quarterly_filings_that_do_not_add_up_to_the_annual_report_are_flagged():
    bad = [FILINGS[0], _filing("2026-03-31", 120, 12.0, annual_revenue=500 * CR, annual_net_income=44.5 * CR), *FILINGS[2:]]
    m = _build(bad)
    check = next(c for c in m.checks if c.metric == "revenue_ttm")
    assert check.status == "mismatch" and m.verification_status == "mismatch"


def test_price_is_cross_checked_against_bhavcopy():
    assert next(c for c in _build(bhav=200.0).checks if c.metric == "price").status == "verified"
    assert next(c for c in _build(bhav=180.0).checks if c.metric == "price").status == "mismatch"
    assert next(c for c in _build(bhav=None).checks if c.metric == "price").status == "unavailable"


def test_eps_check_allows_a_mid_quarter_buyback():
    """Basic EPS uses the weighted-average share count, which sits between
    the quarter-start and quarter-end counts (measured: Wipro's June 2026
    buyback put period-end shares 5.5% below the EPS-implied count)."""
    latest = _filing("2026-06-30", 130, 13.0, paid_up_capital=95 * CR, eps_basic=13.0 * CR / (97.5 * CR))
    m = _build([latest, *FILINGS[1:]])
    assert next(c for c in m.checks if c.metric == "eps").status == "verified"


def test_bonus_issue_adjusts_prices_shares_and_eps():
    ex = TODAY - timedelta(days=60)  # 31 Jul 2026, after the Jun-26 quarter end
    bonus = CorporateAction(ex_date=ex, subject="Bonus 1:1", split_factor=2.0)
    closes = [(d, 200.0 if d < ex else 100.0) for d, _ in CLOSES]
    m = _build(closes=closes, actions=[bonus], bhav=100.0)
    series = analytics.adjusted_closes(closes, [bonus])
    assert series.iloc[0] == pytest.approx(100.0)  # no fake 50% crash
    # Latest filing (Jun-26) predates the ex-date, so its share count doubles.
    assert m.shares_outstanding == pytest.approx(200 * CR)
    assert m.market_cap == pytest.approx(100.0 * 200 * CR)


def test_dividend_yield_uses_dividends_with_ex_date_in_last_year():
    actions = [
        CorporateAction(ex_date=TODAY - timedelta(days=30), subject="Interim Dividend - Rs 6 Per Share", dividend=6.0),
        CorporateAction(ex_date=TODAY - timedelta(days=400), subject="Dividend - Rs 50 Per Share", dividend=50.0),
    ]
    m = _build(actions=actions)
    assert m.dividends_ttm == pytest.approx(6.0)
    assert m.dividend_yield == pytest.approx(6.0 / 200.0)


def test_corporate_action_parsing():
    d = date(2026, 1, 1)
    assert parse_action(d, "Bonus 1:1").split_factor == 2.0
    assert parse_action(d, "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share").split_factor == 5.0
    assert parse_action(d, "Interim Dividend Rs 11 Per Share/ Special Dividend Rs 46 Per Share").dividend == 57.0
    assert parse_action(d, "Interim Dividend - Re 1 Per Share").dividend == 1.0
    assert parse_action(d, "Buy Back").split_factor is None


def _xbrl(facts: list[tuple[str, str, str]], contexts: dict[str, tuple[str, str]], dims: set[str] = frozenset()) -> str:
    ctx = "".join(
        f'<xbrli:context id="{cid}"><xbrli:entity/>'
        + ('<xbrli:segment><xbrldi:explicitMember dimension="x">y</xbrldi:explicitMember></xbrli:segment>' if cid in dims else "")
        + f"<xbrli:period><xbrli:startDate>{s}</xbrli:startDate><xbrli:endDate>{e}</xbrli:endDate></xbrli:period></xbrli:context>"
        for cid, (s, e) in contexts.items()
    )
    body = "".join(f'<in-bse-fin:{tag} contextRef="{cid}" unitRef="INR" decimals="-7">{v}</in-bse-fin:{tag}>' for tag, cid, v in facts)
    return f"<xbrli:xbrl>{ctx}{body}</xbrli:xbrl>"


META = {"symbol": "EXT", "qe_Date": "31-MAR-2026", "xbrl": "https://x/f.xml", "broadcast_Date": "09-Apr-2026 23:42:16", "audited": "Audited"}
CTX = {"Q": ("2026-01-01", "2026-03-31"), "FY": ("2025-04-01", "2026-03-31"), "SEG": ("2026-01-01", "2026-03-31")}


def test_xbrl_picks_quarter_and_full_year_by_context_dates_and_ignores_segments():
    xml = _xbrl([
        ("RevenueFromOperations", "SEG", "1"),
        ("RevenueFromOperations", "FY", "2670210000000"),
        ("RevenueFromOperations", "Q", "706980000000"),
        ("ProfitLossForPeriod", "Q", "134200000000"),
        ("ProfitOrLossAttributableToOwnersOfParent", "Q", "133490000000"),
        ("ProfitOrLossAttributableToNonControllingInterests", "Q", "710000000"),
    ], CTX, dims={"SEG"})
    f = parse_filing_xbrl(xml, META)
    assert f.revenue == 706980000000 and f.annual_revenue == 2670210000000
    assert f.net_income == 133490000000 and f.notes == []


def test_mis_tagged_owners_profit_is_corrected_by_accounting_identity_and_noted():
    """Tech Mahindra's Dec-2025 filing tagged owners' profit as Rs 198.7 Cr
    while its total profit was Rs 1,118.6 Cr and minority interest Rs 5.1 Cr."""
    xml = _xbrl([
        ("RevenueFromOperations", "Q", "143930000000"),
        ("ProfitLossForPeriod", "Q", "11186000000"),
        ("ProfitOrLossAttributableToOwnersOfParent", "Q", "1987000000"),
        ("ProfitOrLossAttributableToNonControllingInterests", "Q", "51000000"),
    ], CTX)
    f = parse_filing_xbrl(xml, META)
    assert f.net_income == pytest.approx(11186000000 - 51000000)
    assert len(f.notes) == 1 and "1,113.5" in f.notes[0]


def test_zero_owners_profit_falls_back_to_total_profit():
    xml = _xbrl([
        ("RevenueFromOperations", "Q", "43030000000"),
        ("ProfitLossForPeriod", "Q", "4830430000"),
        ("ProfitOrLossAttributableToOwnersOfParent", "Q", "0"),
    ], CTX)
    assert parse_filing_xbrl(xml, META).net_income == 4830430000


def test_mis_dated_quarter_is_left_unavailable_not_guessed():
    xml = _xbrl([("RevenueFromOperations", "H", "34099000000")], {"H": ("2025-10-01", "2026-03-31")})
    f = parse_filing_xbrl(xml, META)
    assert f.revenue is None and any("three-month revenue" in n for n in f.notes)


def test_filing_notes_surface_as_informational_checks():
    noted = _filing("2026-06-30", 130, 13.0, notes=["Quarter: corrected."])
    m = _build([noted, *FILINGS[1:]])
    info = [c for c in m.checks if c.informational]
    assert len(info) == 1 and info[0].detail == "Quarter: corrected."
    assert m.verification_status == "verified"
    assert m.quarters[-1].notes == ["Quarter: corrected."]


def _company(symbol: str, cap: float) -> CompanyMetrics:
    return CompanyMetrics(name=symbol, short_name=symbol, symbol=symbol, nse=symbol, tier="x", market_cap=cap)


def test_concentration_hhi_and_effective_count():
    companies = [_company("A", 50), _company("B", 30), _company("C", 20)]
    conc = analytics.compute_concentration(companies)
    assert conc.hhi == pytest.approx(0.25 + 0.09 + 0.04)
    assert conc.effective_companies == pytest.approx(1 / 0.38)
    assert conc.largest == "A" and conc.top3_share == pytest.approx(1.0)
    assert companies[0].market_cap_weight == pytest.approx(0.5)


def test_diversification_reduces_volatility_for_imperfectly_correlated_returns():
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2025-01-01", periods=250)
    common = rng.normal(0, 0.01, len(days))
    prices = {s: 100 * np.cumprod(1 + common + rng.normal(0, 0.01, len(days))) for s in ("A", "B", "C")}
    closes = pd.DataFrame(prices, index=days)
    companies = [_company(s, 1.0) for s in ("A", "B", "C")]
    analytics.apply_price_stats(companies, closes)
    div = analytics.compute_diversification(companies, closes)

    assert div.symbols == ["A", "B", "C"]
    assert 0.2 < div.average_pairwise_correlation < 0.8
    assert div.equal_weight.volatility < min(c.volatility_1y for c in companies)
    assert div.equal_weight.diversification_ratio > 1
    assert div.least_correlated[0].correlation <= div.most_correlated[0].correlation
    assert all(math.isfinite(v) for row in div.covariance for v in row)


def test_short_price_history_is_excluded_from_risk_stats():
    days = pd.bdate_range("2025-01-01", periods=40)
    closes = pd.DataFrame({"A": np.linspace(100, 110, 40), "B": np.linspace(100, 90, 40)}, index=days)
    companies = [_company("A", 1.0), _company("B", 1.0)]
    analytics.apply_price_stats(companies, closes)
    assert companies[0].volatility_1y is None
    assert analytics.compute_diversification(companies, closes).symbols == []


def test_universe_is_nifty_it_and_resolves_before_sec_tickers():
    [it] = list_industries()
    assert it.id == "information-technology" and it.company_count == 10
    assert find_universe_company("TCS").symbol == "TCS.NS"
    assert [c.nse for c in find_universe_mentions("compare hcl technologies with Wipro")] == ["HCLTECH", "WIPRO"]
    entity = CompanyResolver().resolve("TCS")
    assert entity.name == "Tata Consultancy Services" and entity.ticker == "TCS.NS"


def test_find_mentions_does_not_duplicate_universe_companies():
    found = CompanyResolver().find_mentions("Analyze Infosys and TCS")
    assert found == ["Infosys", "Tata Consultancy Services"]


@pytest.fixture()
def client():
    from app.main import app

    c = TestClient(app)
    c.post("/api/auth/demo")
    return c


def test_api_lists_industries(client):
    resp = client.get("/api/industries")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "information-technology"


def test_api_serves_labelled_demo_snapshot_in_demo_mode(client):
    resp = client.get("/api/industries/information-technology")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "demo"
    assert len(body["companies"]) == len(get_industry("information-technology").companies)
    assert body["concentration"]["total_market_cap"] > 0
    assert len(body["diversification"]["correlation"]) == len(body["diversification"]["symbols"])


def test_api_unknown_industry_is_404(client):
    assert client.get("/api/industries/banking").status_code == 404
