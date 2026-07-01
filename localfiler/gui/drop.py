"""A QWidget that accepts drag-and-drop and reports what was dropped via callbacks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QWidget


class DropTarget(QWidget):
    """Calls a handler when a URL or path is dropped on it.

    ``on_web_url`` fires for a dropped web link (or URL-like text); ``on_local_path``
    fires for a dropped file/folder. Pass only the handler(s) a tab cares about.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_web_url: Callable[[str], None] | None = None,
        on_local_path: Callable[[Path], None] | None = None,
    ):
        super().__init__(parent)
        self._on_web_url = on_web_url
        self._on_local_path = on_local_path
        self.setAcceptDrops(True)

    def _match(self, mime):
        """Return ('web', text) or ('path', Path) for an acceptable drop, else None."""
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    if self._on_local_path is not None:
                        return ("path", Path(url.toLocalFile()))
                elif self._on_web_url is not None:
                    return ("web", url.toString())
        if self._on_web_url is not None and mime.hasText():
            text = mime.text().strip()
            # Only web URLs — not arbitrary text, and not Qt's "file:///…".
            if "://" in text and not text.lower().startswith("file:"):
                return ("web", text)
        return None

    # -- Qt drag/drop overrides ---------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._match(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        if self._match(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        hit = self._match(event.mimeData())
        if hit is None:
            return
        kind, value = hit
        if kind == "web":
            self._on_web_url(value)
        else:
            self._on_local_path(value)
        event.acceptProposedAction()
