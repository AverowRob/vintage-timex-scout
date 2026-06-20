"""eBay credential / connectivity check.

Run this after putting your production App ID + Cert ID into `.env`. It tells you
definitively whether the Browse API works for your keyset — distinguishing a
bad/missing credential from an approval/permission gate from a live success —
by surfacing eBay's real error body instead of degrading to the fixture.

    python scripts/check_ebay.py

Exit code 0 = live API works. Non-zero = blocked (reason printed).
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

# Make src/ importable without install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines into os.environ."""
    import os

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_dotenv(ROOT / ".env")

    from timex_scout.config import load_ebay_config
    from timex_scout.sources.ebay import EbaySource

    cfg = load_ebay_config()
    print(f"Environment : {cfg.environment}")
    print(f"Marketplace : {cfg.marketplace_id}")
    print(f"Credentials : {'present' if cfg.has_credentials else 'MISSING'}")

    if not cfg.has_credentials:
        print(
            "\n✗ No credentials in .env. Add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET "
            "(production App ID + Cert ID), then re-run."
        )
        return 2

    # fallback disabled so failures surface instead of silently using the fixture.
    src = EbaySource(cfg, fallback_to_fixture=False)

    # Step 1: OAuth app token.
    try:
        token = src._get_token()  # noqa: SLF001 — intentional for diagnostics
        print(f"\n✓ OAuth token obtained (len={len(token)}).")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"\n✗ OAuth failed: HTTP {exc.code}")
        _explain_oauth(exc.code, body)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ OAuth failed: {exc}")
        return 3

    # Step 2: a tiny real Browse search.
    try:
        page = src._search_page(token, "timex", limit=3, offset=0)  # noqa: SLF001
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"\n✗ Browse search failed: HTTP {exc.code}")
        print(_pretty(body))
        if exc.code in (401, 403):
            print(
                "\n→ Token works but this keyset can't call Browse yet. This is the "
                "approval/permission gate. Wait for approval, or build on the "
                "fixture meanwhile (the pipeline doesn't care)."
            )
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ Browse search failed: {exc}")
        return 4

    total = page.get("total", 0)
    items = page.get("itemSummaries") or []
    print(f"\n✓ LIVE eBay works. 'timex' total≈{total}; showing {len(items)}:")
    for it in items:
        price = (it.get("price") or {}).get("value")
        cur = (it.get("price") or {}).get("currency")
        print(f"   - {price} {cur}  {it.get('title', '')[:60]}")
    print("\nYou're live. The adapter will use the API automatically from now on.")
    return 0


def _explain_oauth(code: int, body: str) -> None:
    print(_pretty(body))
    if code == 400:
        print("\n→ Usually a malformed request or wrong grant type (unlikely here).")
    elif code in (401, 403):
        print(
            "\n→ Invalid credentials, or App ID/Cert ID swapped, or the keyset isn't "
            "activated yet. Double-check you copied the PRODUCTION App ID into "
            "EBAY_CLIENT_ID and Cert ID into EBAY_CLIENT_SECRET."
        )


def _pretty(body: str) -> str:
    try:
        return json.dumps(json.loads(body), indent=2)[:1200]
    except Exception:  # noqa: BLE001
        return body[:1200]


if __name__ == "__main__":
    raise SystemExit(main())
