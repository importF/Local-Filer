"""Background QThread workers so the UI never freezes during network I/O.

Both tagging workers produce a list of ``TaggingJob``; the main window then runs
the preview/save loop on the UI thread.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core import downloader, setup, updater
from ..core.filename_parser import parse
from ..core.metadata import aggregator
from ..core.tagging import read_cover, read_tags
from ..models import SongMetadata


@dataclass
class TaggingJob:
    path: Path
    metadata: SongMetadata
    # Original yt-dlp info dict (downloads only), for re-searching in the preview.
    info: dict | None = None
    # Downloads: where the file moves on Save. None means save in place (Tag Folder).
    target_dir: Path | None = None
    # True if just downloaded into Downloads/, so it's discarded on skip/cancel.
    is_download: bool = False
    # The file's pre-existing tags + cover (Tag Folder), shown as the Original column.
    original: dict | None = None
    original_cover: bytes | None = None


class SearchWorker(QThread):
    """Re-run the metadata lookup for one song with user-corrected terms."""

    done = Signal(object)  # emits a fresh SongMetadata
    failed = Signal(str)

    def __init__(self, artist: str, title: str, info: dict | None = None):
        super().__init__()
        self.artist = artist
        self.title = title
        self.info = info

    def run(self) -> None:
        try:
            meta = aggregator.gather(self.artist or None, self.title or None, self.info)
            self.done.emit(meta)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class YtDlpCheckWorker(QThread):
    """Check the installed yt-dlp.exe version against the latest GitHub release."""

    done = Signal(str, str)  # (current_version, latest_version_or_empty)
    failed = Signal(str)

    def run(self) -> None:
        try:
            current = downloader.ytdlp_version()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        latest = ""
        try:
            import requests

            resp = requests.get(
                "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                timeout=10,
            )
            resp.raise_for_status()
            latest = (resp.json().get("tag_name") or "").strip()
        except Exception:  # noqa: BLE001 - offline / rate-limited -> unknown latest
            latest = ""
        self.done.emit(current, latest)


class YtDlpUpdateWorker(QThread):
    """Run ``yt-dlp.exe -U`` off the UI thread."""

    done = Signal(bool, str)  # (succeeded, combined_output)

    def run(self) -> None:
        ok, output = downloader.ytdlp_update()
        self.done.emit(ok, output)


class AppUpdateCheckWorker(QThread):
    """Check GitHub for a newer Local Filer release."""

    done = Signal(str, str)  # (tag, download_url) - empty tag means up to date
    failed = Signal(str)

    def run(self) -> None:
        try:
            result = updater.check_latest()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        tag, url = result if result else ("", "")
        self.done.emit(tag, url)


class AppUpdateDownloadWorker(QThread):
    """Download + stage a new Local Filer release; the caller applies it after."""

    status = Signal(str)
    progress_value = Signal(float)  # overall fraction 0.0-1.0
    done = Signal(str)              # staged app folder path
    failed = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self) -> None:
        try:
            staged = updater.download_and_stage(
                self.url, progress_cb=self.progress_value.emit, status_cb=self.status.emit
            )
            self.done.emit(str(staged))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class SetupWorker(QThread):
    """Download the missing external binaries off the UI thread.

    Runs each task in sequence, mapping its byte-level fraction into one
    continuous 0.0–1.0 overall value. A per-task failure is isolated and the
    failed task names are reported via ``done`` (an empty list means success).
    """

    status = Signal(str)
    progress_value = Signal(float)  # overall fraction 0.0-1.0
    done = Signal(list)             # task names that failed (empty == success)

    def __init__(self, tasks: list[str]):
        super().__init__()
        self.tasks = list(tasks)

    def run(self) -> None:
        total = len(self.tasks) or 1
        failed: list[str] = []
        for i, task in enumerate(self.tasks):
            def progress_cb(frac, idx=i):
                self.progress_value.emit((idx + frac) / total)

            self.progress_value.emit(i / total)
            try:
                setup.run_task(task, progress_cb=progress_cb,
                               status_cb=self.status.emit)
            except Exception as exc:  # noqa: BLE001 - isolate per-task failures
                failed.append(task)
                self.status.emit(f"{task} failed: {exc}")
            self.progress_value.emit((i + 1) / total)
        self.done.emit(failed)


class DownloadWorker(QThread):
    """Download (and convert) every video in a URL, then fetch metadata."""

    progress = Signal(str)
    progress_value = Signal(float)  # overall fraction 0.0-1.0
    finished_jobs = Signal(list)
    failed = Signal(str)

    def __init__(self, url: str, target_dir: Path, audio_format: str = "mp3",
                 number_tracks: bool = False):
        super().__init__()
        self.url = url
        self.target_dir = target_dir
        self.audio_format = audio_format
        self.number_tracks = number_tracks

    def run(self) -> None:
        from .. import config

        jobs: list[TaggingJob] = []
        try:
            config.ensure_dirs()
            entries = downloader.extract_entries(self.url)
            total = len(entries)
            self.progress.emit(f"Found {total} item(s) to download.")
            self.progress_value.emit(0.0)

            for i, entry in enumerate(entries):
                title = entry.get("title", "?")
                self.progress.emit(f"[{i + 1}/{total}] Downloading: {title}")
                video_url = downloader.entry_url(entry)

                # Download fills the first 60% of this song's slice; metadata the rest.
                def hook(d, idx=i):
                    if d.get("status") == "downloading":
                        size = d.get("total_bytes") or d.get("total_bytes_estimate")
                        frac = (d.get("downloaded_bytes", 0) / size) if size else 0.0
                        self.progress_value.emit((idx + 0.6 * frac) / total)
                    elif d.get("status") == "finished":
                        self.progress_value.emit((idx + 0.6) / total)

                try:
                    mp3_path, info = downloader.download_audio(
                        video_url, config.DOWNLOADS_DIR, progress_cb=hook,
                        audio_format=self.audio_format,
                    )
                except Exception as exc:  # noqa: BLE001 - keep going on per-item failure
                    self.progress.emit(f"[{i + 1}/{total}] FAILED to download {title}: {exc}")
                    self.progress_value.emit((i + 1) / total)
                    continue

                self.progress.emit(f"[{i + 1}/{total}] Looking up metadata...")
                self.progress_value.emit((i + 0.7) / total)
                artist, parsed_title = parse(mp3_path.name)
                meta = aggregator.gather(artist, parsed_title, info)
                meta.source_file = str(mp3_path)
                # Album/playlist (opt-in): seed the track # from the entry position
                # so a failed item doesn't shift the rest. Off for single downloads.
                if self.number_tracks and total > 1:
                    meta.track = str(i + 1)
                jobs.append(
                    TaggingJob(
                        path=mp3_path,
                        metadata=meta,
                        info=info,
                        target_dir=self.target_dir,
                        is_download=True,
                    )
                )
                self.progress_value.emit((i + 1) / total)

            self.progress.emit("Done fetching. Opening preview...")
            self.finished_jobs.emit(jobs)
        except Exception as exc:  # noqa: BLE001 - surface fatal errors to the UI
            self.failed.emit(str(exc))


class ScanWorker(QThread):
    """Scan a folder of audio files and fetch metadata for each, in parallel.

    Several files are looked up at once (each file's providers also run
    concurrently). Fine-grained signals drive the progress window; ``finished_jobs``
    still arrives in the original file order.
    """

    # Files looked up at once. Each fans out to ~5 provider threads.
    MAX_WORKERS = 4

    progress = Signal(str)
    progress_value = Signal(float)  # overall fraction 0.0-1.0
    total_known = Signal(int)
    file_started = Signal(int, str)          # (index, filename)
    provider_update = Signal(int, str, str)  # (index, provider, status)
    file_done = Signal(int)                  # (index)
    finished_jobs = Signal(list)
    failed = Signal(str)

    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths = list(paths)

    def _process(self, index: int, path: Path) -> TaggingJob:
        self.file_started.emit(index, path.name)
        existing = read_tags(path)
        artist, title = parse(path.name)
        # Existing tags guide the search, but the merged result stays "from
        # source"; the file's own tags show in the Original column.
        search_artist = existing.get("artist") or artist
        search_title = existing.get("title") or title or path.stem
        meta = aggregator.gather(
            search_artist, search_title, None,
            on_provider=lambda prov, status: self.provider_update.emit(index, prov, status),
        )
        meta.source_file = str(path)
        return TaggingJob(
            path=path,
            metadata=meta,
            original=existing,
            original_cover=read_cover(path),
        )

    def run(self) -> None:
        try:
            files = self.paths
            total = len(files)
            self.total_known.emit(total)
            self.progress.emit(f"Found {total} file(s) to tag.")
            self.progress_value.emit(0.0)
            if total == 0:
                self.finished_jobs.emit([])
                return

            jobs_by_index: dict[int, TaggingJob] = {}
            done = 0
            workers = min(self.MAX_WORKERS, total)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._process, i, p): i
                           for i, p in enumerate(files)}
                for future in as_completed(futures):
                    index = futures[future]
                    jobs_by_index[index] = future.result()
                    done += 1
                    self.file_done.emit(index)
                    self.progress.emit(f"[{done}/{total}] {files[index].name}")
                    self.progress_value.emit(done / total)

            jobs = [jobs_by_index[i] for i in range(total)]
            self.progress.emit("Done scanning. Opening preview...")
            self.finished_jobs.emit(jobs)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
