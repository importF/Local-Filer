"""First-run / repair setup: fetch the external binaries the app shells out to.

``yt-dlp.exe`` / ``ffmpeg.exe`` / ``ffprobe.exe`` are gitignored, so a fresh copy
has none. This downloads them into ``config.BASE_DIR``:
- yt-dlp: the ``yt-dlp.exe`` asset from the latest GitHub release.
- ffmpeg: gyan.dev's ``ffmpeg-release-essentials.zip``, taking only ``ffmpeg.exe``
  and ``ffprobe.exe`` from its ``bin/``.

Qt-free and synchronous; ``gui.worker.SetupWorker`` runs it off the UI thread.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .. import config

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Progress callbacks: a fraction 0.0–1.0 for the current step, and a status line.
ProgressCb = Callable[[float], None]
StatusCb = Callable[[str], None]

# Component name -> the file that proves it's installed.
_COMPONENT_FILES: dict[str, Path] = {
    "yt-dlp": config.YT_DLP_EXE,
    "ffmpeg": config.FFMPEG_EXE,
    "ffprobe": config.FFPROBE_EXE,
}

_CHUNK = 64 * 1024


# --- status -----------------------------------------------------------------
def component_status() -> dict[str, bool]:
    """Map each component name to whether its binary is present."""
    return {name: path.exists() for name, path in _COMPONENT_FILES.items()}


def missing_components() -> list[str]:
    """Names of the components whose binaries are absent, in display order.

    Startup treats a non-empty result as a hard gate: ``main.py`` runs the
    mandatory setup and refuses to launch until this is empty.
    """
    return [name for name, present in component_status().items() if not present]


def install_plan() -> list[str]:
    """The download tasks needed for the missing components (ffmpeg+ffprobe share one zip)."""
    missing = set(missing_components())
    tasks: list[str] = []
    if "yt-dlp" in missing:
        tasks.append("yt-dlp")
    if missing & {"ffmpeg", "ffprobe"}:
        tasks.append("ffmpeg")
    return tasks


# --- download helpers -------------------------------------------------------
def _mb_text(label: str, got: int, total: int) -> str:
    """A human-readable "label 42.1 / 109.7 MB" (or just "42.1 MB" if size unknown)."""
    mb = got / 1_048_576
    if total:
        return f"{label} {mb:.1f} / {total / 1_048_576:.1f} MB"
    return f"{label} {mb:.1f} MB"


def _download(url: str, dest: Path, progress_cb: ProgressCb | None,
              status_cb: StatusCb | None = None, label: str | None = None) -> None:
    """Stream ``url`` to ``dest``, reporting 0.0–1.0 progress as bytes arrive.

    Writes to a ``.part`` file and atomically renames, so a half-finished
    download never masquerades as a valid binary. When ``status_cb`` + ``label``
    are given, also emits a live "X / Y MB" status (throttled) so a long download
    visibly keeps moving.
    """
    tmp = dest.with_name(dest.name + ".part")
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        last_status = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                got += len(chunk)
                if total and progress_cb:
                    progress_cb(got / total)
                # Refresh the byte counter roughly every 1 MB.
                if status_cb and label and got - last_status >= 1_048_576:
                    last_status = got
                    status_cb(_mb_text(label, got, total))
    if progress_cb:
        progress_cb(1.0)
    if status_cb and label:
        status_cb(_mb_text(label, total or got, total))
    os.replace(tmp, dest)


def _extract_ffmpeg(zip_path: Path) -> None:
    """Pull ``ffmpeg.exe`` + ``ffprobe.exe`` out of the build zip's bin/."""
    wanted = {"ffmpeg.exe": config.FFMPEG_EXE, "ffprobe.exe": config.FFPROBE_EXE}
    found: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            normalized = member.replace("\\", "/")
            leaf = normalized.rsplit("/", 1)[-1]
            if leaf in wanted and "/bin/" in normalized:
                with zf.open(member) as src, open(wanted[leaf], "wb") as dst:
                    shutil.copyfileobj(src, dst)
                found.add(leaf)
    missing = set(wanted) - found
    if missing:
        raise RuntimeError(
            f"ffmpeg archive did not contain {', '.join(sorted(missing))}"
        )


# --- installers -------------------------------------------------------------
def install_ytdlp(progress_cb: ProgressCb | None = None,
                  status_cb: StatusCb | None = None) -> None:
    if status_cb:
        status_cb("Downloading yt-dlp…")
    config.BASE_DIR.mkdir(parents=True, exist_ok=True)
    _download(YTDLP_URL, config.YT_DLP_EXE, progress_cb, status_cb, "Downloading yt-dlp…")


def install_ffmpeg(progress_cb: ProgressCb | None = None,
                   status_cb: StatusCb | None = None) -> None:
    if status_cb:
        status_cb("Downloading ffmpeg…")
    config.BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_zip = config.BASE_DIR / "_ffmpeg_download.part.zip"
    try:
        _download(FFMPEG_URL, tmp_zip, progress_cb, status_cb, "Downloading ffmpeg…")
        if status_cb:
            # Big archive → the unzip alone takes a few seconds with no byte
            # progress; say so (the dialog's spinner shows it's still alive).
            status_cb("Extracting ffmpeg… (this can take a moment)")
        _extract_ffmpeg(tmp_zip)
    finally:
        try:
            tmp_zip.unlink()
        except FileNotFoundError:
            pass


def run_task(task: str, progress_cb: ProgressCb | None = None,
             status_cb: StatusCb | None = None) -> None:
    """Run one install task by name (``"yt-dlp"`` or ``"ffmpeg"``)."""
    if task == "yt-dlp":
        install_ytdlp(progress_cb, status_cb)
    elif task == "ffmpeg":
        install_ffmpeg(progress_cb, status_cb)
    else:
        raise ValueError(f"unknown setup task: {task!r}")
