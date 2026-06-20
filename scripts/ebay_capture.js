/*
 * ebay_capture.js — the browser-side half of the eBay source while the
 * developer-portal/API is unavailable (see docs/data-capture.md).
 *
 * eBay blocks server-side/headless fetching, but a real signed-in Chrome on this
 * Mac mini loads search pages normally. This script scrapes the current eBay
 * search-results page into the eBay Browse-API record shape, so the existing
 * adapter (sources/ebay.py) normalizes it with zero code change. Results
 * accumulate in localStorage (which survives same-origin navigation), deduped by
 * item id, across the filtered queries/pages from sources/ebay_search.py.
 *
 * How it is driven (by the agent via the Claude-in-Chrome extension, or by hand
 * in DevTools): navigate to each URL from default_capture_plan(), call
 * scrapeCurrentPage() after each load, then downloadCapture() at the end. The
 * resulting JSON is copied to src/timex_scout/fixtures/ebay_sample.json.
 *
 * Productionization path: the same DOM logic can run under Playwright driving the
 * real Chrome profile (non-headless, to stay un-throttled) so the capture runs
 * unattended on the Mac mini — or, preferably, swap to the live Browse API by
 * adding credentials to .env (no code change).
 */

const CONDITION_BY_LABEL = {
  "Pre-Owned": "3000", "Pre-owned": "3000",
  "Brand New": "1000", "New": "1000",
  "New (Other)": "1500", "Open Box": "1500",
  "Parts Only": "7000", "For parts or not working": "7000",
};

const STORE_KEY = "__timex";

/** Scrape every listing card on the current page; merge into localStorage by id. */
function scrapeCurrentPage() {
  const cards = [...document.querySelectorAll("li.s-card")];
  const store = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
  let added = 0;

  for (const card of cards) {
    const a = card.querySelector('a[href*="/itm/"]');
    if (!a) continue;
    const m = a.href.match(/\/itm\/(?:[^/?#]*\/)?(\d{6,})/);
    if (!m) continue;
    const id = m[1];
    if (store[id]) continue; // dedupe across queries/pages

    const titleEl = card.querySelector(".s-card__title");
    let title = titleEl
      ? titleEl.innerText.replace(/^New Listing/i, "")
          .replace(/\s*Opens in a new window or tab\s*/i, "").trim()
      : "";
    if (!title || /Shop on eBay/i.test(title)) continue; // skip ad slots

    const priceEl = card.querySelector(".s-card__price");
    const pm = (priceEl ? priceEl.innerText : "").match(/([\d,]+\.\d{2})/);
    if (!pm) continue;

    const cond = (card.querySelector(".s-card__subtitle") || {}).innerText
      ? card.querySelector(".s-card__subtitle").innerText.trim() : "";
    const attr =
      (card.querySelector(".su-card-container__attributes__secondary") || {})
        .innerText || "";
    const shipM = attr.match(/\+?\s*C?\s*\$?([\d,]+\.\d{2})\s*shipping/i);
    const locM = attr.match(/from\s+([A-Za-z .,'-]+)/);
    const imgEl = card.querySelector("img.s-card__image, img");
    const img = imgEl
      ? (imgEl.currentSrc || imgEl.src || "")
          .replace(/s-l\d+\.(jpg|webp|png)/, "s-l500.$1")
      : "";

    const item = {
      itemId: id,
      title,
      itemWebUrl: "https://www.ebay.ca/itm/" + id, // clean URL, no tracking qs
      price: { value: pm[1].replace(/,/g, ""), currency: "CAD" },
      condition: cond || null,
      conditionId: CONDITION_BY_LABEL[cond] || null,
    };
    if (img) item.image = { imageUrl: img };
    if (locM) item.itemLocation = { country: locM[1].trim().replace(/\s+/g, " ") };
    if (shipM) item.shippingOptions = [
      { shippingCostType: "FIXED",
        shippingCost: { value: shipM[1].replace(/,/g, ""), currency: "CAD" } },
    ];

    store[id] = item;
    added++;
  }

  localStorage.setItem(STORE_KEY, JSON.stringify(store));
  return { added, total: Object.keys(store).length };
}

/** Reset the accumulator before a fresh capture run. */
function resetCapture() {
  localStorage.removeItem(STORE_KEY);
}

/** Download the accumulated store as a Browse-API-shaped fixture JSON. */
function downloadCapture(filename = "timex_ebay_capture.json") {
  const items = Object.values(JSON.parse(localStorage.getItem(STORE_KEY) || "{}"));
  const fixture = {
    href: "captured-via-browser: ebay.ca Wristwatches, price<=C$50, parts excluded",
    _note: "REAL ebay.ca listings captured through a real browser while the " +
      "developer API was pending. Source-side filters cut ~10,000+ raw to a " +
      "bounded, deduped ingest. Browse-API shape: adapter normalizes unchanged.",
    total: 10000, limit: items.length, offset: 0, itemSummaries: items,
  };
  const blob = new Blob([JSON.stringify(fixture, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
  return items.length;
}
