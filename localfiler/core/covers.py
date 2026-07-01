"""Download, cache, and dedupe album covers in the Covers/ folder.

Cached files are named ``<Artist - Album>__<hash>.<ext>``, where the hash comes
from the image URL (or its bytes). Keying on the image means a chosen cover is
saved and embedded as-is, while identical images download only once.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

import requests

from .. import config

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# Hosts that serve YouTube thumbnails / channel art — embedded but not cached.
_YT_THUMB_HOSTS = ("ytimg.com", "youtube.com", "ggpht.com")


def is_youtube_thumb(url: str | None) -> bool:
    return bool(url) and any(host in url for host in _YT_THUMB_HOSTS)


def _ext_for(content_type: str) -> str:
    content_type = (content_type or "").lower()
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def _ext_for_bytes(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def sanitize(name: str) -> str:
    name = _ILLEGAL.sub("", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120].strip(" .")


def cache_key(artist: str, album: str, title: str) -> str:
    if artist and album:
        base = f"{artist} - {album}"
    elif album:
        base = album
    elif artist and title:
        base = f"{artist} - {title}"
    else:
        base = title or "cover"
    return sanitize(base) or "cover"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def display_name(path: str | Path) -> str:
    """Human-readable label for a cached cover (drops the ``__hash`` suffix)."""
    stem = Path(path).stem
    return stem.rsplit("__", 1)[0] if "__" in stem else stem


def _existing_with_suffix(suffix: str) -> Path | None:
    """Find a cached image whose stem ends with ``__<suffix>`` (any extension)."""
    if not config.COVERS_DIR.exists():
        return None
    for path in config.COVERS_DIR.iterdir():
        if (
            path.is_file()
            and path.suffix.lower() in _IMG_EXTS
            and path.stem.endswith(f"__{suffix}")
        ):
            return path
    return None


def cached_for_url(cover_url: str) -> Path | None:
    """The cached file for this exact image URL, if already downloaded."""
    return _existing_with_suffix(_hash(cover_url))


def list_saved() -> list[Path]:
    """Every cached cover image, newest first."""
    if not config.COVERS_DIR.exists():
        return []
    images = [
        p for p in config.COVERS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMG_EXTS
    ]
    return sorted(images, key=lambda p: p.stat().st_mtime, reverse=True)


def get_or_download(cover_url: str | None, artist: str, album: str, title: str) -> Path | None:
    """Return a local cover path for ``cover_url``, downloading it if needed."""
    if not cover_url:
        return None
    config.ensure_dirs()

    existing = cached_for_url(cover_url)
    if existing:
        return existing

    try:
        resp = requests.get(cover_url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    ext = _ext_for(resp.headers.get("Content-Type", ""))
    dest = config.COVERS_DIR / f"{cache_key(artist, album, title)}__{_hash(cover_url)}{ext}"
    dest.write_bytes(resp.content)
    return dest


def save_bytes(data: bytes, artist: str, album: str, title: str) -> Path | None:
    """Cache raw image ``data`` (keyed by the bytes) and return its path."""
    if not data:
        return None
    config.ensure_dirs()
    suffix = "b" + _hash(data.decode("latin-1"))
    existing = _existing_with_suffix(suffix)
    if existing:
        return existing
    dest = config.COVERS_DIR / f"{cache_key(artist, album, title)}__{suffix}{_ext_for_bytes(data)}"
    dest.write_bytes(data)
    return dest


def download_temp(cover_url: str | None) -> Path | None:
    """Download a cover to a throwaway temp file (not cached). Caller deletes it."""
    if not cover_url:
        return None
    try:
        resp = requests.get(cover_url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    fd, name = tempfile.mkstemp(suffix=_ext_for(resp.headers.get("Content-Type", "")))
    with os.fdopen(fd, "wb") as handle:
        handle.write(resp.content)
    return Path(name)
