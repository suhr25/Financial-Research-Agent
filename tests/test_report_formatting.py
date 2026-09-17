"""Report-facing formatting of metrics and values.

Two things this guards, both of which made the Financials section look
broken in the UI:
  - raw XBRL concept tags leaking into the report as metric names
  - raw magnitudes (466822988000) shown instead of readable figures
"""
from app.analysis.normalizer import ClaimNormalizer
from app.generation.report_generator import _is_financial_metric, humanize_metric, humanize_value
from app.schemas import Basis, Claim, ClaimType, Evidence


def _claim(metric: str, value: str, unit: str | None, period: str | None = "Q3 2024") -> Claim:
    text = "evidence"
    c = Claim(
        research_run_id="r", claim_type=ClaimType.NUMERIC, entity="Apple Inc.", metric=metric,
        value=value, unit=unit, period=period, basis=Basis.UNKNOWN, source_id="s",
        evidence_span=Evidence(source_id="s", start_char=0, end_char=len(text), evidence_text=text),
        statement="stmt",
    )
    ClaimNormalizer().normalize([c])
    return c


def test_raw_xbrl_tag_is_humanized():
    assert humanize_metric("revenuefromcontractwithcustomerexcludingassessedtax") == "Revenue"
    assert humanize_metric("netincomeloss") == "Net income"


def test_known_metric_keys_get_report_labels():
    assert humanize_metric("operating_margin") == "Operating margin"
    assert humanize_metric("eps_diluted") == "Diluted EPS"


def test_unknown_metric_falls_back_to_readable_text():
    assert humanize_metric("some_new_metric") == "Some new metric"


def test_large_currency_values_are_abbreviated():
    assert humanize_value(_claim("revenue", "466822988000", "USD")) == "$466.82B"
    assert humanize_value(_claim("ebitda", "194237006000", "USD")) == "$194.24B"


def test_ratio_and_percent_both_render_as_percent():
    assert humanize_value(_claim("operating_margin", "0.326", None)) == "32.6%"
    assert humanize_value(_claim("average_net_profit_margin", "35.86", "%")) == "35.9%"


def test_small_currency_value_is_not_abbreviated():
    assert humanize_value(_claim("eps_diluted", "5.11", "USD")) == "$5.11"


def test_financial_metric_match_tolerates_source_specific_names():
    """Alpha Vantage, SEC XBRL and the mock extractor each name the same
    metric differently; all must reach the Financial Performance section."""
    for metric in ("revenue", "revenue_ttm", "revenuefromcontractwithcustomerexcludingassessedtax",
                   "annual_revenue", "operating_margin_ttm", "ebitda", "eps_diluted"):
        assert _is_financial_metric(metric), metric


def test_non_financial_metric_is_excluded():
    assert not _is_financial_metric("risk_factor")
