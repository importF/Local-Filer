"""Guess (artist, title) from messy filenames and YouTube video titles."""

from __future__ import annotations

import re
from pathlib import Path

# Parenthetical / bracketed junk common in YouTube titles.
_JUNK_PATTERNS = [
    r"\((?:official\s*)?(?:music\s*)?video\)",
    r"\(official\s*audio\)",
    r"\(official\s*(?:music\s*)?(?:video|audio|visualizer|lyric\s*video)\)",
    r"\(lyrics?\)",
    r"\(lyric\s*video\)",
    r"\(audio\)",
    r"\(visuali[sz]er\)",
    r"\(prod\.?[^)]*\)",
    r"\(unreleased[^)]*\)",
    r"\(extended[^)]*\)",
    r"\(mastered[^)]*\)",
    r"\[[^\]]*\]",            # anything in square brackets
    r"\bofficial\s*(?:music\s*)?video\b",
    r"\bunreleased\b",
    r"\bofficial\s*audio\b",
    r"\bfresh\s*leak\b",
    r"\bHD\b",
    r"\b4K\b",
    r"\blyrics?\b",
    r"-\s*SoundLoadMate\.com",
    r"ssstik\.io[_\d]*",
    r"☆|♫|★|☽|✩|✨",  # decorative unicode stars/notes
]

_JUNK_RE = re.compile("|".join(_JUNK_PATTERNS), re.IGNORECASE)
# Leading track number like "3. ", "05 ", "12) ", "07-" followed by more text.
_TRACK_NUM_RE = re.compile(r"^\s*\d{1,3}[\.\)\-_\s]+(?=\S)")
# Empty or asterisk-only brackets left behind after junk removal.
_EMPTY_BRACKETS_RE = re.compile(r"\(\s*\*?\s*\)|\[\s*\*?\s*\]")


def clean(text: str) -> str:
    """Strip junk markers, leftover empty brackets, and collapse whitespace."""
    text = _JUNK_RE.sub(" ", text)
    text = text.replace("｜", " ").replace("|", " ")
    text = _EMPTY_BRACKETS_RE.sub(" ", text)
    text = text.replace("*", " ")
    text = _EMPTY_BRACKETS_RE.sub(" ", text)  # again, in case a star unwrapped one
    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return text.strip()


def parse(filename: str) -> tuple[str | None, str]:
    """Return a best-guess ``(artist, title)``; artist may be ``None``."""
    stem = Path(filename).stem
    stem = _TRACK_NUM_RE.sub("", stem)
    cleaned = clean(stem)

    # Split on the first " - " separator if present.
    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        artist = left.strip()
        title = right.strip()
        if artist and title:
            return artist, title

    return None, cleaned
