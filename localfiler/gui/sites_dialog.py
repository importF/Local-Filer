"""Supported sites dialog — fetches yt-dlp's list and shows it with a search box."""

from __future__ import annotations

import re

import requests
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

_SITES_URL = (
    "https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/supportedsites.md"
)
# Matches lines like: " - **extractor**: Display Name" or " - **extractor**"
_LINE_RE = re.compile(r"^\s*-\s+\*\*[^*]+\*\*(?::\s*(.+))?", re.MULTILINE)
_NAME_RE = re.compile(r"^\s*-\s+\*\*([^*]+)\*\*(?::\s*(.+))?")


def _parse(markdown: str) -> list[str]:
    """Return sorted unique display names from the markdown."""
    names: set[str] = set()
    for line in markdown.splitlines():
        m = _NAME_RE.match(line)
        if m:
            display = (m.group(2) or m.group(1) or "").strip()
            if display:
                names.add(display)
    return sorted(names, key=str.casefold)


class _FetchWorker(QThread):
    done = Signal(list)   # list[str] of site names
    failed = Signal(str)

    def run(self) -> None:
        try:
            resp = requests.get(_SITES_URL, timeout=15)
            resp.raise_for_status()
            self.done.emit(_parse(resp.text))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class SitesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Supported download sites (yt-dlp)")
        self.setMinimumSize(480, 560)
        self._all_sites: list[str] = []
        self._worker: _FetchWorker | None = None

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search sites…")
        self.search.textChanged.connect(self._filter)
        self.search.setEnabled(False)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)

        self.status = QLabel("Fetching list from GitHub…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_widget.addItem(QListWidgetItem("Loading…"))

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(self.status)
        layout.addWidget(close_btn)

        self._fetch()

    def _fetch(self) -> None:
        self._worker = _FetchWorker()
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_done(self, sites: list[str]) -> None:
        self._all_sites = sites
        self.status.setText(f"{len(sites):,} supported sites")
        self.search.setEnabled(True)
        self._populate(sites)

    def _on_failed(self, msg: str) -> None:
        self.list_widget.clear()
        self.status.setText(f"Failed to load: {msg}")

    def _populate(self, sites: list[str]) -> None:
        self.list_widget.clear()
        for name in sites:
            self.list_widget.addItem(QListWidgetItem(name))

    def _filter(self, text: str) -> None:
        q = text.strip().casefold()
        if not q:
            self._populate(self._all_sites)
        else:
            self._populate([s for s in self._all_sites if q in s.casefold()])

    def closeEvent(self, event):  # noqa: N802
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(5000)
        super().closeEvent(event)
