"""Genius provider — the primary metadata source.

Pulls title, primary artist, release year, album, and song-art image. Requires a
Genius API token; without one this provider is skipped.
"""

from __future__ import annotations

import re

import lyricsgenius

from ... import config
from ...models import (
    STATUS_ERROR,
    STATUS_NO_MATCH,
    STATUS_OK,
    STATUS_SKIPPED,
    ProviderOutcome,
    ProviderResult,
)

NAME = "genius"

_client: "lyricsgenius.Genius | None" = None
_token_used: str | None = None


def _get_client():
    global _client, _token_used
    token = config.get_genius_token()
    if not token:
        return None
    if _client is None or _token_used != token:
        _client = lyricsgenius.Genius(
            token,
            remove_section_headers=True,
            skip_non_songs=True,
            timeout=12,
            retries=1,
        )
        if hasattr(_client, "verbose"):
            _client.verbose = False
        _token_used = token
    return _client


_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# Year tied to when the song was made (preferred over a leak date).
_RECORDED_RE = re.compile(
    r"(?:record(?:ed)?|made|created|produced|written|cut|laid down)\b[^.]{0,40}?(19\d{2}|20\d{2})",
    re.IGNORECASE,
)


def _structured_year(data: dict) -> str | None:
    components = data.get("release_date_components") or {}
    if components.get("year"):
        return str(components["year"])
    for key in ("release_date", "release_date_for_display"):
        match = _YEAR_RE.search(str(data.get(key) or ""))
        if match:
            return match.group(1)
    return None


def _bio_text(data: dict) -> str:
    desc = data.get("description")
    if isinstance(desc, dict):
        return desc.get("plain") or ""
    return str(desc or "")


def _year_from_bio(text: str) -> str | None:
    """Best-guess recording year from a Genius bio (e.g. 'recorded in 2016')."""
    if not text:
        return None
    recorded = _RECORDED_RE.search(text)
    if recorded:
        return recorded.group(1)
    years = _YEAR_RE.findall(text)
    if years:
        # A recording usually predates its leak, so take the earliest year.
        return min(years)
    return None


def search(artist: str | None, title: str | None, info: dict | None = None) -> ProviderOutcome:
    if not title:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no title to search")

    client = _get_client()
    if client is None:
        return ProviderOutcome(NAME, STATUS_SKIPPED, "no Genius token set")

    # Use the search + song-detail endpoints directly; search_song() also
    # scrapes the lyrics page, which we don't need and is slower in bulk.
    query = f"{title} {artist}".strip() if artist else title
    try:
        hits = (client.search_songs(query) or {}).get("hits") or []
    except Exception as exc:  # noqa: BLE001 - network/parse error
        return ProviderOutcome(NAME, STATUS_ERROR, f"{type(exc).__name__}: {exc}")

    if not hits:
        who = f"{artist} - {title}" if artist else title
        return ProviderOutcome(NAME, STATUS_NO_MATCH, f"no Genius result for '{who}'")

    song_id = hits[0]["result"]["id"]
    try:
        data = (client.song(song_id, text_format="plain") or {}).get("song") or {}
    except Exception as exc:  # noqa: BLE001 - network/parse error
        return ProviderOutcome(NAME, STATUS_ERROR, f"{type(exc).__name__}: {exc}")

    bio = _bio_text(data)
    year = _structured_year(data) or _year_from_bio(bio)

    album = None
    album_data = data.get("album")
    if isinstance(album_data, dict):
        album = album_data.get("name")

    cover = data.get("song_art_image_url") or data.get("header_image_url")

    result = ProviderResult(
        provider=NAME,
        title=data.get("title"),
        artist=(data.get("primary_artist") or {}).get("name"),
        album=album,
        year=year,
        cover_url=cover,
        lyrics=None,  # not fetched
        bio=bio or None,
        url=data.get("url"),
    )
    return ProviderOutcome(NAME, STATUS_OK, "", result)
