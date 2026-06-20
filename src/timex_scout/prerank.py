"""The cheap keyword pre-rank (FR-4, README §7 box 4).

Scores each gated survivor by how well its title + condition text matches the
taste profile, then orders them. Plain text matching: fast, free, transparent
("matched: marlin, 1970s"). It keeps a bounded top pool for the LLM judge and
caps LLM cost; nothing is deleted — a low score just ranks lower and stays in
"view all" (D16).
"""

from __future__ import annotations

from .models import Listing
from .profile import TasteProfile

# How many survivors reach the LLM judge. Bounded to cap cost (NFR-2); the rest
# remain browsable. Open for tuning once real per-run LLM cost is measured (D27).
DEFAULT_POOL_SIZE = 12


def score_listing(listing: Listing, profile: TasteProfile) -> tuple[float, list[str]]:
    """Return (score, matched positive keywords) for one listing."""
    text = listing.search_text()
    score = 0.0
    matches: list[str] = []
    for keyword, weight in profile.weights.items():
        if keyword in text:
            score += weight
            if weight > 0:
                matches.append(keyword)
    return score, matches


def prerank(
    listings: list[Listing], profile: TasteProfile, pool_size: int = DEFAULT_POOL_SIZE
) -> tuple[list[Listing], list[Listing]]:
    """Score + order all survivors; split into (top pool, the rest).

    Order: by pre-rank score desc, tie-break cheaper-first (D27 / box 6).
    Annotates each listing's prerank_score / prerank_matches for transparency.
    """
    for listing in listings:
        score, matches = score_listing(listing, profile)
        listing.prerank_score = score
        listing.prerank_matches = matches

    ordered = sorted(
        listings,
        key=lambda l: (-(l.prerank_score or 0.0), l.price if l.price is not None else 1e9),
    )
    return ordered[:pool_size], ordered[pool_size:]
