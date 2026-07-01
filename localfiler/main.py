"""Entry point. Run with:  python -m localfiler.main"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from . import config
from .core import setup
from .gui.main_window import MainWindow
from .gui.setup_dialog import SetupDialog


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Local Filer")

    config.ensure_dirs()
    # yt-dlp + ffmpeg/ffprobe are required. If any is missing, run the mandatory
    # one-time setup — it can't be skipped, and we refuse to launch until they're
    # all in place.
    if setup.missing_components():
        SetupDialog().exec()
        if setup.missing_components():
            QMessageBox.critical(
                None,
                "Setup required",
                "Local Filer needs yt-dlp and ffmpeg to run, and setup didn't "
                "finish. Please relaunch and complete the one-time setup.",
            )
            return 1

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
