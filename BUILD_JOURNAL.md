# Build Journal — Vintage Timex Scout

The build story: the main problems we hit and how we solved them, roughly in order.
The [README](README.md) is the plan (the *what* and *why*); this is the *how it went* —
the key changes and the reasoning behind them. The full decision log is in README Appendix A.

## The decisions that shaped it most

1. **Let the AI score every listing against an editable taste brief, not a keyword shortcut.** Once volume measured manageable and the AI was fast and cheap, this was more accurate — and "taste" became a plain-English document the collector owns and edits, instead of a hidden formula.
2. **Turning off the AI's "thinking" made it ~6× faster** (~90s → ~15s) — which is what made "score everything" practical in the first place.
3. **A browser-capture workaround kept us moving while eBay's API approval was stuck**, behind an adapter built so the eventual switch to the live feed was a one-line change.
4. **Editing taste is free; re-scoring is one deliberate click** — cost is driven by *how often* you re-score, not the price of each.
5. **The judge has to show its work** — a deterministic gate plus an AI broken-check keep broken watches out, and every score is stated with its reason in the same breath.

---

## Phase 1 — Real data and a working pipeline

**Scaffold + eBay adapter.** Built the `src/` layout, the shared `Listing` schema, and the eBay Browse adapter (OAuth app-token, paginated search, normalize). The source layer is standard-library only, so the make-or-break ranking work was never blocked on dependency installs.

**eBay API approval blocked the live feed.** Production access needed approval (days out, past the deadline), and the developer portal login failed entirely. Rather than stall, we made the project independent of live eBay: the adapter degrades to a bundled fixture, and going live later is a credentials swap with zero code change.

**Captured real listings through the browser.** Server-side fetches were what eBay throttled; a real signed-in Chrome loads pages normally. We scraped ~400 genuine ebay.ca listings — real titles, prices, conditions, photos — into the same Browse-API shape the adapter already normalizes. A legitimate, portal-free way to get real data (no proxies or CAPTCHA-solving).

**Volume is 10,000+ — so filtering starts in the query.** A broad "vintage timex" search returns over 10,000 results. We push eBay's own filters into the search URL — category, price ≤ $50, exclude for-parts — collapsing it to a bounded, deduped ~400-listing ingest before anything reaches our code. It only removes what the gate would drop anyway, so no recall is lost.

**Full pipeline + UI, end-to-end on real data.** Gate → rank → judge → present → learn, with a FastAPI + server-rendered HTML front end. A few Python 3.14 web-stack snags (a missing form-parsing package, a template-cache crash, a framework signature change) — all confined to the web tier, exactly as the standard-library source layer was meant to ensure.

**Ground-truth correction.** The taste seed was guessing at a "mechanical Marlin collector." Fetching the brief's three actual example watches revealed a quirkier, character-dial-leaning taste where quartz is fine, and the seed was recalibrated — guessing the ground truth would have made the core ranking subtly wrong.

## Phase 2 — The core pivot: an AI judging against a taste brief

**Keywords → the AI scores everything.** The original design used keyword matching to pre-rank and only sent a small pool to the AI. Once volume measured low and the judge was fast and cheap, the keyword proxy was both unnecessary and less accurate (keywords miss meaning — an unlisted collab, "deadstock" phrased ten ways). The AI now scores every gated listing; keywords survive only as a no-AI fallback.

**Taste became an editable brief.** Taste is no longer hidden keyword weights but a plain-English markdown **brief** the collector reads and edits — the inspectable rubric, in prose. This is the heart of the product.

**Performance: thinking off.** The model "thinks" by default, which is wasted on bulk scoring and slow enough to time out. Disabling it cut a full score from ~90s to ~15s — the single biggest speed lever, and what made scoring everything feasible. (Setting temperature to zero also stabilized run-to-run counts.)

## Phase 3 — Making the judge trustworthy and explainable

**Two-layer not-broken.** Broken watches were slipping into the contenders — "Runs 4 Repair", "run/stop" — phrasings the deterministic keyword gate missed. Fixed with two layers: an expanded deterministic gate, plus an AI "broken" flag in the scoring pass that catches the long tail. A broken watch is now removed entirely.

**Score and reason can't contradict each other.** A generic watch was scoring 90 with the reason "generic, no specific signals" — because the score and the reason came from two separate AI calls. Fixed by having the surfaced score and its reason come from the *same* self-justifying call, so the AI can't rate something highly while describing it as generic.

