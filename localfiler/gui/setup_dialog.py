"""First-run setup dialog (mandatory).

Shown at startup when a required binary is missing. Downloads it on a background
``SetupWorker`` with a progress bar + status line. It can't be skipped: there's
no Cancel button, the window can't be closed while an install is running, and if
it's closed before setup finishes the app refuses to launch (see ``main.py``).
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..core import setup
from .worker import SetupWorker

# Rough download sizes, just for the heads-up text.
_TASK_BLURB = {
    "yt-dlp": "yt-dlp (the download engine, ~18 MB)",
    "ffmpeg": "ffmpeg + ffprobe (audio conversion, ~110 MB)",
}

# Bar runs 0–1000 for smooth byte-level updates.
_BAR_MAX = 1000

# A little spinner so the user always sees motion — even when the byte counter
# and bar sit still (e.g. while the 110 MB ffmpeg archive is being unzipped).
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPIN_INTERVAL_MS = 120


class SetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Local Filer — Setup")
        self.setMinimumWidth(440)
        self._worker: SetupWorker | None = None
        self._tasks = setup.install_plan()

        self._intro = QLabel()
        self._intro.setWordWrap(True)

        self._bar = QProgressBar()
        self._bar.setRange(0, _BAR_MAX)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        self._bar.hide()  # only shown once setup is running

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #888;")
        self._status.hide()

        # "Still working" heartbeat: an animated spinner + elapsed seconds, so a
        # long download / silent unzip never looks frozen.
        self._activity = QLabel("")
        self._activity.setStyleSheet("color: #5599ff;")
        self._activity.hide()
        self._spin_frame = 0
        self._elapsed_ms = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(_SPIN_INTERVAL_MS)
        self._spin_timer.timeout.connect(self._tick)

        self._run_btn = QPushButton("Run setup")
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self._start)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        self._close_btn.hide()

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self._run_btn)
        buttons.addWidget(self._close_btn)

        layout = QVBoxLayout()
        layout.addWidget(self._intro)
        layout.addSpacing(8)
        layout.addWidget(self._bar)
        layout.addWidget(self._status)
        layout.addWidget(self._activity)
        layout.addStretch()
        layout.addLayout(buttons)
        self.setLayout(layout)

        self._show_initial()

    # ------------------------------------------------------------- UI states
    def _show_initial(self) -> None:
        if not self._tasks:
            # Nothing missing.
            self._intro.setText(
                "Everything's already installed — you're good to go. ✓"
            )
            self._run_btn.hide()
            self._close_btn.show()
            return
        items = "".join(f"<li>{_TASK_BLURB.get(t, t)}</li>" for t in self._tasks)
        self._intro.setText(
            "<b>Local Filer needs a couple of components to download and "
            "convert audio.</b><br>This one-time setup will fetch:"
            f"<ul>{items}</ul>The files are saved next to the app."
        )

    def _start(self) -> None:
        self._run_btn.setEnabled(False)
        self._bar.setValue(0)
        self._bar.show()
        self._status.setText("Starting…")
        self._status.show()
        self._elapsed_ms = 0
        self._activity.show()
        self._spin_timer.start()

        worker = SetupWorker(self._tasks)
        worker.status.connect(self._status.setText)
        worker.progress_value.connect(self._on_progress)
        worker.done.connect(self._on_done)
        self._worker = worker  # keep a reference so the QThread isn't GC'd
        worker.start()

    # ------------------------------------------------------------- callbacks
    def _tick(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(_SPINNER)
        self._elapsed_ms += _SPIN_INTERVAL_MS
        self._activity.setText(
            f"{_SPINNER[self._spin_frame]}  still working… ({self._elapsed_ms // 1000}s)"
        )

    def _on_progress(self, fraction: float) -> None:
        self._bar.setValue(int(max(0.0, min(1.0, fraction)) * _BAR_MAX))

    def _on_done(self, failed: list) -> None:
        self._spin_timer.stop()
        self._activity.hide()
        self._bar.setValue(_BAR_MAX if not failed else self._bar.value())
        if not failed:
            self._status.setStyleSheet("color: #10b981;")  # green
            self._status.setText("All set — everything's installed. ✓")
            self._close_btn.show()
            self._close_btn.setDefault(True)
            return
        # Retry just the still-missing pieces.
        self._tasks = setup.install_plan()
        names = ", ".join(failed)
        self._status.setStyleSheet("color: #ef4444;")  # red
        self._status.setText(
            f"Couldn't install: {names}. Check your connection and retry."
        )
        self._run_btn.setText("Retry")
        self._run_btn.setEnabled(True)
        self._close_btn.show()  # give up → the app won't launch

    # ---------------------------------------------------------- close guarding
    def _install_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def closeEvent(self, event) -> None:
        # Can't bail mid-install (also avoids destroying a live QThread).
        if self._install_running():
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self) -> None:
        # No cancel: ignore Esc / window-close while an install is in progress.
        if self._install_running():
            return
        super().reject()
