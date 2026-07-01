"""Editable metadata + cover preview shown before tags are written.

Columns: "From source" (a provider's result, read-only) -> "Save as" (the values
written, editable) -> "Original" (the file's own data). Per-field arrows copy one
value across. ``action`` tells the caller how to proceed: SAVE, SAVE_ALL, SKIP,
CANCEL, or BACK.
"""

from __future__ import annotations

import html as _html
from pathlib import Path

import requests
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
)

from .. import config
from ..core import covers
from ..models import STATUS_OK
from .worker import SearchWorker

_FIELDS = ("title", "artist", "album", "year")
_LABELS = {"title": "Title", "artist": "Artist", "album": "Album", "year": "Year"}


class _CoverLoader(QThread):
    """Fetch a cover image URL off the UI thread."""

    loaded = Signal(bytes)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self) -> None:
        try:
            resp = requests.get(self.url, timeout=8)
            resp.raise_for_status()
            self.loaded.emit(resp.content)
        except requests.RequestException:
            self.loaded.emit(b"")


def _field(obj, name: str) -> str:
    """Read a field from either a SongMetadata or a ProviderResult."""
    return getattr(obj, name, None) or ""


class PreviewDialog(QDialog):
    SAVE = 1
    SAVE_ALL = 2
    SKIP = 3
    CANCEL = 4
    BACK = 5

    COVER_SIZE = 150

    def __init__(self, job, index: int, total: int, parent=None,
                 draft: dict | None = None, can_go_back: bool = False,
                 headless: bool = False):
        super().__init__(parent)
        self.job = job
        self.action = self.CANCEL
        self._finished = False  # True once a button set the action
        self._presets_cache: list[tuple[str, object]] = []
        # Headless: seed the Save-as column without showing the window or starting
        # cover threads. Used by bulk "Apply to All".
        self._headless = headless

        # Bulk navigation: the Save-as draft from this song's last visit, and
        # whether a Back step is possible (False on the first song).
        self._draft = draft
        self._is_bulk = total > 1
        self._can_go_back = can_go_back
        self.draft: dict | None = None  # captured on exit for the caller to stash

        self._src_cover_url: str | None = None
        self._final_cover_url: str | None = None
        self._final_local_cover: Path | None = None
        self._final_cover_cleared = False  # user pressed "Remove cover"

        # The right-hand "Original" column: the file's existing tags + embedded
        # cover (Tag Folder), or the native downloaded metadata + thumbnail URL
        # (downloads). Save-as is seeded from here first, gaps filled from the
        # best match.
        self._orig_fields: dict[str, str] = {}
        self._orig_cover_url: str | None = None
        self._original_cover: bytes | None = None
        if job.original is not None:
            self._orig_fields = dict(job.original)
            self._original_cover = job.original_cover
            self._has_original = True
        else:
            src = self._source_result() if job.is_download else None
            if src is not None:
                self._orig_fields = {
                    "title": _field(src, "title"),
                    "artist": _field(src, "artist"),
                    "album": _field(src, "album"),
                    "year": _field(src, "year"),
                    "track": _field(job.metadata, "track"),
                }
                self._orig_cover_url = _field(src, "cover_url") or None
                self._has_original = True
            else:
                self._has_original = False

        # Retain loader references so a running QThread isn't GC'd; tokens let us
        # ignore stale results.
        self._loaders: list[_CoverLoader] = []
        self._src_token = 0
        self._orig_token = 0
        self._final_token = 0
        self._search_worker: SearchWorker | None = None

        # Audio preview, created lazily on first Play; stops when we leave the song.
        self._player: QMediaPlayer | None = None
        self._audio_out: QAudioOutput | None = None

        # Single item: title with the song name.
        if total == 1:
            self.setWindowTitle(job.path.stem)
        else:
            self.setWindowTitle(f"Preview {index} of {total}")
        self.setMinimumWidth(960 if self._has_original else 720)

        self.src_edits: dict[str, QLineEdit] = {}
        self.final_edits: dict[str, QLineEdit] = {}
        self.orig_edits: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)

        # Top bar: filename + destination on the left, Play + Sources on the right.
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setToolTip("Play / stop the audio (stops when you leave this song)")
        self.play_btn.clicked.connect(self._toggle_play)
        sources_btn = QPushButton("Sources")
        sources_btn.setToolTip("Show which sources were used, found no match, or errored")
        sources_btn.clicked.connect(self._show_sources)
        header = self._wrap(f"File: {job.path.name}\nSaves to: {self._destination_text()}")
        top_bar = QHBoxLayout()
        top_bar.addWidget(header, 1)
        top_bar.addWidget(self.play_btn)
        top_bar.addWidget(sources_btn)
        layout.addLayout(top_bar)

        layout.addLayout(self._build_source_row())
        layout.addLayout(self._build_grid())

        copy_all_btn = QPushButton("⮕  Copy all to Save")
        copy_all_btn.clicked.connect(self._copy_all)
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_row.addWidget(copy_all_btn)
        copy_row.addStretch()
        layout.addLayout(copy_row)

        self.bio_view = QPlainTextEdit()
        self.bio_view.setReadOnly(True)
        self.bio_view.setMaximumHeight(80)
        self.bio_view.setPlaceholderText("No bio found.")
        layout.addWidget(self.bio_view)

        layout.addLayout(self._build_action_buttons())

        # Populate the "From source" dropdown / bio.
        self._refresh_from_metadata()
        # Seed the "Save as" column from the Original column, or the best match.
        if self._has_original:
            self._init_final_from_original()
        else:
            self._set_final_from(self.job.metadata)

        # A cover already applied to this job (pasted in the recap, or from a
        # previous save) wins over the default cover.
        applied = self.job.metadata.cover_path
        if applied and Path(applied).exists():
            self._final_local_cover = Path(applied)
            self._final_cover_url = None
            self._final_cover_cleared = False
            self._final_token += 1
            self._set_pixmap(self.final_cover_label, Path(applied).read_bytes())

        # Revisiting via Back/forward: restore last time's Save-as edits.
        if self._draft is not None:
            self._restore_draft()

    # -- layout builders ----------------------------------------------
    def _build_source_row(self):
        self.source_combo = QComboBox()
        self.source_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.search_btn = QPushButton("🔍 Search again (uses Save-as Title + Artist)")
        self.search_btn.clicked.connect(self._search_again)
        row = QHBoxLayout()
        row.addWidget(QLabel("From source:"))
        row.addWidget(self.source_combo, 1)
        row.addWidget(self.search_btn)
        return row

    def _build_grid(self) -> QGridLayout:
        # Column order:  label | From source | -> | Save as | <- | Original
        grid = QGridLayout()
        has_orig = self._has_original
        self._final_col = 3  # Save as

        grid.addWidget(self._bold("From source"), 0, 1)
        grid.addWidget(self._bold("Save as"), 0, 3)
        if has_orig:
            grid.addWidget(self._bold(self._original_column_title()), 0, 5)

        for r, name in enumerate(_FIELDS, start=1):
            grid.addWidget(QLabel(f"{_LABELS[name]}:"), r, 0)

            src = QLineEdit()
            src.setReadOnly(True)
            self.src_edits[name] = src
            grid.addWidget(src, r, 1)
            grid.addWidget(
                self._copy_button("→", f"Copy {_LABELS[name]} from source into Save as",
                                  lambda n=name: self._copy_field(n)),
                r, 2,
            )

            final = QLineEdit()
            self.final_edits[name] = final
            grid.addWidget(final, r, 3)

            if has_orig:
                grid.addWidget(
                    self._copy_button("←", f"Copy {_LABELS[name]} from original into Save as",
                                      lambda n=name: self._copy_original_field(n)),
                    r, 4,
                )
                orig = QLineEdit()
                orig.setReadOnly(True)
                self.orig_edits[name] = orig
                grid.addWidget(orig, r, 5)

        # Track # row (providers don't supply one, so no source cell).
        track_row = len(_FIELDS) + 1
        grid.addWidget(QLabel("Track #:"), track_row, 0)
        self.track_edit = QLineEdit()
        grid.addWidget(self.track_edit, track_row, 3)
        if has_orig:
            grid.addWidget(
                self._copy_button("←", "Copy track # from original into Save as",
                                  self._copy_original_track),
                track_row, 4,
            )
            self.orig_track_edit = QLineEdit()
            self.orig_track_edit.setReadOnly(True)
            grid.addWidget(self.orig_track_edit, track_row, 5)

        # Cover row
        img_row = len(_FIELDS) + 2
        grid.addWidget(QLabel("Cover:"), img_row, 0)

        self.src_cover_label = self._make_cover_label()
        grid.addWidget(self.src_cover_label, img_row, 1)
        grid.addWidget(
            self._copy_button("→", "Copy cover from source into Save as", self._copy_image),
            img_row, 2,
        )

        self.final_cover_label = self._make_cover_label()
        grid.addWidget(self.final_cover_label, img_row, 3)

        album_btn = QPushButton("Album cover…")
        album_btn.setToolTip("Pick from your saved covers or search album art online")
        album_btn.clicked.connect(self._open_cover_picker)
        choose_btn = QPushButton("Choose image…")
        choose_btn.clicked.connect(self._choose_local_cover)
        remove_btn = QPushButton("Remove cover")
        remove_btn.setToolTip("Save with no cover (and strip any existing one)")
        remove_btn.clicked.connect(self._clear_cover)
        cover_btns = QHBoxLayout()
        cover_btns.addWidget(album_btn)
        cover_btns.addWidget(choose_btn)
        cover_btns.addWidget(remove_btn)
        grid.addLayout(cover_btns, img_row + 1, 3)

        if has_orig:
            grid.addWidget(
                self._copy_button("←", "Copy cover from original into Save as",
                                  self._copy_original_image),
                img_row, 4,
            )
            self.orig_cover_label = self._make_cover_label()
            grid.addWidget(self.orig_cover_label, img_row, 5)
            self._populate_original()

        return grid

    def _copy_button(self, symbol: str, tooltip: str, slot) -> QPushButton:
        btn = QPushButton(symbol)
        btn.setFixedWidth(32)
        btn.setToolTip(tooltip)
        btn.clicked.connect(lambda _=False: slot())
        return btn

    def _build_action_buttons(self) -> QHBoxLayout:
        save_btn = QPushButton("Save")
        save_all_btn = QPushButton("Save && Apply to All")
        skip_btn = QPushButton("Skip")
        cancel_btn = QPushButton("Cancel")
        save_btn.setDefault(True)
        save_btn.clicked.connect(lambda: self._finish(self.SAVE))
        save_all_btn.clicked.connect(lambda: self._finish(self.SAVE_ALL))
        skip_btn.clicked.connect(lambda: self._finish(self.SKIP))
        cancel_btn.clicked.connect(lambda: self._finish(self.CANCEL))
        row = QHBoxLayout()
        # Back is only meaningful in a bulk job; disabled on the first song.
        if self._is_bulk:
            back_btn = QPushButton("←  Back")
            back_btn.setToolTip("Return to the previous song (your edits here are kept)")
            back_btn.setEnabled(self._can_go_back)
            back_btn.clicked.connect(lambda: self._finish(self.BACK))
            row.addWidget(back_btn)
        row.addWidget(save_btn)
        row.addWidget(save_all_btn)
        row.addStretch()
        row.addWidget(skip_btn)
        row.addWidget(cancel_btn)
        return row

    def _make_cover_label(self) -> QLabel:
        label = QLabel("No cover")
        label.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("border: 1px solid #888; color: #888;")
        return label

    def _bold(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold;")
        return label

    def _wrap(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        return label

    # -- presets / sources --------------------------------------------
    def _presets(self) -> list[tuple[str, object]]:
        meta = self.job.metadata
        presets: list[tuple[str, object]] = [("✨ Best match (merged)", meta)]
        for outcome in meta.outcomes:
            if outcome.status != STATUS_OK or outcome.result is None:
                continue
            if outcome.provider == "source":
                # Downloads show this as the Original column, so skip it here;
                # Tag Folder keeps it (a YouTube search), relabelled.
                if self.job.is_download:
                    continue
                presets.append((self._source_label(), outcome.result))
            else:
                presets.append((outcome.provider.capitalize(), outcome.result))
        return presets

    def _source_result(self):
        """The original downloaded file's own metadata (YouTube/SoundCloud)."""
        for outcome in self.job.metadata.outcomes:
            if (
                outcome.provider == "source"
                and outcome.status == STATUS_OK
                and outcome.result is not None
            ):
                return outcome.result
        return None

    def _source_label(self) -> str:
        """Display name for the 'source' provider — a real platform, not 'Source'."""
        if not self.job.is_download:
            return "YouTube"  # Tag Folder: a YouTube search of title + artist
        info = self.job.info or {}
        return info.get("extractor_key") or info.get("extractor") or "Downloaded file"

    def _original_column_title(self) -> str:
        return "Downloaded file" if self.job.is_download else "Original (file)"

    def _refresh_from_metadata(self) -> None:
        self._presets_cache = self._presets()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for label, _obj in self._presets_cache:
            self.source_combo.addItem(label)
        self.source_combo.setCurrentIndex(0)
        self.source_combo.blockSignals(False)
        self._on_preset_changed(0)
        self._rebuild_bio()

    def _on_preset_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._presets_cache):
            return
        _label, obj = self._presets_cache[index]
        for name in _FIELDS:
            self.src_edits[name].setText(_field(obj, name))
        self._src_cover_url = _field(obj, "cover_url") or None
        self._load_cover("src", self._src_cover_url)

    def _sources_html(self) -> str:
        symbols = {"ok": "✅", "no_match": "—", "skipped": "⚠", "error": "✖"}
        words = {"ok": "used", "no_match": "no match", "skipped": "skipped", "error": "error"}
        blocks = []
        for outcome in self.job.metadata.outcomes:
            symbol = symbols.get(outcome.status, "?")
            provider = self._source_label() if outcome.provider == "source" else outcome.provider
            verb = words.get(outcome.status, outcome.status)
            if outcome.provider == "source" and outcome.status == STATUS_OK:
                verb = "downloaded from" if self.job.is_download else "searched"
            head = f"{symbol} <b>{_html.escape(provider)}</b> — {verb}"
            lines = [head]
            result = outcome.result
            if outcome.status == "ok" and result is not None:
                for fld, lab in (("title", "Title"), ("artist", "Artist"),
                                 ("album", "Album"), ("year", "Year")):
                    val = getattr(result, fld, None)
                    if val:
                        lines.append(f"&nbsp;&nbsp;{lab}: {_html.escape(str(val))}")
                url = getattr(result, "url", None)
                if url:
                    safe = _html.escape(url, quote=True)
                    lines.append(f'&nbsp;&nbsp;<a href="{safe}">Open page ↗</a>')
            elif outcome.detail:
                lines.append(f"&nbsp;&nbsp;<i>{_html.escape(outcome.detail)}</i>")
            blocks.append("<br>".join(lines))
        return "<br><br>".join(blocks) if blocks else "No providers ran."

    def _show_sources(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Sources")
        dialog.setMinimumSize(480, 380)

        label = QLabel(self._sources_html())
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(label)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)

        layout = QVBoxLayout(dialog)
        layout.addWidget(scroll)
        layout.addWidget(close_btn)
        dialog.exec()

    def _rebuild_bio(self) -> None:
        self.bio_view.setPlainText(self.job.metadata.bio or "")

    def _destination_text(self) -> str:
        if self.job.is_download and self.job.target_dir is not None:
            return str(self.job.target_dir)
        return str(self.job.path.parent)  # Tag Folder: saved in place

    def _populate_original(self) -> None:
        original = self._orig_fields
        for name in _FIELDS:
            self.orig_edits[name].setText(original.get(name, "") or "")
        self.orig_track_edit.setText(original.get("track", "") or "")
        if self._original_cover:
            self._set_pixmap(self.orig_cover_label, self._original_cover)
        elif self._orig_cover_url:
            self._load_cover("orig", self._orig_cover_url)
        else:
            self.orig_cover_label.setText("No cover")

    # -- copy actions -------------------------------------------------
    def _copy_field(self, name: str) -> None:
        self.final_edits[name].setText(self.src_edits[name].text())

    def _copy_original_field(self, name: str) -> None:
        self.final_edits[name].setText(self.orig_edits[name].text())

    def _copy_original_track(self) -> None:
        self.track_edit.setText(self.orig_track_edit.text())

    def _copy_image(self) -> None:
        self._final_local_cover = None
        self._final_cover_cleared = False
        self._final_cover_url = self._src_cover_url
        self._load_cover("final", self._final_cover_url)

    def _clear_cover(self) -> None:
        self._final_local_cover = None
        self._final_cover_url = None
        self._final_cover_cleared = True
        self._final_token += 1  # invalidate any in-flight URL load
        self.final_cover_label.setText("No cover")
        self.final_cover_label.setPixmap(QPixmap())

    def _copy_original_image(self) -> None:
        if self._original_cover:
            # Tag Folder: persist the embedded bytes into Covers/ to re-embed on save.
            config.ensure_dirs()
            is_png = self._original_cover[:8] == b"\x89PNG\r\n\x1a\n"
            ext = ".png" if is_png else ".jpg"
            key = covers.cache_key(
                self.final_edits["artist"].text(),
                self.final_edits["album"].text(),
                self.final_edits["title"].text(),
            )
            dest = config.COVERS_DIR / f"{key}{ext}"
            dest.write_bytes(self._original_cover)
            self._final_local_cover = dest
            self._final_cover_url = None
            self._final_cover_cleared = False
            self._final_token += 1  # invalidate any in-flight URL load
            self._set_pixmap(self.final_cover_label, self._original_cover)
        elif self._orig_cover_url:
            # Download: the original cover is a thumbnail URL.
            self._final_local_cover = None
            self._final_cover_url = self._orig_cover_url
            self._final_cover_cleared = False
            self._load_cover("final", self._final_cover_url)

    def _copy_all(self) -> None:
        for name in _FIELDS:
            self._copy_field(name)
        self._copy_image()

    def _set_final_from(self, obj) -> None:
        """Fully fill the Save-as column (used only for the initial load)."""
        for name in _FIELDS:
            self.final_edits[name].setText(_field(obj, name))
        self.track_edit.setText(_field(self.job.metadata, "track"))
        self._final_local_cover = None
        self._final_cover_url = _field(obj, "cover_url") or None
        self._load_cover("final", self._final_cover_url)

    def _init_final_from_original(self) -> None:
        """Seed Save-as from the Original column, filling gaps from the best match."""
        original = self._orig_fields
        merged = self.job.metadata
        for name in _FIELDS:
            value = (original.get(name) or "").strip() or _field(merged, name)
            self.final_edits[name].setText(value)
        self.track_edit.setText(original.get("track", "") or "")
        # Prefer the original's own cover; otherwise the best match's.
        if self._original_cover or self._orig_cover_url:
            self._copy_original_image()
        else:
            self._final_local_cover = None
            self._final_cover_url = _field(merged, "cover_url") or None
            self._load_cover("final", self._final_cover_url)

    def _fill_empty_final_from(self, obj) -> None:
        """Only fill Save-as fields/image that are currently empty.

        Used after a re-search so existing values the user copied are preserved.
        """
        for name in _FIELDS:
            if not self.final_edits[name].text().strip():
                self.final_edits[name].setText(_field(obj, name))
        if (
            not self._final_cover_url
            and self._final_local_cover is None
            and not self._final_cover_cleared  # respect a deliberate "Remove cover"
        ):
            url = _field(obj, "cover_url") or None
            if url:
                self._final_cover_url = url
                self._load_cover("final", url)

    # -- re-search ----------------------------------------------------
    def _search_again(self) -> None:
        artist = self.final_edits["artist"].text().strip()
        title = self.final_edits["title"].text().strip()
        if not title and not artist:
            return
        self.search_btn.setEnabled(False)
        self.search_btn.setText("Searching…")
        self.source_combo.setEnabled(False)

        self._search_worker = SearchWorker(artist, title, self.job.info)
        self._search_worker.done.connect(self._on_research_done)
        self._search_worker.failed.connect(self._on_research_failed)
        self._search_worker.start()

    def _on_research_done(self, meta) -> None:
        meta.source_file = self.job.metadata.source_file
        self.job.metadata = meta
        # Refresh the source side, but only fill empty Save-as fields.
        self._refresh_from_metadata()
        self._fill_empty_final_from(meta)
        self._reset_search_button()

    def _on_research_failed(self, message: str) -> None:
        self.bio_view.setPlainText(f"Search failed: {message}")
        self._reset_search_button()

    def _reset_search_button(self) -> None:
        self.search_btn.setEnabled(True)
        self.search_btn.setText("🔍 Search again (uses Save-as Title + Artist)")
        self.source_combo.setEnabled(True)

    # -- cover handling -----------------------------------------------
    def _load_cover(self, which: str, cover_url: str | None) -> None:
        if self._headless:
            return  # no UI to update and no network fetch wanted
        if which == "src":
            self._src_token += 1
            token = self._src_token
            label = self.src_cover_label
        elif which == "orig":
            self._orig_token += 1
            token = self._orig_token
            label = self.orig_cover_label
        else:
            self._final_token += 1
            token = self._final_token
            label = self.final_cover_label

        if not cover_url:
            label.setText("No cover")
            label.setPixmap(QPixmap())
            return

        label.setText("Loading…")
        loader = _CoverLoader(cover_url)
        loader.loaded.connect(
            lambda data, w=which, t=token, lbl=label: self._on_cover_loaded(w, t, lbl, data)
        )
        self._loaders.append(loader)
        loader.start()

    def _on_cover_loaded(self, which: str, token: int, label: QLabel, data: bytes) -> None:
        tokens = {"src": self._src_token, "orig": self._orig_token, "final": self._final_token}
        if token != tokens[which]:
            return  # a newer selection superseded this load
        self._set_pixmap(label, data)

    def _set_pixmap(self, label: QLabel, data: bytes) -> None:
        if not data:
            label.setText("No cover")
            label.setPixmap(QPixmap())
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            label.setPixmap(
                pixmap.scaled(
                    self.COVER_SIZE,
                    self.COVER_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def _choose_local_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover image", "", "Images (*.jpg *.jpeg *.png *.webp)"
        )
        if path:
            self._final_local_cover = Path(path)
            self._final_cover_url = None
            self._final_cover_cleared = False
            self._set_pixmap(self.final_cover_label, self._final_local_cover.read_bytes())

    def _open_cover_picker(self) -> None:
        from .cover_picker_dialog import CoverPickerDialog

        seed = f"{self.final_edits['artist'].text()} {self.final_edits['album'].text()}".strip()
        dialog = CoverPickerDialog(seed_query=seed, parent=self)
        if not dialog.exec() or dialog.choice is None:
            return
        kind, value = dialog.choice
        self._final_cover_cleared = False
        if kind == "local":
            self._final_local_cover = Path(value)
            self._final_cover_url = None
            self._final_token += 1  # invalidate any in-flight URL load
            self._set_pixmap(self.final_cover_label, self._final_local_cover.read_bytes())
        else:  # ("url", str) — a fresh online cover; embedded as-is on save
            self._final_local_cover = None
            self._final_cover_url = value
            self._load_cover("final", value)

    @classmethod
    def apply_default_metadata(cls, job, parent=None) -> None:
        """Write the preview's default Save-as values onto ``job.metadata`` with no UI.

        Builds the dialog headlessly so the per-song seeding runs, matching what a
        manual unedited Save would write. Used by bulk "Save & Apply to All".
        """
        dialog = cls(job, 1, 1, parent, headless=True)
        try:
            dialog._apply_edits()
        finally:
            dialog._stop_all_threads()
            dialog.deleteLater()

    # -- finishing ----------------------------------------------------
    def _apply_edits(self) -> None:
        meta = self.job.metadata
        meta.title = self.final_edits["title"].text().strip()
        meta.artist = self.final_edits["artist"].text().strip()
        meta.album = self.final_edits["album"].text().strip()
        meta.year = self.final_edits["year"].text().strip()
        meta.track = self.track_edit.text().strip()
        if self._final_local_cover is not None:
            meta.cover_path = str(self._final_local_cover)
            meta.cover_url = None
            meta.remove_cover = False
        elif self._final_cover_cleared:
            meta.cover_path = None
            meta.cover_url = None
            meta.remove_cover = True
        else:
            meta.cover_path = None
            meta.cover_url = self._final_cover_url
            meta.remove_cover = False

    # -- bulk navigation drafts ---------------------------------------
    def _capture_draft(self) -> dict:
        """Snapshot the Save-as column so a revisit can restore these edits."""
        return {
            "fields": {name: self.final_edits[name].text() for name in _FIELDS},
            "track": self.track_edit.text(),
            "cover_url": self._final_cover_url,
            "local_cover": str(self._final_local_cover) if self._final_local_cover else None,
            "cleared": self._final_cover_cleared,
        }

    def _restore_draft(self) -> None:
        draft = self._draft or {}
        for name in _FIELDS:
            self.final_edits[name].setText(draft.get("fields", {}).get(name, ""))
        self.track_edit.setText(draft.get("track", "") or "")

        local = draft.get("local_cover")
        if local and Path(local).exists():
            self._final_local_cover = Path(local)
            self._final_cover_url = None
            self._final_cover_cleared = False
            self._set_pixmap(self.final_cover_label, self._final_local_cover.read_bytes())
        elif draft.get("cleared"):
            self._clear_cover()
        else:
            self._final_local_cover = None
            self._final_cover_cleared = False
            self._final_cover_url = draft.get("cover_url")
            self._load_cover("final", self._final_cover_url)

    def _stop_thread(self, thread) -> None:
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(12000)

    def _stop_all_threads(self) -> None:
        self._stop_play()
        for loader in self._loaders:
            self._stop_thread(loader)
        self._loaders.clear()
        self._stop_thread(self._search_worker)

    # -- audio preview ------------------------------------------------
    def _toggle_play(self) -> None:
        if (
            self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        ):
            self._stop_play()
            return
        if not self.job.path.exists():
            self.bio_view.setPlainText("Can't play: file not found.")
            return
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._audio_out = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_out)
            self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.setSource(QUrl.fromLocalFile(str(self.job.path)))
        self._player.play()
        self.play_btn.setText("⏹  Stop")

    def _stop_play(self) -> None:
        if self._player is not None:
            self._player.stop()
        if hasattr(self, "play_btn"):
            self.play_btn.setText("▶  Play")

    def _on_playback_state(self, state) -> None:
        # Reset the label when playback ends on its own (or is stopped).
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self.play_btn.setText("▶  Play")

    def _finish(self, action: int) -> None:
        self._finished = True
        self.action = action
        # Capture the Save-as edits so a revisit can restore them.
        self.draft = self._capture_draft()
        self._stop_all_threads()
        if action in (self.SAVE, self.SAVE_ALL):
            self._apply_edits()
            self.accept()
        else:  # SKIP, CANCEL, BACK
            self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Closing via "X" or Esc means skip this one, not cancel the batch.
        if not self._finished:
            self.action = self.SKIP
            self.draft = self._capture_draft()
        self._stop_all_threads()
        super().closeEvent(event)
