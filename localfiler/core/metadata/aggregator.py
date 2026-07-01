"""Run metadata providers concurrently and merge into one SongMetadata.

Priority: Genius -> iTunes -> Deezer -> MusicBrainz -> YouTube fallback. Each
field is filled from the highest-priority provider that supplied it. Every
provider's outcome is recorded so the preview can explain what was used.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from ...models import STATUS_ERROR, STATUS_OK, ProviderOutcome, ProviderResult, SongMetadata
from . import deezer, genius, itunes, musicbrainz, youtube

# Order matters: earlier providers win field-by-field.
_PROVIDERS = [genius, itunes, deezer, musicbrainz, youtube]

# Callback: (provider_name, status), where status is "running" then the final STATUS_*.
ProviderProgress = Callable[[str, str], None]


def gather(
    artist: str | None,
    title: str | None,
    info: dict | None = None,
    on_provider: Optional[ProviderProgress] = None,
) -> SongMetadata:
    """Query every provider concurrently, record each outcome, and merge them.

    The merge applies providers in fixed priority order, so the result is
    deterministic regardless of completion order. ``on_provider`` is called from
    worker threads, so it must be thread-safe (e.g. a Qt queued signal).
    """
    def run_one(provider) -> ProviderOutcome:
        if on_provider is not None:
            on_provider(provider.NAME, "running")
        try:
            outcome = provider.search(artist, title, info)
        except Exception as exc:  # noqa: BLE001 - unexpected provider crash
            outcome = ProviderOutcome(provider.NAME, STATUS_ERROR, f"{type(exc).__name__}: {exc}")
        if on_provider is not None:
            on_provider(outcome.provider, outcome.status)
        return outcome

    with ThreadPoolExecutor(max_workers=len(_PROVIDERS)) as pool:
        # map preserves _PROVIDERS order, so outcomes stay in priority order.
        outcomes = list(pool.map(run_one, _PROVIDERS))

    results = [o.result for o in outcomes if o.status == STATUS_OK and o.result]
    meta = _merge(results, artist, title)
    meta.outcomes = outcomes
    meta.sources = [o.provider for o in outcomes if o.status == STATUS_OK]
    return meta


def _merge(results: list[ProviderResult], artist: str | None, title: str | None) -> SongMetadata:
    def pick(field: str) -> str | None:
        for result in results:
            value = getattr(result, field)
            if value:
                return value
        return None

    return SongMetadata(
        title=pick("title") or (title or ""),
        artist=pick("artist") or (artist or ""),
        album=pick("album") or "",
        year=pick("year") or "",
        cover_url=pick("cover_url"),
        lyrics=pick("lyrics") or "",
        bio=pick("bio") or "",
    )
