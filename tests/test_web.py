"""Tests for the web layer's filter/sort logic (E4)."""

from timex_scout.models import Listing, RawCondition
from timex_scout.web.app import Filters, _apply_filters


def _l(id_, price, score):
    l = Listing(source="ebay", id=id_, url="u", title="t", price=price,
                currency="CAD", raw_condition=RawCondition(label="Pre-Owned"))
    l.interest_score = score
    return l


_SET = [_l("a", 10.0, 50), _l("b", 45.0, 95), _l("c", 25.0, 70), _l("d", 5.0, 30)]


def test_price_max_filter():
    out = _apply_filters(_SET, Filters(price_max=20))
    assert {l.id for l in out} == {"a", "d"}


def test_min_score_filter():
    out = _apply_filters(_SET, Filters(min_score=70))
    assert {l.id for l in out} == {"b", "c"}


def test_sort_interest_default():
    out = _apply_filters(_SET, Filters())
    assert [l.id for l in out] == ["b", "c", "a", "d"]  # by score desc


def test_sort_price_ascending():
    out = _apply_filters(_SET, Filters(sort="price_asc"))
    assert [l.id for l in out] == ["d", "a", "c", "b"]


def test_query_without_page_roundtrip():
    f = Filters(price_min=10, price_max=50, min_score=60, sort="price_desc")
    q = f.query_without_page()
    assert "price_min=10" in q and "price_max=50" in q
    assert "min_score=60" in q and "sort=price_desc" in q
    assert "page" not in q
