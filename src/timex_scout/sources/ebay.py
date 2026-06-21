"""eBay source adapter (E1, README §7 box 1).

Pulls active Timex listings from the eBay Browse API (ebay.ca, CAD) and
normalizes each into a `Listing`. Two responsibilities only — pull and
normalize; gating, ranking and judging all live downstream so this stays a
thin, swappable converter (NFR-4).

Auth is the client-credentials "app token" flow: the application authenticates
as itself, no per-user login (README §8). Resilience (NFR-3): any failure —
missing credentials, auth error, network error — degrades to the bundled
offline fixture (or, if disabled, an empty list) so a run never crashes.

Uses only the standard library so the source layer runs with zero install.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..config import SHIP_TO_POSTAL, EbayConfig, load_ebay_config
from ..models import Listing, RawCondition
from .base import Source
from .ebay_search import DEFAULT_QUERIES, WRISTWATCH_CATEGORY, browse_filter

logger = logging.getLogger(__name__)

_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "ebay_sample.json"

# eBay Browse caps a page at 200; we paginate up to the caller's limit.
_MAX_PAGE_SIZE = 200
# Cap items per query in the live plan, so one broad query can't crowd out the
# taste-aligned ones before dedup (mirrors the ~60-120/query browser capture).
_PER_QUERY_CAP = 120
# conditionId 7000 == "For parts or not working". Captured for the gate, not
# filtered here (the gate is the deterministic not-broken step, FR-3).
_NOT_WORKING_CONDITION_ID = "7000"

# Full-detail enrichment (D41): after the summary search, call getItem on EVERY
# listing to pull the seller's description + structured item specifics (Model, Year,
# Movement, box/papers…), which the taste judge then scores on. One API call per
# listing — a deliberately slower, higher-accuracy Fetch (the user fetches rarely).
_ENRICH = os.environ.get("EBAY_ENRICH", "1").strip().lower() not in ("0", "false", "no")
_ENRICH_WORKERS = int(os.environ.get("EBAY_ENRICH_WORKERS", "10"))
_DESC_MAX = 1500  # store a cleaned snippet; the modal shows it, the judge a slice of it


class EbaySource(Source):
    """Live eBay Browse adapter with an offline fixture fallback."""

    name = "ebay"
    display = "eBay"

    @property
    def configured(self) -> bool:
        return self.config.has_credentials

    def __init__(
        self,
        config: EbayConfig | None = None,
        *,
        fallback_to_fixture: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.config = config or load_ebay_config()
        self.fallback_to_fixture = fallback_to_fixture
        self.timeout = timeout
        self._token: str | None = None
        # How the last fetch got its data: "live" (Browse API) or "fixture"
        # (cached snapshot). Surfaced in the UI so the user knows if Re-pull was
        # a real live pull or a re-run on cached data.
        self.last_mode: str = "fixture"
        self.last_error: str | None = None

    # --- Public API --------------------------------------------------------

    def fetch(self, query: str = "timex", limit: int = 200) -> list[Listing]:
        """Return up to `limit` active listings, normalized. Never raises."""
        if not self.config.has_credentials:
            logger.warning(
                "eBay credentials not set (EBAY_CLIENT_ID/EBAY_CLIENT_SECRET); "
                "using offline fixture."
            )
            self.last_mode, self.last_error = "fixture", "no API credentials"
            return self._fixture_listings(limit)

        try:
            summaries = self._search_plan(limit)
            if _ENRICH and summaries:
                self._enrich(summaries)            # full description + item specifics (D41)
        except Exception as exc:  # noqa: BLE001 — resilience boundary (NFR-3)
            logger.warning("eBay fetch failed (%s); degrading gracefully.", exc)
            self.last_mode, self.last_error = "fixture", str(exc)
            return self._fixture_listings(limit)

        listings = [self._normalize(item) for item in summaries]
        if not listings and self.fallback_to_fixture:
            # A live pull that returns nothing (e.g. sandbox, which has no real
            # Timex inventory) should not blank the demo — fall back to the fixture
            # and flag it, so the source dot stays "wired" (yellow), not a live 0.
            logger.info("eBay live pull returned 0 listings; using fixture.")
            self.last_mode = "fixture"
            self.last_error = "live source returned 0 listings (e.g. sandbox)"
            return self._fixture_listings(limit)
        logger.info("eBay: fetched %d live listings.", len(listings))
        self.last_mode, self.last_error = "live", None
        return listings

    # --- HTTP plumbing -----------------------------------------------------

    def _get_token(self) -> str:
        """Fetch (and cache) a client-credentials app token."""
        if self._token:
            return self._token

        creds = f"{self.config.client_id}:{self.config.client_secret}".encode()
        auth = base64.b64encode(creds).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": _OAUTH_SCOPE}
        ).encode()
        req = urllib.request.Request(
            self.config.oauth_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("eBay OAuth response missing access_token")
        self._token = token
        return token

    def _search_plan(self, limit: int) -> list[dict[str, Any]]:
        """Run the taste-aligned query plan with box-0 filters; dedupe by id.

        Mirrors the browser capture: the same `DEFAULT_QUERIES` and the same
        source-side filters (category, price cap, exclude-parts), so a live pull
        yields the same bounded, relevant, deduped pool the fixture does."""
        token = self._get_token()
        seen: set[str] = set()
        collected: list[dict[str, Any]] = []
        for query in DEFAULT_QUERIES:
            if len(collected) >= limit:
                break
            for item in self._search_query(token, query, min(_PER_QUERY_CAP, limit)):
                item_id = item.get("itemId")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                collected.append(item)
                if len(collected) >= limit:
                    break
        return collected

    def _enrich(self, summaries: list[dict[str, Any]]) -> None:
        """getItem EVERY summary in parallel and fold description + item specifics +
        the full image set back into the dict (D41). Best-effort per item: a failed
        getItem (or a rate-limit hit) just leaves that listing on summary data."""
        token = self._get_token()

        def one(item: dict[str, Any]) -> None:
            full = self._get_item(token, item.get("itemId", ""))
            if not full:
                return
            item["description"] = full.get("description")
            item["localizedAspects"] = full.get("localizedAspects")
            # getItem carries the complete gallery; prefer it over the summary's.
            if full.get("image"):
                item["image"] = full["image"]
            if full.get("additionalImages"):
                item["additionalImages"] = full["additionalImages"]

        with ThreadPoolExecutor(max_workers=_ENRICH_WORKERS) as pool:
            list(pool.map(one, summaries))
        enriched = sum(1 for s in summaries if s.get("localizedAspects") is not None)
        logger.info("eBay: enriched %d/%d listings with full details.", enriched, len(summaries))

    def _get_item(self, token: str, item_id: str) -> dict[str, Any] | None:
        """Fetch one item's full details (getItem). None on any error (resilience)."""
        if not item_id:
            return None
        url = (
            f"{self.config.api_host}/buy/browse/v1/item/"
            f"{urllib.parse.quote(item_id, safe='')}"
        )
        req = urllib.request.Request(
            url, method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": self.config.marketplace_id,
                "X-EBAY-C-ENDUSERCTX": _ship_ctx(self.config.marketplace_id),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 — per-item best-effort
            logger.debug("getItem failed for %s (%s).", item_id, exc)
            return None

    def _search_query(self, token: str, query: str, cap: int) -> list[dict[str, Any]]:
        """Page through one query (with filters) up to `cap` items."""
        out: list[dict[str, Any]] = []
        offset = 0
        while len(out) < cap:
            page_size = min(_MAX_PAGE_SIZE, cap - len(out))
            page = self._search_page(token, query, page_size, offset)
            items = page.get("itemSummaries") or []
            if not items:
                break
            out.extend(items)
            offset += page_size
            if offset >= page.get("total", 0):
                break
        return out

    def _search_page(
        self, token: str, query: str, limit: int, offset: int
    ) -> dict[str, Any]:
        # Box 0 (source-side filters) applied server-side, identical to the
        # capture: Wristwatches category, item price <= cap, exclude For-Parts.
        params = urllib.parse.urlencode({
            "q": query,
            "category_ids": WRISTWATCH_CATEGORY,
            "filter": browse_filter(),
            "limit": limit,
            "offset": offset,
        })
        req = urllib.request.Request(
            f"{self.config.browse_search_url}?{params}",
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": self.config.marketplace_id,
                "X-EBAY-C-ENDUSERCTX": _ship_ctx(self.config.marketplace_id),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    # --- Normalization (the converter, README §7) --------------------------

    def _normalize(self, item: dict[str, Any]) -> Listing:
        """Convert one eBay item summary into a `Listing`."""
        price_obj = item.get("price") or {}
        price = _to_float(price_obj.get("value"))

        condition_id = _str_or_none(item.get("conditionId"))
        raw_condition = RawCondition(
            label=item.get("condition"),
            condition_id=condition_id,
            description=item.get("subtitle") or item.get("shortDescription"),
        )

        return Listing(
            source=self.name,
            id=str(item.get("itemId", "")),
            url=item.get("itemWebUrl", ""),
            title=item.get("title", ""),
            price=price,
            currency=price_obj.get("currency"),
            raw_condition=raw_condition,
            item_location=_format_location(item.get("itemLocation")),
            images=_collect_images(item),
            seller=(item.get("seller") or {}).get("username"),
            listing_end=item.get("itemEndDate"),
            listed_at=item.get("itemCreationDate"),     # in the summary — no extra call
            shipping_cost=_shipping_cost(item),         # to the ship-to location (FR-2)
            description=_clean_description(item.get("description")),
            item_specifics=_collect_specifics(item.get("localizedAspects")),
            raw=item,
        )

    # --- Fixture fallback --------------------------------------------------

    def _fixture_listings(self, limit: int) -> list[Listing]:
        if not self.fallback_to_fixture:
            return []
        data = self.load_fixture()
        items = (data.get("itemSummaries") or [])[:limit]
        return [self._normalize(item) for item in items]

    @staticmethod
    def load_fixture() -> dict[str, Any]:
        """The bundled offline sample Browse response (for dev and tests)."""
        with _FIXTURE_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)


# --- Small pure helpers ----------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _ship_ctx(marketplace_id: str) -> str:
    """X-EBAY-C-ENDUSERCTX value so eBay quotes shipping to the ship-to location
    (the basis for the brief's total-cost budget). EBAY_CA → country=CA."""
    country = marketplace_id.replace("EBAY_", "") or "CA"
    return f"contextualLocation=country={country},zip={SHIP_TO_POSTAL}"


def _shipping_cost(item: dict[str, Any]) -> float | None:
    """The cheapest shipping option's cost to the ship-to location, or None if eBay
    returned no shipping figure (local pickup / no calculated quote). Free shipping
    is 0.0, not None."""
    costs: list[float] = []
    for opt in item.get("shippingOptions") or []:
        value = (opt.get("shippingCost") or {}).get("value")
        cost = _to_float(value)
        if cost is not None:
            costs.append(cost)
    return min(costs) if costs else None


def _collect_images(item: dict[str, Any]) -> list[str]:
    """Hero image first, then any additional gallery images (URLs only)."""
    images: list[str] = []
    hero = (item.get("image") or {}).get("imageUrl")
    if hero:
        images.append(hero)
    for extra in item.get("additionalImages") or []:
        url = extra.get("imageUrl")
        if url:
            images.append(url)
    return images


def _format_location(loc: dict[str, Any] | None) -> str | None:
    if not loc:
        return None
    parts = [loc.get("city"), loc.get("stateOrProvince"), loc.get("country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_description(desc: str | None) -> str | None:
    """eBay descriptions are HTML; strip tags + entities to plain text, collapse
    whitespace, and truncate to a stored snippet."""
    if not desc:
        return None
    text = _TAG_RE.sub(" ", desc)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:_DESC_MAX].rstrip()


def _collect_specifics(aspects: Any) -> dict[str, str]:
    """getItem's localizedAspects -> {name: value} (the structured item specifics)."""
    out: dict[str, str] = {}
    for a in aspects or []:
        name, value = a.get("name"), a.get("value")
        if name and value:
            out[str(name)] = str(value)
    return out
