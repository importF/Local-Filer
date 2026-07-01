"""Free-text album-cover search across Genius + iTunes + Deezer.

Reuses each provider's ``search`` and returns at most one cover per provider —
the three previews shown in the cover picker's online search.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ...models import STATUS_OK
from . import deezer, genius, itunes

# Order = display order of the previews.
_PROVIDERS = [genius, itunes, deezer]


@dataclass
class CoverHit:
    provider: str   # provider NAME
    label: str      # album (or title) for the hover tooltip
    cover_url: str


def search(query: str) -> list[CoverHit]:
    """Return up to 3 cover hits (one per provider) for a free-text query."""
    q = (query or "").strip()
    if not q:
        return []

    def one(provider) -> CoverHit | None:
        try:
            outcome = provider.search(None, q, None)
        except Exception:  # noqa: BLE001 - one provider must not break the search
            return None
        result = outcome.result
        if outcome.status != STATUS_OK or result is None:
            return None
        url = getattr(result, "cover_url", None)
        if not url:
            return None
        label = result.album or result.title or q
        return CoverHit(provider.NAME, label, url)

    with ThreadPoolExecutor(max_workers=len(_PROVIDERS)) as pool:
        hits = list(pool.map(one, _PROVIDERS))
    return [h for h in hits if h is not None]
