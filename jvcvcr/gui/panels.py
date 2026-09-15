"""The individual panels that make up the main window.

Every panel takes the :class:`DeviceController` and drives it directly.  Panels
that care about device state expose ``on_state(state)``, which the main window
calls whenever new state arrives (already marshalled onto the GUI thread).
"""

from __future__ import annotations

import datetime
from collections import deque

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLayout, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import protocol as P
from ..device import Direction, LogEntry
from ..macros import Macro, MacroLibrary, MacroRunner, Step, parse_hex
from ..protocol import Deck
from . import theme

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


class ElidedLabel(QLabel):
    """A label that truncates with an ellipsis instead of widening its parent.

    Device values vary a lot in length -- "Stop" versus "Reverse Search (very
    fastest)", "--" versus "Title 12 (Play list)  Ch 7" -- and a plain QLabel
    would push the whole column wider as they change, so the panel visibly
    resizes while you are watching it. Ignoring the horizontal size hint keeps
    the layout fixed and shortens the text instead.
    """

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.ElideRight):
        super().__init__(text)
        self._full = text
        self._mode = mode
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = text or ""
        self.setToolTip(self._full if self._is_truncated() else "")
        self._apply()

    def fullText(self) -> str:  # noqa: N802 - Qt naming
        return self._full

    def set_reserved_width(self, width: int) -> None:
        """Claim a fixed width, for use inside a horizontal row.

        The default Ignored policy exists to stop a long value widening its
        parent, but Qt reports a zero minimum for Ignored widgets -- so in an
        HBox the label collapses to nothing. A fixed policy plus an explicit
        width keeps the reservation while still eliding the text inside it.
        """
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.setFixedWidth(width)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply()

    def _available(self) -> int:
        return max(0, self.width() - 2)

    def _is_truncated(self) -> bool:
        return QFontMetrics(self.font()).horizontalAdvance(self._full) > self._available()

    def _apply(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, self._mode, self._available()))


