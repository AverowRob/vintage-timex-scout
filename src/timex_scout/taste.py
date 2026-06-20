"""The taste brief — a natural-language "training file" the LLM judge references.

The AI-native replacement for a brittle keyword list (README D4 wanted a rubric
from the 3 examples; this is that rubric, in prose). A small markdown document
that states what makes a listing interesting (the brief's guidance), the example
watches the collector likes (the 3 ground-truth seeds + anything liked in the
app), and what's not. The LLM scores every gated listing against this doc.

It is the explicit, inspectable definition of "interesting": editable by hand
(it's just markdown) and grown by likes — the ".md file the AI references" the
collector described. Kept in a lightweight store (state/taste.md), survives a
restart (D11).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .profile import GROUND_TRUTH_EXAMPLES

_LIKED_HEADING = "## Watches I've liked"
_PASSED_HEADING = "## Passed on (specific watches I'm NOT interested in)"

_SEED = """# Taste brief — what makes a vintage Timex "interesting"

## What I'm looking for (from the brief)
Interesting pieces, **item price ≤ $50**, **not broken** (a dead battery is
fine). The brief's definition of interesting: **collabs / collaborations,
deadstock, and vintage models.** In practice:
- **Collabs & character / advertising dials** — brand tie-ins, promo dials,
  cartoon characters (e.g. Mickey, Snoopy, a Breyers ice-cream dial).
- **Deadstock / NOS** — new old stock, unworn, ideally boxed.
- **Vintage model lines** — Marlin, Viscount, Camper, Mercury, Marauder and
  other named vintage Timex; distinctive dials (bullseye, linen, sunburst).

Quartz is fine — not a negative. Minor faults are OK if the piece is interesting.

## Example watches I like (the seed)
{examples}

What ties these together: distinctive / characterful dials, named vintage model
lines, and collectibility — across both quartz and mechanical.

## Not interesting
**Must be a Timex** — a non-Timex watch (e.g. a Seiko or Lorus Mickey-Mouse
piece) scores 0, even with a fun dial. Also off-taste: generic modern digitals,
Ironman, smartwatches, plain Expedition / Indiglo, replacement straps or bands,
and bulk "watch lots".

{liked_heading}
_(grows as you like watches in the app)_
"""


@dataclass
class TasteBrief:
    """The collector's taste, as an editable markdown brief the judge reads."""

    text: str

    @classmethod
    def seed(cls) -> "TasteBrief":
        examples = "\n".join(
            f"{i}. **{e['title']}** — {e['note']}"
            for i, e in enumerate(GROUND_TRUTH_EXAMPLES, 1)
        )
        return cls(text=_SEED.format(examples=examples, liked_heading=_LIKED_HEADING))

    def add_liked(self, title: str, reason: str = "") -> None:
        """Append a liked watch to the brief as a positive example (the loop), with an
        optional reason for WHAT the collector likes — so the LLM learns the right
        trait (the bullseye dial, NOS, the model line), not just the whole title."""
        title = title.strip()
        if not title:
            return
        self.remove_liked(title)  # de-dupe / update (e.g. adding a reason later)
        line = f"- {title}"
        if reason.strip():
            line += f" — {reason.strip()}"
        if _LIKED_HEADING in self.text:
            self.text = self.text.rstrip() + "\n" + line + "\n"
        else:
            self.text = self.text.rstrip() + f"\n\n{_LIKED_HEADING}\n{line}\n"

    def remove_liked(self, title: str) -> None:
        """Drop a liked watch's line from the brief (when un-liked), with or without
        an appended reason."""
        t = title.strip()
        self.text = "\n".join(
            l for l in self.text.splitlines()
            if l.strip() != f"- {t}" and not l.strip().startswith(f"- {t} —")
        )

    def add_disliked(self, title: str, reason: str = "") -> None:
        """Record a downvoted watch (with an optional reason) so the LLM learns to
        rate similar pieces LOWER — never excluded (D16). Soft, editable, visible."""
        title = title.strip()
        if not title:
            return
        self.remove_disliked(title)  # de-dupe / update
        line = f"- {title}"
        if reason.strip():
            line += f" — {reason.strip()}"
        if _PASSED_HEADING not in self.text:
            self.text = self.text.rstrip() + f"\n\n{_PASSED_HEADING}\n"
        idx = self.text.index(_PASSED_HEADING) + len(_PASSED_HEADING)
        self.text = self.text[:idx] + "\n" + line + self.text[idx:]

    def remove_disliked(self, title: str) -> None:
        t = title.strip()
        self.text = "\n".join(
            l for l in self.text.splitlines()
            if l.strip() != f"- {t}" and not l.strip().startswith(f"- {t} —")
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text, encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "TasteBrief":
        if path.exists():
            return cls(text=path.read_text(encoding="utf-8"))
        return cls.seed()
