"""The pipeline: source -> gate -> score-all -> order (README §7, revised).

One on-demand pull, end to end. Returns everything the UI needs: the surfaced
contenders, the full gated set ("view all"), what was dropped and why, and the
funnel counts.

Revision (measured volume ~478): instead of a keyword pre-rank capping the LLM
to a top-12 pool, the LLM now scores EVERY gated listing against the taste brief
(cheap + fast at this volume — the make-or-break quality lever). "Contenders" are
the listings whose real score clears a threshold, not a fixed count. The keyword
pre-rank survives as (a) the no-LLM fallback and (b) a cost guard that pre-filters
to MAX_LLM_SCORE if a pull ever returns thousands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import PRICE_CAP_CAD
from .gate import GateResult, apply_gate
from .judge import MAX_LLM_SCORE, KeywordJudge, confirm, detail, provider, score_all
from .models import Listing
from .prerank import prerank
from .profile import TasteProfile
from .sources.base import Source
from .taste import TasteBrief

# A listing is a "contender" if its score clears this bar. The value maps to the
# LLM rubric's own top band — the scoring prompt defines 90-100 = "standout"
# (a clear collab / character / advertising dial, deadstock/NOS, or rare model) —
# so "Top contenders" is exactly the standout tier. It also sits above the natural
# gap in the score distribution (on-taste clusters ≤~85, standout ≥90). Nothing is
# hidden: every listing is in "View all", ranked by score, and the min-score filter
# moves the cut. See D36.
CONTENDER_THRESHOLD = int(os.environ.get("CONTENDER_THRESHOLD", "90"))

# Cap on how many candidates the combined re-judge (pass 2) reconciles. The pool
# is everyone scored within CONFIRM_MARGIN of the bar; this bounds the cost of the
# more expensive score+reason call to the listings that could actually surface.
CONFIRM_CAP = int(os.environ.get("CONFIRM_CAP", "80"))


@dataclass
class PullResult:
    contenders: list[Listing]                      # on-taste (score >= threshold)
    rest: list[Listing]                            # gated but below threshold
    dropped: list[tuple[Listing, GateResult]]      # gate failures + reasons
    judge_kind: str                                # which judge actually ran
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def all_gated(self) -> list[Listing]:
        return self.contenders + self.rest


def _by_score_then_price(listing: Listing) -> tuple:
    return (-(listing.interest_score or 0),
            listing.price if listing.price is not None else 1e9)


def rank_survivors(
    survivors: list[Listing],
    dropped: list[tuple[Listing, GateResult]],
    profile: TasteProfile,
    brief: TasteBrief,
    fetched: int,
    *,
    threshold: int = CONTENDER_THRESHOLD,
    detect_broken: bool = True,
) -> PullResult:
    """Score and order already-gated survivors. Used for live re-ranking after a
    like/edit, without re-fetching or re-gating.

    `detect_broken=True` (a fresh fetch) judges the LLM not-broken backstop and freezes
    it on each listing; `detect_broken=False` (a taste Reapply) leaves `working_status`
    alone so the gated count can't drift between fetches — broken-ness is a property of
    the watch, not the brief."""
    # Pre-rank annotates every survivor (keyword score + matches) — used for the
    # tie-break, the "matched:" transparency line, and the cost-guard ordering.
    prerank(survivors, profile, len(survivors))

    # Score everything against the taste brief. If a pull is ever huge, cap the
    # LLM to the top MAX_LLM_SCORE by pre-rank and keyword-score the remainder.
    if provider() != "keyword" and len(survivors) > MAX_LLM_SCORE:
        by_keyword = sorted(survivors, key=lambda l: -(l.prerank_score or 0))
        head, tail = by_keyword[:MAX_LLM_SCORE], by_keyword[MAX_LLM_SCORE:]
        judge_kind = score_all(head, brief.text, detect_broken)
        KeywordJudge().score(tail, None)
    else:
        judge_kind = score_all(survivors, brief.text, detect_broken)

    # The LLM's broken-flag is the gate's robust backstop: anything it marks broken
    # (e.g. "Runs 4 Repair", "run/stop" that the deterministic keywords missed) is
    # removed entirely — a broken watch must never be a contender (FR-3).
    llm_broken = [l for l in survivors if l.working_status == "broken"]
    if llm_broken:
        survivors = [l for l in survivors if l.working_status != "broken"]
        dropped = list(dropped) + [
            (l, GateResult(passed=False, price_ok=True, not_broken=False,
                           reason="broken — LLM-detected")) for l in llm_broken
        ]

    # Pass 2: re-judge the contender CANDIDATE pool — everyone the bulk pass put
    # within reach of the bar (threshold - margin) — with a combined score+reason
    # call. This reconciles the surfaced score with a stated signal, so a generic
    # listing the bulk pass over-scored to 90 gets corrected down and drops out of
    # contenders instead of showing "90 · generic" (the two passes can't disagree
    # on what's surfaced). Bounded to the top CONFIRM_CAP by score to keep it cheap.
    margin = int(os.environ.get("CONFIRM_MARGIN", "20"))
    pool = [l for l in survivors if (l.interest_score or 0) >= threshold - margin]
    pool = sorted(pool, key=_by_score_then_price)[:CONFIRM_CAP]
    confirm(pool, brief.text)

    ordered = sorted(survivors, key=_by_score_then_price)
    contenders = [l for l in ordered if (l.interest_score or 0) >= threshold]
    rest = [l for l in ordered if (l.interest_score or 0) < threshold]
    # Pass 3: granular factor-breakdown + narrative for the contenders, shown in the
    # detail modal (NFR-1). Small set, so it's cheap; anything in "view all" gets the
    # same breakdown on demand when its modal is opened (see /explain).
    detail(contenders, brief.text)
    counts = {
        "fetched": fetched,
        "gated": len(survivors),
        "dropped": len(dropped),
        "contenders": len(contenders),
        "rest": len(rest),
        "scored": len(survivors),
    }
    return PullResult(contenders, rest, dropped, judge_kind, counts)


def gate_only(
    sources: Source | list[Source], *, query: str = "timex", limit: int = 1000,
    cap: float = PRICE_CAP_CAD,
) -> tuple[list[Listing], list[tuple[Listing, GateResult]], int]:
    """Boxes 1-3: fetch from every source + normalize + hard gate. Cached so
    re-ranks are cheap. Each source emits the shared `Listing`, so combining
    marketplaces is just concatenation (NFR-4); one source failing returns [] and
    never breaks the others (NFR-3)."""
    if isinstance(sources, Source):
        sources = [sources]
    listings: list[Listing] = []
    for source in sources:
        listings.extend(source.fetch(query, limit))
    survivors, dropped = apply_gate(listings, cap)
    return survivors, dropped, len(listings)


def run_pull(
    sources: Source | list[Source],
    profile: TasteProfile,
    brief: TasteBrief,
    *,
    query: str = "timex",
    limit: int = 1000,
    cap: float = PRICE_CAP_CAD,
) -> PullResult:
    """One full on-demand pull, end to end."""
    survivors, dropped, fetched = gate_only(sources, query=query, limit=limit, cap=cap)
    return rank_survivors(survivors, dropped, profile, brief, fetched)