class FlowLayout(QLayout):
    """A layout that wraps its children onto new rows as width shrinks.

    Used for button strips that would otherwise force a horizontal scrollbar
    (or clip) when the window is docked to the side of a screen.
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        if parent is not None:
            self.enable_height_for_width(parent)

    @staticmethod
    def enable_height_for_width(widget: QWidget) -> None:
        """Let a wrapping layout's host report its true wrapped height.

        Without this the host advertises the height of a single row, so a
        parent layout gives it far too little space and the wrapped rows are
        clipped to nothing.
        """
        policy = widget.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Minimum)
        widget.setSizePolicy(policy)

    def addItem(self, item) -> None:  # noqa: N802 - Qt naming
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt naming
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(),
                                  -margins.right(), -margins.bottom())
        x, y, line_height = effective.x(), effective.y(), 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


class FlowWidget(QWidget):
    """A host for a :class:`FlowLayout` that reports its real height.

    Qt derives a widget's height hint from ``heightForWidth(sizeHint().width())``,
    and a flow layout's natural width hint is its *narrowest* item -- so an
    eight-button strip advertised itself as eight rows tall (~240px) when it
    actually renders as two (~57px). Anything sizing itself around that hint,
    such as a splitter pane, ends up with a large phantom gap. Reporting the
    height at the width actually in use fixes it.
    """

    def __init__(self, spacing: int = 6, fallback_width: int = 320):
        super().__init__()
        self._fallback_width = fallback_width
        self.flow = FlowLayout(self, spacing=spacing)
        self.flow.setContentsMargins(0, 0, 0, 0)

    def _width(self) -> int:
        return self.width() or self._fallback_width

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(self._width(), self.flow.heightForWidth(self._width()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, self.flow.heightForWidth(self._width()))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # A width change can change the row count, and therefore the height.
        self.updateGeometry()


def scrollable(widget: QWidget) -> QScrollArea:
    """Wrap a widget so it scrolls rather than being squashed when small."""
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    return area


def heading(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("role", "heading")
    return label


def dim(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "dim")
    return label


def card() -> QFrame:
    frame = QFrame()
    frame.setProperty("role", "card")
    return frame


def separator() -> QFrame:
    line = QFrame()
    line.setProperty("role", "separator")
    line.setFixedHeight(1)
    return line


def button(text: str, role: str = "", tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    if role:
        btn.setProperty("role", role)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


# --------------------------------------------------------------------------
# deck selector
# --------------------------------------------------------------------------


class DeckSelector(QWidget):
    """VCR / DVD toggle.  Every command is deck-scoped, so this stays visible."""

    deckChanged = Signal(object)

    def __init__(self, compact: bool = False) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4 if compact else 6)

        self.buttons: dict[Deck, QPushButton] = {}
        labels = ((Deck.VCR, "VCR"), (Deck.DVD, "DVD")) if compact else (
            (Deck.VCR, "VCR Deck"), (Deck.DVD, "DVD Deck"))
        for deck, label in labels:
            btn = button(label, role="deck")
            btn.setCheckable(True)
            if compact:
                btn.setFixedWidth(56)
            btn.clicked.connect(lambda _checked, d=deck: self._pick(d))
            layout.addWidget(btn)
            self.buttons[deck] = btn
        self.buttons[Deck.VCR].setChecked(True)
        self._current = Deck.VCR
        self.setSizePolicy(QSizePolicy.Fixed if compact else QSizePolicy.Preferred,
                           QSizePolicy.Fixed)

    def _pick(self, deck: Deck) -> None:
        self.set_deck(deck)
        self.deckChanged.emit(deck)

    def set_deck(self, deck: Deck) -> None:
        self._current = deck
        for d, btn in self.buttons.items():
            btn.setChecked(d is deck)

    @property
    def deck(self) -> Deck:
        return self._current


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


class StatusPanel(QFrame):
    """Live decode of Status Sense, the counter, and the JVC status byte."""

    clearErrorRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        # Header: deck selector and transport state share a row. Merging the
        # deck buttons in here rather than stacking them above saves a whole
        # row, and keeps "which deck am I driving" next to "what is it doing".
        header = QHBoxLayout()
        header.setSpacing(8)
        self.deck_selector = DeckSelector(compact=True)
        header.addWidget(self.deck_selector)
        self.transport = ElidedLabel("Not connected")
        self.transport.setProperty("role", "transport")
        self.transport.setFixedHeight(26)
        self.transport.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        header.addWidget(self.transport, 1)
        layout.addLayout(header)

        # Counter and remaining sit on one line: the counter is the number you
        # watch, remaining is reference, and side by side they cost one row
        # instead of three.
        counter_row = QHBoxLayout()
        counter_row.setSpacing(8)
        self.counter = QLabel(self.BLANK_TIME)
        self.counter.setProperty("role", "bigvalue")
        # Monospace (from the stylesheet) keeps every digit the same width,
        # so a ticking counter doesn't shuffle anything sideways.
        self.counter.setFixedHeight(36)
        counter_row.addWidget(self.counter)
        counter_row.addStretch(1)
        self.remaining_inline = QLabel("--:--:--")
        self.remaining_inline.setProperty("role", "value")
        self.remaining_inline.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        self.remaining_inline.setToolTip("Remaining time in the current mode")
        counter_row.addWidget(self.remaining_inline)
        layout.addLayout(counter_row)

        self.counter_caption = dim("Counter")
        layout.addWidget(self.counter_caption)

        # One elided line replaces four label/value rows when compact.
        self.summary_line = ElidedLabel("--")
        self.summary_line.setProperty("role", "value")
        self.summary_line.setFixedHeight(20)
        self.summary_line.hide()
        layout.addWidget(self.summary_line)

        self.detail_separator = separator()
        layout.addWidget(self.detail_separator)

        self.detail_host = QWidget()
        grid = QGridLayout(self.detail_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(5)
        self.fields: dict[str, ElidedLabel] = {}
        for row, (key, label) in enumerate(
            (
                ("remaining", "Remaining"),
                ("media", "Media"),
                ("mode", "Rec mode"),
                ("input", "Input"),
                ("position", "Position"),
            )
        ):
            caption = dim(label)
            caption.setFixedWidth(84)
            grid.addWidget(caption, row, 0)
            value = ElidedLabel("--")
            value.setProperty("role", "value")
            value.setFixedHeight(20)
            grid.addWidget(value, row, 1)
            self.fields[key] = value
        layout.addWidget(self.detail_host)

        # The alert area always occupies the same space, whether or not there
        # is anything to say. Otherwise it would push the transport controls
        # down the moment a tape reached its end or an error latched --
        # exactly when you least want buttons moving under the cursor.
        # A stack rather than two stacked rows keeps that reservation small.
        self.alerts = QStackedWidget()
        self.alerts.setFixedHeight(34)
        self.alerts.setStyleSheet("background: transparent;")

        self.flags = ElidedLabel("")
        self.flags.setProperty("role", "warn")
        self.flags.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.alerts.addWidget(self.flags)

        self.error_banner = QWidget()
        banner_layout = QHBoxLayout(self.error_banner)
        banner_layout.setContentsMargins(10, 2, 6, 2)
        self.error_banner.setProperty("role", "banner")
        self.error_banner.setProperty("state", "error")
        self.error_text = QLabel("Command error latched")
        self.error_text.setProperty("role", "banner-text")
        banner_layout.addWidget(self.error_text, 1)
        self.clear_btn = button("Clear")
        self.clear_btn.clicked.connect(self.clearErrorRequested.emit)
        banner_layout.addWidget(self.clear_btn)
        self.alerts.addWidget(self.error_banner)

        self._set_error_visible(False)

        layout.addWidget(self.alerts)
        # No trailing stretch, and a fixed vertical policy: the card hugs its
        # content instead of absorbing spare height, which would otherwise
        # push the transport controls off the bottom of a narrow window.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    #: Placeholder that matches a real timecode's width, so swapping between
    #: them doesn't change the label's size.
    BLANK_TIME = "--:--:--"

    def set_offline(self, message: str = "Not connected") -> None:
        """Show plainly that nothing here is live."""
        self.transport.setText(message)
        self._set_transport_state("idle")
        self.counter.setText(self.BLANK_TIME)
        self.remaining_inline.setText(self.BLANK_TIME)
        self.summary_line.setText("--")
        for field in self.fields.values():
            field.setText("--")
        self.flags.setText("")
        self._set_error_visible(False)

    def set_compact(self, compact: bool) -> None:
        """Collapse the detail grid into one line to save vertical space.

        Nothing is lost: the same values appear in the summary line, and the
        alert row shrinks rather than disappearing so the panel still never
        changes height as conditions come and go.
        """
        if getattr(self, "_compact", None) == compact:
            return
        self._compact = compact
        self.detail_host.setVisible(not compact)
        self.detail_separator.setVisible(not compact)
        self.summary_line.setVisible(compact)
        # The caption costs a row, and in compact form every row counts; the
        # tooltip keeps the explanation one hover away.
        self.counter_caption.setVisible(not compact)
        self.counter.setToolTip(
            "Counter, with remaining time on the right" if compact else "")
        self.alerts.setFixedHeight(24 if compact else 34)
        self.clear_btn.setProperty("role", "compact" if compact else "")
        self.updateGeometry()

    def _set_transport_state(self, state: str) -> None:
        """Colour the transport label via a property, so the stylesheet owns
        the colour and a theme change repaints it without touching layout."""
        self.transport.setProperty("state", state)
        self.transport.style().unpolish(self.transport)
        self.transport.style().polish(self.transport)

    def _set_error_visible(self, visible: bool) -> None:
        """Swap the alert area between flags and the error banner.

        Both pages are the same height, so this never changes the panel's
        footprint. The error takes the space when both apply, since it is the
        more urgent of the two; the flags stay available as a tooltip.
        """
        self.alerts.setCurrentIndex(1 if visible else 0)
        if visible:
            self.error_banner.setToolTip(self.flags.fullText())

    def on_state(self, state) -> None:
        status = state.status
        if status is None:
            self.transport.setText("Waiting for status...")
            self._set_transport_state("idle")
            self.counter.setText(self.BLANK_TIME)
            self.remaining_inline.setText(self.BLANK_TIME)
            self.summary_line.setText("--")
            for field in self.fields.values():
                field.setText("--")
            self.flags.setText("")
            self._set_error_visible(False)
            return

        self.transport.setText(status.transport)
        if status.recording:
            self._set_transport_state("rec")
        elif status.standby:
            self._set_transport_state("idle")
        elif status.stopped:
            self._set_transport_state("stop")
        else:
            self._set_transport_state("play")

        self.counter.setText(state.counter or self.BLANK_TIME)
        self.remaining_inline.setText(state.remaining or self.BLANK_TIME)
        self.fields["remaining"].setText(state.remaining or self.BLANK_TIME)

        if status.media_missing:
            media = "Cassette out" if status.deck is Deck.VCR else "No disc"
        elif state.jvc and state.jvc.disc_type:
            media = state.jvc.disc_type
        else:
            media = "Cassette in" if status.deck is Deck.VCR else "Disc in"
        self.fields["media"].setText(media)

        select = state.select
        self.fields["mode"].setText((select.rec_mode if select else None) or "--")
        self.fields["input"].setText((select.input if select else None) or "--")

        if status.deck is Deck.DVD and state.title is not None:
            source = "Play list" if state.title_is_playlist else "Original"
            chapter = state.chapter if state.chapter is not None else "--"
            self.fields["position"].setText(
                f"Title {state.title} ({source})  Ch {chapter}"
            )
        else:
            self.fields["position"].setText("--")

        notes = []
        if status.record_forbidden:
            notes.append("Record inhibited (tab removed / disc protected)")
        if status.abnormality:
            notes.append("Unit reports an abnormality")
        if status.end_sensor:
            notes.append("Tape at end")
        if status.start_sensor:
            notes.append("Tape at start")
        if state.jvc and state.jvc.dubbing:
            notes.append("Dubbing in progress")
        if state.jvc and state.jvc.ep_tape:
            notes.append("EP-recorded tape")
        if status.repeat_playback:
            notes.append("Repeat playback")
        self.flags.setText(" · ".join(notes))
        # The compact line carries the same facts as the detail grid.
        summary = [self.fields["media"].fullText()]
        for key in ("mode", "input"):
            value = self.fields[key].fullText()
            if value and value != "--":
                summary.append(value)
        position = self.fields["position"].fullText()
        if position and position != "--":
            summary.append(position)
        self.summary_line.setText("  ·  ".join(summary))

        self._set_error_visible(state.error_latched or status.command_error)


# --------------------------------------------------------------------------
# keyboard shortcuts
# --------------------------------------------------------------------------

#: Window-wide keyboard shortcuts, bound in the main window and listed under
#: Help -> Keyboard shortcuts.
SHORTCUT_HINTS = {
    "play": "Space",
    "still": "K",
    "stop": "S",
    "rew": "J",
    "ff": "L",
    "step_rev": ",",
    "step_fwd": ".",
}


# --------------------------------------------------------------------------
# macros
# --------------------------------------------------------------------------


class MacroPanel(QWidget):
    """Named command sequences with delays, saved as shareable JSON."""

    def __init__(self, controller, library: MacroLibrary) -> None:
        super().__init__()
        self.controller = controller
        self.library = library
        self.runner = MacroRunner(controller)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # A splitter rather than a fixed two-column layout, so the list can be
        # collapsed away entirely when the window is narrow.
        self.splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(self.splitter, 1)

        left_host = QWidget()
        left = QVBoxLayout(left_host)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        left.addWidget(heading("Macros"))
        self.macro_list = QListWidget()
        self.macro_list.currentTextChanged.connect(self._show_macro)
        left.addWidget(self.macro_list, 1)

        buttons = QHBoxLayout()
        new_btn = button("New")
        new_btn.clicked.connect(self._new_macro)
        buttons.addWidget(new_btn)
        delete_btn = button("Delete", role="danger")
        delete_btn.clicked.connect(self._delete_macro)
        buttons.addWidget(delete_btn)
        left.addLayout(buttons)
        left_host.setMinimumWidth(150)
        self.splitter.addWidget(left_host)

        right_host = QWidget()
        right = QVBoxLayout(right_host)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setProperty("role", "dim")
        # Descriptions run to a few lines; a constant height stops the step
        # list jumping as you click between macros.
        self.description.setFixedHeight(50)
        self.description.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        right.addWidget(self.description)

        self.step_list = QListWidget()
        right.addWidget(self.step_list, 1)

        add_row = QHBoxLayout()
        self.step_kind = QComboBox()
        self.step_kind.addItems(["command", "deck", "remote", "raw", "wait"])
        add_row.addWidget(self.step_kind)
        self.step_value = QLineEdit()
        self.step_value.setPlaceholderText(
            "e.g. play / VCR / 0x41 / B8 34 31 / 2.5"
        )
        add_row.addWidget(self.step_value, 1)
        add_step = button("Add step")
        add_step.clicked.connect(self._add_step)
        add_row.addWidget(add_step)
        remove_step = button("Remove")
        remove_step.clicked.connect(self._remove_step)
        add_row.addWidget(remove_step)
        right.addLayout(add_row)

        run_row = QHBoxLayout()
        self.run_btn = button("▶  Run macro", role="primary")
        self.run_btn.clicked.connect(self._run)
        run_row.addWidget(self.run_btn, 1)
        self.cancel_btn = button("Cancel", role="danger")
        self.cancel_btn.clicked.connect(self.runner.cancel)
        self.cancel_btn.setEnabled(False)
        run_row.addWidget(self.cancel_btn)
        right.addLayout(run_row)

        self.status = ElidedLabel("")
        self.status.setProperty("role", "dim")
        self.status.setFixedHeight(18)
        right.addWidget(self.status)
        right_host.setMinimumWidth(220)
        self.splitter.addWidget(right_host)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([220, 460])

        self._refresh_list()

    # -- library -----------------------------------------------------------

    def _refresh_list(self, select: str | None = None) -> None:
        self.macro_list.clear()
        for macro in self.library.macros:
            self.macro_list.addItem(macro.name)
        if select:
            matches = self.macro_list.findItems(select, Qt.MatchExactly)
            if matches:
                self.macro_list.setCurrentItem(matches[0])
        elif self.macro_list.count():
            self.macro_list.setCurrentRow(0)

    @property
    def current(self) -> Macro | None:
        item = self.macro_list.currentItem()
        return self.library.find(item.text()) if item else None

    def _show_macro(self, name: str) -> None:
        macro = self.library.find(name)
        self.step_list.clear()
        if macro is None:
            self.description.setText("")
            return
        self.description.setText(macro.description)
        for step in macro.steps:
            self.step_list.addItem(step.describe())

    def _new_macro(self) -> None:
        name, ok = QInputDialog.getText(self, "New macro", "Name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.library.find(name):
            QMessageBox.warning(self, "Name in use",
                                f"A macro called {name!r} already exists.")
            return
        self.library.replace(Macro(name=name))
        self.library.save()
        self._refresh_list(select=name)

    def _delete_macro(self) -> None:
        macro = self.current
        if macro is None:
            return
        if QMessageBox.question(
            self, "Delete macro", f"Delete {macro.name!r}?"
        ) != QMessageBox.Yes:
            return
        self.library.remove(macro.name)
        self.library.save()
        self._refresh_list()

    # -- steps -------------------------------------------------------------

    def _add_step(self) -> None:
        macro = self.current
        if macro is None:
            QMessageBox.information(self, "No macro", "Create a macro first.")
            return
        kind = self.step_kind.currentText()
        text = self.step_value.text().strip()
        try:
            step = self._build_step(kind, text)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid step", str(exc))
            return
        macro.steps.append(step)
        self.library.save()
        self._show_macro(macro.name)
        self.step_value.clear()

    def _build_step(self, kind: str, text: str) -> Step:
        if kind == "wait":
            try:
                seconds = float(text)
            except ValueError:
                raise ValueError("Enter a number of seconds, e.g. 2.5") from None
            if seconds < 0:
                raise ValueError("Wait time cannot be negative")
            return Step("wait", seconds)
        if kind == "deck":
            value = text.upper()
            if value not in ("VCR", "DVD"):
                raise ValueError("Enter VCR or DVD")
            return Step("deck", value)
        if kind == "command":
            if text not in P.SIMPLE_BY_KEY:
                keys = ", ".join(sorted(P.SIMPLE_BY_KEY))
                raise ValueError(f"Unknown command. Available: {keys}")
            return Step("command", text)
        if kind == "remote":
            data = parse_hex(text)
            if len(data) != 1:
                raise ValueError("Enter a single hex byte, e.g. 41")
            return Step("remote", data[0])
        if kind == "raw":
            parse_hex(text)  # validate now rather than at run time
            return Step("raw", text)
        raise ValueError(f"Unknown step kind {kind}")

    def _remove_step(self) -> None:
        macro = self.current
        row = self.step_list.currentRow()
        if macro is None or row < 0:
            return
        del macro.steps[row]
        self.library.save()
        self._show_macro(macro.name)

    # -- running -----------------------------------------------------------

    def _run(self) -> None:
        macro = self.current
        if macro is None or not macro.steps:
            return
        if not self.controller.connected:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the deck first.")
            return
        destructive = [
            s for s in macro.steps
            if s.kind == "command"
            and P.SIMPLE_BY_KEY.get(str(s.value))
            and P.SIMPLE_BY_KEY[str(s.value)].destructive
        ]
        if destructive:
            names = ", ".join(P.SIMPLE_BY_KEY[str(s.value)].label
                              for s in destructive)
            if QMessageBox.warning(
                self,
                "Macro contains destructive steps",
                f"This macro runs: {names}.\n\nRun it anyway?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            ) != QMessageBox.Yes:
                return
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.runner.run(macro)

    def on_macro_step(self, index: int, step: Step) -> None:
        self.status.setText(f"Step {index + 1}: {step.describe()}")
        self.step_list.setCurrentRow(index)

    def on_macro_finish(self, ok: bool, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status.setText(message)


# --------------------------------------------------------------------------
# console
# --------------------------------------------------------------------------


class ConsolePanel(QWidget):
    """Raw byte sender and a colour-coded traffic log."""

    MAX_ROWS = 2000

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        send_row = QHBoxLayout()
        self.hex_entry = QLineEdit()
        self.hex_entry.setPlaceholderText(
            "Hex bytes to send, e.g. 3A  or  B8 34 31"
        )
        self.hex_entry.returnPressed.connect(self._send_raw)
        send_row.addWidget(self.hex_entry, 1)
        send_btn = button("Send", role="primary")
        send_btn.clicked.connect(self._send_raw)
        send_row.addWidget(send_btn)
        layout.addLayout(send_row)

        layout.addWidget(dim("Query:"))
        # Nine buttons will not fit on one row in a narrow window, so they
        # wrap onto as many rows as needed rather than being clipped.
        query_host = FlowWidget(spacing=6)
        query_row = query_host.flow
        for opcode in P.SENSE_PAYLOAD_LEN:
            btn = button(P.SENSE_NAMES[opcode])
            btn.clicked.connect(lambda _=False, o=opcode: self.controller.query(o))
            query_row.addWidget(btn)
        layout.addWidget(query_host)

        controls = QHBoxLayout()
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        controls.addWidget(self.autoscroll)
        self.hide_polls = QCheckBox("Hide polling traffic")
        self.hide_polls.setChecked(True)
        self.hide_polls.setToolTip(
            "Status polling repeats constantly; hiding it makes your own "
            "commands easy to follow."
        )
        self.hide_polls.toggled.connect(self._rebuild)
        controls.addWidget(self.hide_polls)
        controls.addStretch(1)
        clear = button("Clear log")
        clear.clicked.connect(self._clear)
        controls.addWidget(clear)
        layout.addLayout(controls)

        self._link_widgets = [self.hex_entry, send_btn, query_host]

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "", "Bytes", "Meaning"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        # Every entry is kept so toggling the filter can show history that was
        # previously hidden, rather than only affecting future traffic.
        self._entries: deque[LogEntry] = deque(maxlen=self.MAX_ROWS)

    def _visible(self, entry: LogEntry) -> bool:
        if not entry.data:
            return True  # synthetic entries (e.g. a timeout note) always show
        # Keyed off the entry's own tag rather than its first byte: a sense
        # reply carries no opcode, so guessing from the bytes would hide the
        # queries while showing every reply -- which is exactly the noise
        # this checkbox exists to remove.
        if self.hide_polls.isChecked() and entry.sense_opcode is not None:
            return False
        return True

    def _send_raw(self) -> None:
        text = self.hex_entry.text().strip()
        if not text:
            return
        try:
            data = parse_hex(text)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid hex", str(exc))
            return
        self.controller.send(data)
        self.hex_entry.clear()

    def _clear(self) -> None:
        self._entries.clear()
        self.table.setRowCount(0)

    def set_link_enabled(self, connected: bool) -> None:
        """Enable only the parts that need a live link.

        The traffic log stays readable while disconnected -- it is often the
        thing you want to look at after a link drops.
        """
        for widget in self._link_widgets:
            widget.setEnabled(connected)

    def refresh_theme(self) -> None:
        """Repaint rows after a palette change.

        The direction column is drawn with an explicit colour, which the
        stylesheet cannot reach, so those rows have to be rebuilt.
        """
        self._rebuild()

    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        for entry in self._entries:
            if self._visible(entry):
                self._add_row(entry)
        if self.autoscroll.isChecked():
            self.table.scrollToBottom()

    def append(self, entry: LogEntry) -> None:
        self._entries.append(entry)
        if not self._visible(entry):
            return
        self._add_row(entry)
        if self.autoscroll.isChecked():
            self.table.scrollToBottom()

    def _add_row(self, entry: LogEntry) -> None:
        row = self.table.rowCount()
        if row >= self.MAX_ROWS:
            self.table.removeRow(0)
            row -= 1
        self.table.insertRow(row)

        stamp = datetime.datetime.fromtimestamp(entry.timestamp)
        cells = [
            stamp.strftime("%H:%M:%S.%f")[:-3],
            entry.direction.value,
            entry.hex,
            entry.label,
        ]
        palette = theme.ACTIVE
        colour = palette.tx if entry.direction is Direction.TX else palette.rx
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column in (0, 2):
                font = QFont(theme.MONO.split(",")[0].strip("'\" "))
                font.setPointSize(9)
                item.setFont(font)
            if column == 1:
                item.setForeground(QColor(colour))
            self.table.setItem(row, column, item)
