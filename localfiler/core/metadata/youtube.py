"""Download-source provider — built from the yt-dlp info dict.

Works for any yt-dlp source. Downloads pass the info dict in directly; the Tag
Folder flow has none, so it searches YouTube for the first matching result.
Reported as "source".
"""

from __future__ import annotations

from ...models import (
    STATUS_NO_MATCH,
    STATUS_OK,
    ProviderOutcome,
    ProviderResult,
)
from .. import downloader
from ..filename_parser import clean

NAME = "source"


def _best_thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    if thumbs:
        # yt-dlp lists thumbnails worst-to-best; take the last.
        return thumbs[-1].get("url")
    return None


def search(artist: str | None, title: str | None, info: dict | None = None) -> ProviderOutcome:
    if not info:
        # Tag Folder: no download info, so search YouTube for the first result.
        query = " ".join(p for p in (title, artist) if p).strip()
        if not query:
            return ProviderOutcome(NAME, STATUS_NO_MATCH, "nothing to search")
        info = downloader.search_first(query)
        if not info:
            return ProviderOutcome(NAME, STATUS_NO_MATCH, f"no YouTube result for '{query}'")

    yt_title = info.get("track") or clean(info.get("title") or "") or title
    yt_artist = info.get("artist") or info.get("creator") or info.get("uploader") or artist
    year = (info.get("upload_date") or "")[:4] or None

    page_url = info.get("webpage_url")
    if not page_url and info.get("id"):
        page_url = f"https://www.youtube.com/watch?v={info['id']}"

    result = ProviderResult(
        provider=NAME,
        title=yt_title,
        artist=yt_artist,
        album=info.get("album"),
        year=year,
        cover_url=_best_thumbnail(info),
        url=page_url,
    )
    return ProviderOutcome(NAME, STATUS_OK, "", result)
