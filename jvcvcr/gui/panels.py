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
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import protocol as P
from ..device import Direction, LogEntry
from ..macros import Macro, MacroLibrary, MacroRunner, Step, parse_hex
from ..protocol import Cmd, Deck
from . import icons, theme

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
# transport
# --------------------------------------------------------------------------

#: Keyboard shortcuts, shown in tooltips so they are discoverable at the
#: control rather than only in a menu. Bound in the main window.
SHORTCUT_HINTS = {
    "play": "Space",
    "still": "K",
    "stop": "S",
    "rew": "J",
    "ff": "L",
    "step_rev": ",",
    "step_fwd": ".",
}


class TransportPanel(QFrame):
    """The core virtual remote: transport keys plus a shuttle speed control."""

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self._headings = []
        transport_heading = heading("Transport")
        self._headings.append(transport_heading)
        layout.addWidget(transport_heading)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.buttons: dict[str, QPushButton] = {}

        # Icon name per command, drawn rather than typed -- see gui/icons.py.
        self._button_icons: dict[str, str] = {}

        def add(key: str, shape: str, row: int, col: int, span: int = 1):
            cmd = P.SIMPLE_BY_KEY[key]
            tip = cmd.label
            if cmd.tooltip:
                tip = f"{tip} — {cmd.tooltip}"
            shortcut = SHORTCUT_HINTS.get(key)
            if shortcut:
                tip = f"{tip}\n\nShortcut: {shortcut}"
            btn = button("", role="transport", tooltip=tip)
            btn.setIconSize(QSize(18, 18))
            btn.clicked.connect(lambda _=False, k=key: self.controller.send_command(k))
            grid.addWidget(btn, row, col, 1, span)
            self.buttons[key] = btn
            self._button_icons[key] = shape
            return btn

        add("rew", "rew", 0, 0)
        add("play", "play", 0, 1)
        add("still", "pause", 0, 2)
        add("ff", "ff", 0, 3)
        add("step_rev", "step_rev", 1, 0)
        add("stop", "stop", 1, 1)
        add("eject", "eject", 1, 2)
        add("step_fwd", "step_fwd", 1, 3)
        layout.addLayout(grid)

        viss = QHBoxLayout()
        viss.setSpacing(6)
        self.viss_buttons = []
        for key, text in (("viss_rev", "VISS Rev"), ("viss_fwd", "VISS Fwd")):
            btn = button(text, tooltip="Search to the VHS index start position")
            btn.clicked.connect(lambda _=False, k=key: self.controller.send_command(k))
            viss.addWidget(btn)
            self.viss_buttons.append(btn)
        layout.addLayout(viss)

        shuttle_heading = heading("Shuttle")
        self._headings.append(shuttle_heading)
        layout.addWidget(shuttle_heading)

        # Label, slider and reset share one row rather than stacking, which
        # costs two fewer rows than the old label-above / button-below shape.
        self.shuttle_row = QHBoxLayout()
        self.shuttle_row.setSpacing(6)
        layout.addLayout(self.shuttle_row)

        # "Still" through "Reverse Search (very fastest)" is a wide range of
        # text; a fixed width and elision keep the slider from shifting as
        # you drag.
        self.shuttle_label = ElidedLabel("1x")
        self.shuttle_label.setProperty("role", "value")
        self.shuttle_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.shuttle_label.setFixedHeight(22)
        self.shuttle_label.set_reserved_width(104)
        self.shuttle_row.addWidget(self.shuttle_label)

        self.shuttle = QSlider(Qt.Horizontal)
        self.shuttle.setToolTip(
            "Shuttle speed. Negative values search in reverse.\n"
            "Only works while the deck is playing."
        )
        self.shuttle.valueChanged.connect(self._shuttle_preview)
        self.shuttle.sliderReleased.connect(self._shuttle_apply)
        self.shuttle_row.addWidget(self.shuttle, 1)

        self.shuttle_reset = button("⟲", tooltip="Return the shuttle to 1x")
        self.shuttle_reset.setFixedWidth(40)
        self.shuttle_reset.clicked.connect(self._shuttle_reset)
        self.shuttle_row.addWidget(self.shuttle_reset)

        # Quick keys: the remote codes with no direct-opcode equivalent, kept
        # beside the transport so a capture never needs a tab switch.
        from .quickkeys import QuickKeysBar

        self.quick_keys = QuickKeysBar(self.controller)
        layout.addWidget(self.quick_keys)

        # Hug the content for the same reason as the status card above.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._compact = False
        self._speeds: list[P.ShuttleSpeed] = []
        self.refresh_theme()
        self.set_deck(Deck.VCR)

    def refresh_theme(self) -> None:
        """Redraw the icons in the current palette's text colour."""
        colour = theme.ACTIVE.text
        for key, shape in self._button_icons.items():
            self.buttons[key].setIcon(icons.icon(shape, colour))
        self.quick_keys.updateGeometry()

    def set_compact(self, compact: bool) -> None:
        """Shrink the transport for a narrow window.

        The buttons stay finger-sized; what goes is the headings and the
        second VISS row, which are labels rather than controls.
        """
        if self._compact == compact:
            return
        self._compact = compact
        for widget in self._headings:
            widget.setVisible(not compact)
        for btn in self.buttons.values():
            btn.setMinimumHeight(30 if compact else 34)
        self.quick_keys.set_compact(compact)
        self.updateGeometry()

    def set_deck(self, deck: Deck) -> None:
        """Rebuild the shuttle scale and grey out commands the deck lacks."""
        self._speeds = list(P.shuttle_speeds_for(deck))
        count = len(self._speeds)
        # The scale runs reverse-fastest ... still ... forward-fastest, so the
        # magnitude picks the speed and the sign picks the direction. Centre is
        # Still, which gives the slider a natural detent.
        self.shuttle.blockSignals(True)
        self.shuttle.setRange(-(count - 1), count - 1)
        self.shuttle.setValue(self._one_x_index())
        self.shuttle.blockSignals(False)
        self._shuttle_preview(self.shuttle.value())

        for key, btn in self.buttons.items():
            btn.setEnabled(deck in P.SIMPLE_BY_KEY[key].decks)
        for btn in self.viss_buttons:
            btn.setEnabled(deck is Deck.VCR)

    def _one_x_index(self) -> int:
        return next(
            (i for i, s in enumerate(self._speeds) if s.label == "1x"), 0
        )

    def _speed_at(self, offset: int) -> P.ShuttleSpeed:
        """Magnitude indexes the speed list; 0 is Still, the end is fastest."""
        index = min(len(self._speeds) - 1, abs(offset))
        return self._speeds[index]

    def _shuttle_preview(self, value: int) -> None:
        speed = self._speed_at(value)
        if speed.label == "Still":
            self.shuttle_label.setText("Still")
        else:
            self.shuttle_label.setText(
                f"{'◀ ' if value < 0 else '▶ '}{speed.label}"
            )

    def _shuttle_apply(self) -> None:
        value = self.shuttle.value()
        speed = self._speed_at(value)
        self.controller.send(P.shuttle(value >= 0, speed.byte))

    def _shuttle_reset(self) -> None:
        self.shuttle.setValue(self._one_x_index())
        self._shuttle_apply()


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------


