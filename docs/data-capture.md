# Data capture: getting real eBay listings without API access

The production data path is the **eBay Browse API** (add credentials to `.env`,
no code change). Until developer-portal access is granted, we collect a real,
bounded snapshot by driving a **real Chrome browser** on the Mac mini — which
eBay serves normally, unlike blocked server-side/headless requests.

This is the legitimate workaround for eBay's bot detection: it isn't evasion,
it's using an actual signed-in browser session. We do **not** use proxy
rotation, fingerprint spoofing, or CAPTCHA solving.

## The volume problem (why filters come first)

A single "vintage timex watch" search returns **10,000+** results. We cannot
ingest that, and most of it is junk (parts lots, straps, off-taste digitals).
So the funnel gains a **box 0: source-side filtering** — push eBay's own native
filters into the query before anything reaches our code:

| Filter | eBay param | Effect |
|---|---|---|
| Category = Wristwatches | `_sacat=31387` | drops straps, parts kits, jewelry |
| Item price ≤ C$50 | `_udhi=50` | the budget gate, at the source |
| Exclude "For parts / not working" | `LH_ItemCondition=3000\|1000\|1500` | removes structured junk |

We deliberately do **not** filter movement, gender, or anything taste-related at
the source — that would hurt recall (D16). Box 0 only removes what the
deterministic gate would drop anyway. The gate still earns its place: ~28 of the
captured "Pre-Owned/New" listings carry broken text ("FOR PARTS OR REPAIR",
"Not Working") that eBay's structured filter misses — the gate's keyword layer
catches those.

The filter logic lives in code: [`sources/ebay_search.py`](../src/timex_scout/sources/ebay_search.py)
(`build_search_url`, `default_capture_plan`).

## Procedure

1. Open eBay in a real, signed-in Chrome on the Mac mini.
2. Load [`scripts/ebay_capture.js`](../scripts/ebay_capture.js) (DevTools console,
   or it is injected by the agent via the Claude-in-Chrome extension).
3. `resetCapture()` once to clear the accumulator.
4. For each URL from `default_capture_plan()` (broad query × 4 pages + one page
   each for marlin / camper / viscount / automatic): navigate, then
   `scrapeCurrentPage()`. Results dedupe by item id in `localStorage`.
5. `downloadCapture()` → `~/Downloads/timex_ebay_capture.json`.
6. Copy into place:
   ```sh
   cp ~/Downloads/timex_ebay_capture.json src/timex_scout/fixtures/ebay_sample.json
   ```

The adapter ([`sources/ebay.py`](../src/timex_scout/sources/ebay.py)) reads this
file whenever credentials are absent, normalizing it exactly as it would live API
results. Last capture: **409 unique listings**, all with real images, parts
excluded at source.

## Refreshing / scaling

Re-run the procedure anytime for fresh listings, or widen `DEFAULT_QUERIES` /
`DEFAULT_BROAD_PAGES` in `ebay_search.py` for more volume. Each page is ~60
listings; dedupe makes overlapping queries safe.

## Productionization

- **Preferred:** eBay Browse API — drop credentials in `.env`, zero code change.
- **Unattended browser:** the same DOM logic can run under Playwright driving the
  real Chrome profile (non-headless, to stay un-throttled) as a scheduled job on
  the Mac mini.
