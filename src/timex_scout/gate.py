"""The deterministic hard gate (FR-3, README §7 box 3).

Two pass/fail checks, no model judgment:
  1. item price at or under the cap (PRICE_CAP_CAD)
  2. not explicitly broken

"Not broken" is a deterministic yes/no over the structured condition field plus
broken keywords in the title/description. A dead battery is fine (D14/FR-3) — a
"needs battery" listing is not broken. This is the funnel's *second* noise cut:
source-side filtering (box 0) already dropped structured For-Parts at the query,
but ~28 captured "used" listings still disclose breakage in their *text*
("FOR PARTS OR REPAIR", "not working") — those are the gate's job.

Only the gate removes listings. The pre-rank that follows merely orders the
survivors (D16, miss-aversion), so a keyword miss can never bury a watch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import PRICE_CAP_CAD
from .models import Listing

# Explicit broken signals. Phrase-anchored to avoid false positives — note we do
# NOT include bare "as is" (often used on working watches) or "battery" (a dead
# battery is fine). "stop"/"stops" is only broken in the running-then-stops sense.
_BROKEN_PATTERNS = [
    # for parts / repair (incl. the texting shorthand "4 parts", "4 repair")
    r"for parts", r"4\s*parts", r"parts\s*(only|/|&|or)?\s*repair", r"parts or repair",
    r"spares?\s*(or|/|&)?\s*repair", r"for repair", r"4\s*repair", r"needs repair",
    r"needs work", r"project watch", r"restoration",
    # not working / running
    r"not working", r"non[- ]?working", r"doesn'?t work", r"does not work",
    r"stop(s|ped)?\s*working", r"quit working", r"not running", r"does not run",
    r"doesn'?t run", r"won'?t run", r"wont run", r"won'?t wind", r"doesn'?t wind",
    r"movement seized", r"\bseized\b", r"\bbroken\b", r"not functional",
    # runs-then-stops, in any separator form incl. "run/stop"
    r"(tick|run|start|wind)s?\s*(then|and|&|,|/)\s*stop",
    r"runs?\s*(but|then)?\s*stop", r"run\s*4\s*repair",
    # as-is when paired with a broken signal
    r"as[- ]is.*(repair|parts|broken|not\s*work|stop)",
]
_BROKEN_RE = re.compile("|".join(_BROKEN_PATTERNS), re.IGNORECASE)

# eBay conditionId for "For parts or not working".
_FOR_PARTS_CONDITION_ID = "7000"


@dataclass
class GateResult:
    """Why a listing passed or failed — checkable, per the success criteria."""

    passed: bool
    price_ok: bool
    not_broken: bool
    reason: str


def is_broken(listing: Listing) -> tuple[bool, str | None]:
    """Return (broken?, matched_signal). Structured field first, then keywords."""
    if listing.raw_condition.condition_id == _FOR_PARTS_CONDITION_ID:
        return True, "condition: for parts or not working"
    label = (listing.raw_condition.label or "").lower()
    if "parts" in label or "not working" in label:
        return True, f"condition: {listing.raw_condition.label}"
    match = _BROKEN_RE.search(listing.search_text())
    if match:
        return True, f"text: '{match.group(0)}'"
    return False, None


def price_ok(listing: Listing, cap: float = PRICE_CAP_CAD) -> bool:
    """TOTAL landed cost (item price + shipping to the ship-to location) at or under
    the cap — the brief's budget basis (FR-2). Shipping unknown counts as 0."""
    landed = listing.landed_cost
    return landed is not None and landed <= cap


def is_timex(listing: Listing) -> bool:
    """Brand filter: the product is Timex-only (D23). eBay's fuzzy search leaks
    non-Timex (a Seiko/Lorus Mickey-Mouse watch on a 'mickey' query), so require
    the brand to actually appear in the title/condition text."""
    return "timex" in listing.title.lower() or "timex" in listing.raw_condition.text()


def evaluate(listing: Listing, cap: float = PRICE_CAP_CAD) -> GateResult:
    """Run the gate checks (brand, price, not-broken) and annotate condition."""
    timex = is_timex(listing)
    ok_price = price_ok(listing, cap)
    broken, signal = is_broken(listing)
    not_broken = not broken

    # Honest condition surfacing (NFR-5): broken vs unknown (never claim working).
    listing.working_status = "broken" if broken else "unknown"
    if broken and signal:
        listing.disclosed_damage = signal

    if not timex:
        reason = "not a Timex (brand filter)"
    elif not ok_price:
        landed = listing.landed_cost
        if landed is None:
            reason = f"over budget (no price > ${cap:.0f})"
        else:
            ship = f" incl. ${listing.shipping_cost:.2f} ship" if listing.shipping_known else ""
            reason = f"over budget (${landed:.2f} total{ship} > ${cap:.0f})"
    elif broken:
        reason = f"broken — {signal}"
    else:
        reason = "passed"
    return GateResult(
        passed=timex and ok_price and not_broken,
        price_ok=ok_price,
        not_broken=not_broken,
        reason=reason,
    )


def apply_gate(
    listings: list[Listing], cap: float = PRICE_CAP_CAD
) -> tuple[list[Listing], list[tuple[Listing, GateResult]]]:
    """Split listings into (survivors, dropped-with-reason)."""
    survivors: list[Listing] = []
    dropped: list[tuple[Listing, GateResult]] = []
    for listing in listings:
        result = evaluate(listing, cap)
        if result.passed:
            survivors.append(listing)
        else:
            dropped.append((listing, result))
    return survivors, dropped
