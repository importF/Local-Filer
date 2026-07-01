"""Live progress window for a parallel Tag Folder scan.

An overall bar plus a card per in-flight file, each showing provider "chips" that
recolour as they run and finish. Cards appear on file_started and vanish on
file_done. Driven by ScanWorker signals.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# (provider key as emitted by the aggregator, short chip label)
_PROVIDERS = [
    ("genius", "Genius"),
    ("itunes", "iTunes"),
    ("deezer", "Deezer"),
    ("musicbrainz", "MusicBrainz"),
    ("source", "YouTube"),
]

# status -> (background, text colour)
_CHIP_COLORS = {
    "pending": ("#1d1d26", "#5b6172"),
    "running": ("#6366f1", "#fff"),
    "ok": ("#10b981", "#05231a"),
    "no_match": ("#2a2f3a", "#8a93a6"),
    "skipped": ("#f59e0b", "#3a2905"),
    "error": ("#f43f5e", "#fff"),
}


def _chip_style(status: str) -> str:
    bg, fg = _CHIP_COLORS.get(status, _CHIP_COLORS["pending"])
    return (
        f"background:{bg}; color:{fg}; border-radius:10px; "
        "padding:3px 10px; font-size:10px; font-weight:700; letter-spacing:.3px;"
    )


class _FileCard(QFrame):
    """One in-flight file: its name + a chip per provider."""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame{background:#14141b; border:1px solid #23232e;"
            " border-left:3px solid #6366f1; border-radius:8px;}"
        )

        short = Path(name).stem
        title = QLabel(short)
        title.setStyleSheet("border:none; color:#e7e9ee; font-weight:600;")
        title.setWordWrap(False)
        title.setFixedWidth(170)
        metrics = title.fontMetrics()
        title.setText(metrics.elidedText(short, Qt.TextElideMode.ElideRight, 168))
        title.setToolTip(name)

        self.chips: dict[str, QLabel] = {}
        chip_row = QHBoxLayout()
        chip_row.setSpacing(5)
        for key, label in _PROVIDERS:
            chip = QLabel(label)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setStyleSheet(_chip_style("pending"))
            self.chips[key] = chip
            chip_row.addWidget(chip)

        row = QHBoxLayout(self)
        row.addWidget(title, 1)
        row.addLayout(chip_row)

    def set_status(self, provider: str, status: str) -> None:
        chip = self.chips.get(provider)
        if chip is not None:
            chip.setStyleSheet(_chip_style(status))


class ScanProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Looking up metadata…")
        self.setMinimumSize(620, 360)

        self._total = 0
        self._done = 0
        self._cards: dict[int, _FileCard] = {}
        # Buffer provider updates that arrive before their file's card exists.
        self._pending: dict[int, list[tuple[str, str]]] = {}

        self.setStyleSheet("QDialog{background:#0d0d12;}")

        self.heading = QLabel("Scanning folder…")
        self.heading.setStyleSheet("font-size:15px; font-weight:700; color:#f1f2f6;")

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate until the total is known
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(7)
        self.bar.setStyleSheet(
            "QProgressBar{background:#1d1d26; border:none; border-radius:4px;}"
            "QProgressBar::chunk{background:#6366f1; border-radius:4px;}"
        )

        self._cards_layout = QVBoxLayout()
        self._cards_layout.addStretch()
        inner = QWidget()
        inner.setLayout(self._cards_layout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        layout = QVBoxLayout(self)
        now = QLabel("NOW LOOKING UP")
        now.setStyleSheet("color:#5b6172; font-size:10px; font-weight:700; letter-spacing:1px;")

        layout.addWidget(self.heading)
        layout.addWidget(self.bar)
        layout.addWidget(now)
        layout.addWidget(scroll, 1)

    # -- slots wired to ScanWorker signals ----------------------------
    def set_total(self, total: int) -> None:
        self._total = total
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(0)
        self._update_heading()

    def on_file_started(self, index: int, name: str) -> None:
        if index in self._cards:
            return
        card = _FileCard(name, self)
        self._cards[index] = card
        # Insert above the trailing stretch.
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        for provider, status in self._pending.pop(index, []):
            card.set_status(provider, status)

    def on_provider_update(self, index: int, provider: str, status: str) -> None:
        card = self._cards.get(index)
        if card is None:
            self._pending.setdefault(index, []).append((provider, status))
            return
        card.set_status(provider, status)

    def on_file_done(self, index: int) -> None:
        self._pending.pop(index, None)
        card = self._cards.pop(index, None)
        if card is not None:
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._done += 1
        if self._total:
            self.bar.setValue(self._done)
        self._update_heading()

    def _update_heading(self) -> None:
        if self._total:
            self.heading.setText(f"Looking up metadata…  {self._done} / {self._total} files")
