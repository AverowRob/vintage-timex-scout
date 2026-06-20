"""Tests for the eBay search-URL builder (source-side volume control)."""

from timex_scout.sources.ebay_search import (
    DEFAULT_BROAD_PAGES,
    DEFAULT_QUERIES,
    browse_filter,
    build_search_url,
    default_capture_plan,
)


def test_browse_filter_matches_box0():
    # The live Browse API filter must encode the same box-0 rules as the capture.
    f = browse_filter()
    assert "price:[..50],priceCurrency:CAD" in f      # item price cap
    assert "conditionIds:{3000|1000|1500}" in f       # used/new...
    assert "7000" not in f                             # ...never For-Parts


def test_browse_filter_can_disable_parts_exclusion():
    assert "conditionIds" not in browse_filter(exclude_parts=False)


def test_query_plan_includes_taste_aligned_queries():
    # Both the capture and the live API pull the ground-truth styles.
    assert "timex easy reader" in DEFAULT_QUERIES
    assert "timex la cell" in DEFAULT_QUERIES


def test_default_url_applies_all_source_side_filters():
    url = build_search_url("timex marlin")
    assert "_nkw=timex+marlin" in url
    assert "_sacat=31387" in url           # Wristwatches category
    assert "_udhi=50" in url               # budget gate at the source
    assert "LH_ItemCondition=3000" in url  # used/new...
    assert "7000" not in url               # ...but never For-Parts
    assert "_pgn=1" in url


def test_pagination_and_price_cap_are_parameterized():
    url = build_search_url("timex", page=3, max_price=75)
    assert "_pgn=3" in url
    assert "_udhi=75" in url


def test_exclude_parts_can_be_disabled():
    url = build_search_url("timex", exclude_parts=False)
    assert "LH_ItemCondition" not in url


def test_capture_plan_paginates_broad_query_and_adds_model_lines():
    plan = default_capture_plan()
    # broad query paginated N deep + one page each for the remaining model lines
    assert len(plan) == DEFAULT_BROAD_PAGES + (len(DEFAULT_QUERIES) - 1)
    assert any("_pgn=%d" % DEFAULT_BROAD_PAGES in u for u in plan)
    assert any("timex+camper" in u for u in plan)
