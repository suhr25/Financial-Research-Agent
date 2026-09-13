"""A live run must never substitute mock data for a failed provider.

The mock fixtures carry generic illustrative figures (Apple-shaped
numbers). Falling back to one after a live provider failure put those
figures into a *different* company's research run under a real-looking
company-named title - e.g. "$85.8B revenue" attributed to Microsoft.
Missing evidence is the honest outcome; fabricated evidence is not.
"""
import pytest

from app.config import get_settings
from app.retrieval.base import mock_fallback_allowed
from app.retrieval.financial_data import YFinanceProvider
from app.retrieval.sec_edgar import SECEdgarProvider
from app.schemas import CompanyEntity


@pytest.fixture
def live_mode(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_mock_fallback_allowed_in_demo_mode():
    get_settings.cache_clear()
    assert mock_fallback_allowed("anything", "any reason") is True


def test_mock_fallback_refused_in_live_mode(live_mode):
    assert mock_fallback_allowed("yfinance", "simulated failure") is False


def test_company_without_cik_yields_no_sec_source_in_live_mode(live_mode):
    """A non-SEC-registrant is a genuine absence of filing evidence."""
    foreign = CompanyEntity(name="Reliance Industries Limited", ticker="RELIANCE", cik=None, resolved=True)
    assert SECEdgarProvider().fetch(foreign, "FY2024") == []


def test_company_without_ticker_yields_no_financial_source_in_live_mode(live_mode):
    unresolved = CompanyEntity(name="Unknown Co", ticker=None, cik=None, resolved=False)
    assert YFinanceProvider().fetch(unresolved, "Q3 2024") == []


def test_demo_mode_still_returns_labelled_mock_sources():
    """DEMO_MODE is the one place synthetic sources are correct - and they
    must stay explicitly labelled."""
    get_settings.cache_clear()
    sources = SECEdgarProvider().fetch(CompanyEntity(name="Apple Inc.", ticker="AAPL", cik=None), "Q3 2024")
    assert len(sources) == 1
    assert sources[0].source_tier.value == "mock"
    assert "MOCK" in sources[0].document_text[:80].upper()
