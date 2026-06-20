"""Tests for the eBay source adapter — normalization and graceful degradation.

These run fully offline against the bundled fixture, which is a real ebay.ca
snapshot captured through the browser while developer-portal access was pending
(see docs/data-capture.md): Wristwatches category, item price <= C$50, parts
excluded at the source, across several taste-relevant queries. Assertions are
structural so they survive a re-capture.
"""

from timex_scout.config import EbayConfig
from timex_scout.sources.ebay import EbaySource


def _source() -> EbaySource:
    # No credentials -> fetch() uses the offline fixture (NFR-3).
    return EbaySource(EbayConfig(client_id=None, client_secret=None))


def test_fetch_without_credentials_uses_fixture():
    listings = _source().fetch("timex", limit=1000)
    # A real multi-query capture; bounded but substantial.
    assert len(listings) >= 200
    assert all(l.source == "ebay" for l in listings)


def test_every_listing_has_core_fields():
    for l in _source().fetch(limit=1000):
        assert l.id.isdigit()
        assert l.url.startswith("https://www.ebay.ca/itm/")
        assert l.title
        assert isinstance(l.price, float) and l.price > 0
        assert l.currency == "CAD"
        assert l.images and l.images[0].startswith("https://i.ebayimg.com/")


def test_model_lines_present_for_ranking():
    # The taste-relevant queries guarantee strong matches in the pool, so the
    # ranking stages have real contenders to surface.
    titles = " ".join(l.title.lower() for l in _source().fetch(limit=1000))
    for model in ("marlin", "camper", "viscount"):
        assert model in titles


def test_source_side_filter_removed_structured_parts():
    # Box 0 (eBay condition filter) excludes For-Parts (conditionId 7000) before
    # ingest. The gate's *keyword* layer still handles text-disclosed breakage.
    listings = _source().fetch(limit=1000)
    assert all(l.raw_condition.condition_id != "7000" for l in listings)


def test_fallback_disabled_returns_empty():
    src = EbaySource(
        EbayConfig(client_id=None, client_secret=None),
        fallback_to_fixture=False,
    )
    assert src.fetch() == []
