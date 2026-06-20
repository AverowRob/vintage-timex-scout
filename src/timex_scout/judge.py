"""The LLM interest judge (FR-4, README §7 box 5) — the one place AI ranks.

Scores the top pool 0-100 with a one-line reason (NFR-1 explainable), catching
what keywords miss. It runs only on the small pool after the gate and pre-rank,
so the expensive model never touches full volume (NFR-2 cost-aware).

Provider-agnostic (NFR-4): a single `_chat()` routes to whichever LLM key is
present — Gemini (the user's default) or Claude (Anthropic SDK). With no key, or
on any error, scoring falls back to `KeywordJudge`, which derives a 0-100 score
from the pre-rank so the UI always renders (NFR-3 resilient).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .models import Listing
from .profile import TasteProfile

logger = logging.getLogger(__name__)

# Cheap text tiers — the judge is a small-pool scoring task (README §8: "a
# cheaper text model for the MVP judge"). Overridable by env.
GEMINI_DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CLAUDE_DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

_GEMINI_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY", "GENAI_API_KEY")
_CLAUDE_KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")


def _env(names: tuple[str, ...]) -> str | None:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def provider() -> str:
    """Which LLM will answer: 'gemini' (default), 'claude', or 'keyword' (none).

    Honors an explicit JUDGE_PROVIDER (gemini|claude|keyword); default is "auto".
    In auto we prefer the user's chosen default (Gemini) and otherwise fall back
    to the keyword judge — we deliberately do NOT auto-select Claude from an
    ambient ANTHROPIC_API_KEY in the host environment. Claude is an explicit
    opt-in (JUDGE_PROVIDER=claude) so the app never silently uses host creds.
    """
    pref = os.environ.get("JUDGE_PROVIDER", "auto").strip().lower()
    if pref == "keyword":
        return "keyword"
    if pref == "claude":
        return "claude" if _env(_CLAUDE_KEYS) else "keyword"
    if pref == "gemini":
        return "gemini" if _env(_GEMINI_KEYS) else "keyword"
    return "gemini" if _env(_GEMINI_KEYS) else "keyword"  # auto


def _chat(prompt: str) -> str | None:
    """One-shot completion via the configured provider; None if no key present.

    Used by both the judge (score the pool) and taste extraction (read the 3
    examples) — one place to talk to an LLM.
    """
    if (key := _env(_GEMINI_KEYS)):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_DEFAULT_MODEL}:generateContent?key={key}"
        )
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                # Temperature 0 for the most stable, repeatable scoring run-to-run
                # (the scores still cluster, but the contender count stops swinging).
                "temperature": 0,
                "responseMimeType": "application/json",
                "maxOutputTokens": 8192,
                # Gemini 2.5 "thinks" by default — wasted on a scoring/classification
                # task and slow enough to time out on big batches. Turn it off.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }).encode()
        req = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=_CHAT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    if (key := _env(_CLAUDE_KEYS)):
        import anthropic  # lazy: only when Claude is the chosen provider

        msg = anthropic.Anthropic(api_key=key).messages.create(
            model=CLAUDE_DEFAULT_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return next((b.text for b in msg.content if b.type == "text"), "")
    return None


def _slice_json(text: str, open_ch: str, close_ch: str) -> str:
    start, end = text.find(open_ch), text.rfind(close_ch)
    return text[start : end + 1] if start != -1 and end != -1 else text


class KeywordJudge:
    """No LLM. Maps the pre-rank score to 0-100 so the UI always has scores.

    Reads each listing's pre-rank score/matches (set by `prerank`), not the
    profile — the `profile` arg is unused, kept for call-site symmetry."""

    kind = "keyword"

    def score(self, listings: list[Listing], profile: TasteProfile | None = None) -> None:
        if not listings:
            return
        scores = [l.prerank_score or 0.0 for l in listings]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        for l in listings:
            norm = ((l.prerank_score or 0.0) - lo) / span
            l.interest_score = int(round(40 + norm * 55))  # 40-95 band
            matched = ", ".join(l.prerank_matches[:4]) or "no strong keyword match"
            l.reason = f"keyword match: {matched}"


# Volume guard: the most listings we'll send to the LLM in one pull. At the
# measured volume (~478) everything is scored; if a future pull ever returns
# thousands, the keyword pre-rank pre-filters to this cap to bound cost (D27).
MAX_LLM_SCORE = int(os.environ.get("MAX_LLM_SCORE", "700"))
# Per-call sizing. Scoring is two passes (README §7, revised):
#   1. score ALL listings — score only, no reason → tiny output, big chunks, fast
#   2. write reasons for the CONTENDERS only → small set, one call
# This keeps "the LLM scores everything" (no keyword gatekeeping) while staying
# fast (~15-20s for ~478). Failed chunks get one retry.
_SCORE_CHUNK = 160
_REASON_CHUNK = 60
_MAX_WORKERS = 4
_CHAT_TIMEOUT = 45.0

_RUBRIC = (
    "Be DISCRIMINATING — spread the scores, don't cluster at the top:\n"
    "  90-100: standout — STRONG alignment with the taste brief: a clear collab / "
    "character / advertising dial, deadstock/NOS, or a rare/distinctive model. "
    "Reserve this for the best.\n"
    "  70-89: solidly on-taste — a named vintage model line in good shape.\n"
    "  40-69: ordinary vintage Timex, nothing special.\n"
    "  0-39: off-taste — generic/modern digital, Indiglo, strap/band, a "
    "multi-watch 'lot', OR anything matching a trait the collector has PASSED ON in "
    "the brief.\n"
    "PASSED-ON TRAITS ARE A HARD NEGATIVE — THEY OVERRIDE EVERYTHING ELSE. Read the "
    "brief's 'Passed on' / 'Not interesting' notes. If — and ONLY if — those notes "
    "name a model, dial, era, or character the collector does not want, then ANY "
    "listing with that exact trait scores 0-39 (near 0 for a clear match), EVEN IF "
    "the trait would normally be on-taste. Penalize ONLY traits the brief actually "
    "lists as passed-on — never invent a dislike, and never treat an example trait "
    "the brief lists as LIKED as if it were passed-on. Honor the stated reason for "
    "each pass and don't over-generalize past it.\n"
    "Most listings should land below 90.\n"
)


# High-signal item specifics (eBay getItem, D41) the judge weighs alongside the
# title — they map onto the brief: model line, era, movement, deadstock/box, dial.
_SPEC_KEYS = (
    "Model", "Year Manufactured", "Movement", "With Original Box/Packaging",
    "With Papers", "Reference Number", "Features", "Dial Pattern", "Dial Color",
    "Vintage", "Country of Origin", "Display", "Type", "Style",
)


def _facts(l: Listing) -> str:
    """Compact, high-signal enrichment for a listing row: curated item specifics +
    a short description snippet (empty when not enriched, e.g. fixture items)."""
    parts = []
    specs = l.item_specifics or {}
    picked = [f"{k}={specs[k]}" for k in _SPEC_KEYS if specs.get(k)]
    if picked:
        parts.append("specifics: " + "; ".join(picked))
    if l.description:
        parts.append("desc: " + l.description[:240])
    return ("  | " + " | ".join(parts)) if parts else ""


def _rows(listings: list[Listing]) -> str:
    rows = []
    for i, l in enumerate(listings):
        cond = l.raw_condition.label or "unknown"
        price = f"${l.price:.2f}" if l.price is not None else "?"
        rows.append(f"{i}. [{price}, {cond}] {l.title}{_facts(l)}")
    return "\n".join(rows)


def _score_prompt(listings: list[Listing], brief_text: str) -> str:
    return (
        "You score vintage Timex watch listings for a collector by how well each "
        "ALIGNS with this taste brief:\n\n<<<TASTE BRIEF>>>\n" + brief_text +
        "\n<<<END BRIEF>>>\n\n" + _RUBRIC +
        "\nAlso flag BROKEN: set \"broken\": true if the listing indicates the watch "
        "does NOT run / needs repair to work — e.g. 'for parts', 'for repair', "
        "'4 repair', 'runs then stops', 'run/stop', 'movement seized', 'as-is for "
        "repair', 'project watch', 'not working'. A watch that merely NEEDS A "
        "BATTERY / new battery is NOT broken (broken=false). If unclear, broken=false.\n"
        "\nEach row may include eBay item specifics (Model, Year, Movement, box/papers, "
        "dial) and a description snippet after the title — weigh these alongside the "
        "title; they often reveal the model line, era, or deadstock/boxed status.\n"
        "\nListings:\n" + _rows(listings) +
        '\n\nReturn ONLY a JSON array, one object per listing: '
        '[{"index": <int>, "score": <0-100>, "broken": <true|false>}, ...]'
    )


def _score_chunk(chunk: list[Listing], brief_text: str, detect_broken: bool = True) -> bool:
    """Pass 1: score one chunk in place (no reason). True on success.

    `detect_broken` controls the LLM not-broken backstop. Broken-ness is a property
    of the watch, NOT of the taste brief, so it's judged once per fetch (detect=True)
    and then frozen; taste re-scores (Reapply) pass detect=False and leave
    `working_status` untouched. When detect=True the flag is set AUTHORITATIVELY each
    run (broken or "unknown"), never additively — otherwise it accumulates across runs
    and the gated count drifts down (see the gate-count-drift fix)."""
    try:
        raw = _chat(_score_prompt(chunk, brief_text))
        by_index = {int(x["index"]): x for x in json.loads(_slice_json(raw or "", "[", "]"))}
        for i, listing in enumerate(chunk):
            if (x := by_index.get(i)) is not None:
                listing.interest_score = max(0, min(100, int(x["score"])))
                if detect_broken:
                    listing.working_status = "broken" if x.get("broken") is True else "unknown"
        return True
    except Exception as exc:  # noqa: BLE001 — per-chunk resilience boundary
        logger.warning("LLM score chunk failed (%s).", exc)
        return False


def _confirm_prompt(listings: list[Listing], brief_text: str) -> str:
    """Pass 2, combined: re-score AND justify each listing in ONE judgment.

    The bulk pass (pass 1) scores in big chunks with no room to justify each
    number, so it can over-score a generic listing at the margin. Here we force
    the model to commit to a score and NAME the signal that earns it in the same
    breath — so the surfaced score and its reason can't contradict (a listing it
    can only call 'generic' must also score it low, dropping it from contenders).
    """
    return (
        "You re-judge a shortlist of vintage Timex listings for a collector. For "
        "EACH listing, decide a score 0-100 against the taste brief AND name the "
        "1-3 SPECIFIC signals behind that score — the two must agree.\n\n"
        "<<<TASTE BRIEF>>>\n" + brief_text + "\n<<<END BRIEF>>>\n\n" + _RUBRIC +
        "\nThe reason must justify the score: a high score names a concrete signal "
        "(model line like Marlin/Viscount/Camper, character / advertising / novelty "
        "dial, collab, deadstock / NOS / boxed, distinctive dial like bullseye/linen, "
        "or notable condition). If the only honest reason is 'generic, no specific "
        "signal', the score MUST be below 70 — a generic watch is never a standout. "
        "Do NOT cite price or budget (every listing is already ≤ $50 — it isn't what "
        "makes one stand out). Keep the reason <= 16 words.\n\n"
        "Listings:\n" + _rows(listings) +
        '\n\nReturn ONLY a JSON array: '
        '[{"index": <int>, "score": <0-100>, "reason": "<text>"}, ...]'
    )


def _confirm_chunk(chunk: list[Listing], brief_text: str) -> bool:
    try:
        raw = _chat(_confirm_prompt(chunk, brief_text))
        by_index = {int(x["index"]): x for x in json.loads(_slice_json(raw or "", "[", "]"))}
        for i, listing in enumerate(chunk):
            if (x := by_index.get(i)) is not None:
                listing.interest_score = max(0, min(100, int(x["score"])))
                listing.reason = str(x.get("reason", "")).strip()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM confirm chunk failed (%s).", exc)
        return False


def confirm(listings: list[Listing], brief_text: str) -> None:
    """Pass 2: re-score AND reason a small candidate pool in one call, so every
    surfaced score is backed by a stated signal (NFR-1). No-op without an LLM —
    those listings keep their pass-1 score and the keyword 'matched:' line."""
    if not listings or provider() == "keyword":
        return
    chunks = [listings[i : i + _REASON_CHUNK] for i in range(0, len(listings), _REASON_CHUNK)]
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        list(pool.map(lambda c: _confirm_chunk(c, brief_text), chunks))


# Impact tokens the detail pass may use for each factor (rendered as symbol+word
# in the modal). Kept narrow so the UI mapping is total.
_IMPACTS = {"strong+", "+", "neutral", "-", "strong-"}


def _detail_rows(listings: list[Listing]) -> str:
    rows = []
    for i, l in enumerate(listings):
        cond = l.raw_condition.label or "unknown"
        price = f"${l.price:.2f}" if l.price is not None else "?"
        score = l.interest_score if l.interest_score is not None else "?"
        rows.append(f"{i}. (current score {score}) [{price}, {cond}] {l.title}{_facts(l)}")
    return "\n".join(rows)


def _detail_prompt(listings: list[Listing], brief_text: str) -> str:
    """Pass 3 (granular): break a score into weighted factors + a short narrative.

    Runs on a tiny set — the contenders up front, or one listing on demand — so we
    can afford the richer output. Each factor names a concrete taste signal and how
    much it pushed the score; the narrative says how they net out to the number.
    """
    return (
        "You explain, for a vintage Timex collector, WHY each listing earned its "
        "score against this taste brief.\n\n<<<TASTE BRIEF>>>\n" + brief_text +
        "\n<<<END BRIEF>>>\n\n" + _RUBRIC +
        "\nFor EACH listing give 2-4 FACTORS and a 2-3 sentence NARRATIVE.\n"
        "- A factor is one concrete taste signal — model line (Marlin, Viscount, "
        "Camper), character / advertising / novelty dial, collab, deadstock / NOS / "
        "boxed, distinctive dial (bullseye, linen), or condition — with an impact, "
        "one of exactly: \"strong+\", \"+\", \"neutral\", \"-\", \"strong-\".\n"
        "- The narrative must justify the listing's CURRENT score (shown per row): "
        "say how the factors net out to it. Be specific to THIS watch; don't invent "
        "details not implied by the title/condition.\n"
        "- Do NOT cite price or budget (every listing is already ≤ $50). Keep each "
        "factor signal ≤ 6 words and the narrative ≤ 55 words.\n\n"
        "Listings:\n" + _detail_rows(listings) +
        '\n\nReturn ONLY a JSON array: [{"index": <int>, "factors": [{"signal": '
        '"<text>", "impact": "<token>"}, ...], "narrative": "<text>"}, ...]'
    )


def _detail_chunk(chunk: list[Listing], brief_text: str) -> bool:
    try:
        raw = _chat(_detail_prompt(chunk, brief_text))
        by_index = {int(x["index"]): x for x in json.loads(_slice_json(raw or "", "[", "]"))}
        for i, listing in enumerate(chunk):
            if (x := by_index.get(i)) is None:
                continue
            factors = []
            for f in (x.get("factors") or [])[:4]:
                signal = str(f.get("signal", "")).strip()
                if not signal:
                    continue
                impact = str(f.get("impact", "neutral")).strip()
                factors.append({"signal": signal,
                                "impact": impact if impact in _IMPACTS else "neutral"})
            listing.score_factors = factors
            listing.score_narrative = str(x.get("narrative", "")).strip()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM detail chunk failed (%s).", exc)
        return False


def detail(listings: list[Listing], brief_text: str) -> None:
    """Pass 3: granular factor-breakdown + narrative for a tiny set (the contenders,
    or one listing on demand). No-op without an LLM."""
    if not listings or provider() == "keyword":
        return
    chunks = [listings[i : i + _REASON_CHUNK] for i in range(0, len(listings), _REASON_CHUNK)]
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        list(pool.map(lambda c: _detail_chunk(c, brief_text), chunks))


def score_all(listings: list[Listing], brief_text: str, detect_broken: bool = True) -> str:
    """Pass 1: score EVERY gated listing against the taste brief (the make-or-break).

    Scores only (reasons come later, for contenders) so output stays tiny and the
    whole set scores in a few big concurrent chunks. Any chunk that fails degrades
    to the keyword judge for those items; the whole thing degrades to keyword with
    no LLM configured (NFR-3). Returns the judge kind used.

    `detect_broken` is forwarded to the not-broken backstop: True on a fresh fetch
    (judge broken once), False on taste re-scores (Reapply) so the gated count is
    stable between fetches — broken-ness doesn't depend on the brief.
    """
    if not listings:
        return provider()
    if provider() == "keyword":
        KeywordJudge().score(listings, None)
        return "keyword"

    chunks = [listings[i : i + _SCORE_CHUNK] for i in range(0, len(listings), _SCORE_CHUNK)]

    def attempt(cs: list[list[Listing]]) -> list[tuple[list[Listing], bool]]:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            return list(pool.map(lambda c: (c, _score_chunk(c, brief_text, detect_broken)), cs))

    results = attempt(chunks)
    failed = [c for c, ok in results if not ok]
    if failed:  # one retry pass for stragglers (transient timeouts/rate limits)
        retried = attempt(failed)
        results = [(c, ok) for c, ok in results if ok] + retried

    # Backfill any listing still unscored after the retry.
    missing = [l for l in listings if l.interest_score is None]
    if missing:
        KeywordJudge().score(missing, None)
    oks = [ok for _, ok in results]
    if not any(oks):
        return "keyword (llm failed)"
    return provider() if all(oks) else f"{provider()} (partial)"


def extract_taste_keywords(example_titles: list[str]) -> dict[str, float] | None:
    """Read the 3 example watches and extract a weighted keyword profile (E3-Next).

    The README's "LLM reads the three examples and extracts a weighted, editable
    keyword profile" step. Returns {keyword: weight} or None if no LLM is
    configured / on error — callers fall back to the curated seed.
    """
    examples = "\n".join(f"- {t}" for t in example_titles if t)
    prompt = (
        "A vintage Timex collector picked these three example watches as the seed "
        "for their taste:\n" + examples + "\n\n"
        "Extract 10-20 lowercase keyword signals that capture what makes a Timex "
        "listing interesting to THIS collector (model lines, dial styles, eras, "
        "character/advertising/novelty dials, deadstock, legibility). Weight each "
        "1.0-3.0 by importance. Add a few NEGATIVE weights (-1 to -2.5) for what "
        "this collector would find dull (generic digitals, smartwatches, straps, "
        "watch lots).\n"
        'Return ONLY a JSON object: {"keyword": weight, ...}'
    )
    try:
        raw = _chat(prompt)
        if not raw:
            return None
        data = json.loads(_slice_json(raw, "{", "}"))
        return {str(k): float(v) for k, v in data.items()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Taste extraction failed (%s); using curated seed.", exc)
        return None
