"""Paths and persisted settings."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Project root: two levels up in dev, or the launcher's folder when frozen.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = BASE_DIR / "Outputs"
COVERS_DIR = BASE_DIR / "Covers"
# Downloads land here first, then move to an Outputs subfolder on Save.
DOWNLOADS_DIR = BASE_DIR / "Downloads"
FFMPEG_DIR = BASE_DIR
FFMPEG_EXE = BASE_DIR / "ffmpeg.exe"
FFPROBE_EXE = BASE_DIR / "ffprobe.exe"
YT_DLP_EXE = BASE_DIR / "yt-dlp.exe"
SETTINGS_FILE = BASE_DIR / "localfiler_settings.json"


def ensure_dirs() -> None:
    """Create the Outputs, Covers, and Downloads folders."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def get_genius_token() -> str | None:
    """Genius API token from settings, falling back to an env var."""
    token = load_settings().get("genius_token")
    if token:
        return token
    return os.environ.get("GENIUS_TOKEN") or None


def set_genius_token(token: str) -> None:
    settings = load_settings()
    settings["genius_token"] = token.strip()
    save_settings(settings)


def output_subfolders() -> list[Path]:
    """Subfolders inside Outputs/, sorted by name."""
    if not OUTPUTS_DIR.exists():
        return []
    return sorted((p for p in OUTPUTS_DIR.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
