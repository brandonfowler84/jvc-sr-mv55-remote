"""The user-configurable strip of remote keys under the transport controls.

Most of the wired-remote table duplicates a direct opcode that the Transport,
Record or DVD panels already expose.  A handful do not -- tracking, TBC,
counter reset, CM skip and friends -- and those are the ones you reach for
repeatedly while a capture is running.  Keeping them beside the transport
buttons means never switching tabs mid-tape.

Which keys appear is a preference: everyone's tape workflow is different.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from .. import protocol as P
from ..protocol import Deck
from .panels import FlowWidget

#: Sensible starting set: the keys with no direct-opcode equivalent, plus
#: Audio, which is the one people look for first.
DEFAULT_QUICK_KEYS: tuple[int, ...] = (
    0x41,  # Tracking +
    0x42,  # Tracking -
    0x88,  # TBC On/Off
    0x39,  # Counter Reset
    0x96,  # CM Skip
    0xDC,  # Instant Replay
    0x17,  # Audio
    0x38,  # Display
)

MAX_QUICK_KEYS = 12


def parse_codes(raw) -> list[int]:
    """Read a stored preference back into a list of key codes."""
    if not raw:
        return list(DEFAULT_QUICK_KEYS)
    if isinstance(raw, str):
        parts = [p for p in raw.replace(",", " ").split() if p]
    else:
        parts = list(raw)
    codes: list[int] = []
    for part in parts:
        try:
            code = int(part, 16) if isinstance(part, str) else int(part)
        except (TypeError, ValueError):
            continue
        if code in P.REMOTE_CODE_NAMES and code not in codes:
            codes.append(code)
    return codes or list(DEFAULT_QUICK_KEYS)


def format_codes(codes) -> str:
    return " ".join(f"{c:02X}" for c in codes)


class QuickKeysBar(FlowWidget):
    """A row of remote keys, wrapping as the window narrows."""

    def __init__(self, controller, codes=None) -> None:
        super().__init__(spacing=5)
        from .panels import button  # local import: avoids a circular import

        self._button = button
        self.controller = controller
        self._codes = list(codes or DEFAULT_QUICK_KEYS)
        self._deck = Deck.VCR
        self._compact = False

        self._layout = self.flow
        self._rebuild()

    # -- configuration -----------------------------------------------------

    @property
    def codes(self) -> list[int]:
        return list(self._codes)

    def set_codes(self, codes) -> None:
        self._codes = list(codes)
        self._rebuild()

    def set_deck(self, deck: Deck) -> None:
        self._deck = deck
        self._rebuild()

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self._rebuild()

    # -- rendering ---------------------------------------------------------

    def _rebuild(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        by_code = {k.code: k for k in P.REMOTE_KEYS}
        for code in self._codes:
            key = by_code.get(code)
            if key is None:
                continue
            btn = self._button(key.name, role="compact")
            scope = f"{key.deck.value} deck only" if key.deck else "Both decks"
            btn.setToolTip(f"{key.name}\ncode 0x{key.code:02X} · {scope}")
            # Greyed rather than hidden, so the strip keeps a stable shape as
            # you switch decks instead of reflowing under the cursor.
            btn.setEnabled(key.deck is None or key.deck is self._deck)
            btn.clicked.connect(
                lambda _=False, c=code: self.controller.send_remote(c)
            )
            self._layout.addWidget(btn)
        self.updateGeometry()


class QuickKeysDialog(QDialog):
    """Pick which remote keys appear in the strip."""

    def __init__(self, codes, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure quick keys")
        self.resize(620, 520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose the remote keys to keep beside the transport controls. "
            f"Up to {MAX_QUICK_KEYS}; drag to reorder."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        columns = QHBoxLayout()

        available_box = QVBoxLayout()
        available_box.addWidget(QLabel("Available keys"))
        self.available = QListWidget()
        self.available.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for key in sorted(P.REMOTE_KEYS, key=lambda k: (k.group, k.name)):
            item = QListWidgetItem(f"{key.name}   ({key.group})")
            item.setData(Qt.UserRole, key.code)
            self.available.addItem(item)
        available_box.addWidget(self.available, 1)
        columns.addLayout(available_box, 1)

        middle = QVBoxLayout()
        middle.addStretch(1)
        add = QPushButton("Add  →")
        add.clicked.connect(self._add)
        middle.addWidget(add)
        remove = QPushButton("←  Remove")
        remove.clicked.connect(self._remove)
        middle.addWidget(remove)
        reset = QPushButton("Restore defaults")
        reset.clicked.connect(self._reset)
        middle.addWidget(reset)
        middle.addStretch(1)
        columns.addLayout(middle)

        chosen_box = QVBoxLayout()
        chosen_box.addWidget(QLabel("Shown in the strip"))
        self.chosen = QListWidget()
        self.chosen.setDragDropMode(QAbstractItemView.InternalMove)
        chosen_box.addWidget(self.chosen, 1)
        columns.addLayout(chosen_box, 1)

        layout.addLayout(columns, 1)

        self.warning = QLabel("")
        self.warning.setProperty("role", "warn")
        layout.addWidget(self.warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_chosen(codes)

    # -- editing -----------------------------------------------------------

    def _fill_chosen(self, codes) -> None:
        self.chosen.clear()
        names = P.REMOTE_CODE_NAMES
        for code in codes:
            if code in names:
                item = QListWidgetItem(names[code])
                item.setData(Qt.UserRole, code)
                self.chosen.addItem(item)

    def _add(self) -> None:
        existing = set(self.selected_codes())
        for item in self.available.selectedItems():
            code = item.data(Qt.UserRole)
            if code in existing:
                continue
            if self.chosen.count() >= MAX_QUICK_KEYS:
                self.warning.setText(
                    f"That is the maximum of {MAX_QUICK_KEYS}. Remove one first."
                )
                return
            new = QListWidgetItem(P.REMOTE_CODE_NAMES[code])
            new.setData(Qt.UserRole, code)
            self.chosen.addItem(new)
            existing.add(code)
        self.warning.setText("")

    def _remove(self) -> None:
        for item in self.chosen.selectedItems():
            self.chosen.takeItem(self.chosen.row(item))
        self.warning.setText("")

    def _reset(self) -> None:
        self._fill_chosen(DEFAULT_QUICK_KEYS)
        self.warning.setText("")

    def selected_codes(self) -> list[int]:
        return [
            self.chosen.item(i).data(Qt.UserRole)
            for i in range(self.chosen.count())
        ]
