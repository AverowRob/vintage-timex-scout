"""The normalized `Listing` schema (README §7).

Every source converts its own format into this one shared record, so everything
downstream — the gate, the pre-rank, the LLM judge, the UI — deals with a single
shape and never cares which marketplace a listing came from (NFR-4 Extensible).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawCondition:
    """A listing's condition as the marketplace reports it.

    Structured (`label`/`condition_id`) plus free text (`description`). The
    deterministic gate (FR-3) reads all three to decide "not broken": the
    structured field catches "For parts or not working", the free text catches
    broken keywords a "Used" label hides.
    """

    label: str | None = None         # e.g. "Used", "For parts or not working"
    condition_id: str | None = None  # eBay conditionId, e.g. "3000"
    description: str | None = None    # free-text subtitle / short description

    def text(self) -> str:
        """All condition-bearing text, lowercased, for keyword scanning."""
        parts = [self.label or "", self.description or ""]
        return " ".join(p for p in parts if p).lower()


@dataclass
class Listing:
    """One normalized marketplace listing."""

    # --- Core (filled by every source adapter) ---
    source: str                       # e.g. "ebay"
    id: str                           # source-native id (unique within source)
    url: str                          # link out to the original listing (FR-6)
    title: str
    price: float | None               # item price in `currency`
    currency: str | None              # e.g. "CAD"
    raw_condition: RawCondition = field(default_factory=RawCondition)
    item_location: str | None = None  # human-readable, e.g. "Toronto, CA"
    images: list[str] = field(default_factory=list)  # URLs (not stored, FR-5)
    seller: str | None = None
    listing_end: str | None = None    # ISO 8601 string, if provided
    listed_at: str | None = None      # ISO 8601 — when the listing was created (its "age")
    # Full-detail enrichment (eBay getItem, D41): the seller's written description
    # (cleaned) and the structured item specifics (Model, Year, Movement, Box/Papers…).
    # High-signal for the taste judge and for human review; empty for fixture items.
    description: str | None = None
    item_specifics: dict = field(default_factory=dict)  # {name: value}
    raw: dict = field(default_factory=dict)  # debug blob: the source's raw item

    # --- Added downstream by the pipeline ---
    # Gate (E10): set when the not-broken check inspects condition.
    working_status: str | None = None     # "unknown" | "working" | "broken"
    disclosed_damage: str | None = None
    # Pre-rank (FR-4): cheap keyword score against the taste profile.
    prerank_score: float | None = None
    prerank_matches: list[str] = field(default_factory=list)
    # LLM judge (FR-4): the one place AI ranks.
    interest_score: int | None = None     # 0-100
    reason: str | None = None             # one-line, human-readable (NFR-1)
    # Rich explanation (NFR-1), shown in the detail modal. Precomputed for the
    # contenders; filled on demand for anything else. `score_factors` is a list of
    # {"signal": str, "impact": "strong+"|"+"|"neutral"|"-"|"strong-"}.
    score_factors: list[dict] = field(default_factory=list)
    score_narrative: str | None = None

    # Shipping to the ship-to location (FR-2): the budget is TOTAL landed cost
    # (item + shipping ≤ cap), not item price alone. `shipping_cost` is None when the
    # marketplace returns no shipping figure (e.g. local-pickup / calculated-no-quote).
    shipping_cost: float | None = None
    landed_cost_cad: float | None = None  # reserved (FX-normalized landed cost, D19)

    @property
    def landed_cost(self) -> float | None:
        """Item price + shipping = the brief's budget basis. Unknown shipping counts
        as 0 (optimistic) and is flagged in the UI, so an item never disappears just
        because the seller didn't quote shipping."""
        if self.price is None:
            return None
        return self.price + (self.shipping_cost or 0.0)

    @property
    def shipping_known(self) -> bool:
        return self.shipping_cost is not None

    def search_text(self) -> str:
        """Title + condition text, lowercased — what the pre-rank matches on."""
        return f"{self.title} {self.raw_condition.text()}".lower()
