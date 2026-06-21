"""Tests for the deterministic gate (FR-3)."""

from timex_scout.config import EbayConfig
from timex_scout.gate import apply_gate, evaluate, is_broken
from timex_scout.models import Listing, RawCondition
from timex_scout.sources.ebay import EbaySource

# No-credentials config pins the source to the offline fixture, so the gate test
# never hits the network — even when another test's import of the web app has run
# load_dotenv() and put real API keys into the environment.
_OFFLINE = EbayConfig(client_id=None, client_secret=None)


def _listing(title="Timex Marlin 1972", price=30.0, label="Pre-Owned", cond_id="3000", desc=None):
    return Listing(
        source="ebay", id="1", url="u", title=title, price=price, currency="CAD",
        raw_condition=RawCondition(label=label, condition_id=cond_id, description=desc),
    )


def test_landed_cost_over_cap_is_dropped():
    """Budget is TOTAL: item + shipping > $50 fails even when the item alone is under."""
    l = _listing(price=45.0)
    l.shipping_cost = 10.0                         # $55 landed
    r = evaluate(l)
    assert r.passed is False and r.price_ok is False
    assert "total" in r.reason and "55.00" in r.reason


def test_landed_cost_under_cap_passes():
    l = _listing(price=40.0)
    l.shipping_cost = 8.0                           # $48 landed
    assert evaluate(l).passed is True
    assert l.landed_cost == 48.0


def test_free_shipping_at_cap_passes():
    l = _listing(price=50.0)
    l.shipping_cost = 0.0                           # exactly $50, free shipping
    assert evaluate(l).passed is True


def test_unknown_shipping_gates_on_item_price():
    """No shipping quote → treat as 0 (optimistic), flag via shipping_known=False."""
    l = _listing(price=45.0)                        # shipping_cost None
    assert l.shipping_known is False
    assert l.landed_cost == 45.0
    assert evaluate(l).passed is True


def test_non_timex_is_dropped():
    from timex_scout.gate import is_timex
    seiko = _listing(title="Seiko 4N01 Mickey Mouse Disney Womens Watch Gold")
    assert is_timex(seiko) is False
    assert evaluate(seiko).passed is False        # brand filter (D23)
    assert is_timex(_listing(title="Vintage Timex Marlin")) is True


def test_over_budget_is_dropped():
    assert evaluate(_listing(price=51.0)).passed is False
    assert evaluate(_listing(price=50.0)).passed is True  # cap is inclusive


def test_structured_for_parts_is_broken():
    broken, signal = is_broken(_listing(label="Parts Only", cond_id="7000"))
    assert broken and "for parts" in signal.lower()


def test_text_disclosed_broken_is_caught_even_when_label_used():
    # The key case box 0 can't catch: "Pre-Owned" label, broken text.
    broken, _ = is_broken(_listing(title="Vintage Timex Mens Watch Face Not Working"))
    assert broken
    broken2, _ = is_broken(_listing(title="Timex Marlin FOR PARTS OR REPAIR"))
    assert broken2


def test_texting_shorthand_and_slash_broken_signals():
    # Real misses the user caught: "Runs 4 Repair" and "run/stop".
    assert is_broken(_listing(title="Vintage 1971 Timex Marlin Manual Watch Gold Tone Runs 4 Repair"))[0]
    assert is_broken(_listing(title="Timex Viscount Calendar Automatic run/stop"))[0]
    assert is_broken(_listing(title="Timex Marlin project watch for restoration"))[0]


def test_dead_battery_is_not_broken():
    broken, _ = is_broken(_listing(title="Timex Camper - needs battery, new battery"))
    assert broken is False


def test_as_is_alone_is_not_broken():
    # NOS Snoopy "As Is" was a working collectible — bare "as is" must not gate it out.
    broken, _ = is_broken(_listing(title="NOS Timex Snoopy Watch Working - As Is - Boxed"))
    assert broken is False


def test_evaluate_sets_working_status():
    l = _listing(title="Timex not running")
    evaluate(l)
    assert l.working_status == "broken" and l.disclosed_damage


def test_apply_gate_on_real_fixture():
    listings = EbaySource(_OFFLINE).fetch(limit=1000)
    survivors, dropped = apply_gate(listings)
    # Real capture: source-side filter removed structured parts, but text-disclosed
    # broken listings remain and must be dropped here.
    assert len(survivors) > 0 and len(dropped) > 0
    assert all(s.working_status != "broken" for s in survivors)
    assert all((s.price or 0) <= 50 for s in survivors)
