"""Etsy source adapter (E1 Next — the second marketplace, D3).

Pulls active vintage-Timex listings from the Etsy Open API v3 and normalizes each
into the shared `Listing` (NFR-4), so everything downstream treats it exactly
like an eBay listing. Public listing search uses keystring auth (the `x-api-key`
header) — no per-user OAuth needed.

**Status: keys requested, pending Etsy review.** This adapter is wired and ready;
until the key is approved, live calls return 401/403 and it degrades to an empty
result (NFR-3) — so it never breaks a pull, and the source dot shows "wired"
(yellow) rather than "live" (green). The moment the key is approved, Re-pull
returns Etsy listings with no code change.

Uses only the standard library (like the eBay adapter), so the source layer stays
install-free.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..config import EtsyConfig, load_etsy_config
from ..models import Listing, RawCondition
from .base import Source

logger = logging.getLogger(__name__)

# Etsy caps a page at 100; we paginate up to the caller's limit.
_MAX_PAGE_SIZE = 100
# Item price cap, mirroring box 0 (the gate re-checks deterministically). Note:
# Etsy prices may be USD/EUR, not CAD — FX normalization is deferred (D19/D28).
_MAX_PRICE = 50


class EtsySource(Source):
    """Etsy Open API v3 adapter (keystring auth), with graceful degradation."""

    name = "etsy"
    display = "Etsy"

    def __init__(self, config: EtsyConfig | None = None, *, timeout: float = 20.0) -> None:
        self.config = config or load_etsy_config()
        self.timeout = timeout
        # "live" (API returned data), "wired" (configured but call failed / pending),
        # or "off" (no key). Surfaced as the source dot's colour.
        self.last_mode: str = "off"
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return self.config.has_credentials

    # --- Public API --------------------------------------------------------

    def fetch(self, query: str = "vintage timex", limit: int = 200) -> list[Listing]:
        """Return up to `limit` active Etsy listings, normalized. Never raises."""
        if not self.configured:
            self.last_mode, self.last_error = "off", "no API key"
            return []
        try:
            results = self._search_all(query, limit)
        except urllib.error.HTTPError as exc:
            # 401/403 while the key is pending review is the expected state.
            self.last_mode = "wired"
            self.last_error = f"HTTP {exc.code} (key pending/unauthorized?)"
            logger.warning("Etsy fetch not live yet (%s).", self.last_error)
            return []
        except Exception as exc:  # noqa: BLE001 — resilience boundary (NFR-3)
            self.last_mode, self.last_error = "wired", str(exc)
            logger.warning("Etsy fetch failed (%s).", exc)
            return []

        listings = [self._normalize(item) for item in results]
        logger.info("Etsy: fetched %d live listings.", len(listings))
        self.last_mode, self.last_error = "live", None
        return listings

    # --- HTTP plumbing -----------------------------------------------------

    def _search_all(self, query: str, limit: int) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        offset = 0
        while len(collected) < limit:
            page_size = min(_MAX_PAGE_SIZE, limit - len(collected))
            page = self._search_page(query, page_size, offset)
            results = page.get("results") or []
            if not results:
                break
            collected.extend(results)
            offset += page_size
            if offset >= page.get("count", 0):
                break
        return collected[:limit]

    def _search_page(self, query: str, limit: int, offset: int) -> dict[str, Any]:
        params = urllib.parse.urlencode({
            "keywords": query,
            "limit": limit,
            "offset": offset,
            "max_price": _MAX_PRICE,
            "sort_on": "score",
            "includes": "Images",
        })
        req = urllib.request.Request(
            f"{self.config.active_listings_url}?{params}",
            method="GET",
            headers={"x-api-key": self.config.keystring or "", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    # --- Normalization (the converter, README §7) --------------------------

    def _normalize(self, item: dict[str, Any]) -> Listing:
        price_obj = item.get("price") or {}
        price = _etsy_price(price_obj)
        images = [
            img.get("url_570xN") or img.get("url_fullxN")
            for img in (item.get("images") or [])
            if img.get("url_570xN") or img.get("url_fullxN")
        ]
        return Listing(
            source=self.name,
            id=str(item.get("listing_id", "")),
            url=item.get("url", ""),
            title=item.get("title", ""),
            price=price,
            currency=price_obj.get("currency_code"),
            # Etsy has no structured condition field; the gate scans the title.
            raw_condition=RawCondition(description=item.get("description")),
            item_location=None,
            images=images,
            seller=_str_or_none(item.get("shop_id")),
            listing_end=item.get("ending_tsz") and str(item["ending_tsz"]),
            raw=item,
        )


def _etsy_price(price_obj: dict[str, Any]) -> float | None:
    """Etsy prices are {amount, divisor, currency_code} → amount / divisor."""
    amount, divisor = price_obj.get("amount"), price_obj.get("divisor")
    if amount is None or not divisor:
        return None
    try:
        return float(amount) / float(divisor)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None
