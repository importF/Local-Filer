"""iTunes Search API provider (no key required)."""

from __future__ import annotations

import requests

from ...models import (
    STATUS_ERROR,
    STATUS_NO_MATCH,
    STATUS_OK,
    ProviderOutcome,
    ProviderResult,
)

NAME = "itunes"
_ENDPOINT = "https://itunes.apple.com/search"


def search(artist: str | None, title: str | None, info: dict | None = None) -> ProviderOutcome:
    term = " ".join(p for p in (artist, title) if p).strip()
    if not term:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no artist/title to search")

    try:
        resp = requests.get(
            _ENDPOINT,
            params={"term": term, "entity": "song", "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except requests.RequestException as exc:
        return ProviderOutcome(NAME, STATUS_ERROR, str(exc))

    if not results:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no iTunes match")

    track = results[0]
    artwork = (track.get("artworkUrl100", "") or "").replace("100x100bb", "600x600bb")
    year = (track.get("releaseDate") or "")[:4]

    result = ProviderResult(
        provider=NAME,
        title=track.get("trackName"),
        artist=track.get("artistName"),
        album=track.get("collectionName"),
        year=year or None,
        cover_url=artwork or None,
        url=track.get("trackViewUrl"),
    )
    return ProviderOutcome(NAME, STATUS_OK, "", result)
