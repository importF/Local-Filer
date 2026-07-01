"""Per-song album-cover picker.

Top: a searchable gallery of the covers cached in Covers/. Bottom: a free-text
online search (Genius + iTunes + Deezer) showing up to three previews. ``choice``
is set on Apply to ``("local", Path)`` or ``("url", str)``.
"""

from __future__ import annotations

import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import covers
from ..core.metadata import cover_search

_SAVED_SIZE = 96
_ONLINE_SIZE = 72
_SAVED_COLS = 5


class _ImgLoader(QThread):
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


class _SearchWorker(QThread):
    done = Signal(list)  # list[cover_search.CoverHit]

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self) -> None:
        try:
            self.done.emit(cover_search.search(self.query))
        except Exception:  # noqa: BLE001
            self.done.emit([])


class _Thumb(QLabel):
    """A clickable, selectable cover thumbnail carrying a payload."""

    clicked = Signal(object)  # emits self

    def __init__(self, size: int, payload):
        super().__init__()
        self.payload = payload  # ("local", Path) or ("url", CoverHit)
        self.selected = False
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("…")
        self._base_style = "border:1px solid #555; color:#888;"
        self.setStyleSheet(self._base_style)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_image(self, data: bytes) -> None:
        pix = QPixmap()
        if data and pix.loadFromData(data):
            self.setPixmap(
                pix.scaled(
                    self.width(), self.height(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.setText("no img")

    def set_selected(self, on: bool) -> None:
        self.selected = on
        self.setStyleSheet(
            ("border:3px solid #4d9bff;" if on else self._base_style)
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.clicked.emit(self)


class CoverPickerDialog(QDialog):
    def __init__(self, seed_query: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Album cover")
        self.setMinimumSize(560, 560)
        self.choice: tuple[str, object] | None = None

        self._selected: _Thumb | None = None
        self._saved_thumbs: list[_Thumb] = []
        self._online_thumbs: list[_Thumb] = []
        self._loaders: list[_ImgLoader] = []
        self._search_worker: _SearchWorker | None = None

        layout = QVBoxLayout(self)

        # --- Saved covers (fills most of the window) ---
        layout.addWidget(self._bold("Saved covers"))
        self.saved_search = QLineEdit()
        self.saved_search.setPlaceholderText("Search saved covers (artist / album)…")
        self.saved_search.textChanged.connect(self._filter_saved)
        layout.addWidget(self.saved_search)

        self._saved_host = QWidget()
        self._saved_grid = _FlowGrid(self._saved_host, _SAVED_COLS)
        saved_scroll = QScrollArea()
        saved_scroll.setWidgetResizable(True)
        saved_scroll.setWidget(self._saved_host)
        layout.addWidget(saved_scroll, 1)

        # --- Online search (smaller, image-only, tooltip = provider + title) ---
        row = QHBoxLayout()
        row.addWidget(self._bold("Search online"))
        row.addWidget(QLabel("Genius · iTunes · Deezer"))
        row.addStretch()
        layout.addLayout(row)

        search_row = QHBoxLayout()
        self.online_search = QLineEdit()
        self.online_search.setPlaceholderText("Search album art online…")
        self.online_search.setText(seed_query)
        self.online_search.returnPressed.connect(self._run_online_search)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._run_online_search)
        search_row.addWidget(self.online_search, 1)
        search_row.addWidget(self.search_btn)
        layout.addLayout(search_row)

        self._online_row = QHBoxLayout()
        self._online_status = QLabel("")
        self._online_status.setStyleSheet("color:#888;")
        self._online_row.addWidget(self._online_status)
        self._online_row.addStretch()
        layout.addLayout(self._online_row)

        # --- buttons ---
        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.apply_btn = QPushButton("Apply selected")
        self.apply_btn.setDefault(True)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        btns.addWidget(cancel_btn)
        btns.addWidget(self.apply_btn)
        layout.addLayout(btns)

        self._load_saved()

    # -- helpers ------------------------------------------------------
    def _bold(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight:bold;")
        return label

    def _load_saved(self) -> None:
        for path in covers.list_saved():
            thumb = _Thumb(_SAVED_SIZE, ("local", path))
            thumb.setToolTip(covers.display_name(path))
            thumb.clicked.connect(self._on_thumb_clicked)
            try:
                thumb.set_image(path.read_bytes())
            except OSError:
                thumb.setText("no img")
            self._saved_thumbs.append(thumb)
        self._filter_saved("")
        if not self._saved_thumbs:
            self.saved_search.setPlaceholderText("No saved covers yet — search online below.")

    def _filter_saved(self, text: str) -> None:
        needle = text.strip().lower()
        visible = [
            t for t in self._saved_thumbs
            if not needle or needle in (t.toolTip() or "").lower()
        ]
        self._saved_grid.set_items(visible)

    def _run_online_search(self) -> None:
        query = self.online_search.text().strip()
        if not query:
            return
        self.search_btn.setEnabled(False)
        self._online_status.setText("Searching…")
        self._clear_online_thumbs()
        self._search_worker = _SearchWorker(query)
        self._search_worker.done.connect(self._on_search_done)
        self._search_worker.start()

    def _on_search_done(self, hits) -> None:
        self.search_btn.setEnabled(True)
        if not hits:
            self._online_status.setText("No covers found.")
            return
        self._online_status.setText("")
        for hit in hits:
            thumb = _Thumb(_ONLINE_SIZE, ("url", hit))
            thumb.setToolTip(f"{hit.provider.capitalize()} — {hit.label}")
            thumb.clicked.connect(self._on_thumb_clicked)
            self._online_thumbs.append(thumb)
            self._online_row.insertWidget(self._online_row.count() - 1, thumb)
            loader = _ImgLoader(hit.cover_url)
            loader.loaded.connect(thumb.set_image)
            self._loaders.append(loader)
            loader.start()

    def _clear_online_thumbs(self) -> None:
        for thumb in self._online_thumbs:
            if thumb is self._selected:
                self._selected = None
                self.apply_btn.setEnabled(False)
            thumb.setParent(None)
            thumb.deleteLater()
        self._online_thumbs.clear()

    def _on_thumb_clicked(self, thumb: _Thumb) -> None:
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = thumb
        thumb.set_selected(True)
        self.apply_btn.setEnabled(True)

    def _apply(self) -> None:
        if self._selected is None:
            return
        kind, value = self._selected.payload
        if kind == "local":
            self.choice = ("local", value)
        else:  # ("url", CoverHit)
            self.choice = ("url", value.cover_url)
        self._stop_threads()
        self.accept()

    def _stop_threads(self) -> None:
        for loader in self._loaders:
            if loader.isRunning():
                loader.quit()
                loader.wait(8000)
        self._loaders.clear()
        if self._search_worker is not None and self._search_worker.isRunning():
            self._search_worker.quit()
            self._search_worker.wait(8000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._stop_threads()
        super().closeEvent(event)


class _FlowGrid:
    """Lays a list of fixed-size widgets into a fixed-column grid in ``host``."""

    def __init__(self, host: QWidget, columns: int):
        from PySide6.QtWidgets import QGridLayout
        self._host = host
        self._columns = columns
        self._layout = QGridLayout(host)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

    def set_items(self, widgets: list[QWidget]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for i, widget in enumerate(widgets):
            widget.setParent(self._host)
            widget.show()
            self._layout.addWidget(widget, i // self._columns, i % self._columns)
