"""The source contract.

A `Source` pulls active listings from one marketplace and returns them already
normalized into `Listing` records. Keeping this interface tiny is what makes
NFR-4 (Extensible) true: a new marketplace implements `fetch` and nothing
downstream changes. Resilience (NFR-3) is part of the contract — `fetch` should
degrade gracefully (return what it can, ideally `[]`) rather than raise, so one
source failing never breaks the run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Listing


class Source(ABC):
    #: Short stable identifier, written into every Listing.source it emits.
    name: str = "source"

    @abstractmethod
    def fetch(self, query: str, limit: int) -> list[Listing]:
        """Return up to `limit` active listings for `query`, normalized.

        Implementations should not raise on network/auth failure; log and
        return the best available result (degrade gracefully, NFR-3).
        """
        raise NotImplementedError
