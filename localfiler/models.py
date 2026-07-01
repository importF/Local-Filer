"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderResult:
    """One metadata provider's answer for a song."""

    provider: str
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    year: Optional[str] = None
    cover_url: Optional[str] = None
    lyrics: Optional[str] = None
    bio: Optional[str] = None
    url: Optional[str] = None  # link to this source's page


STATUS_OK = "ok"              # provider returned a usable match
STATUS_NO_MATCH = "no_match"  # provider ran but found nothing
STATUS_SKIPPED = "skipped"    # provider couldn't run (e.g. no API token)
STATUS_ERROR = "error"        # provider raised / network failed


@dataclass
class ProviderOutcome:
    """What one provider did for a song. ``detail`` holds the reason for non-ok statuses."""

    provider: str
    status: str
    detail: str = ""
    result: Optional[ProviderResult] = None


@dataclass
class SongMetadata:
    """Merged metadata for a song, edited in the preview, then written to tags."""

    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    track: str = ""
    cover_url: Optional[str] = None
    cover_path: Optional[str] = None
    source_file: Optional[str] = None
    lyrics: str = ""
    bio: str = ""
    # Strip any embedded cover on save instead of adding one.
    remove_cover: bool = False
    sources: list[str] = field(default_factory=list)
    outcomes: list[ProviderOutcome] = field(default_factory=list)
