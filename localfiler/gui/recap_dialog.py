"""End-of-bulk recap: a scrollable list of every song and the tags applied.

Saved songs show their cover + tags with an Edit button; skipped downloads show
an Open button (deleted only when the recap closes). Covers are interactive:
left-click to copy then click others to paste, or right-click to apply to all
songs / the album. Returns ``action`` (CLOSE/EDIT) + ``edit_index``.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import covers
from ..core.tagging import read_cover


class _CoverThumb(QLabel):
    """A saved song's cover thumbnail; emits clicks carrying the song's index."""

    left_clicked = Signal(int)
    right_clicked = Signal(int, object)  # index, global QPoint

    def __init__(self, index: int, size: int):
        super().__init__()
        self.index = index
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._copied = False
        self._paste_mode = False
        self._apply_style()
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_image(self, data: bytes | None) -> None:
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            self.setText("")
            self.setPixmap(
                pixmap.scaled(
                    self.width(), self.height(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.setPixmap(QPixmap())
            self.setText("♪")

    def set_copied(self, on: bool) -> None:
        self._copied = on
        self._apply_style()

    def set_paste_mode(self, on: bool) -> None:
        self._paste_mode = on
        self.setToolTip("Click to paste the copied cover here" if on and not self._copied else "")
        self._apply_style()

    def _apply_style(self) -> None:
        if self._copied:
            self.setStyleSheet("border:3px solid #ffce3d; color:#888;")
        elif self._paste_mode:
            self.setStyleSheet("border:2px dashed #4d9bff; color:#888;")
        else:
            self.setStyleSheet("border:1px solid #888; color:#888;")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.left_clicked.emit(self.index)
        elif event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit(self.index, event.globalPosition().toPoint())


class RecapDialog(QDialog):
    CLOSE = 0
    EDIT = 1
    THUMB_SIZE = 56

    def __init__(self, jobs, statuses, parent=None, apply_cover_cb=None, drafts=None):
        super().__init__(parent)
        self.jobs = jobs
        self.statuses = statuses
        self._drafts = drafts  # per-index Save-as drafts (so cover edits persist)
        # Re-saves a job after its cover changed: (job) -> bool. None disables copy/paste.
        self._apply_cover_cb = apply_cover_cb
        self.action = self.CLOSE
        self.edit_index: int | None = None

        self._thumbs: dict[int, _CoverThumb] = {}
        self._copied_cover = None   # Path of the cover currently "copied"
        self._copy_source: int | None = None

        self.setWindowTitle("Batch recap")
        self.setMinimumSize(600, 520)

        saved_count = statuses.count("saved")
        intro = QLabel(
            f"{saved_count} of {len(jobs)} saved. Click <b>Edit</b> to fix a song. "
            "Left-click a cover to copy it, then click other songs to paste; "
            "right-click a cover to apply it to all songs or the whole album."
        )
        intro.setWordWrap(True)

        self.copy_note = QLabel("")
        self.copy_note.setStyleSheet("color:#ffce3d;")
        self.copy_note.setVisible(False)

        last_saved = max((i for i, s in enumerate(statuses) if s == "saved"), default=None)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Default order", "Album", "Artist", "Name"])
        self.sort_combo.currentTextChanged.connect(self._rebuild_rows)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort:"))
        sort_row.addWidget(self.sort_combo)
        sort_row.addStretch()

        self._rows_box = QVBoxLayout()
        inner = QWidget()
        inner.setLayout(self._rows_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        self._rebuild_rows("Default order")

        buttons = QDialogButtonBox()
        if last_saved is not None:
            back_btn = QPushButton("←  Back (edit last)")
            back_btn.clicked.connect(lambda: self._edit(last_saved))
            buttons.addButton(back_btn, QDialogButtonBox.ButtonRole.ActionRole)
        close_btn = buttons.addButton("Close", QDialogButtonBox.ButtonRole.AcceptRole)
        close_btn.setDefault(True)
        close_btn.clicked.connect(self._close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.copy_note)
        layout.addLayout(sort_row)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    def _order_indices(self, mode: str) -> list[int]:
        order = list(range(len(self.jobs)))
        if mode == "Album":
            order.sort(key=lambda i: (self.jobs[i].metadata.album or "").lower())
        elif mode == "Artist":
            order.sort(key=lambda i: (self.jobs[i].metadata.artist or "").lower())
        elif mode == "Name":
            order.sort(key=lambda i: (self.jobs[i].metadata.title or self.jobs[i].path.stem).lower())
        return order

    def _rebuild_rows(self, mode: str) -> None:
        self._cancel_copy()
        while self._rows_box.count():
            item = self._rows_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._thumbs.clear()
        for i in self._order_indices(mode):
            self._rows_box.addWidget(self._row(i, self.jobs[i], self.statuses[i]))
        self._rows_box.addStretch()

    # ------------------------------------------------------------------ rows
    def _row(self, index: int, job, status: str | None) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(frame)

        if status == "saved":
            thumb = _CoverThumb(index, self.THUMB_SIZE)
            thumb.set_image(self._cover_bytes(job))
            if self._apply_cover_cb is not None:
                thumb.left_clicked.connect(self._on_left)
                thumb.right_clicked.connect(self._on_right)
            self._thumbs[index] = thumb
            row.addWidget(thumb, 0, Qt.AlignmentFlag.AlignTop)

            meta = job.metadata
            title = (meta.title or job.path.stem).strip()
            artist = (meta.artist or "").strip()
            head = f"<b>{_esc(title)}</b>" + (f" — {_esc(artist)}" if artist else "")

            bits = []
            if meta.album:
                bits.append(_esc(meta.album))
            if meta.year:
                bits.append(_esc(str(meta.year)))
            if meta.track:
                bits.append(f"track {_esc(str(meta.track))}")
            sub = "  •  ".join(bits) if bits else "(no album/year/track)"

            text = QLabel(f"{head}<br><span style='color:#888;'>{sub}</span>")
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setWordWrap(True)
            dest = QLabel(f"→ {job.path.parent}")
            dest.setStyleSheet("color: #888; font-size: 11px;")

            col = QVBoxLayout()
            col.addWidget(text)
            col.addWidget(dest)
            row.addLayout(col, 1)

            edit_btn = QPushButton("Edit")
            edit_btn.setFixedWidth(72)
            edit_btn.clicked.connect(lambda _=False, i=index: self._edit(i))
            row.addWidget(edit_btn, 0, Qt.AlignmentFlag.AlignTop)
        else:
            note = "will be deleted on close" if job.is_download else "skipped (unchanged)"
            label = QLabel(f"{_esc(job.path.stem)} — <i>{note}</i>")
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(True)
            label.setStyleSheet("color: #c06; ")
            row.addWidget(label, 1)

            open_btn = QPushButton("Open")
            open_btn.setFixedWidth(72)
            open_btn.setToolTip("Reopen this song's preview to tag and keep it")
            open_btn.clicked.connect(lambda _=False, i=index: self._edit(i))
            row.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)

        return frame

    def _cover_bytes(self, job) -> bytes | None:
        try:
            return read_cover(job.path)
        except Exception:  # noqa: BLE001 - never let a bad file break the recap
            return None

    # ----------------------------------------------------------- cover copy/paste
    def _on_left(self, index: int) -> None:
        if self._copied_cover is None:
            self._start_copy(index)
        elif index == self._copy_source:
            self._cancel_copy()  # clicking the source again cancels
        else:
            self._apply_to([index], self._copied_cover)

    def _on_right(self, index: int, pos) -> None:
        menu = QMenu(self)
        if self._copied_cover is not None and index != self._copy_source:
            menu.addAction("Paste copied cover here",
                           lambda: self._apply_to([index], self._copied_cover))
            menu.addSeparator()
        all_action = menu.addAction("Apply this cover to all songs")
        album = (self.jobs[index].metadata.album or "").strip()
        album_action = menu.addAction("Apply this cover to this album")
        album_action.setEnabled(bool(album))
        if self._copied_cover is not None:
            menu.addSeparator()
            menu.addAction("Cancel copy", self._cancel_copy)

        chosen = menu.exec(pos)
        if chosen is None:
            return
        if chosen is all_action:
            self._apply_source_cover_to(index, self._saved_indices())
        elif chosen is album_action:
            targets = [i for i in self._saved_indices()
                       if (self.jobs[i].metadata.album or "").strip().lower() == album.lower()]
            self._apply_source_cover_to(index, targets)

    def _start_copy(self, index: int) -> None:
        cover = self._cover_file_of(index)
        if cover is None:
            self.copy_note.setText("That song has no cover to copy.")
            self.copy_note.setVisible(True)
            return
        self._copied_cover = cover
        self._copy_source = index
        title = (self.jobs[index].metadata.title or self.jobs[index].path.stem).strip()
        self.copy_note.setText(
            f"📋 Copied “{title}” cover — click a song to paste, or right-click → Cancel."
        )
        self.copy_note.setVisible(True)
        for i, thumb in self._thumbs.items():
            thumb.set_paste_mode(i != index)
            thumb.set_copied(i == index)

    def _cancel_copy(self) -> None:
        self._copied_cover = None
        self._copy_source = None
        self.copy_note.setVisible(False)
        for thumb in self._thumbs.values():
            thumb.set_paste_mode(False)
            thumb.set_copied(False)

    def _apply_source_cover_to(self, source_index: int, targets: list[int]) -> None:
        cover = self._cover_file_of(source_index)
        if cover is None:
            self.copy_note.setText("That song has no cover to apply.")
            self.copy_note.setVisible(True)
            return
        self._apply_to(targets, cover)

    def _apply_to(self, indices: list[int], cover_path) -> None:
        if self._apply_cover_cb is None:
            return
        for index in indices:
            job = self.jobs[index]
            job.metadata.cover_path = str(cover_path)
            job.metadata.cover_url = None
            job.metadata.remove_cover = False
            # Keep the saved draft in sync so Edit shows the pasted cover.
            if self._drafts is not None and index in self._drafts:
                draft = self._drafts[index]
                draft["local_cover"] = str(cover_path)
                draft["cover_url"] = None
                draft["cleared"] = False
            if self._apply_cover_cb(job):
                thumb = self._thumbs.get(index)
                if thumb is not None:
                    thumb.set_image(self._cover_bytes(job))

    def _cover_file_of(self, index: int):
        """A local file holding song ``index``'s current cover (or None)."""
        job = self.jobs[index]
        data = self._cover_bytes(job)
        if not data:
            return None
        meta = job.metadata
        return covers.save_bytes(data, meta.artist or "", meta.album or "", meta.title or "")

    def _saved_indices(self) -> list[int]:
        return [i for i, s in enumerate(self.statuses) if s == "saved"]

    # -------------------------------------------------------------- finishing
    def _edit(self, index: int) -> None:
        self.action = self.EDIT
        self.edit_index = index
        self.accept()

    def _close(self) -> None:
        self.action = self.CLOSE
        self.edit_index = None
        self.accept()


def _esc(text: str) -> str:
    return html.escape(str(text))
