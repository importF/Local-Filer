"""Self-update progress dialog. Downloads + stages the new release with a
progress bar + spinner (same look as SetupDialog), then hands off to a
detached script and quits the app so the swap can happen.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..core import updater
from .worker import AppUpdateDownloadWorker

_BAR_MAX = 1000
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPIN_INTERVAL_MS = 120


class UpdateDialog(QDialog):
    def __init__(self, tag: str, url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Local Filer — Update")
        self.setMinimumWidth(440)
        self._worker: AppUpdateDownloadWorker | None = None
        self._url = url

        self._intro = QLabel(f"Downloading Local Filer {tag}…")
        self._intro.setWordWrap(True)

        self._bar = QProgressBar()
        self._bar.setRange(0, _BAR_MAX)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")

        self._status = QLabel("Starting…")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #888;")

        # Same "still working" heartbeat as SetupDialog.
        self._activity = QLabel("")
        self._activity.setStyleSheet("color: #5599ff;")
        self._spin_frame = 0
        self._elapsed_ms = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(_SPIN_INTERVAL_MS)
        self._spin_timer.timeout.connect(self._tick)

        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        self._close_btn.hide()

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self._close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self._intro)
        layout.addSpacing(8)
        layout.addWidget(self._bar)
        layout.addWidget(self._status)
        layout.addWidget(self._activity)
        layout.addStretch()
        layout.addLayout(buttons)

        self._start()

    def _start(self) -> None:
        self._spin_timer.start()
        worker = AppUpdateDownloadWorker(self._url)
        worker.status.connect(self._status.setText)
        worker.progress_value.connect(self._on_progress)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        self._worker = worker  # keep a reference so the QThread isn't GC'd
        worker.start()

    def _tick(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(_SPINNER)
        self._elapsed_ms += _SPIN_INTERVAL_MS
        self._activity.setText(
            f"{_SPINNER[self._spin_frame]}  still working… ({self._elapsed_ms // 1000}s)"
        )

    def _on_progress(self, fraction: float) -> None:
        self._bar.setValue(int(max(0.0, min(1.0, fraction)) * _BAR_MAX))

    def _on_done(self, staged_dir: str) -> None:
        self._spin_timer.stop()
        self._activity.hide()
        self._bar.setValue(_BAR_MAX)
        self._status.setStyleSheet("color: #10b981;")
        self._status.setText("Downloaded — restarting to finish the update…")
        # Give the "Downloaded" message a beat on screen before the app quits.
        QTimer.singleShot(600, lambda: self._apply(Path(staged_dir)))

    def _apply(self, staged_dir: Path) -> None:
        bat_path = updater.write_apply_script(staged_dir)
        updater.launch_apply_and_exit(bat_path)
        QApplication.instance().quit()

    def _on_failed(self, message: str) -> None:
        self._spin_timer.stop()
        self._activity.hide()
        self._status.setStyleSheet("color: #ef4444;")
        self._status.setText(f"Update failed: {message}")
        self._close_btn.show()

    # ---------------------------------------------------------- close guarding
    def _download_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def closeEvent(self, event) -> None:
        if self._download_running():
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self) -> None:
        if self._download_running():
            return
        super().reject()
