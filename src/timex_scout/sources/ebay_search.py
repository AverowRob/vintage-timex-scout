"""eBay search-URL builder — the project's encoding of *source-side* volume
control (the funnel's "box 0", added after we measured real volume).

A single "vintage timex watch" search returns 10,000+ results. We cannot — and
should not — ingest that. eBay's own native filters are the cheapest, earliest
noise cut available, so we push as much filtering as possible *into the query*
before a single listing reaches our code:

  * category = Wristwatches (31387)         — drops straps, parts lots, jewelry
  * item price <= C$50  (_udhi=50)          — the budget gate, at the source
  * condition excludes "For parts/not working" — removes structured junk

What we deliberately do NOT filter at the source (to protect recall, D16):
movement (Camper is quartz, Marlin is mechanical), gender, or anything taste-
related. Those are for the pre-rank and the LLM judge, which must still see the
listing. The source filter only removes things the deterministic gate would drop
anyway, so it is pure volume win with no recall loss.

This module is used two ways, both producing the same URLs:
  * the live eBay Browse adapter (when API creds exist) maps these filters to
    Browse API `filter=` params;
  * the browser-capture runbook (docs/data-capture.md, scripts/ebay_capture.js)
    drives these exact URLs through a real Chrome to collect a bounded, deduped
    snapshot while developer-portal access is pending.
"""

from __future__ import annotations

import urllib.parse

EBAY_CA_SEARCH = "https://www.ebay.ca/sch/i.html"

# Jewelry & Watches > Watches, Parts & Accessories > Wristwatches.
WRISTWATCH_CATEGORY = "31387"

# eBay conditionIds. We include used + new + new-other and exclude 7000 (parts).
COND_USED = "3000"
COND_NEW = "1000"
COND_NEW_OTHER = "1500"
COND_FOR_PARTS = "7000"
_DEFAULT_CONDITIONS = (COND_USED, COND_NEW, COND_NEW_OTHER)

# _sop sort codes.
_SORT = {"best_match": "12", "newly_listed": "10", "price_low": "15"}

# The taste-relevant query set we capture by default: a broad sweep plus the
# model lines that resemble the brief's ground-truth watches. Multiple targeted
# queries beat one broad query for surfacing relevant pieces; results are
# deduped by item id across all of them.
DEFAULT_QUERIES = (
    "vintage timex watch",
    "timex marlin",
    "timex viscount",
    "timex camper",
    "vintage timex automatic",
    # Ground-truth-aligned queries (the brief's taste: character/advertising dials,
    # clean legible models) so both the capture and the live API pull these styles.
    "timex easy reader",
    "timex la cell",
    "timex mickey mouse",
    "timex advertising dial",
)

# How deep to paginate the broad query. Bounded on purpose: we cap ingestion and
# let the pre-rank + LLM narrow further, rather than crawl thousands of pages.
DEFAULT_BROAD_PAGES = 4


def build_search_url(
    query: str,
    *,
    page: int = 1,
    max_price: float | None = 50,
    category: str | None = WRISTWATCH_CATEGORY,
    exclude_parts: bool = True,
    per_page: int = 60,
    sort: str = "best_match",
) -> str:
    """Build one filtered ebay.ca search URL (box 0 of the funnel)."""
    params: list[tuple[str, str]] = [("_nkw", query)]
    if category:
        params.append(("_sacat", category))
    if max_price is not None:
        params.append(("_udhi", _fmt_price(max_price)))
    if exclude_parts:
        # eBay encodes multi-condition as a pipe-joined list.
        params.append(("LH_ItemCondition", "|".join(_DEFAULT_CONDITIONS)))
    if sort in _SORT:
        params.append(("_sop", _SORT[sort]))
    params.append(("_ipg", str(per_page)))
    params.append(("_pgn", str(page)))
    return f"{EBAY_CA_SEARCH}?{urllib.parse.urlencode(params)}"


def browse_filter(max_price: float | None = 50, exclude_parts: bool = True) -> str:
    """The same box-0 filters as `build_search_url`, in eBay **Browse API** syntax.

    The live adapter passes this as the `filter` query param (and category via
    `category_ids`), so a live pull applies the identical price cap + condition
    exclusion as the browser capture. One source of truth for both paths.
    """
    parts: list[str] = []
    if max_price is not None:
        parts.append(f"price:[..{_fmt_price(max_price)}],priceCurrency:CAD")
    if exclude_parts:
        parts.append("conditionIds:{" + "|".join(_DEFAULT_CONDITIONS) + "}")
    return ",".join(parts)


def default_capture_plan() -> list[str]:
    """The default set of filtered URLs to capture (broad sweep + model lines).

    The broad query is paginated `DEFAULT_BROAD_PAGES` deep; the targeted model
    queries take page 1 only. Deduped downstream by item id.
    """
    urls = [
        build_search_url(DEFAULT_QUERIES[0], page=p)
        for p in range(1, DEFAULT_BROAD_PAGES + 1)
    ]
    urls += [build_search_url(q, page=1) for q in DEFAULT_QUERIES[1:]]
    return urls


def _fmt_price(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
