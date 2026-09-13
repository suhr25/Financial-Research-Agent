"""Period-relevance selection for SEC EDGAR companyfacts.

The companyfacts payload spans many years of every concept. Emitting it
all and letting the extractor truncate meant the requested quarter could
be cut off entirely, leaving only unrelated annual figures in evidence -
so a "Q3 2024" query produced FY2024/FY2023 claims.
"""
from app.retrieval.sec_edgar import _matches_period, _parse_requested_period


def test_parses_quarterly_period():
    assert _parse_requested_period("Q3 2024") == ("Q3", "2024")


def test_parses_annual_period():
    assert _parse_requested_period("FY2024") == ("FY", "2024")
    assert _parse_requested_period("FY 2024") == ("FY", "2024")


def test_parses_bare_year_as_year_only():
    assert _parse_requested_period("2024") == (None, "2024")


def test_no_period_requested():
    assert _parse_requested_period(None) == (None, None)
    assert _parse_requested_period("") == (None, None)


def test_matches_exact_fiscal_period():
    assert _matches_period({"fp": "Q3", "fy": 2024}, ("Q3", "2024")) is True


def test_rejects_wrong_quarter_and_wrong_year():
    assert _matches_period({"fp": "Q2", "fy": 2024}, ("Q3", "2024")) is False
    assert _matches_period({"fp": "Q3", "fy": 2023}, ("Q3", "2024")) is False


def test_annual_entry_does_not_satisfy_a_quarterly_request():
    """The exact bug this guards: an FY row being treated as good evidence
    for a Q3 question."""
    assert _matches_period({"fp": "FY", "fy": 2024}, ("Q3", "2024")) is False


def test_nothing_matches_when_no_period_was_requested():
    assert _matches_period({"fp": "Q3", "fy": 2024}, (None, None)) is False
