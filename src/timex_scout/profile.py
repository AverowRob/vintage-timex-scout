"""The taste profile (README §7, E7).

The explicit, inspectable definition of "interesting": a weighted keyword list,
seeded from the collector's three example watches, editable by hand, and updated
by likes (the learning loop). It is a plain list, not hidden model state — the
whole point is that the user can see and edit what the system matches on.

The seed is grounded in the brief's three ground-truth listings (fetched live):
  1. eBay 377073705816 — "Timex Men's Easy Reader Logo Quartz" (clean, legible,
     working quartz)
  2. eBay 117111976291 — "'Breyers' Ice Cream ... Timex La Cell" (advertising /
     novelty character dial)
  3. etsy 4469739360 — "Vintage Timex Marlin, green bullseye dial" (~1992,
     mechanical calendar). Read for taste keywords only; Etsy is NOT an MVP
     source (D3 — deferred). The buyer tolerates minor faults ("running, date
     wrong, selling as is") when the piece is interesting.

Plus the brief's explicit definition of "interesting" (direct quote): "Collabs
(or collaborations), deadstock, vintage models."

Together these reveal a taste built on **distinctive / characterful dials**
(advertising, novelty, bullseye), **named vintage model lines** (Marlin), and
**collectibility** (collabs, deadstock) — across both quartz and mechanical, and
condition-tolerant. The weights below reflect that. (See BUILD_JOURNAL: the
original seed over-indexed on plain mechanical Marlins until the real listings
were fetched.)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# The brief's three taste-seed listings, shown in the UI so the user can see what
# taste the system is matching on.
GROUND_TRUTH_EXAMPLES: list[dict[str, str]] = [
    {"title": "Timex Men's Easy Reader Logo Quartz WR Watch Works Great",
     "source": "ebay", "url": "https://www.ebay.ca/itm/377073705816",
     "image": "https://i.ebayimg.com/images/g/r6QAAeSwSqlpypD3/s-l500.webp",
     "note": "clean, legible quartz"},
    {"title": "“Breyers” Ice Cream Watch Genuine Leather Timex La Cell",
     "source": "ebay", "url": "https://www.ebay.ca/itm/117111976291",
     "image": "https://i.ebayimg.com/images/g/4KwAAOSwoyVk68S-/s-l500.webp",
     "note": "advertising / novelty character dial"},
    {"title": "Vintage Timex Marlin, green bullseye dial (~1992)",
     "source": "etsy", "url": "https://www.etsy.com/ca/listing/4469739360",
     "image": "https://i.etsystatic.com/28052433/r/il/288786/7790629598/il_794xN.7790629598_q99c.jpg",
     "note": "mechanical, distinctive bullseye dial"},
]

# Curated seed weights, grounded in the ground-truth above. Positive = more
# interesting; negative = down-weight (never exclude — only the gate excludes).
_SEED_WEIGHTS: dict[str, float] = {
    # The brief's explicit definition of "interesting" (direct quote): "Collabs
    # (or collaborations), deadstock, vintage models." These are the core taste
    # signals, weighted strongly. (Deadstock/vintage/model lines also appear below.)
    "collab": 3.0, "collaboration": 3.0, "collaborations": 3.0,
    # Character / novelty / advertising dials — the strongest signal from GT#2.
    "character dial": 3.0, "advertising": 3.0, "novelty": 2.5, "promo": 2.0,
    "advertising dial": 3.0, "ice cream": 1.5, "breyers": 2.0, "mickey": 2.5,
    "snoopy": 2.5, "disney": 2.0, "peanuts": 2.0, "cartoon": 2.0,
    # Clean legible models — the signal from GT#1.
    "easy reader": 2.5, "la cell": 2.5, "logo": 1.0, "marlin": 2.5,
    # Collectible vintage model lines (still genuinely desirable).
    "viscount": 2.0, "camper": 2.0, "mercury": 2.0, "sprite": 1.8,
    "marauder": 1.8, "dynabeat": 1.8, "marlin mercury": 2.5,
    # Vintage mechanical character (positive, but quartz is fine too).
    "hand-wind": 1.5, "hand wind": 1.5, "manual wind": 1.5, "wind-up": 1.2,
    "mechanical": 1.2, "automatic": 1.2, "self-wind": 1.2, "jewel": 0.8,
    # Eras.
    "1960s": 1.3, "1970s": 1.3, "1980s": 0.8, "1960": 0.9, "1970": 0.9,
    "vintage": 1.0,
    # Desirable condition / dials.
    "nos": 2.5, "deadstock": 2.5, "new old stock": 2.5, "serviced": 1.0,
    "bullseye": 3.0,  # GT#3: green bullseye Marlin — a distinctive collector dial
    "linen dial": 1.3, "sunburst": 1.0, "military": 1.3, "field watch": 1.3,
    "cushion": 0.9, "day-date": 0.6, "calendar": 0.8,  # GT#3 had a date/calendar
    # Down-weights — genuine non-target junk (the gate still keeps them browsable).
    "ironman": -2.0, "smartwatch": -2.5, "gps": -2.0, "digital": -1.2,
    "indiglo": -0.8, "expedition": -0.6, "lot of": -2.5, "job lot": -2.5,
    "strap": -2.0, "band only": -2.5, "for parts": -1.0,
}

# Generic tokens to ignore when extracting keywords from a liked listing.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "with", "for", "mens", "men", "womens",
    "women", "watch", "watches", "wrist", "wristwatch", "timex", "vintage",
    "dial", "tone", "gold", "silver", "steel", "case", "band", "works",
    "working", "used", "new", "rare", "great", "nice", "size", "mm",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class TasteProfile:
    """A weighted keyword profile. The product's notion of 'interesting'."""

    weights: dict[str, float] = field(default_factory=lambda: dict(_SEED_WEIGHTS))

    @classmethod
    def seed(cls) -> "TasteProfile":
        return cls(weights=dict(_SEED_WEIGHTS))

    def positive_keywords(self) -> list[str]:
        return sorted((k for k, v in self.weights.items() if v > 0),
                      key=lambda k: -self.weights[k])

    def negative_keywords(self) -> list[str]:
        return sorted((k for k, v in self.weights.items() if v < 0),
                      key=lambda k: self.weights[k])

    def bump(self, keyword: str, by: float = 1.0) -> None:
        """Hand-edit / learning-loop hook: raise a keyword's weight."""
        keyword = keyword.strip().lower()
        if keyword:
            self.weights[keyword] = self.weights.get(keyword, 0.0) + by

    def merge_weights(self, extra: dict[str, float]) -> None:
        """Merge LLM-extracted weights into the seed (E3-Next), taking the max so
        a curated signal is never weakened by extraction."""
        for k, v in extra.items():
            k = k.strip().lower()
            if k:
                self.weights[k] = max(self.weights.get(k, 0.0), float(v))

    def learn_from_title(self, title: str, by: float = 1.5) -> list[str]:
        """Extract salient keywords from a liked listing and add them (E7).

        Heuristic extraction. Returns the keywords it added so the UI can show
        what the like taught the system.
        """
        added: list[str] = []
        tokens = [t for t in _TOKEN_RE.findall(title.lower())
                  if t not in _STOPWORDS and len(t) > 2 and not t.isdigit()]
        for tok in tokens:
            self.bump(tok, by)
            added.append(tok)
        return added

    # --- Optional tiny persistence (survives a restart; D11) ---
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.weights, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "TasteProfile":
        if path.exists():
            return cls(weights=json.loads(path.read_text(encoding="utf-8")))
        return cls.seed()