class RecordPanel(QWidget):
    """Recording, deliberately gated behind an explicit arming step."""

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        setup = QGroupBox("Recording setup")
        form = QGridLayout(setup)
        form.setColumnStretch(1, 1)
        form.setVerticalSpacing(8)

        form.addWidget(QLabel("Input"), 0, 0)
        self.input_combo = QComboBox()
        self.input_combo.addItems(P.INPUTS.keys())
        form.addWidget(self.input_combo, 0, 1)
        apply_input = button("Apply")
        apply_input.clicked.connect(self._apply_input)
        form.addWidget(apply_input, 0, 2)

        form.addWidget(QLabel("Record mode"), 1, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(P.REC_MODES.keys())
        self.mode_combo.setCurrentText("SP")
        self.mode_combo.setToolTip(
            "XP/SP/LP/EP are the standard DVD modes. FR values are free-rate "
            "modes that fit exactly that many minutes onto one disc."
        )
        form.addWidget(self.mode_combo, 1, 1)
        apply_mode = button("Apply")
        apply_mode.clicked.connect(self._apply_mode)
        form.addWidget(apply_mode, 1, 2)

        self.mode_hint = ElidedLabel("")
        self.mode_hint.setProperty("role", "dim")
        self.mode_hint.setFixedHeight(18)
        form.addWidget(self.mode_hint, 2, 1, 1, 2)
        self.mode_combo.currentTextChanged.connect(self._update_mode_hint)
        self._update_mode_hint(self.mode_combo.currentText())

        layout.addWidget(setup)

        control = QGroupBox("Record control")
        control_layout = QVBoxLayout(control)
        control_layout.setSpacing(10)

        explain = dim(
            "The deck requires a Rec Request before it will accept Record. "
            "Arming here sends that request; Stop clears it again."
        )
        explain.setWordWrap(True)
        control_layout.addWidget(explain)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.arm_btn = button("Arm", role="primary",
                              tooltip="Send Rec Request (FA)")
        self.arm_btn.setCheckable(True)
        self.arm_btn.setMinimumHeight(40)
        self.arm_btn.clicked.connect(self._toggle_arm)
        row.addWidget(self.arm_btn, 1)

        self.rec_btn = button("Record", role="record")
        self.rec_btn.setIconSize(QSize(16, 16))
        self.rec_btn.clicked.connect(lambda: self.controller.send_command("rec"))
        self.rec_btn.setEnabled(False)
        row.addWidget(self.rec_btn, 1)

        self.rec_pause_btn = button("Record Pause")
        self.rec_pause_btn.setIconSize(QSize(16, 16))
        self.rec_pause_btn.setMinimumHeight(40)
        self.rec_pause_btn.clicked.connect(
            lambda: self.controller.send_command("rec_pause")
        )
        self.rec_pause_btn.setEnabled(False)
        row.addWidget(self.rec_pause_btn, 1)
        control_layout.addLayout(row)

        stop = button("Stop (also disarms)")
        stop.setIconSize(QSize(16, 16))
        self._stop_btn = stop
        stop.clicked.connect(lambda: self.controller.send_command("stop"))
        control_layout.addWidget(stop)

        # Warnings come and go as media is swapped or tabs are removed;
        # reserving two lines keeps the panel from resizing underneath you.
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setProperty("role", "warn")
        self.warning.setFixedHeight(34)
        self.warning.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        control_layout.addWidget(self.warning)

        layout.addWidget(control)

        clock = QGroupBox("Clock")
        clock_layout = QHBoxLayout(clock)
        sync = button("Sync clock and date from this PC")
        sync.clicked.connect(self._sync_clock)
        clock_layout.addWidget(sync)
        self.clock_label = ElidedLabel("Unit clock: --")
        self.clock_label.setProperty("role", "dim")
        clock_layout.addWidget(self.clock_label, 1)
        layout.addWidget(clock)

        layout.addStretch(1)

    def refresh_theme(self) -> None:
        self.rec_btn.setIcon(icons.icon("record", theme.ACTIVE.rec, size=16))
        self.rec_pause_btn.setIcon(icons.icon("pause", theme.ACTIVE.text, size=16))
        self._stop_btn.setIcon(icons.icon("stop", theme.ACTIVE.text, size=16))

    def _update_mode_hint(self, mode: str) -> None:
        minutes = P.REC_MODE_MINUTES.get(mode)
        if minutes:
            hours, mins = divmod(minutes, 60)
            span = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
            self.mode_hint.setText(f"About {span} on a 4.7 GB disc")
        else:
            self.mode_hint.setText("")

    def _apply_input(self) -> None:
        self.controller.send(P.set_input(self.input_combo.currentText()))

    def _apply_mode(self) -> None:
        self.controller.send(P.set_rec_mode(self.mode_combo.currentText()))

    def _toggle_arm(self, checked: bool) -> None:
        if checked:
            self.controller.send_command("rec_request")
        else:
            self.controller.send_command("stop")

    def _sync_clock(self) -> None:
        date_cmd, clock_cmd = P.sync_clock()
        self.controller.send(date_cmd)
        self.controller.send(clock_cmd)
        self.controller.query(Cmd.DATE_SENSE)
        self.controller.query(Cmd.CLOCK_SENSE)

    def on_state(self, state) -> None:
        armed = state.armed
        self.arm_btn.setChecked(armed)
        self.arm_btn.setText("Armed" if armed else "Arm")
        self.rec_btn.setEnabled(armed)
        self.rec_pause_btn.setEnabled(armed)

        notes = []
        status = state.status
        if status:
            if status.record_forbidden:
                notes.append(
                    "The deck reports recording is inhibited - check the "
                    "cassette's erase tab or the disc's protection."
                )
            if status.media_missing:
                notes.append("No media in the selected deck.")
        self.warning.setText(" ".join(notes))

        if state.clock:
            h, m, s = state.clock
            text = f"{h:02d}:{m:02d}:{s:02d}"
            if state.date:
                mo, d, y = state.date
                text = f"{mo:02d}/{d:02d}/{2000 + y}  {text}"
            self.clock_label.setText(f"Unit clock: {text}")
        else:
            self.clock_label.setText("Unit clock: not set")

        if state.select:
            if state.select.rec_mode:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentText(state.select.rec_mode)
                self.mode_combo.blockSignals(False)
                self._update_mode_hint(state.select.rec_mode)
            if state.select.input:
                self.input_combo.blockSignals(True)
                self.input_combo.setCurrentText(state.select.input)
                self.input_combo.blockSignals(False)


# --------------------------------------------------------------------------
# DVD navigation
# --------------------------------------------------------------------------


class DvdPanel(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Kept at a constant height and blanked rather than hidden, so
        # switching decks doesn't shift every control on this tab.
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setProperty("role", "warn")
        self.notice.setFixedHeight(34)
        self.notice.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.notice)

        search = QGroupBox("Search")
        search_layout = QGridLayout(search)
        search_layout.setColumnStretch(4, 1)  # soak up the spare width

        search_layout.addWidget(QLabel("Title"), 0, 0)
        self.title_spin = QSpinBox()
        self.title_spin.setRange(1, 999)
        self.title_spin.setFixedWidth(90)
        search_layout.addWidget(self.title_spin, 0, 1)
        self.playlist_check = QCheckBox("Play list")
        self.playlist_check.setToolTip(
            "Search the PLAY LIST instead of ORIGINAL titles"
        )
        search_layout.addWidget(self.playlist_check, 0, 2)
        go_title = button("Go", role="primary")
        go_title.setFixedWidth(90)
        go_title.clicked.connect(self._title_search)
        search_layout.addWidget(go_title, 0, 3)

        search_layout.addWidget(QLabel("Chapter"), 1, 0)
        self.chapter_spin = QSpinBox()
        self.chapter_spin.setRange(1, 999)
        self.chapter_spin.setFixedWidth(90)
        search_layout.addWidget(self.chapter_spin, 1, 1)
        go_chapter = button("Go", role="primary")
        go_chapter.setFixedWidth(90)
        go_chapter.clicked.connect(self._chapter_search)
        search_layout.addWidget(go_chapter, 1, 3)
        layout.addWidget(search)

        skip = QGroupBox("Skip")
        skip_layout = QGridLayout(skip)
        self._skip_icons: dict[str, tuple] = {}
        for col, (key, text, shape) in enumerate(
            (
                ("prev_title", "Title", "skip_rev"),
                ("prev_chapter", "Chapter", "skip_rev"),
                ("next_chapter", "Chapter", "skip_fwd"),
                ("next_title", "Title", "skip_fwd"),
            )
        ):
            btn = button(text)
            btn.setIconSize(QSize(16, 16))
            btn.clicked.connect(lambda _=False, k=key: self.controller.send_command(k))
            skip_layout.addWidget(btn, 0, col)
            self._skip_icons[key] = (btn, shape)
        layout.addWidget(skip)

        nav = QGroupBox("Menus and navigation")
        nav_outer = QVBoxLayout(nav)
        nav_outer.setSpacing(10)

        menus = QHBoxLayout()
        for key, text in (("top_menu", "Top Menu"), ("menu", "Disc Menu")):
            btn = button(text)
            btn.clicked.connect(lambda _=False, k=key: self.controller.send_command(k))
            menus.addWidget(btn)
        for label, value in (
            ("Main Menu", P.SETUP_MAIN_MENU),
            ("DVD Navi", P.SETUP_DVD_NAVI),
            ("Close", P.SETUP_CLOSE),
        ):
            btn = button(label)
            btn.clicked.connect(lambda _=False, v=value: self.controller.send(P.setup(v)))
            menus.addWidget(btn)
        menus.addStretch(1)
        nav_outer.addLayout(menus)

        pad_row = QHBoxLayout()
        pad_grid = QGridLayout()
        pad_grid.setSpacing(6)
        self._pad_icons: dict[str, tuple] = {}
        pad = [("up", "up", 0, 1), ("left", "left", 1, 0),
               ("set", "", 1, 1), ("right", "right", 1, 2),
               ("down", "down", 2, 1)]
        for key, shape, row, col in pad:
            btn = button("OK" if not shape else "", role="transport")
            btn.setFixedSize(64, 44)
            btn.setIconSize(QSize(18, 18))
            btn.setToolTip(P.SIMPLE_BY_KEY[key].label)
            btn.clicked.connect(lambda _=False, k=key: self.controller.send_command(k))
            pad_grid.addWidget(btn, row, col)
            if shape:
                self._pad_icons[key] = (btn, shape)
        pad_row.addLayout(pad_grid)
        pad_row.addStretch(1)
        nav_outer.addLayout(pad_row)
        layout.addWidget(nav)

        disc = QGroupBox("Disc")
        disc_layout = QHBoxLayout(disc)
        finalize = button("Finalize", role="danger")
        finalize.clicked.connect(
            lambda: self._confirm(
                "finalize",
                "Finalize this disc?",
                "Finalizing makes the disc playable in other players. On DVD-R "
                "this cannot be undone and no further recording is possible.",
            )
        )
        disc_layout.addWidget(finalize)

        cancel = button("Cancel Finalization")
        cancel.clicked.connect(
            lambda: self.controller.send_command("cancel_finalize")
        )
        disc_layout.addWidget(cancel)

        erase = button("Erase Disc", role="danger")
        erase.clicked.connect(
            lambda: self._confirm(
                "erase",
                "Erase this disc?",
                "This permanently deletes everything on the rewritable disc "
                "in the DVD deck. It cannot be undone.",
            )
        )
        disc_layout.addWidget(erase)
        disc_layout.addStretch(1)
        layout.addWidget(disc)

        layout.addStretch(1)

    def refresh_theme(self) -> None:
        colour = theme.ACTIVE.text
        for btn, shape in self._pad_icons.values():
            btn.setIcon(icons.icon(shape, colour))
        for btn, shape in self._skip_icons.values():
            btn.setIcon(icons.icon(shape, colour, size=16))

    def _confirm(self, key: str, title: str, body: str) -> None:
        answer = QMessageBox.warning(
            self, title, body,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            self.controller.send_command(key)

    def _title_search(self) -> None:
        self.controller.send(
            P.title_search(self.title_spin.value(), self.playlist_check.isChecked())
        )

    def _chapter_search(self) -> None:
        self.controller.send(P.chapter_search(self.chapter_spin.value()))

    def set_deck(self, deck: Deck) -> None:
        self.notice.setText(
            ""
            if deck is Deck.DVD
            else "These commands apply to the DVD deck. Switch the deck "
                 "selector to DVD before using them."
        )

    def on_state(self, state) -> None:
        if state.title is not None:
            self.title_spin.blockSignals(True)
            self.title_spin.setValue(max(1, state.title))
            self.title_spin.blockSignals(False)
        if state.chapter is not None:
            self.chapter_spin.blockSignals(True)
            self.chapter_spin.setValue(max(1, state.chapter))
            self.chapter_spin.blockSignals(False)


# --------------------------------------------------------------------------
# remote code browser
# --------------------------------------------------------------------------


class RemotePanel(QWidget):
    """Every wired-remote key the unit accepts over Remote Data (0x9F).

    This is the route to anything the dedicated panels do not cover:
    tracking, TBC, counter reset, ten-key entry, jog detents and so on.

    Laid out as one category at a time rather than a single long list.  The
    full table is ~85 keys; showing them all at once meant scrolling past
    everything to reach anything, at any window width.  A category holds a
    dozen or so, which fits without scrolling, and each key is a button that
    fires on click -- a remote should not make you select a key and then
    press Send.
    """

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        intro = dim("Click a key to send it. Hover for its code.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all keys by name or hex code...")
        self.search.textChanged.connect(self._rebuild)
        row.addWidget(self.search, 1)
        self.show_all = QCheckBox("Include other deck's keys")
        self.show_all.setToolTip(
            "Some keys only apply to one deck. They are hidden unless that "
            "deck is selected; tick this to see them anyway."
        )
        self.show_all.toggled.connect(self._rebuild)
        row.addWidget(self.show_all)
        layout.addLayout(row)

        # Category chips. A wrapping layout so they reflow rather than
        # clipping when the window is docked narrow.
        self._groups = (P.COMMON_GROUP,) + P.REMOTE_GROUPS
        self._chip_host = FlowWidget(spacing=6)
        chips = self._chip_host.flow
        self._chips: dict[str, QPushButton] = {}
        chip_host = self._chip_host
        for group in self._groups:
            # "&&" escapes the ampersand; a single one would be swallowed as
            # a keyboard mnemonic, rendering "Slow & Frame" as "Slow Frame"
            # with an underlined F.
            chip = button(group.replace("&", "&&"))
            chip.setCheckable(True)
            chip.clicked.connect(lambda _=False, g=group: self._pick_group(g))
            chips.addWidget(chip)
            self._chips[group] = chip
        layout.addWidget(chip_host)

        # The same choice as a dropdown, for when the window is too narrow
        # for the chips to fit without eating three rows of a short pane.
        # Exactly one of the two is visible at a time; see resizeEvent.
        self.category_combo = QComboBox()
        self.category_combo.addItems(self._groups)
        self.category_combo.currentTextChanged.connect(self._pick_group)
        self.category_combo.hide()
        layout.addWidget(self.category_combo)

        self.heading_label = heading("")
        layout.addWidget(self.heading_label)

        self._key_host = FlowWidget(spacing=6)
        self._keys = self._key_host.flow
        layout.addWidget(self._key_host)

        self.empty_note = dim("")
        self.empty_note.setWordWrap(True)
        layout.addWidget(self.empty_note)

        layout.addStretch(1)

        self._deck = Deck.VCR
        self._group = P.COMMON_GROUP
        self._pick_group(P.COMMON_GROUP)

    # -- state -------------------------------------------------------------

    def set_deck(self, deck: Deck) -> None:
        self._deck = deck
        self._rebuild()

    #: Below this the category chips are replaced by a dropdown.
    NARROW_WIDTH = 520

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        narrow = self.width() < self.NARROW_WIDTH
        self._chip_host.setVisible(not narrow)
        self.category_combo.setVisible(narrow)

    def _pick_group(self, group: str) -> None:
        if group not in self._chips:
            return
        self._group = group
        for name, chip in self._chips.items():
            chip.setChecked(name == group)
        if self.category_combo.currentText() != group:
            self.category_combo.blockSignals(True)
            self.category_combo.setCurrentText(group)
            self.category_combo.blockSignals(False)
        self._rebuild()

    def _visible_keys(self) -> list:
        needle = self.search.text().strip().lower()
        if needle:
            # Searching looks across every category, so a key you can name is
            # always one keystroke away regardless of where it is filed.
            candidates = list(P.REMOTE_KEYS)
        else:
            candidates = list(P.remote_keys_in(self._group))
        return [
            k for k in candidates
            if (self.show_all.isChecked() or k.deck is None or k.deck is self._deck)
            and (not needle
                 or needle in k.name.lower()
                 or needle in f"{k.code:02x}")
        ]

    # -- rendering ---------------------------------------------------------

    def _rebuild(self) -> None:
        searching = bool(self.search.text().strip())
        for chip in self._chips.values():
            chip.setEnabled(not searching)

        while self._keys.count():
            item = self._keys.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        keys = self._visible_keys()
        self.heading_label.setText(
            f"Search results ({len(keys)})" if searching else self._group
        )

        for key in keys:
            btn = button(key.name)
            btn.setMinimumWidth(150)
            scope = f"{key.deck.value} deck only" if key.deck else "Both decks"
            btn.setToolTip(f"{key.name}\ncode 0x{key.code:02X} · {scope}")
            if key.deck is not None and key.deck is not self._deck:
                # Shown only because "include other deck's keys" is ticked;
                # make clear it will not do anything on the current deck.
                btn.setText(f"{key.name}  ({key.deck.value})")
            btn.clicked.connect(
                lambda _=False, c=key.code: self.controller.send_remote(c)
            )
            self._keys.addWidget(btn)

        self._key_host.setVisible(bool(keys))
        if keys:
            self.empty_note.setText("")
        elif searching:
            self.empty_note.setText("No keys match that search.")
        else:
            self.empty_note.setText(
                f"No {self._group} keys apply to the {self._deck.value} deck. "
                "Tick \"Include other deck's keys\" to see them anyway."
            )
        self._key_host.updateGeometry()


# --------------------------------------------------------------------------
# on-screen display
# --------------------------------------------------------------------------

#: Remote key codes this panel drives.  Named here rather than inlined so the
#: navigation tables below read as menu steps instead of hex.
_K_SETUP = 0x37       # Set Up -- opens and closes the Main Menu
_K_ENTER = 0x3C
_K_UP = 0x82
_K_DOWN = 0x86
_K_LEFT = 0x84
_K_RIGHT = 0x80
_K_DISPLAY = 0x38
_K_ON_SCREEN_VCR = 0x1E
_K_ON_SCREEN = 0x8E
_K_AUTO_TRACKING = 0x40

#: Rows of VCR FUNCTION SET, page 1, top to bottom (manual p. 60-61).  The
#: index is how many Cursor Down presses reach that row once the page opens.
_VCR_FUNCTION_SET = (
    "S-VHS ET",
    "VIDEO CALIBRATION",
    "PICTURE CONTROL",
    "VIDEO STABILIZER",
    "SUPERIMPOSE",
    "DIGITAL R3",
    "NEXT PAGE",
)


class ScreenPanel(QWidget):
    """Getting rid of the text the deck superimposes on its video output.

    Two different things put text over the picture and they need different
    fixes, which is why they get a panel of their own rather than another
    handful of buttons on the Remote tab:

    * **Operational indicators** -- PLAY, STOP, the counter.  Governed by the
      ``SUPERIMPOSE`` setting.  The On Screen key clears whatever is showing,
      but while SUPERIMPOSE is AUTO or ON the next transport command puts it
      straight back, so clearing is a stopgap and the setting is the fix.
    * **VIDEO CALIBRATION** -- a blinking panel the deck shows at the start of
      automatic tracking while it profiles the tape.  No key dismisses it; it
      has its own setup item, and it only runs at all while automatic tracking
      is on.

    Both settings live in the VCR's setup menu, and the deck reports neither
    over the serial link -- ``SUPERIMPOSE`` is a three-way (AUTO/ON/OFF) whose
    current value there is no way to read back.  So this panel deliberately
    stops short of claiming to set them: it drives the menu to the right row
    and then hands over the arrow keys, because a blind sequence of value
    presses would be as likely to land on ON as on OFF.
    """

    #: Emitted from the macro runner's thread; a queued connection carries
    #: them back to the GUI thread.
    navStepped = Signal(int, object)
    navFinished = Signal(bool, str)

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.runner = MacroRunner(controller)
        self.runner.on_step = self.navStepped.emit
        self.runner.on_finish = self.navFinished.emit
        self.navFinished.connect(self._nav_finished)
        self._deck = Deck.VCR
        self._pending: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        intro = dim(
            "The deck draws its own text over the video output. What clears "
            "it depends on which text it is."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_now_box())
        layout.addWidget(self._build_setup_box())
        layout.addWidget(self._build_related_box())
        layout.addStretch(1)

        self._set_guide(
            "Pick a setting above and the deck's menu will open with that row "
            "highlighted."
        )
        self._sync_deck()

    # -- construction ------------------------------------------------------

    def _build_now_box(self) -> QGroupBox:
        box = QGroupBox("Clear what is showing now")
        inner = QVBoxLayout(box)
        inner.setSpacing(8)

        explain = dim(
            "PLAY, STOP and the counter are the superimpose display. On "
            "Screen clears it immediately."
        )
        explain.setWordWrap(True)
        inner.addWidget(explain)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.clear_btn = button(
            "Hide overlay now", role="primary",
            tooltip="On Screen (0x1E on the VCR deck, 0x8E on the DVD deck)",
        )
        self.clear_btn.setMinimumHeight(40)
        self.clear_btn.clicked.connect(self._clear_now)
        row.addWidget(self.clear_btn, 1)

        counter_btn = button("Counter display", tooltip="Display (0x38)")
        counter_btn.clicked.connect(
            lambda: self.controller.send_remote(_K_DISPLAY))
        row.addWidget(counter_btn, 1)
        inner.addLayout(row)

        self.clear_note = dim("")
        self.clear_note.setWordWrap(True)
        inner.addWidget(self.clear_note)
        return box

    def _build_setup_box(self) -> QGroupBox:
        box = QGroupBox("Stop it coming back")
        inner = QVBoxLayout(box)
        inner.setSpacing(8)

        explain = dim(
            "Both fixes are settings in the VCR's own menu, and the deck will "
            "not report their current values over the serial link. These "
            "buttons open the menu and move the highlight onto the right row; "
            "read the value off the TV and change it with the arrows."
        )
        explain.setWordWrap(True)
        inner.addWidget(explain)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.superimpose_btn = button("Go to SUPERIMPOSE", role="primary")
        self.superimpose_btn.setToolTip(
            "AUTO / ON / OFF. OFF stops the operational indicators entirely, "
            "including on anything you dub."
        )
        self.superimpose_btn.clicked.connect(lambda: self._goto("SUPERIMPOSE"))
        row.addWidget(self.superimpose_btn, 1)

        self.calibration_btn = button("Go to VIDEO CALIBRATION")
        self.calibration_btn.setToolTip(
            "ON / OFF. OFF stops the blinking calibration panel at the start "
            "of playback. Note that PICTURE CONTROL drops from AUTO to NORM "
            "when this is off."
        )
        self.calibration_btn.clicked.connect(
            lambda: self._goto("VIDEO CALIBRATION"))
        row.addWidget(self.calibration_btn, 1)
        inner.addLayout(row)

        self.guide = QLabel("")
        self.guide.setWordWrap(True)
        inner.addWidget(self.guide)

        inner.addWidget(separator())

        keys = QGridLayout()
        keys.setSpacing(6)
        for text, code, r, c in (
            ("▲", _K_UP, 0, 1),
            ("◀", _K_LEFT, 1, 0),
            ("Enter", _K_ENTER, 1, 1),
            ("▶", _K_RIGHT, 1, 2),
            ("▼", _K_DOWN, 2, 1),
        ):
            btn = button(text, tooltip=f"code 0x{code:02X}")
            btn.setMinimumWidth(56)
            btn.clicked.connect(
                lambda _=False, x=code: self.controller.send_remote(x))
            keys.addWidget(btn, r, c)

        self.close_btn = button(
            "Close menu", tooltip="Set Up (0x37) again, which saves and exits")
        self.close_btn.clicked.connect(self._close_menu)
        keys.addWidget(self.close_btn, 1, 3)
        keys.setColumnStretch(4, 1)
        inner.addLayout(keys)

        self.deck_note = dim("")
        self.deck_note.setWordWrap(True)
        inner.addWidget(self.deck_note)
        return box

    def _build_related_box(self) -> QGroupBox:
        box = QGroupBox("Related")
        inner = QVBoxLayout(box)
        inner.setSpacing(8)

        self.tracking_btn = button(
            "Auto Tracking On/Off",
            tooltip="Auto Tracking On/Off (0x40), VCR deck",
        )
        self.tracking_btn.clicked.connect(
            lambda: self.controller.send_remote(_K_AUTO_TRACKING))
        inner.addWidget(self.tracking_btn, 0, Qt.AlignLeft)

        note = dim(
            "Video Calibration only runs while automatic tracking is on, so "
            "turning tracking off suppresses it for this tape without "
            "touching the menu. It is a toggle and the deck does not report "
            "which way it went -- watch the picture."
        )
        note.setWordWrap(True)
        inner.addWidget(note)
        return box

    # -- state -------------------------------------------------------------

    def set_deck(self, deck: Deck) -> None:
        self._deck = deck
        self._sync_deck()

    def _sync_deck(self) -> None:
        vcr = self._deck is Deck.VCR
        for widget in (self.superimpose_btn, self.calibration_btn,
                       self.tracking_btn):
            widget.setEnabled(vcr)
        if vcr:
            self.deck_note.setText(
                "Menu rows are taken from the manual (p. 60-61); if the "
                "highlight lands somewhere else, walk it there with the "
                "arrows."
            )
            self.clear_note.setText(
                "While SUPERIMPOSE is AUTO or ON this only clears the current "
                "display -- the next transport command brings it back."
            )
        else:
            self.deck_note.setText(
                "These are VCR-deck settings. Select the VCR deck at the top "
                "left to reach them."
            )
            self.clear_note.setText(
                "On the DVD deck this sends On Screen twice, which is what "
                "clears the on-screen bar."
            )

    def _set_guide(self, text: str) -> None:
        self.guide.setText(text)

    # -- actions -----------------------------------------------------------

    def _clear_now(self) -> None:
        if self._deck is Deck.VCR:
            # One press clears it on the VCR deck (manual p. 15).
            self.controller.send_remote(_K_ON_SCREEN_VCR)
        else:
            # The DVD deck cycles indicators -> on-screen bar -> nothing, so
            # clearing from the indicators takes two presses (manual p. 14).
            self.controller.send_remote(_K_ON_SCREEN)
            self.controller.send_remote(_K_ON_SCREEN)

    def _close_menu(self) -> None:
        self.controller.send_remote(_K_SETUP)
        self._set_guide(
            "Menu closed. The setting is kept even if the unit is unplugged."
        )

    def _goto(self, row: str) -> None:
        if self.runner.running:
            return
        if not self.controller.connected:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the deck first.")
            return
        depth = _VCR_FUNCTION_SET.index(row)
        steps = [
            Step("remote", _K_SETUP),
            # The menu is drawn over video and takes a moment to appear; keys
            # sent into that gap are dropped.
            Step("wait", 1.5),
            Step("remote", _K_DOWN),   # Main Menu: COPY SET -> FUNCTION SET
            Step("remote", _K_ENTER),
            Step("wait", 1.5),
        ]
        steps += [Step("remote", _K_DOWN)] * depth
        self._pending = row
        self.superimpose_btn.setEnabled(False)
        self.calibration_btn.setEnabled(False)
        self._set_guide(f"Opening the menu and moving to {row}...")
        self.runner.run(Macro(name=f"Go to {row}", steps=steps))

    def _nav_finished(self, ok: bool, message: str) -> None:
        self._sync_deck()
        row = self._pending
        self._pending = None
        if not ok:
            self._set_guide(f"Could not get there: {message}")
            return
        self._set_guide(
            f"The TV should now show FUNCTION SET with {row} highlighted. "
            f"Press ▶ until its value reads OFF, then Close menu. If a "
            f"different row is highlighted, move onto {row} with ▲ "
            f"▼ first."
        )


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
