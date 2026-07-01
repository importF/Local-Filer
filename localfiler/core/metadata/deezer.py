"""Deezer API provider (no key required)."""

from __future__ import annotations

import requests

from ...models import (
    STATUS_ERROR,
    STATUS_NO_MATCH,
    STATUS_OK,
    ProviderOutcome,
    ProviderResult,
)

NAME = "deezer"
_ENDPOINT = "https://api.deezer.com/search"


def search(artist: str | None, title: str | None, info: dict | None = None) -> ProviderOutcome:
    query = " ".join(p for p in (artist, title) if p).strip()
    if not query:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no artist/title to search")

    try:
        resp = requests.get(_ENDPOINT, params={"q": query, "limit": 1}, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except requests.RequestException as exc:
        return ProviderOutcome(NAME, STATUS_ERROR, str(exc))

    if not data:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no Deezer match")

    track = data[0]
    album = track.get("album") or {}
    cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")

    result = ProviderResult(
        provider=NAME,
        title=track.get("title"),
        artist=(track.get("artist") or {}).get("name"),
        album=album.get("title"),
        year=None,  # Deezer search doesn't return a year
        cover_url=cover,
        url=track.get("link"),
    )
    return ProviderOutcome(NAME, STATUS_OK, "", result)
