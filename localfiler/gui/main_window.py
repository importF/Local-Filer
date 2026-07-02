"""Main window: a tabbed UI for downloading new songs and tagging existing folders."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..core import covers, downloader, tagging, updater
from ..models import SongMetadata
from .drop import DropTarget
from .preview_dialog import PreviewDialog
from .recap_dialog import RecapDialog
from .scan_progress_dialog import ScanProgressDialog
from .sites_dialog import SitesDialog
from .update_dialog import UpdateDialog
from .worker import (
    AppUpdateCheckWorker,
    DownloadWorker,
    ScanWorker,
    TaggingJob,
    YtDlpCheckWorker,
    YtDlpUpdateWorker,
)

_NEW_FOLDER_LABEL = "➕ New folder…"


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        config.ensure_dirs()
        self.setWindowTitle("Local Filer")
        self.resize(720, 520)
        self._worker = None  # keep a reference so the QThread isn't GC'd
        self._current_flow = "download"  # "download" or "scan" (for cancel wording)
        self._scan_progress = None  # live progress window during a Tag Folder scan
        self._recap_only = False  # "Only show recap" for the batch in progress

        self._yt_worker = None  # keep update/check QThread alive while running
        self._app_update_worker = None  # keep the self-update check QThread alive
        self._pending_update_tag = ""
        self._pending_update_url = ""

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.tabs.addTab(self._build_tag_tab(), "Tag Folder / Song")
        self.tabs.addTab(self._build_settings_tab(), "Settings")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)  # minimalist: no percentage

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Activity log…")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.progress_bar)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log_view)

        # Genius is required: lock Download / Tag Folder until a token is set.
        self._update_tabs_enabled()
        # Put the cursor in the token field on a fresh start (after the window
        # is shown, so the focus actually sticks).
        if not config.get_genius_token():
            QTimer.singleShot(0, self.token_edit.setFocus)

    # ------------------------------------------------------------------ tabs
    def _build_download_tab(self) -> QWidget:
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "Paste a URL — YouTube, SoundCloud, and 1000+ more sites supported…"
        )

        # --- example hints strip ---
        examples_label = QLabel(
            "<b>Examples:</b>  "
            "youtube.com/watch?v=…  •  youtube.com/playlist?list=…  •  "
            "soundcloud.com/<i>artist</i>/<i>track</i>  •  "
            "soundcloud.com/<i>artist</i>/sets/<i>album</i>"
        )
        examples_label.setWordWrap(True)
        examples_label.setStyleSheet("color: #888; font-size: 11px;")

        sites_btn = QPushButton("View all supported sites…")
        sites_btn.setFlat(True)
        sites_btn.setStyleSheet(
            "color: #5599ff; font-size: 11px; padding: 2px 10px;"
        )
        sites_btn.clicked.connect(self._show_sites_dialog)

        hint_row = QHBoxLayout()
        hint_row.addWidget(examples_label, 1)
        hint_row.addWidget(sites_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")

        self.folder_combo = QComboBox()
        self._refresh_folder_combo()
        self.folder_combo.currentTextChanged.connect(self._on_folder_combo_changed)

        self.format_combo = QComboBox()
        self.format_combo.addItem("MP3 (default — best for Spotify)", userData="mp3")
        self.format_combo.addItem("M4A (YouTube-native, higher quality)", userData="m4a")
        self.format_combo.addItem(
            "Native (no re-encode — cleanest; codec/extension depends on the site)",
            userData="native",
        )
        self.format_combo.setCurrentIndex(0)

        # Off by default; only meaningful for an album/playlist.
        self.number_tracks_check = QCheckBox("Number tracks by position (album / playlist)")
        self.number_tracks_check.setToolTip(
            "When on, a multi-item download is numbered 1, 2, 3… in order.\n"
            "Leave off for a folder of unrelated songs."
        )

        self.download_btn = QPushButton("Download && Tag")
        self.download_btn.clicked.connect(self._start_download)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("URL:"))
        layout.addWidget(self.url_edit)
        layout.addLayout(hint_row)
        layout.addWidget(sep)
        layout.addWidget(QLabel("Save into Outputs subfolder:"))
        layout.addWidget(self.folder_combo)
        layout.addWidget(QLabel("File type:"))
        layout.addWidget(self.format_combo)
        layout.addWidget(self.number_tracks_check)
        layout.addWidget(self.download_btn)
        layout.addStretch()

        # Drop a URL anywhere on the tab to fill the URL field.
        tab = DropTarget(on_web_url=self._on_url_dropped)
        tab.setLayout(layout)
        return tab

    def _on_url_dropped(self, url: str) -> None:
        self.url_edit.setText(url.strip())
        self.tabs.setCurrentIndex(0)

    def _show_sites_dialog(self) -> None:
        dlg = SitesDialog(self)
        dlg.exec()

    def _build_tag_tab(self) -> QWidget:
        # --- Folder ---
        self.tag_folder_edit = QLineEdit()
        self.tag_folder_edit.setPlaceholderText("Folder containing songs to tag…")
        folder_browse_btn = QPushButton("Browse…")
        folder_browse_btn.clicked.connect(self._browse_tag_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.tag_folder_edit)
        folder_row.addWidget(folder_browse_btn)

        self.scan_btn = QPushButton("Scan && Tag Folder")
        self.scan_btn.clicked.connect(self._start_scan)

        self.tag_recap_only_check = QCheckBox("Only show recap")
        self.tag_recap_only_check.setToolTip("Skip search; read tags locally, straight to recap.")

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")

        # --- Single file ---
        self.tag_file_edit = QLineEdit()
        self.tag_file_edit.setPlaceholderText("A single song file to tag…")
        file_browse_btn = QPushButton("Browse…")
        file_browse_btn.clicked.connect(self._browse_tag_file)
        file_row = QHBoxLayout()
        file_row.addWidget(self.tag_file_edit)
        file_row.addWidget(file_browse_btn)

        self.scan_file_btn = QPushButton("Scan && Tag")
        self.scan_file_btn.clicked.connect(self._start_scan_file)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Folder to tag:"))
        layout.addLayout(folder_row)
        layout.addWidget(self.scan_btn)
        layout.addWidget(self.tag_recap_only_check)
        layout.addSpacing(8)
        layout.addWidget(sep)
        layout.addSpacing(8)
        layout.addWidget(QLabel("File to tag:"))
        layout.addLayout(file_row)
        layout.addWidget(self.scan_file_btn)
        layout.addStretch()

        # Drop a folder (fills the folder field) or a file (fills the file field).
        tab = DropTarget(on_local_path=self._on_tag_path_dropped)
        tab.setLayout(layout)
        return tab

    def _on_tag_path_dropped(self, path: Path) -> None:
        self.tabs.setCurrentIndex(1)
        if path.is_dir():
            self.tag_folder_edit.setText(str(path))
        else:
            self.tag_file_edit.setText(str(path))

    def _build_settings_tab(self) -> QWidget:
        # --- Genius API token (persisted in localfiler_settings.json) ---
        self.token_edit = QLineEdit()
        self.token_edit.setPlaceholderText("Genius API access token…")
        self.token_edit.setText(config.get_genius_token() or "")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_token_btn = QPushButton("👁")
        self.show_token_btn.setCheckable(True)
        self.show_token_btn.setFixedWidth(36)
        self.show_token_btn.setToolTip("Show / hide the token")
        self.show_token_btn.toggled.connect(self._toggle_token_visibility)
        save_token_btn = QPushButton("Save token")
        save_token_btn.clicked.connect(self._save_genius_token)
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        token_row.addWidget(self.show_token_btn)
        token_row.addWidget(save_token_btn)

        genius_desc = QLabel(
            "Token used to look up metadata for unreleased / leaked tracks. "
            "Required: the Download and Tag Folder tabs are locked until it's set."
        )
        genius_desc.setWordWrap(True)
        genius_desc.setStyleSheet("color: #888;")

        # Prominent nudge shown until a token is set (toggled in _update_tabs_enabled).
        self._genius_hint = QLabel(
            "⚠  Enter your Genius API token."
        )
        self._genius_hint.setWordWrap(True)
        self._genius_hint.setStyleSheet(
            "background: #3a2e0a; color: #f4b400; border: 1px solid #f4b400; "
            "border-radius: 4px; padding: 6px 8px;"
        )

        self.genius_help_btn = QPushButton("How to get a token…")
        self.genius_help_btn.clicked.connect(self._show_genius_help)
        help_row = QHBoxLayout()
        help_row.addWidget(self.genius_help_btn)
        help_row.addStretch()

        # --- Updates: Local Filer itself, plus the yt-dlp download engine ---
        self._app_name_label = QLabel("Local Filer")
        self._app_name_label.setStyleSheet(
            "font-family: 'Segoe UI Semibold', 'Segoe UI'; font-size: 17px; "
            "font-weight: 800; letter-spacing: 0.3px; color: #fff;"
        )
        self._app_version_label = QLabel(f"v{updater.current_version()}")
        self._app_version_label.setStyleSheet("color: #888; font-size: 11px;")
        app_name_col = QVBoxLayout()
        app_name_col.setSpacing(0)
        app_name_col.addWidget(self._app_name_label)
        app_name_col.addWidget(self._app_version_label)

        self.app_update_btn = QPushButton("Update now")
        self.app_update_btn.clicked.connect(self._run_app_update)
        self.app_update_btn.hide()  # only shown once an update is found
        self.app_check_updates_btn = QPushButton("Check for updates")
        self.app_check_updates_btn.clicked.connect(self._check_app_update)
        if not updater.is_frozen():
            self.app_check_updates_btn.setEnabled(False)
            self.app_check_updates_btn.setToolTip("Only available in the packaged app.")
        self._app_status_label = QLabel("")
        self._app_status_label.setWordWrap(True)
        self._app_status_label.setStyleSheet("color: #888;")
        self._app_status_label.hide()  # no reserved space until there's something to say
        app_update_row = QHBoxLayout()
        app_update_row.addLayout(app_name_col, 1)
        app_update_row.addWidget(self.app_update_btn, 0, Qt.AlignmentFlag.AlignTop)
        app_update_row.addWidget(self.app_check_updates_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._ytdlp_label = QLabel("yt-dlp")
        self.update_btn = QPushButton("Update now")
        self.update_btn.clicked.connect(self._run_ytdlp_update)
        self.update_btn.hide()  # only shown once an update is found
        self.check_updates_btn = QPushButton("Check for updates")
        self.check_updates_btn.clicked.connect(self._check_ytdlp_updates)
        ytdlp_desc = QLabel(
            "The download engine. Update it if downloads start failing on a site."
        )
        ytdlp_desc.setWordWrap(True)
        ytdlp_desc.setStyleSheet("color: #888;")
        ytdlp_row = QHBoxLayout()
        ytdlp_row.addWidget(self._ytdlp_label, 1)
        ytdlp_row.addWidget(self.update_btn)
        ytdlp_row.addWidget(self.check_updates_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")

        layout = QVBoxLayout()
        layout.addWidget(QLabel("<b>Genius API</b>"))
        layout.addWidget(self._genius_hint)
        layout.addWidget(genius_desc)
        layout.addLayout(token_row)
        layout.addLayout(help_row)
        layout.addSpacing(12)
        layout.addWidget(sep)
        layout.addSpacing(12)
        layout.addWidget(QLabel("<b>Updates</b>"))
        layout.addLayout(app_update_row)
        layout.addWidget(self._app_status_label)
        layout.addSpacing(4)
        layout.addLayout(ytdlp_row)
        layout.addWidget(ytdlp_desc)
        layout.addStretch()

        tab = QWidget()
        tab.setLayout(layout)
        return tab

    # ----------------------------------------------------------- folder combo
    def _refresh_folder_combo(self) -> None:
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        self.folder_combo.addItem("(Outputs root)", userData=str(config.OUTPUTS_DIR))
        for folder in config.output_subfolders():
            self.folder_combo.addItem(folder.name, userData=str(folder))
        self.folder_combo.addItem(_NEW_FOLDER_LABEL, userData=None)
        self.folder_combo.blockSignals(False)

    def _on_folder_combo_changed(self, text: str) -> None:
        if text != _NEW_FOLDER_LABEL:
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            new_folder = config.OUTPUTS_DIR / covers.sanitize(name)
            new_folder.mkdir(parents=True, exist_ok=True)
            self._refresh_folder_combo()
            idx = self.folder_combo.findText(new_folder.name)
            self.folder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            self.folder_combo.setCurrentIndex(0)

    def _selected_target_dir(self) -> Path:
        data = self.folder_combo.currentData()
        return Path(data) if data else config.OUTPUTS_DIR

    # --------------------------------------------------------------- actions
    def _start_download(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "No URL", "Please paste a URL first.")
            return
        target = self._selected_target_dir()
        audio_format = self.format_combo.currentData() or "mp3"
        number_tracks = self.number_tracks_check.isChecked()
        self._recap_only = False  # Tag Folder only
        self._current_flow = "download"
        self.log(f"Will save into: {target} (as {audio_format.upper()})")
        self._set_busy(True)

        worker = DownloadWorker(
            url, target, audio_format=audio_format, number_tracks=number_tracks
        )
        worker.progress.connect(self.log)
        worker.progress_value.connect(self._on_progress)
        worker.finished_jobs.connect(self._on_jobs_ready)
        worker.failed.connect(self._on_worker_failed)
        self._run_worker(worker)

    # Audio containers we can tag (matches core.tagging's dispatch).
    _AUDIO_EXTS = (".mp3", ".m4a", ".mp4", ".aac", ".m4b", ".flac", ".opus", ".ogg", ".oga")

    def _start_scan(self) -> None:
        folder = self.tag_folder_edit.text().strip()
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, "No folder", "Please choose a valid folder.")
            return
        paths = sorted(
            p for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in self._AUDIO_EXTS
        )
        if not paths:
            QMessageBox.warning(self, "Empty folder", "No taggable audio files found.")
            return
        self.log(f"Scanning folder: {folder}")
        self._run_scan(paths)

    def _start_scan_file(self) -> None:
        file = self.tag_file_edit.text().strip()
        if not file or not Path(file).is_file():
            QMessageBox.warning(self, "No file", "Please choose a valid song file.")
            return
        self.log(f"Tagging file: {file}")
        self._run_scan([Path(file)])

    def _run_scan(self, paths: list[Path]) -> None:
        self._current_flow = "scan"
        self._recap_only = self.tag_recap_only_check.isChecked()

        if self._recap_only:
            # No search — read tags locally, straight to recap.
            self._set_busy(True)
            self.log(f"Reading {len(paths)} file(s) locally (no search)…")
            jobs = self._build_local_jobs(paths)
            self._on_jobs_ready(jobs)
            self._set_busy(False)
            return

        self._set_busy(True)

        # Live progress window visualising the parallel lookups.
        self._scan_progress = ScanProgressDialog(self)

        worker = ScanWorker(paths)
        worker.total_known.connect(self._scan_progress.set_total)
        worker.file_started.connect(self._scan_progress.on_file_started)
        worker.provider_update.connect(self._scan_progress.on_provider_update)
        worker.file_done.connect(self._scan_progress.on_file_done)
        worker.progress.connect(self.log)
        worker.progress_value.connect(self._on_progress)
        worker.finished_jobs.connect(self._on_jobs_ready)
        worker.failed.connect(self._on_worker_failed)
        self._scan_progress.show()
        self._run_worker(worker)

    def _build_local_jobs(self, paths: list[Path]) -> list[TaggingJob]:
        """Read each file's own tags/cover locally — no metadata search."""
        jobs = []
        for path in paths:
            existing = tagging.read_tags(path)
            cover = tagging.read_cover(path)
            meta = SongMetadata(
                title=existing.get("title") or "",
                artist=existing.get("artist") or "",
                album=existing.get("album") or "",
                year=existing.get("year") or "",
                track=existing.get("track") or "",
                source_file=str(path),
            )
            jobs.append(TaggingJob(path=path, metadata=meta, original=existing, original_cover=cover))
        return jobs

    def _browse_tag_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder of songs", str(config.OUTPUTS_DIR)
        )
        if folder:
            self.tag_folder_edit.setText(folder)

    def _browse_tag_file(self) -> None:
        exts = " ".join(f"*{e}" for e in self._AUDIO_EXTS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a song file", str(config.OUTPUTS_DIR), f"Audio ({exts})"
        )
        if path:
            self.tag_file_edit.setText(path)

    # ------------------------------------------------------------ worker glue
    def _run_worker(self, worker) -> None:
        self._worker = worker
        worker.finished.connect(lambda: self._set_busy(False))
        worker.start()

    def _on_progress(self, fraction: float) -> None:
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))

    def _on_worker_failed(self, message: str) -> None:
        self._close_scan_progress()
        self.log(f"ERROR: {message}")
        QMessageBox.critical(self, "Error", message)

    def _close_scan_progress(self) -> None:
        if self._scan_progress is not None:
            self._scan_progress.close()
            self._scan_progress = None

    def _on_jobs_ready(self, jobs: list[TaggingJob]) -> None:
        self._close_scan_progress()  # done looking up; hide the progress window
        if not jobs:
            self.log("Nothing to tag.")
            return
        self._run_preview_loop(jobs)
        self._refresh_folder_combo()

    # ------------------------------------------------------- preview + saving
    def _run_preview_loop(self, jobs: list[TaggingJob]) -> None:
        total = len(jobs)
        # Per-song outcome; None = not decided. Tracked by index so Back can re-decide.
        statuses: list[str | None] = [None] * total
        drafts: dict[int, dict] = {}  # remembered Save-as edits per song
        cancelled = False
        last_saved_name = ""

        # Reuse the progress bar for the saving phase (1 step per song).
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)

        if self._recap_only:
            # Tag Folder only: no search, no write, straight to recap.
            for i in range(total):
                statuses[i] = "kept"
                self.progress_bar.setValue(i + 1)
        else:
            i = 0
            apply_all = False
            while i < total:
                job = jobs[i]

                if apply_all:
                    # Apply each remaining song's preview defaults, as an unedited Save would.
                    PreviewDialog.apply_default_metadata(job, self)
                    ok = self._save_job(job)
                    statuses[i] = "saved" if ok else "failed"
                    if ok:
                        last_saved_name = job.path.name
                    self.progress_bar.setValue(i + 1)
                    i += 1
                    continue

                dialog = PreviewDialog(
                    job, i + 1, total, self,
                    draft=drafts.get(i), can_go_back=(i > 0),
                )
                dialog.exec()
                action = dialog.action
                drafts[i] = dialog.draft  # keep edits in case this song is revisited

                if action == PreviewDialog.BACK:
                    i -= 1
                    continue

                if action in (PreviewDialog.SAVE, PreviewDialog.SAVE_ALL):
                    # Re-saving a saved song is safe: tags are rewritten in place.
                    ok = self._save_job(job)
                    statuses[i] = "saved" if ok else "failed"
                    if ok:
                        last_saved_name = job.path.name
                    if action == PreviewDialog.SAVE_ALL:
                        apply_all = True
                elif action == PreviewDialog.SKIP:
                    # Keep the file in Downloads/ so Back can still save it; cleaned up at the end.
                    statuses[i] = "skipped"
                    self.log(f"Skipped: {job.path.name}")
                else:  # CANCEL: stop here; un-saved files are cleaned up below
                    cancelled = True
                    verb = "Download" if self._current_flow == "download" else "Tagging"
                    self.log(f"{verb} cancelled.")
                    break

                self.progress_bar.setValue(i + 1)
                i += 1

        # Recap: always in recap-only mode, else for a completed multi-song batch.
        if self._recap_only or (not cancelled and total > 1):
            self._run_recap_loop(jobs, statuses, drafts, total)

        # Remove downloads the user didn't keep.
        for job, status in zip(jobs, statuses):
            if status not in ("saved", "kept"):
                self._discard_download(job)

        # Reset the bar to idle (fixes it lingering after a cancel).
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)

        saved = statuses.count("saved")
        skipped = statuses.count("skipped")
        kept = statuses.count("kept")
        failed = statuses.count("failed")
        self.log(
            f"Finished. Saved {saved}, skipped {skipped}"
            + (f", kept as-is {kept}" if kept else "")
            + (f", failed {failed}" if failed else "")
            + "."
        )
        # The recap already covered a completed bulk job; only single items and
        # cancelled batches still get the message-box summary.
        if not self._recap_only and (cancelled or total == 1):
            self._show_summary(total, saved, skipped, kept, failed, cancelled, last_saved_name)

    def _run_recap_loop(self, jobs, statuses, drafts, total) -> None:
        """Show the recap; let the user re-edit individual songs, then close."""
        while True:
            recap = RecapDialog(jobs, statuses, self, apply_cover_cb=self._save_job,
                                drafts=drafts)
            recap.exec()
            if recap.action != RecapDialog.EDIT or recap.edit_index is None:
                return
            i = recap.edit_index
            # Single-song re-edit (no bulk navigation), seeded with prior edits.
            dialog = PreviewDialog(jobs[i], 1, 1, self, draft=drafts.get(i))
            dialog.exec()
            drafts[i] = dialog.draft
            if dialog.action in (PreviewDialog.SAVE, PreviewDialog.SAVE_ALL):
                statuses[i] = "saved" if self._save_job(jobs[i]) else "failed"
            elif dialog.action == PreviewDialog.SKIP:
                statuses[i] = "skipped"
            # CANCEL / closing the window leaves the song's status unchanged.

    def _show_summary(self, total, saved, skipped, kept, failed, cancelled, last_saved_name) -> None:
        # Single item: a simple success/notice, no "bulk" wording.
        if total == 1:
            if saved == 1:
                QMessageBox.information(self, "Done", f"Saved “{last_saved_name}”.")
            elif failed:
                QMessageBox.warning(self, "Failed", "Could not save the song. See the log.")
            # skipped/kept/cancelled single item: no popup, just the log.
            return

        # Bulk summary.
        parts = [f"Saved: {saved}", f"Skipped: {skipped}"]
        if kept:
            parts.append(f"Kept as-is: {kept}")
        if failed:
            parts.append(f"Failed: {failed}")
        body = "\n".join(parts)
        if cancelled:
            QMessageBox.warning(self, "Cancelled", f"Batch cancelled.\n\n{body}")
        else:
            QMessageBox.information(self, "Batch complete", body)

    def _discard_download(self, job: TaggingJob) -> None:
        """Delete a freshly-downloaded file the user didn't keep (never existing files)."""
        if job.is_download and job.path.exists():
            try:
                job.path.unlink()
            except OSError as exc:
                self.log(f"Could not remove {job.path.name}: {exc}")

    def _save_job(self, job: TaggingJob) -> bool:
        meta = job.metadata
        temp_cover: Path | None = None
        try:
            cover_path = None
            if meta.remove_cover:
                pass  # leave cover_path None; write_tags will strip any embedded art
            elif meta.cover_path and Path(meta.cover_path).exists():
                cover_path = Path(meta.cover_path)
            elif covers.is_youtube_thumb(meta.cover_url):
                # YouTube thumbnails are embedded but never cached in Covers/.
                temp_cover = covers.download_temp(meta.cover_url)
                cover_path = temp_cover
            else:
                cover_path = covers.get_or_download(
                    meta.cover_url, meta.artist, meta.album, meta.title
                )
                if cover_path:
                    meta.cover_path = str(cover_path)

            tagging.write_tags(job.path, meta, cover_path, remove_cover=meta.remove_cover)

            # Downloads: move the tagged file into the chosen Outputs subfolder.
            if job.is_download and job.target_dir is not None:
                job.path = self._move_into(job.path, job.target_dir)

            if cover_path:
                cover_note = f" (cover: {Path(cover_path).name})"
            elif meta.remove_cover:
                cover_note = " (cover removed)"
            else:
                cover_note = " (no cover)"
            self.log(f"Saved: {job.path.name}{cover_note}")
            return True
        except Exception as exc:  # noqa: BLE001 - report but keep the batch going
            self.log(f"FAILED to save {job.path.name}: {exc}")
            return False
        finally:
            if temp_cover is not None:
                try:
                    temp_cover.unlink()
                except OSError:
                    pass

    def _move_into(self, src: Path, target_dir: Path) -> Path:
        """Move ``src`` into ``target_dir``, avoiding name collisions."""
        target_dir.mkdir(parents=True, exist_ok=True)
        # Already in place (e.g. re-saving via Back): don't create a duplicate.
        if src.parent.resolve() == target_dir.resolve():
            return src
        dest = target_dir / src.name
        counter = 1
        while dest.exists():
            dest = target_dir / f"{src.stem} ({counter}){src.suffix}"
            counter += 1
        shutil.move(str(src), str(dest))
        return dest

    # --------------------------------------------------------------- helpers
    def _set_busy(self, busy: bool) -> None:
        self.download_btn.setEnabled(not busy)
        self.scan_btn.setEnabled(not busy)
        self.scan_file_btn.setEnabled(not busy)
        if busy:
            # Indeterminate "spinner" until the worker reports real progress.
            self.progress_bar.setRange(0, 0)

    def _toggle_token_visibility(self, shown: bool) -> None:
        self.token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )

    def _update_tabs_enabled(self) -> None:
        """Lock Download / Tag Folder until a Genius token exists, and nudge the
        user toward the token field while it's missing."""
        has_token = bool(config.get_genius_token())
        self.tabs.setTabEnabled(0, has_token)  # Download
        self.tabs.setTabEnabled(1, has_token)  # Tag Folder
        self._genius_hint.setVisible(not has_token)
        # Highlight the field until it's filled.
        self.token_edit.setStyleSheet(
            "" if has_token else "border: 2px solid #f4b400;"
        )
        if not has_token:
            self.tabs.setCurrentIndex(2)  # Settings — the only usable tab
            self.token_edit.setFocus()

    def _save_genius_token(self) -> None:
        token = self.token_edit.text().strip()
        config.set_genius_token(token)
        self._update_tabs_enabled()
        self.log("Genius token saved." if token else "Genius token cleared.")

    def _show_genius_help(self) -> None:
        """A short, link-rich guide to creating a free Genius API token."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Getting a Genius API token")
        dlg.setMinimumWidth(480)
        body = QLabel(
            "<b>Local Filer needs a free Genius API token</b> to look up song "
            "metadata.<br><br>"
            "<ol>"
            "<li>Sign in, or create a free account, at "
            "<a href='https://genius.com/signup'>genius.com</a>.</li>"
            "<li>Open <a href='https://genius.com/api-clients'>"
            "genius.com/api-clients</a> and click <b>New API Client</b>.</li>"
            "<li>Enter any <i>App Name</i> (e.g. “Local Filer”) and any "
            "<i>App Website URL</i> (e.g. <code>https://example.com</code>), "
            "then <b>Save</b>.</li>"
            "<li>On the new client, click <b>Generate Access Token</b> and copy "
            "the <b>Client Access Token</b>.</li>"
            "<li>Paste it into the token field here and click "
            "<b>Save token</b>.</li>"
            "</ol>"
        )
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay = QVBoxLayout(dlg)
        lay.addWidget(body)
        lay.addLayout(btn_row)
        dlg.exec()

    # ------------------------------------------------------ Local Filer update
    def _set_app_status(self, text: str) -> None:
        self._app_status_label.setText(text)
        self._app_status_label.show()

    def _check_app_update(self) -> None:
        self.app_check_updates_btn.setEnabled(False)
        self.app_update_btn.hide()
        self._set_app_status("Checking…")
        worker = AppUpdateCheckWorker()
        worker.done.connect(self._on_app_checked)
        worker.failed.connect(self._on_app_check_failed)
        self._app_update_worker = worker
        worker.start()

    def _on_app_checked(self, tag: str, url: str) -> None:
        self.app_check_updates_btn.setEnabled(True)
        if tag and updater.is_newer(tag, updater.current_version()):
            self._set_app_status(f"Update available: {tag}")
            self._pending_update_tag = tag
            self._pending_update_url = url
            self.app_update_btn.show()
        else:
            self._set_app_status("Up to date.")

    def _on_app_check_failed(self, message: str) -> None:
        self.app_check_updates_btn.setEnabled(True)
        self._set_app_status(f"Check failed ({message}).")

    def _run_app_update(self) -> None:
        dlg = UpdateDialog(self._pending_update_tag, self._pending_update_url, self)
        dlg.exec()

    # ----------------------------------------------------------- yt-dlp update
    def _check_ytdlp_updates(self) -> None:
        self.check_updates_btn.setEnabled(False)
        self.update_btn.hide()
        self._ytdlp_label.setText("yt-dlp: checking…")
        worker = YtDlpCheckWorker()
        worker.done.connect(self._on_ytdlp_checked)
        worker.failed.connect(self._on_ytdlp_check_failed)
        self._yt_worker = worker
        worker.start()

    def _on_ytdlp_checked(self, current: str, latest: str) -> None:
        self.check_updates_btn.setEnabled(True)
        if latest and latest > current:
            self._ytdlp_label.setText(f"yt-dlp {current} — update available ({latest}).")
            self.update_btn.show()
        elif latest:
            self._ytdlp_label.setText(f"yt-dlp {current} — up to date.")
        else:
            self._ytdlp_label.setText(
                f"yt-dlp {current} — couldn't reach the update server."
            )

    def _on_ytdlp_check_failed(self, message: str) -> None:
        self.check_updates_btn.setEnabled(True)
        self._ytdlp_label.setText(f"yt-dlp: check failed ({message}).")

    def _run_ytdlp_update(self) -> None:
        self.update_btn.setEnabled(False)
        self.check_updates_btn.setEnabled(False)
        self._ytdlp_label.setText("yt-dlp: updating…")
        worker = YtDlpUpdateWorker()
        worker.done.connect(self._on_ytdlp_updated)
        self._yt_worker = worker
        worker.start()

    def _on_ytdlp_updated(self, ok: bool, output: str) -> None:
        self.update_btn.setEnabled(True)
        self.check_updates_btn.setEnabled(True)
        self.log(f"yt-dlp update:\n{output}")
        if ok:
            self.update_btn.hide()
            try:
                version = downloader.ytdlp_version()
            except Exception:  # noqa: BLE001
                version = "?"
            self._ytdlp_label.setText(f"yt-dlp {version} — updated.")
            QMessageBox.information(self, "yt-dlp", f"yt-dlp updated.\n\n{output}")
        else:
            self._ytdlp_label.setText("yt-dlp: update failed (see log).")
            QMessageBox.warning(self, "yt-dlp", f"Update failed:\n\n{output}")

    def log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
