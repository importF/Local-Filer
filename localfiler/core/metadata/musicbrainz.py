"""MusicBrainz provider (fallback). Cover art via the Cover Art Archive."""

from __future__ import annotations

import musicbrainzngs

from ...models import (
    STATUS_ERROR,
    STATUS_NO_MATCH,
    STATUS_OK,
    ProviderOutcome,
    ProviderResult,
)

NAME = "musicbrainz"

musicbrainzngs.set_useragent("LocalFiler", "1.0", "https://github.com/local/localfiler")


def search(artist: str | None, title: str | None, info: dict | None = None) -> ProviderOutcome:
    if not title:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no title to search")

    try:
        result = musicbrainzngs.search_recordings(
            recording=title, artist=artist or "", limit=1
        )
    except Exception as exc:  # noqa: BLE001 - network/parse error
        return ProviderOutcome(NAME, STATUS_ERROR, str(exc))

    recordings = result.get("recording-list") or []
    if not recordings:
        return ProviderOutcome(NAME, STATUS_NO_MATCH, "no MusicBrainz match")

    rec = recordings[0]
    album = None
    year = None
    cover = None

    releases = rec.get("release-list") or []
    if releases:
        release = releases[0]
        album = release.get("title")
        year = (release.get("date") or "")[:4] or None
        mbid = release.get("id")
        if mbid:
            # May 404 for releases without art; covers.get_or_download handles that.
            cover = f"https://coverartarchive.org/release/{mbid}/front-500"

    rec_id = rec.get("id")
    result_obj = ProviderResult(
        provider=NAME,
        title=rec.get("title"),
        artist=rec.get("artist-credit-phrase"),
        album=album,
        year=year,
        cover_url=cover,
        url=f"https://musicbrainz.org/recording/{rec_id}" if rec_id else None,
    )
    return ProviderOutcome(NAME, STATUS_OK, "", result_obj)