**Explainability at two levels.** Every contender shows the specific signals behind its score (e.g. "NOS, boxed, advertising dial" — never the price, which is already gated), and Manage Scout carries a "How a score is decided" panel with the 0–100 bands. The detail view adds a weighted factor breakdown and a short narrative, computed for contenders and on demand for anything else.

**Stable counts.** The "passed the gate" count was drifting down on every re-score, because the AI broken-flag was sticky and accumulated on reused listings. Fixed by judging broken-ness once per fetch and freezing it — it's a property of the watch, not the taste — so re-scoring taste no longer changes the gate count.

**Passed-on traits override taste.** A downvote now strongly penalizes the matching trait into the off-taste band, even one that's normally on-taste: pass on Mickey Mouse and every Mickey watch drops, while other character dials stay high. One gotcha caught in testing — a concrete example placed *inside* the AI's rubric got read as a real rule, so examples are now kept out of the instruction.

## Phase 4 — The learning loop and controlling cost

**Two-directional, reasoned learning.** Liking a watch adds it as a positive reference (with an optional note on *what* you like); downvoting adds a soft, editable "passed on" note (with an optional *why*). Both are managed on a dedicated Manage Scout tab, and neither ever hides a listing — a low score sits at the bottom of "view all", not gone.

**Editing taste is free; re-scoring is batched.** Re-scoring all ~500 listings on every like or dislike was the dominant cost. Now edits just update the brief and queue; you click **Reapply** once to re-score the whole batch — turning a curation session of dozens of changes into a single re-score (~40× cheaper). A banner shows what's pending, and a loading overlay plus a success toast give clear feedback that something happened and finished.

## Phase 5 — Going live on the real eBay API

**eBay production approved → live.** The required account-deletion exemption was auto-granted, the production keyset went in, and a Fetch now pulls ~670 real Timex listings — through the exact same pipeline, zero code change, just as the adapter was designed for. The browser-capture fixture is now the fallback, not the source.

**Photo gallery.** Live listings carry multiple photos, so the detail view became a gallery (large image plus a thumbnail strip) — real value for judging a watch by eye.

**Full-detail enrichment.** The summary search gives only the title; eBay's full-item endpoint also returns the description and structured specifics (model, year, movement, box/papers). We now enrich every listing and feed those high-signal facts to the judge, so it scores on real evidence, not just the title.

**Pull cache (quota guard).** Restarts and code reloads reuse the last fetch instead of re-hitting the API. Combined with batched re-scoring, this means iterating on scores never spends API quota — only the explicit "Fetch Listings" button pulls from eBay.

## Phase 6 — Polish, QA, and ship

**UI refinements.** A "Liked" funnel view; sort that applies on change and persists across pages; a Max-price filter; card heart/✕ icons you can click to undo; a title that reflects the current view; one-shot toasts instead of sticky banners; and source dots that show true live / wired / off status (including after a cache-load restart). Etsy is wired as a second source, pending its own approval.

**End-to-end QA.** A full pass confirmed every functional and non-functional requirement is met — the gate and the AI scoring actually exceed the original plan. The build plan was reconciled to what shipped, the most consequential decisions were flagged, and the most verbose user-facing copy was trimmed.

---

## Bug ledger

Everything hit and fixed, by area:

- **Performance / AI:** scoring timeouts → turned off the model's thinking; a wrong model id → corrected; unstable run-to-run counts → temperature set to zero; an ambient API key silently hijacking the judge → explicit opt-in only.
- **Gate / scoring:** broken watches reaching contenders → two-layer not-broken check; "90 · generic" contradiction → score and reason from one self-justifying call; gate count drift → broken judged once per fetch and frozen; a rubric example read as a rule → examples kept out of the instruction.
- **Environment:** a source dot read wrong because config loaded too late → load order fixed; three Python 3.14 web-stack snags → patched; an env var cached across reloads → full restart; a test accidentally hitting the network → pinned to an offline config.
- **UI:** a silent ~30s re-score → loading overlay + success toast; sort not applying until "Apply" → applies on change; un-clickable like/pass icons → clickable toggles; a sticky "Saved" message reappearing on every view switch → one-shot toast; wrong title per view → view-aware title; source dot stale after restart → status restored from the cache.
