"""Configuration: hard-coded MVP constants and credentials read from the
environment.

Per the brief this is a single-user tool (README §4 Non-Goals), so the few
"settings" that exist are constants here, not user-facing config. Secrets
(eBay app keys, Anthropic key) come from the environment / a `.env` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --- Hard gate (FR-3) ------------------------------------------------------
# The MVP gates on item price only, from a single CAD source (README §3, D28).
PRICE_CAP_CAD: float = 50.0

# --- eBay source (E1) ------------------------------------------------------
# ebay.ca, prices in CAD (D19). Marketplace id drives the Browse API.
EBAY_MARKETPLACE_ID: str = "EBAY_CA"
DEFAULT_QUERY: str = "timex"

# Hard-coded ship-to postal code — the only sensitive field, and it is a
# constant (README §4: no auth needed). Unused until the deferred shipping
# feature (E9 / D28); recorded here so landed-cost work has one source of truth.
SHIP_TO_POSTAL: str = "M6K1V8"

# eBay API hosts by environment.
_EBAY_HOSTS = {
    "production": "https://api.ebay.com",
    "sandbox": "https://api.sandbox.ebay.com",
}


@dataclass(frozen=True)
class EbayConfig:
    """eBay credentials + environment, loaded from the process environment."""

    client_id: str | None
    client_secret: str | None
    environment: str = "production"
    marketplace_id: str = EBAY_MARKETPLACE_ID

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def api_host(self) -> str:
        return _EBAY_HOSTS.get(self.environment, _EBAY_HOSTS["production"])

    @property
    def oauth_url(self) -> str:
        return f"{self.api_host}/identity/v1/oauth2/token"

    @property
    def browse_search_url(self) -> str:
        return f"{self.api_host}/buy/browse/v1/item_summary/search"


@dataclass(frozen=True)
class EtsyConfig:
    """Etsy Open API v3 credentials (keystring auth for public listing search)."""

    keystring: str | None
    shared_secret: str | None

    @property
    def has_credentials(self) -> bool:
        # Public listing search needs only the keystring (x-api-key); the shared
        # secret is for OAuth flows we don't use yet.
        return bool(self.keystring)

    @property
    def active_listings_url(self) -> str:
        return "https://openapi.etsy.com/v3/application/listings/active"


def load_etsy_config() -> EtsyConfig:
    return EtsyConfig(
        keystring=os.environ.get("ETSY_KEYSTRING") or os.environ.get("ETSY_API_KEY") or None,
        shared_secret=os.environ.get("ETSY_SHARED_SECRET") or None,
    )


def load_dotenv(path: str | None = None) -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines into os.environ.

    Lets the user paste an API key into .env and have it picked up, without
    exporting shell vars. Existing environment variables win (never overridden).
    """
    from pathlib import Path

    env_path = Path(path) if path else Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def load_ebay_config() -> EbayConfig:
    """Build the eBay config from environment variables (see .env.example)."""
    return EbayConfig(
        client_id=os.environ.get("EBAY_CLIENT_ID") or None,
        client_secret=os.environ.get("EBAY_CLIENT_SECRET") or None,
        environment=os.environ.get("EBAY_ENV", "production").strip().lower(),
        marketplace_id=EBAY_MARKETPLACE_ID,
    )
