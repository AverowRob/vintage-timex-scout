"""Tests for the keyword pre-rank (FR-4) and the learning loop (E7)."""

from timex_scout.models import Listing, RawCondition
from timex_scout.prerank import prerank, score_listing
from timex_scout.profile import TasteProfile


def _l(id_, title, price=30.0):
    return Listing(source="ebay", id=id_, url="u", title=title, price=price,
                   currency="CAD", raw_condition=RawCondition(label="Pre-Owned"))


def test_score_rewards_taste_keywords():
    p = TasteProfile.seed()
    marlin, _ = score_listing(_l("1", "Vintage Timex Marlin 1970s hand-wind"), p)
    generic, _ = score_listing(_l("2", "Timex Ironman digital lot of 3"), p)
    assert marlin > generic


def test_prerank_orders_and_pools():
    p = TasteProfile.seed()
    listings = [
        _l("a", "Timex Ironman digital sports watch", price=10),
        _l("b", "Vintage Timex Marlin 1972 mechanical hand-wind NOS", price=45),
        _l("c", "Timex Camper military field watch 1980s", price=28),
    ]
    pool, rest = prerank(listings, p, pool_size=2)
    assert pool[0].id in {"b", "c"}            # a strong match leads
    assert pool[0].prerank_score >= pool[1].prerank_score
    assert len(pool) == 2 and len(rest) == 1


def test_tie_break_is_cheaper_first():
    p = TasteProfile.seed()
    a = _l("a", "Vintage Timex Marlin mechanical", price=45)
    b = _l("b", "Vintage Timex Marlin mechanical", price=20)
    pool, _ = prerank([a, b], p, pool_size=2)
    assert pool[0].id == "b"  # same score → cheaper first


def test_learning_loop_reorders():
    p = TasteProfile.seed()
    listings = [
        _l("a", "Timex Sprite orange funky dial 1975"),
        _l("b", "Timex Dynabeat electric gold 1971"),
    ]
    # Before: neither 'sprite' nor 'dynabeat' dominate; like the Dynabeat.
    p.learn_from_title("Timex Dynabeat electric gold 1971")
    pool, _ = prerank(listings, p, pool_size=2)
    assert pool[0].id == "b"  # the liked piece's keywords now rank it first
