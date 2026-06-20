"""Tests for the Etsy source adapter — graceful degradation and config."""

from timex_scout.config import EtsyConfig
from timex_scout.sources.etsy import EtsySource, _etsy_price


def test_no_key_returns_empty_and_off():
    s = EtsySource(EtsyConfig(keystring=None, shared_secret=None))
    assert s.fetch() == []          # never raises, never blocks a pull (NFR-3)
    assert s.last_mode == "off"     # red dot
    assert s.configured is False


def test_with_key_is_configured():
    # Keystring present -> "wired" (yellow), even before approval. Don't hit the
    # network here; just assert it's recognized as configured.
    s = EtsySource(EtsyConfig(keystring="abc123", shared_secret="secret"))
    assert s.configured is True
    assert s.name == "etsy" and s.display == "Etsy"


def test_price_conversion():
    # Etsy prices are {amount, divisor, currency_code}.
    assert _etsy_price({"amount": 4250, "divisor": 100, "currency_code": "USD"}) == 42.5
    assert _etsy_price({"amount": 0, "divisor": 0}) is None
