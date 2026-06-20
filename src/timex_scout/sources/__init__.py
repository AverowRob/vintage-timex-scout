"""Source adapters. Each emits the shared `Listing` record (NFR-4), so adding a
marketplace later is one small converter and nothing else changes.
"""

from .base import Source

__all__ = ["Source"]
