"""Tests for the taste profile mechanics and the LLM extraction fallback."""

import os

from timex_scout.judge import extract_taste_keywords
from timex_scout.profile import GROUND_TRUTH_EXAMPLES, TasteProfile


def test_seed_reflects_ground_truth_taste():
    p = TasteProfile.seed()
    # The corrected seed (grounded in the fetched ground-truth watches) weights
    # character/advertising dials and Easy Reader / La Cell positively, and does
    # not penalize quartz.
    for kw in ("character dial", "advertising", "easy reader", "la cell"):
        assert p.weights.get(kw, 0) > 0
    assert "quartz" not in p.weights  # quartz is not a negative


def test_seed_includes_brief_and_gt3_signals():
    p = TasteProfile.seed()
    # The brief's explicit "interesting" quote: collabs, deadstock, vintage models.
    assert p.weights.get("collab", 0) >= 3.0
    assert p.weights.get("deadstock", 0) >= 2.0
    # GT#3 (Etsy Marlin): the distinctive bullseye dial.
    assert p.weights.get("bullseye", 0) >= 2.0


def test_negative_keywords_surface_junk():
    negs = TasteProfile.seed().negative_keywords()
    assert "lot of" in negs and "smartwatch" in negs


def test_merge_weights_takes_max():
    p = TasteProfile.seed()
    before = p.weights["marlin"]
    p.merge_weights({"marlin": before - 5, "obscure": 9.0})
    assert p.weights["marlin"] == before          # never weakened
    assert p.weights["obscure"] == 9.0             # new signal added


def test_brief_grows_and_shrinks_with_likes_and_dislikes():
    from timex_scout.taste import TasteBrief
    b = TasteBrief.seed()
    b.add_liked("Timex Marlin NOS")
    assert "Timex Marlin NOS" in b.text
    b.add_disliked("Timex Ironman Digital", "too modern, sporty")
    assert "Timex Ironman Digital — too modern, sporty" in b.text
    assert "Passed on" in b.text
    b.remove_disliked("Timex Ironman Digital")
    assert "Timex Ironman Digital" not in b.text
    b.remove_liked("Timex Marlin NOS")
    assert "Timex Marlin NOS" not in b.text


def test_three_ground_truth_examples_present():
    assert len(GROUND_TRUTH_EXAMPLES) == 3
    assert any("La Cell" in e["title"] for e in GROUND_TRUTH_EXAMPLES)


def test_extraction_returns_none_without_key(monkeypatch):
    # No LLM key configured -> extraction declines, caller uses curated seed.
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY",
              "GENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert extract_taste_keywords(["Timex Marlin", "Timex La Cell"]) is None
