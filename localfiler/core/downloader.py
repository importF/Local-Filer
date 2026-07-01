"""yt-dlp wrapper: list entries, download audio, and self-update.

Shells out to the bundled ``yt-dlp.exe`` (``config.YT_DLP_EXE``) rather than the
``yt_dlp`` Python package, so the same binary handles extraction, downloads, and
self-updates (``yt-dlp.exe -U``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .. import config


class YtDlpError(RuntimeError):
    """yt-dlp.exe exited non-zero or produced no usable output."""


# ------------------------------------------------------------------ subprocess
def _no_window_kwargs() -> dict:
    """Stop a console window from flashing up when the GUI shells out."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _exe() -> str:
    exe = config.YT_DLP_EXE
    if not Path(exe).exists():
        raise YtDlpError(
            f"yt-dlp.exe not found at {exe}. It should sit next to ffmpeg.exe "
            "in the app folder."
        )
    return str(exe)


def _run_text(args: list[str], timeout: int | None = 120) -> subprocess.CompletedProcess:
    """Run yt-dlp.exe and capture stdout/stderr as text."""
    return subprocess.run(
        [_exe(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **_no_window_kwargs(),
    )


def _run_json(args: list[str], timeout: int | None = 120) -> dict:
    """Run a ``-J`` command and parse the JSON object it prints."""
    proc = _run_text(args, timeout=timeout)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        raise YtDlpError((proc.stderr or "").strip() or "yt-dlp failed")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise YtDlpError((proc.stderr or "").strip() or "yt-dlp returned no JSON")


# ---------------------------------------------------------------- extraction
def extract_entries(url: str) -> list[dict]:
    """Return one info dict per video in ``url`` (one element for a single video)."""
    data = _run_json(["-J", "--flat-playlist", "--no-warnings", url])
    entries = data.get("entries")
    if entries:
        return [e for e in entries if e]
    return [data]


def search_first(query: str) -> dict | None:
    """Return the info dict for the first YouTube result for ``query``."""
    query = (query or "").strip()
    if not query:
        return None
    try:
        data = _run_json(["-J", "--no-playlist", "--no-warnings", f"ytsearch1:{query}"])
    except (YtDlpError, json.JSONDecodeError, subprocess.SubprocessError):
        return None
    entries = data.get("entries")
    if entries:
        return entries[0]
    # A bare ytsearch result can also come back as the video dict itself.
    return data if data.get("id") else None


def entry_url(entry: dict) -> str:
    """Best playable URL for a (possibly flat) playlist entry."""
    return entry.get("webpage_url") or entry.get("url") or entry.get("id") or ""


# ------------------------------------------------------------------ download
# Prefix for our --progress-template, so progress lines are easy to pick out
# from yt-dlp's other stdout (notably the final --print-json blob).
_PROG = "DLPROG"
_PROGRESS_TEMPLATE = (
    f"download:{_PROG} "
    "%(progress.status)s %(progress.downloaded_bytes)s "
    "%(progress.total_bytes)s %(progress.total_bytes_estimate)s"
)


def _parse_progress(line: str) -> dict | None:
    """Turn a ``DLPROG …`` line into the dict shape progress hooks expect."""
    parts = line.split()
    if len(parts) < 5 or parts[0] != _PROG:
        return None

    def _int(token: str) -> int | None:
        return int(token) if token.isdigit() else None

    return {
        "status": parts[1],
        "downloaded_bytes": _int(parts[2]) or 0,
        "total_bytes": _int(parts[3]),
        "total_bytes_estimate": _int(parts[4]),
    }


def download_audio(
    url: str,
    target_dir: str | Path,
    progress_cb: Callable[[dict], None] | None = None,
    audio_format: str = "mp3",
) -> tuple[Path, dict]:
    """Download audio for one link, then convert to ``audio_format``.

    "mp3"/"m4a" re-encode (or remux) into that container; "native" keeps the
    source codec with no re-encode (extension varies by site). Returns
    ``(audio_path, info_dict)``.
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    # "native" maps to ffmpeg's "best": source codec kept, no re-encode.
    codec = "best" if audio_format == "native" else audio_format

    args = [
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", codec,
        "--audio-quality", "0",
        "--ffmpeg-location", str(config.FFMPEG_DIR),
        "-o", str(target / "%(title)s.%(ext)s"),
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--progress",
        "--progress-template", _PROGRESS_TEMPLATE,
        "--print-json",
        url,
    ]

    info: dict | None = None
    proc = subprocess.Popen(
        [_exe(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_no_window_kwargs(),
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if line.startswith(_PROG):
            if progress_cb:
                parsed = _parse_progress(line)
                if parsed:
                    progress_cb(parsed)
        elif line.lstrip().startswith("{"):
            try:
                info = json.loads(line.strip())
            except json.JSONDecodeError:
                pass
    proc.wait()

    if info is None:
        raise YtDlpError(f"yt-dlp produced no output for {url} (exit {proc.returncode})")

    audio_path = _final_audio_path(info, target, codec)
    return audio_path, info


def _final_audio_path(info: dict, target: Path, codec: str) -> Path:
    """The file left on disk after the audio postprocessor ran."""
    if info.get("filepath"):
        return Path(info["filepath"])
    for req in info.get("requested_downloads") or []:
        if req.get("filepath"):
            return Path(req["filepath"])

    # Fall back to the most recently modified file in the target folder.
    candidates = [p for p in target.iterdir() if p.is_file()]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    raise YtDlpError("could not determine the downloaded file path")


# ------------------------------------------------------------------ updates
def ytdlp_version() -> str:
    """The installed yt-dlp.exe version string (e.g. ``2026.06.09``)."""
    proc = _run_text(["--version"], timeout=30)
    return (proc.stdout or "").strip() or "unknown"


def ytdlp_update() -> tuple[bool, str]:
    """Run ``yt-dlp.exe -U``. Returns ``(succeeded, combined_output)``."""
    proc = _run_text(["-U"], timeout=180)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output or "(no output)"
