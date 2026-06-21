"""FastAPI app: top contenders, view-all, taste profile, filters, learning loop.

Single-user, in-memory (README §4, §8). State held in `AppState`: the source,
the taste profile, the cached gated survivors, the current ranked result, and
the set of liked ids. Liking re-ranks live from the cached survivors — no
re-fetch, no re-gate — so the ranking visibly improves with use (E7).

E4 present surface: top contenders / view-all / liked, each with price + interest
filters, a sort control, and pagination (the gated set is ~370 listings).
"""

from __future__ import annotations

import json
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import PRICE_CAP_CAD, load_dotenv
from ..gate import GateResult
from ..models import Listing, RawCondition
from ..pipeline import CONTENDER_THRESHOLD, PullResult, gate_only, rank_survivors
from ..profile import GROUND_TRUTH_EXAMPLES, TasteProfile
from ..sources.ebay import EbaySource
from ..sources.etsy import EtsySource
from ..taste import TasteBrief

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Disable Jinja2's template LRU cache: its cache key trips an unhashable-key bug
# under Python 3.14. Negligible cost for a single-template demo.
_TEMPLATES.env.cache = None

# Lightweight stores so the taste survives a restart (D11): the markdown taste
# brief (primary, read by the LLM) and the keyword profile (no-LLM fallback).
# Resolve `state/` whether we run from source (repo-root/state, next to src/) or as
# an installed package launched from the repo checkout (cwd/state) — pick whichever
# already holds the committed pull snapshot, so a host like Render finds it too.
def _find_state_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parents[3] / "state",   # src layout: repo-root/state
        Path.cwd() / "state",                             # installed, run from repo root
    ]
    for d in candidates:
        if (d / "last_pull.json").exists():
            return d
    return candidates[0] if candidates[0].parent.exists() else candidates[1]


_STATE_DIR = _find_state_dir()
BRIEF_PATH = _STATE_DIR / "taste.md"
PROFILE_PATH = _STATE_DIR / "taste_profile.json"
# Cache of the last fetched+enriched pull (D41 / quota): so a restart or code reload
# REUSES the listings already pulled instead of hitting eBay again. Only the explicit
# "Fetch Listings" button re-pulls; everything else (Reapply, restart) re-scores this.
PULL_CACHE = _STATE_DIR / "last_pull.json"
PER_PAGE = 24
logger = logging.getLogger(__name__)


