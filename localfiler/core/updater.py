"""Self-update: check GitHub releases, download + stage a new build, then hand
off to a detached batch script that replaces the running install in place.

Only meaningful in the packaged (frozen) app — running from source has no exe
to replace. Qt-free and synchronous; ``gui.worker`` runs it off the UI thread.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .. import config
from .._version import GITHUB_REPO, __version__

ProgressCb = Callable[[float], None]
StatusCb = Callable[[str], None]

_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_CHUNK = 64 * 1024


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def current_version() -> str:
    return __version__


def _parse_version(text: str) -> tuple[int, ...]:
    """"v1.2.3" -> (1, 2, 3); non-numeric parts count as 0."""
    text = text.strip().lstrip("vV")
    parts = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def check_latest() -> tuple[str, str] | None:
    """Return ``(tag, zip_download_url)`` for the latest release, or None if it has no zip asset."""
    resp = requests.get(_API_LATEST, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    tag = (data.get("tag_name") or "").strip()
    assets = data.get("assets") or []
    # Prefer an asset that looks Windows-specific; fall back to any zip.
    zip_asset = next(
        (a for a in assets if a.get("name", "").lower().endswith(".zip")
         and "windows" in a["name"].lower()),
        None,
    )
    if zip_asset is None:
        zip_asset = next((a for a in assets if a.get("name", "").lower().endswith(".zip")), None)
    if not tag or zip_asset is None:
        return None
    return tag, zip_asset["browser_download_url"]


def download_and_stage(url: str, progress_cb: ProgressCb | None = None,
                        status_cb: StatusCb | None = None) -> Path:
    """Download the release zip and extract it. Returns the extracted app folder."""
    stage_root = Path(tempfile.mkdtemp(prefix="LocalFilerUpdate_"))
    zip_path = stage_root / "update.zip"

    if status_cb:
        status_cb("Downloading update…")
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(zip_path, "wb") as fh:
            for chunk in resp.iter_content(_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                got += len(chunk)
                if total and progress_cb:
                    progress_cb(got / total * 0.9)  # last 10% reserved for extraction

    if status_cb:
        status_cb("Extracting…")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(stage_root)
    zip_path.unlink(missing_ok=True)
    if progress_cb:
        progress_cb(1.0)

    # The release zip's top level *is* the app folder: the exe plus _internal/.
    return stage_root


# A separate process does the actual file swap, since the running exe and its
# loaded _internal/ DLLs can't be overwritten while this process is alive.
_APPLY_SCRIPT = """@echo off
setlocal
set "PID={pid}"
set "SRC={src}"
set "DEST={dest}"
set "EXE={exe}"
set "STAGE_ROOT={stage_root}"

:wait
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)

REM Only replace the app's own files (exe + _internal/); leave user data
REM (Outputs, Covers, Downloads, settings, ffmpeg/yt-dlp) untouched.
robocopy "%SRC%\\_internal" "%DEST%\\_internal" /MIR /R:5 /W:2 /NFL /NDL /NJH /NJS >nul
robocopy "%SRC%" "%DEST%" "%EXE%" /R:5 /W:2 /NFL /NDL /NJH /NJS >nul

start "" "%DEST%\\%EXE%"

rmdir /s /q "%STAGE_ROOT%" >nul 2>nul
del "%~f0"
"""


def write_apply_script(staged_dir: Path) -> Path:
    """Write the detached updater script (waits for this process, then swaps files)."""
    if not is_frozen():
        # config.BASE_DIR is the source tree in dev mode, not an install dir —
        # never let robocopy /MIR point at it.
        raise RuntimeError("Self-update only runs in the packaged app.")
    bat_path = Path(tempfile.gettempdir()) / "LocalFilerApplyUpdate.bat"
    content = _APPLY_SCRIPT.format(
        pid=os.getpid(),
        src=str(staged_dir),
        dest=str(config.BASE_DIR),
        exe=Path(sys.executable).name,
        stage_root=str(staged_dir.parent),
    )
    bat_path.write_text(content, encoding="utf-8")
    return bat_path


def launch_apply_and_exit(bat_path: Path) -> None:
    """Launch the updater script detached; the caller must quit the app right after."""
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        ),
        close_fds=True,
    )