def _age(iso: str | None) -> str:
    """Human 'listed N ago' from an ISO 8601 creation date (eBay itemCreationDate)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - dt).days
    except Exception:  # noqa: BLE001
        return ""
    if days <= 0:
        return "listed today"
    if days == 1:
        return "listed yesterday"
    if days < 7:
        return f"listed {days} days ago"
    if days < 14:
        return "listed 1 week ago"
    if days < 60:
        return f"listed {days // 7} weeks ago"
    if days < 365:
        return f"listed {max(1, days // 30)} months ago"
    years = days // 365
    return "listed 1 year ago" if years == 1 else f"listed {years} years ago"


_TEMPLATES.env.filters["age"] = _age


def _listing_to_dict(l: Listing) -> dict:
    d = asdict(l)
    d.pop("raw", None)               # drop the large source debug blob from the cache
    return d


def _listing_from_dict(d: dict) -> Listing:
    d = dict(d)
    d["raw_condition"] = RawCondition(**(d.get("raw_condition") or {}))
    fields = Listing.__dataclass_fields__
    return Listing(**{k: v for k, v in d.items() if k in fields})


@dataclass
class Filters:
    price_min: float | None = None
    price_max: float | None = None
    min_score: int = 0
    sort: str = "interest"          # interest | price_asc | price_desc
    page: int = 1

    @classmethod
    def from_request(cls, request: Request) -> "Filters":
        q = request.query_params

        def num(key):
            v = q.get(key, "").strip()
            try:
                return float(v) if v else None
            except ValueError:
                return None

        return cls(
            price_min=num("price_min"),
            price_max=num("price_max"),
            min_score=int(num("min_score") or 0),
            sort=q.get("sort", "interest"),
            page=max(1, int(num("page") or 1)),
        )

    def query_without_page(self) -> str:
        parts = []
        if self.price_min is not None:
            parts.append(f"price_min={self.price_min:g}")
        if self.price_max is not None:
            parts.append(f"price_max={self.price_max:g}")
        if self.min_score:
            parts.append(f"min_score={self.min_score}")
        if self.sort != "interest":
            parts.append(f"sort={self.sort}")
        return "&".join(parts)


def _apply_filters(listings: list[Listing], f: Filters) -> list[Listing]:
    out = [
        l for l in listings
        if (f.price_min is None or (l.price or 0) >= f.price_min)
        and (f.price_max is None or (l.price or 1e9) <= f.price_max)
        and (l.interest_score or 0) >= f.min_score
    ]
    if f.sort == "price_asc":
        out.sort(key=lambda l: l.price if l.price is not None else 1e9)
    elif f.sort == "price_desc":
        out.sort(key=lambda l: -(l.price or 0))
    else:
        out.sort(key=lambda l: (-(l.interest_score or 0),
                                l.price if l.price is not None else 1e9))
    return out


@dataclass
class AppState:
    ebay: EbaySource = field(default_factory=EbaySource)
    etsy: EtsySource = field(default_factory=EtsySource)
    profile: TasteProfile = field(default_factory=TasteProfile.seed)
    brief: TasteBrief = field(default_factory=TasteBrief.seed)
    survivors: list[Listing] = field(default_factory=list)
    dropped: list[tuple[Listing, GateResult]] = field(default_factory=list)
    fetched: int = 0
    result: PullResult | None = None
    liked_ids: set[str] = field(default_factory=set)        # saved / shortlist
    reference_ids: set[str] = field(default_factory=set)    # influences the brief
    disliked_ids: set[str] = field(default_factory=set)
    disliked_reasons: dict[str, str] = field(default_factory=dict)
    liked_reasons: dict[str, str] = field(default_factory=dict)  # what the user likes about a reference
    last_learned: list[str] = field(default_factory=list)
    last_action: str = ""          # "saved" | "refined" | "passed"
    taste_source: str = "curated seed"
    last_pull_at: str = ""
    last_count: int = 0
    # Cost control (D39): taste edits (likes/dislikes/brief edits) only mutate the
    # brief and queue a re-score — they do NOT re-score the gated listings until the
    # user clicks "Reapply taste". `pending` maps a change KEY (a listing id, or a
    # constant for brief-text edits) to a human-readable note, so undoing a queued
    # change CANCELS its entry instead of stacking another one.
    pending: dict[str, str] = field(default_factory=dict)
    flash: str = ""                # one-shot success toast, shown then cleared on next render

    @property
    def brief_dirty(self) -> bool:
        return bool(self.pending)

    @property
    def sources(self) -> list:
        return [self.ebay, self.etsy]

    def source_status(self) -> list[dict]:
        """Per-source connection state for the funnel dots (tri-state):
        green = live (API returned data), yellow = wired (key set, not live yet —
        e.g. pending approval), red = off (no key)."""
        out = []
        for s in self.sources:
            if s.last_mode == "live":
                state, detail = "live", "live API"
            elif getattr(s, "configured", False):
                state, detail = "wired", "API key set · awaiting approval / first live pull"
            else:
                state, detail = "off", "no API key"
            out.append({"name": s.display, "state": state, "detail": detail})
        return out

    def build_taste(self) -> None:
        """Load the persisted taste (brief + keyword fallback), or seed them.

        The markdown brief is the primary taste the LLM reads; the keyword
        profile is the no-LLM fallback."""
        self.brief = TasteBrief.load(BRIEF_PATH)
        self.profile = TasteProfile.load(PROFILE_PATH)
        self.taste_source = ("edited / restored" if BRIEF_PATH.exists()
                             else "seeded from the brief + 3 examples")

    def full_pull(self) -> None:
        """The ONLY path that hits the marketplaces (Fetch Listings). Fetches +
        enriches + gates, caches the result, then scores. Everything else re-scores
        the cached survivors without re-pulling (D41 / quota)."""
        self.survivors, self.dropped, self.fetched = gate_only(self.sources)
        self.last_pull_at = datetime.now().strftime("%b %d, %Y at %I:%M %p")
        self.last_count = self.fetched
        self.save_pull()                                # cache the enriched pull
        self.rerank(detect_broken=True)                 # judge broken once, per fetch
        self._clear_pending()                           # fresh scores reflect the brief

    def save_pull(self) -> None:
        """Persist the fetched+enriched gate output so a restart can reuse it."""
        try:
            data = {
                "fetched": self.fetched,
                "last_pull_at": self.last_pull_at,
                # Remember how each source did, so the funnel dots stay accurate after a
                # cache-load restart (eBay should read 🟢 live, not 🟡 wired).
                "source_modes": {s.name: s.last_mode for s in self.sources},
                "survivors": [_listing_to_dict(l) for l in self.survivors],
                "dropped": [{"l": _listing_to_dict(l), "g": asdict(g)} for l, g in self.dropped],
            }
            PULL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            PULL_CACHE.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            logger.warning("Could not cache pull (%s).", exc)

    def load_pull(self) -> bool:
        """Reuse the cached pull instead of re-fetching. True if loaded."""
        if not PULL_CACHE.exists():
            return False
        try:
            data = json.loads(PULL_CACHE.read_text(encoding="utf-8"))
            self.survivors = [_listing_from_dict(d) for d in data["survivors"]]
            self.dropped = [(_listing_from_dict(x["l"]), GateResult(**x["g"]))
                            for x in data["dropped"]]
            self.fetched = data.get("fetched", len(self.survivors))
            self.last_pull_at = data.get("last_pull_at", "")
            self.last_count = self.fetched
            modes = data.get("source_modes") or {}
            for s in self.sources:
                if modes.get(s.name):
                    s.last_mode = modes[s.name]
            return True
        except Exception as exc:  # noqa: BLE001 — fall back to a fresh pull on any issue
            logger.warning("Could not load cached pull (%s); re-fetching.", exc)
            return False

    def rerank(self, *, detect_broken: bool = False) -> None:
        """The one expensive path — re-scores every gated listing against the brief.
        Only `full_pull` (Fetch Listings) and `apply_taste` (Reapply) call it.

        Broken-ness is judged only on a fresh fetch (`detect_broken=True`) and frozen
        after, so a taste Reapply re-scores without changing the "Passed the gate"
        count — that number is fixed until you fetch again."""
        self.result = rank_survivors(
            self.survivors, self.dropped, self.profile, self.brief, self.fetched,
            detect_broken=detect_broken,
        )

    def mark_dirty(self, key: str, note: str) -> None:
        """Queue a taste change WITHOUT re-scoring (D39), keyed (by listing id, or a
        constant for brief-text edits) so a later undo can cancel it. The brief is
        saved by the caller; scores stay stale until the user reapplies."""
        self.pending[key] = note

    def unmark(self, key: str) -> bool:
        """Cancel a still-queued change (an undo before Reapply). True if one was
        queued — so the caller knows whether this undo nets out a pending change or
        is itself a fresh change to an already-applied edit."""
        return self.pending.pop(key, None) is not None

    def apply_taste(self) -> None:
        """Re-score every listing against the (edited) brief — the explicit, batched
        re-score the user triggers after queuing refinements."""
        self.rerank()
        self._clear_pending()

    def _clear_pending(self) -> None:
        self.pending = {}

    def find(self, listing_id: str) -> Listing | None:
        return next((l for l in self.survivors if l.id == listing_id), None)

    def liked(self) -> list[Listing]:
        return [l for l in self.survivors if l.id in self.liked_ids]

    def references(self) -> list[Listing]:
        return [l for l in self.survivors if l.id in self.reference_ids]

    def saved_only(self) -> list[Listing]:
        return [l for l in self.survivors
                if l.id in self.liked_ids and l.id not in self.reference_ids]

    def disliked(self) -> list[Listing]:
        return [l for l in self.survivors if l.id in self.disliked_ids]

    def base_listings(self, mode: str) -> list[Listing]:
        assert self.result is not None
        if mode == "all":
            return self.result.all_gated
        if mode == "liked":
            return self.liked()
        return self.result.contenders


# Load .env BEFORE constructing AppState — the source adapters read their
# credentials (eBay, Etsy, Gemini) from the environment at construction time.
load_dotenv()
state = AppState()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    state.build_taste()
    # Reuse the cached pull if we have one — a restart/reload should NOT re-hit eBay
    # (D41 / quota). Score it against the current brief. Only "Fetch Listings" pulls.
    if state.load_pull():
        logger.info("Reusing cached pull (%d survivors); scoring without re-fetching.",
                    len(state.survivors))
        state.rerank(detect_broken=True)
        state._clear_pending()
    else:
        state.full_pull()
    yield


app = FastAPI(title="Vintage Timex Scout", lifespan=_lifespan)


def _take_flash() -> str:
    """Read and clear the one-shot success message (shown once after a re-score)."""
    msg, state.flash = state.flash, ""
    return msg


def _render(request: Request, mode: str) -> HTMLResponse:
    assert state.result is not None
    f = Filters.from_request(request)
    headings = {"all": "All gated listings", "liked": "Liked",
                "contenders": "Top contenders", "taste": "Taste agent"}
    filtered = _apply_filters(state.base_listings(mode), f) if mode != "taste" else []
    total = len(filtered)
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(f.page, pages)
    page_items = filtered[(page - 1) * PER_PAGE: page * PER_PAGE]
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "mode": mode, "heading": headings[mode],
            "listings": page_items, "total": total,
            "page": page, "pages": pages, "filters": f,
            "query_base": f.query_without_page(),
            "result": state.result, "profile": state.profile,
            "brief": state.brief.text, "liked": state.liked(),
            "references": state.references(), "saved_only": state.saved_only(),
            "reference_ids": state.reference_ids,
            "disliked": state.disliked(), "disliked_ids": state.disliked_ids,
            "disliked_reasons": state.disliked_reasons,
            "liked_reasons": state.liked_reasons,
            "examples": GROUND_TRUTH_EXAMPLES, "taste_source": state.taste_source,
            "liked_ids": state.liked_ids, "last_learned": state.last_learned,
            "last_action": state.last_action,
            "sources": state.source_status(), "last_pull_at": state.last_pull_at,
            "last_count": state.last_count, "threshold": CONTENDER_THRESHOLD,
            "price_cap": ("%g" % PRICE_CAP_CAD),
            "brief_dirty": state.brief_dirty, "pending": state.pending,
            "flash": _take_flash(), "liked_count": len(state.liked()),
        },
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render(request, "contenders")


@app.get("/all", response_class=HTMLResponse)
def view_all(request: Request):
    return _render(request, "all")


@app.get("/liked", response_class=HTMLResponse)
def view_liked(request: Request):
    return _render(request, "liked")


@app.get("/taste", response_class=HTMLResponse)
def view_taste(request: Request):
    return _render(request, "taste")


@app.get("/explain/{listing_id}")
def explain_listing(listing_id: str):
    """On-demand granular breakdown for one listing (the detail modal). Contenders
    are precomputed in the pull; everything else is explained lazily on first open,
    then cached on the listing so re-opening is instant."""
    from ..judge import detail as _detail

    if state.result is None:
        return JSONResponse({"error": "no data"}, status_code=503)
    listing = state.find(listing_id)
    if listing is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not listing.score_factors:
        _detail([listing], state.brief.text)
    return JSONResponse({
        "factors": listing.score_factors,
        "narrative": listing.score_narrative or "",
    })


@app.post("/like/{listing_id}")
def like(listing_id: str, refine: int = Form(0), reason: str = Form(""),
         mode: str = Form("contenders")):
    """Two paths (chosen in the modal):
    refine=0 — just save to the shortlist; instant, no taste impact.
    refine=1 — add as a reference in the brief WITH an optional reason (what the user
    likes about it) and queue a re-score (D39): the brief is updated now, but the
    listings are NOT re-scored until "Reapply taste".
    """
    listing = state.find(listing_id)
    if listing:
        state.liked_ids.add(listing_id)
        state.disliked_ids.discard(listing_id)         # mutually exclusive
        state.disliked_reasons.pop(listing_id, None)
        state.brief.remove_disliked(listing.title)
        if refine:
            state.reference_ids.add(listing_id)
            state.liked_reasons[listing_id] = reason.strip()
            state.brief.add_liked(listing.title, reason)
            state.profile.learn_from_title(listing.title)
            state.brief.save(BRIEF_PATH)
            state.profile.save(PROFILE_PATH)
            state.last_learned, state.last_action = [listing.title], "refined"
            state.mark_dirty(listing_id, f"Liked “{listing.title}”")
        else:
            state.last_learned, state.last_action = [listing.title], "saved"
            state.flash = (f"Saved “{listing.title}” to your shortlist — your Scout is "
                           "unchanged (choose “Save & refine” to teach it).")
    return RedirectResponse(f"/{'' if mode == 'contenders' else mode}", status_code=303)


@app.post("/reference/{listing_id}")
def add_reference(listing_id: str):
    """Promote a saved watch to a taste reference (from Manage Scout): queues a re-score."""
    listing = state.find(listing_id)
    if listing:
        state.liked_ids.add(listing_id)
        state.reference_ids.add(listing_id)
        state.brief.add_liked(listing.title)
        state.brief.save(BRIEF_PATH)
        state.last_learned, state.last_action = [listing.title], "refined"
        state.mark_dirty(listing_id, f"Referenced “{listing.title}”")
    return RedirectResponse("/taste", status_code=303)


@app.post("/dislike/{listing_id}")
def dislike(listing_id: str, reason: str = Form(""), mode: str = Form("contenders")):
    """Downvote (E7, negative signal): record the watch — with an optional reason
    so the LLM learns the right thing — in the brief's 'Passed on' section. Queues a
    re-score (D39); soft-penalize only, never hard-exclude (D16)."""
    listing = state.find(listing_id)
    if listing:
        state.liked_ids.discard(listing_id)
        state.brief.remove_liked(listing.title)
        state.disliked_ids.add(listing_id)
        state.disliked_reasons[listing_id] = reason.strip()
        state.brief.add_disliked(listing.title, reason)
        state.brief.save(BRIEF_PATH)
        state.last_learned, state.last_action = [listing.title], "passed"
        state.mark_dirty(listing_id, f"Passed on “{listing.title}”")
    return RedirectResponse(f"/{'' if mode == 'contenders' else mode}", status_code=303)


def _back(mode: str) -> str:
    """Where to return after a toggle: the listing view it was triggered from, or
    Manage Scout (the default, used by its 'remove' buttons which send no mode)."""
    if not mode:
        return "/taste"
    return f"/{'' if mode == 'contenders' else mode}"


@app.post("/unlike/{listing_id}")
def unlike(listing_id: str, mode: str = Form("")):
    listing = state.find(listing_id)
    was_ref = listing_id in state.reference_ids
    state.liked_ids.discard(listing_id)
    state.reference_ids.discard(listing_id)
    state.liked_reasons.pop(listing_id, None)
    if listing and was_ref:  # only a reference change affects scoring
        state.brief.remove_liked(listing.title)
        state.brief.save(BRIEF_PATH)
        # If the "like" was still queued (not yet reapplied), undo cancels it instead
        # of stacking another change; only an applied reference's removal is new work.
        if not state.unmark(listing_id):
            state.mark_dirty(listing_id, f"Removed reference “{listing.title}”")
    if listing:
        state.flash = f"Removed “{listing.title}” from liked"
    return RedirectResponse(_back(mode), status_code=303)


@app.post("/undislike/{listing_id}")
def undislike(listing_id: str, mode: str = Form("")):
    listing = state.find(listing_id)
    state.disliked_ids.discard(listing_id)
    state.disliked_reasons.pop(listing_id, None)
    if listing:
        state.brief.remove_disliked(listing.title)
        state.brief.save(BRIEF_PATH)
        # Undo a still-queued downvote → cancel it; undo an applied one → new change.
        if not state.unmark(listing_id):
            state.mark_dirty(listing_id, f"Un-passed “{listing.title}”")
        state.flash = f"Un-passed “{listing.title}” — no longer downvoted"
    return RedirectResponse(_back(mode), status_code=303)


@app.post("/taste/save")
def taste_save(brief: str = Form(...)):
    """Edit the taste brief by hand — it's the editable .md the judge reads. Saves the
    text and queues a re-score (D39); takes effect on "Reapply taste"."""
    state.brief.text = brief
    state.brief.save(BRIEF_PATH)
    state.mark_dirty("__brief_text__", "Edited the taste brief")
    return RedirectResponse("/taste", status_code=303)


@app.post("/taste/apply")
def taste_apply(mode: str = Form("contenders")):
    """The explicit, batched re-score: apply all queued taste changes to every gated
    listing in ONE pass (D39). This is the only user-triggered LLM scoring run besides
    Fetch Listings, so many likes/dislikes/edits cost a single re-score, not N."""
    n = len(state.pending)
    state.apply_taste()
    c = state.result.counts
    state.flash = (f"Taste reapplied — re-scored {c['gated']} listings, "
                   f"{c['contenders']} contenders" + (f" ({n} change{'s' if n != 1 else ''})" if n else ""))
    return RedirectResponse(f"/{'' if mode == 'contenders' else mode}", status_code=303)


@app.post("/taste/reset")
def taste_reset():
    BRIEF_PATH.unlink(missing_ok=True)
    PROFILE_PATH.unlink(missing_ok=True)
    state.liked_ids.clear()
    state.reference_ids.clear()
    state.disliked_ids.clear()
    state.disliked_reasons.clear()
    state.liked_reasons.clear()
    state.last_learned = []
    state.build_taste()
    state.rerank()
    state._clear_pending()
    return RedirectResponse("/", status_code=303)


@app.post("/refresh")
def refresh():
    state.full_pull()
    c = state.result.counts
    state.flash = f"Fetched {c['fetched']} listings — {c['contenders']} contenders"
    return RedirectResponse("/", status_code=303)
