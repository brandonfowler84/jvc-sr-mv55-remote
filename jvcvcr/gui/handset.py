"""A virtual copy of the RM-SSR005U, the remote that ships with the deck.

The Remote tab is organised for finding a key you can name.  This one is
organised for muscle memory: the same keys, in the same places, in the same
colours as the handset in your drawer, so anyone who has used the deck can
drive it without reading a label.

Every key sends exactly what the remote does -- its Remote Data (0x9F) code --
to whichever deck is selected.  Keys that only mean something on the other
deck are greyed out rather than removed, so the layout never shifts.  The
remote's TV and cable-box keys are drawn too, for the same reason, but the
deck has no code for them and they stay dead.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QLabel, QMessageBox, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .. import protocol as P
from ..protocol import Deck
from . import icons

# --------------------------------------------------------------------------
# key table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HandsetKey:
    """One key on the handset.

    ``vcr`` and ``dvd`` are the Remote Data codes the key sends on each deck;
    None means the key does nothing there.  Most keys send the same code on
    both, but not all -- On Screen is 0x1E on the VCR and 0x8E on the DVD.
    """

    id: str
    label: str
    vcr: int | None = None
    dvd: int | None = None
    #: Printed on the body under the key, as the remote's shifted functions.
    caption: str = ""
    #: The teal lettering of the ten-key's text-entry letters.
    alpha: bool = False
    #: grey / nav / transport / power / dead -- the key's colour.
    style: str = "grey"
    icon: str = ""
    #: Keeps sending while held, like the real remote's arrows.
    repeat: bool = False
    #: Ask before sending: the key can overwrite or permanently change media.
    confirm: bool = False

    def code_for(self, deck: Deck) -> int | None:
        return self.vcr if deck is Deck.VCR else self.dvd

    @property
    def dead(self) -> bool:
        return self.vcr is None and self.dvd is None


def _both(id: str, label: str, code: int, **kw) -> HandsetKey:
    return HandsetKey(id, label, code, code, **kw)


def _dvd(id: str, label: str, code: int, **kw) -> HandsetKey:
    return HandsetKey(id, label, None, code, **kw)


def _tv(id: str, label: str) -> HandsetKey:
    return HandsetKey(id, label, style="dead")


_LETTERS = ("", "ABC", "DEF", "GHI", "JKL", "MNO", "PQRS", "TUV", "WXYZ")

KEYS: tuple[HandsetKey, ...] = (
    # -- top row -----------------------------------------------------------
    _tv("f1", "F1"),
    _tv("f2", "F2"),
    _both("vcr_dvd", "VCR/DVD", 0xD6),
    _both("power", "POWER", 0x0B, style="power"),
    # -- ten key (1-9 carry their text-entry letters) ------------------------
    *(
        _dvd(f"n{n}", str(n), 0x20 + n, caption=_LETTERS[n - 1], alpha=True)
        for n in range(1, 10)
    ),
    _both("cancel", "CANCEL", 0x36),
    _dvd("n0", "0", 0x20, caption="AUX"),
    _dvd("memo", "MEMO/MARK", 0x90),
    # -- function row ------------------------------------------------------
    _tv("f3", "F3"),
    _dvd("finalize", "FINALIZE", 0x3D, confirm=True),
    _both("display", "DISPLAY", 0x38),
    HandsetKey("on_screen", "ON SCREEN", 0x1E, 0x8E),
    # -- navigation --------------------------------------------------------
    _dvd("top_menu", "TOP MENU", 0xE0, style="nav"),
    _dvd("menu", "MENU", 0x81, style="nav"),
    _both("up", "", 0x82, style="nav", icon="up", repeat=True),
    _both("down", "", 0x86, style="nav", icon="down", repeat=True),
    _both("left", "", 0x84, style="nav", icon="left", repeat=True),
    _both("right", "", 0x80, style="nav", icon="right", repeat=True),
    _both("enter", "ENTER", 0x3C, style="nav"),
    _both("setup", "SET UP", 0x37, style="nav"),
    _both("return", "RETURN", 0xD4, style="nav"),
    # -- transport ---------------------------------------------------------
    _both("prev", "", 0x15, style="transport", icon="skip_rev",
          caption="PREVIOUS"),
    _both("next", "", 0x14, style="transport", icon="skip_fwd",
          caption="NEXT"),
    _both("slow_rev", "", 0x07, style="transport", icon="rew",
          caption="SLOW −"),
    _both("play", "", 0x0C, style="transport", icon="play",
          caption="PLAY/SELECT"),
    _both("slow_fwd", "", 0x06, style="transport", icon="ff",
          caption="SLOW +"),
    _both("stop", "", 0x03, style="transport", icon="stop",
          caption="STOP/CLEAR"),
    _both("pause", "", 0x0D, style="transport", icon="pause",
          caption="PAUSE"),
    # -- lower block -------------------------------------------------------
    _both("remain", "REMAIN", 0x31, caption="REC MODE"),
    _both("rec", "REC", 0xCC, icon="record", confirm=True),
    _both("replay", "", 0xDC, icon="replay"),
    _both("skip", "", 0x96, icon="skip_ahead"),
    _dvd("angle", "ANGLE", 0xC0, caption="LIVE CHECK"),
    _dvd("subtitle", "SUBTITLE", 0xC4),
    _both("input_up", "INPUT +", 0x19),
    _tv("vol_up", "TV VOL +"),
    _tv("tv_vcr", "TV/VCR"),
    _both("audio", "AUDIO", 0x17),
    _both("input_down", "INPUT −", 0x18),
    _tv("vol_down", "TV VOL −"),
)

KEYS_BY_ID: dict[str, HandsetKey] = {k.id: k for k in KEYS}

#: Keyboard equivalents, active while the handset has focus.
KEYBOARD: dict[str, str] = {
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Return": "enter",
    "Enter": "enter",
    "Backspace": "return",
    **{str(n): f"n{n}" for n in range(10)},
}

_KEYBOARD_HINTS = {
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "enter": "Enter", "return": "Backspace",
    **{f"n{n}": str(n) for n in range(10)},
}

# --------------------------------------------------------------------------
# look
# --------------------------------------------------------------------------

#: Glyph colour per key style, for the drawn icons.
_ICON_COLOURS = {
    "grey": "#15171a",
    "nav": "#f5f8fb",
    "transport": "#2a2a24",
}
_REC_RED = "#d8392f"

#: The handset keeps its own colours in both themes: it is a picture of a
#: black remote, and a light-theme version would stop looking like the thing
#: it exists to resemble.
_HANDSET_QSS = """
QFrame#handset {
    background: #1a1b1e;
    border: 1px solid #34363b;
    border-radius: 34px;
}
QFrame#handset QWidget { background: transparent; }
QFrame#handset QLabel {
    color: #dfe2e6;
    font-size: 9px;
    font-weight: 600;
}
QFrame#handset QLabel[hk="alpha"] { color: #35b5a6; }
QFrame#handset QLabel[hk="brand"] {
    font-size: 22px; font-weight: 900; letter-spacing: 1px;
}
QFrame#handset QLabel[hk="model"] { font-size: 9px; color: #b9bdc3; }
QFrame#handset QFrame[hk="block"] {
    border: 1px solid #8d9197;
    border-radius: 9px;
}
QFrame#handset QFrame[hk="oval"] {
    background: #0e0f11;
    border: none;
    border-radius: 58px;
}
QFrame#handset QPushButton {
    font-size: 9px;
    font-weight: 700;
    border: 1px solid #0b0c0d;
    border-radius: 9px;
    padding: 2px;
    min-height: 22px;
}
QFrame#handset QPushButton[hk="grey"] { background: #aeb2b8; color: #15171a; }
QFrame#handset QPushButton[hk="grey"]:hover { background: #c3c7cc; }
QFrame#handset QPushButton[hk="grey"]:pressed { background: #868a90; }
QFrame#handset QPushButton[hk="nav"] { background: #7c97ae; color: #f5f8fb; }
QFrame#handset QPushButton[hk="nav"]:hover { background: #91abc1; }
QFrame#handset QPushButton[hk="nav"]:pressed { background: #5a7489; }
QFrame#handset QPushButton[hk="transport"] {
    background: #e9e5c6; color: #2a2a24;
}
QFrame#handset QPushButton[hk="transport"]:hover { background: #f5f2db; }
QFrame#handset QPushButton[hk="transport"]:pressed { background: #c4bf9c; }
QFrame#handset QPushButton[hk="power"] { background: #3350c8; color: #ffffff; }
QFrame#handset QPushButton[hk="power"]:hover { background: #4764dc; }
QFrame#handset QPushButton[hk="power"]:pressed { background: #213796; }
QFrame#handset QPushButton:disabled { background: #4b4d52; color: #82868c; }
QFrame#handset QPushButton[hk="dead"]:disabled {
    background: #2b2d31; color: #62666c; border-color: #232427;
}
"""


def _equal_columns(grid: QGridLayout, count: int) -> None:
    for col in range(count):
        grid.setColumnStretch(col, 1)


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------


class HandsetPanel(QWidget):
    """The remote, drawn as the remote."""

    #: The VCR/DVD key was pressed; carries the deck to switch to.  The main
    #: window moves the deck selector, so what the app targets follows what
    #: the key just did on the unit.
    deckToggleRequested = Signal(object)
    #: A key went out; carries a one-line description for the status bar.
    #: Most keys only change the picture, so without this a press looks
    #: like nothing happened.
    keySent = Signal(str)

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self._deck = Deck.VCR
        self.buttons: dict[str, QPushButton] = {}
        # Clicking a key focuses the panel, which is what arms the keyboard.
        self.setFocusPolicy(Qt.ClickFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_handset())

        for sequence, key_id in KEYBOARD.items():
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(
                lambda k=key_id: self._keyboard_press(k))

        self.set_deck(Deck.VCR)

    # -- construction ------------------------------------------------------

    def _build_handset(self) -> QFrame:
        body = QFrame()
        body.setObjectName("handset")
        body.setStyleSheet(_HANDSET_QSS)
        body.setFixedWidth(300)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(22, 26, 22, 22)
        outer.setSpacing(10)

        outer.addLayout(self._row(("f1", "f2", "vcr_dvd", "power")))

        pad = self._block()
        grid = QGridLayout(pad)
        grid.setContentsMargins(8, 8, 8, 6)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        order = ("n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8", "n9",
                 "cancel", "n0", "memo")
        for index, key_id in enumerate(order):
            grid.addWidget(self._cell(key_id, captioned=True),
                           index // 3, index % 3)
        _equal_columns(grid, 3)
        outer.addWidget(pad)

        outer.addLayout(self._row(("f3", "finalize", "display", "on_screen")))

        oval = QFrame()
        oval.setProperty("hk", "oval")
        nav = QGridLayout(oval)
        nav.setContentsMargins(14, 12, 14, 12)
        nav.setSpacing(6)
        for key_id, row, col in (
            ("top_menu", 0, 0), ("up", 0, 1), ("menu", 0, 2),
            ("left", 1, 0), ("enter", 1, 1), ("right", 1, 2),
            ("setup", 2, 0), ("down", 2, 1), ("return", 2, 2),
        ):
            btn = self._button(KEYS_BY_ID[key_id])
            if key_id in ("up", "down", "left", "right", "enter"):
                btn.setMinimumHeight(32)
            nav.addWidget(btn, row, col)
        _equal_columns(nav, 3)
        outer.addWidget(oval)

        transport = QGridLayout()
        transport.setHorizontalSpacing(14)
        transport.setVerticalSpacing(4)
        for key_id, row, col in (
            ("prev", 0, 0), ("next", 0, 2),
            ("slow_rev", 1, 0), ("play", 1, 1), ("slow_fwd", 1, 2),
            ("stop", 2, 0), ("pause", 2, 2),
        ):
            transport.addWidget(self._cell(key_id, above=True), row, col)
        _equal_columns(transport, 3)
        outer.addLayout(transport)

        lower = self._block()
        grid = QGridLayout(lower)
        grid.setContentsMargins(8, 8, 8, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        for row, ids in enumerate((
            ("remain", "rec", "replay", "skip"),
            ("angle", "subtitle", "input_up", "vol_up"),
            ("tv_vcr", "audio", "input_down", "vol_down"),
        )):
            for col, key_id in enumerate(ids):
                grid.addWidget(self._cell(key_id, captioned=True), row, col)
        _equal_columns(grid, 4)
        outer.addWidget(lower)

        outer.addSpacing(6)
        brand = QLabel("JVC")
        brand.setProperty("hk", "brand")
        brand.setAlignment(Qt.AlignHCenter)
        outer.addWidget(brand)
        model = QLabel("RM-SSR005U  ·  DVD RECORDER")
        model.setProperty("hk", "model")
        model.setAlignment(Qt.AlignHCenter)
        outer.addWidget(model)
        return body

    @staticmethod
    def _block() -> QFrame:
        frame = QFrame()
        frame.setProperty("hk", "block")
        return frame

    def _row(self, ids) -> QGridLayout:
        row = QGridLayout()
        row.setHorizontalSpacing(8)
        for col, key_id in enumerate(ids):
            row.addWidget(self._button(KEYS_BY_ID[key_id]), 0, col)
        _equal_columns(row, len(ids))
        return row

    def _cell(self, key_id: str, *, captioned: bool = False,
              above: bool = False) -> QWidget:
        """A key with its printed caption, above or below it.

        `captioned` reserves the caption line even when blank, so every row
        of a block stays the same height and the keys line up.
        """
        key = KEYS_BY_ID[key_id]
        btn = self._button(key)
        if not (key.caption or captioned):
            return btn
        host = QWidget()
        stack = QVBoxLayout(host)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(1)
        caption = QLabel(key.caption)
        caption.setAlignment(Qt.AlignHCenter)
        caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        caption.setFixedHeight(12)
        if key.alpha:
            caption.setProperty("hk", "alpha")
        if above:
            stack.addWidget(caption)
        stack.addWidget(btn)
        if not above:
            stack.addWidget(caption)
        return host

    def _button(self, key: HandsetKey) -> QPushButton:
        btn = QPushButton(key.label)
        btn.setProperty("hk", key.style)
        # Keys never take focus: the panel does, so the keyboard shortcuts
        # stay live and Space still reaches the window's Play shortcut.
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip(self._tooltip(key))
        # Widths come from the grid, not the label: "F3" and "ON SCREEN" sit
        # side by side on the remote as keys of one size.
        btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        if key.icon:
            colour = _REC_RED if key.icon == "record" else _ICON_COLOURS.get(
                key.style, _ICON_COLOURS["grey"])
            btn.setIcon(icons.icon(key.icon, colour, size=14))
            btn.setIconSize(QSize(14, 14))
        if key.style == "transport":
            btn.setMinimumHeight(28)
        if key.repeat:
            btn.setAutoRepeat(True)
            # Each Remote Data frame takes ~120 ms on the wire with byte
            # pacing, so repeat no faster than the link can drain.
            btn.setAutoRepeatDelay(450)
            btn.setAutoRepeatInterval(220)
        if key.dead:
            btn.setEnabled(False)
        else:
            btn.clicked.connect(lambda _=False, k=key.id: self.press(k))
        self.buttons[key.id] = btn
        return btn

    @staticmethod
    def _tooltip(key: HandsetKey) -> str:
        title = key.label or key.caption or key.id
        if key.dead:
            return (f"{title}\nA TV / cable-box key on the real remote. "
                    "The deck has no code for it.")
        code = key.dvd if key.dvd is not None else key.vcr
        lines = [P.REMOTE_CODE_NAMES.get(code, title)]
        if key.vcr is not None and key.dvd is not None and key.vcr != key.dvd:
            lines.append(f"VCR 0x{key.vcr:02X} · DVD 0x{key.dvd:02X}")
        elif key.vcr is None:
            lines.append(f"code 0x{key.dvd:02X} · DVD deck only")
        elif key.dvd is None:
            lines.append(f"code 0x{key.vcr:02X} · VCR deck only")
        else:
            lines.append(f"code 0x{code:02X} · both decks")
        hint = _KEYBOARD_HINTS.get(key.id)
        if hint:
            lines.append(f"Keyboard: {hint}")
        return "\n".join(lines)

    # -- state -------------------------------------------------------------

    def set_deck(self, deck: Deck) -> None:
        self._deck = deck
        for key in KEYS:
            if not key.dead:
                self.buttons[key.id].setEnabled(key.code_for(deck) is not None)

    # -- pressing ----------------------------------------------------------

    def press(self, key_id: str) -> bool:
        """Send a key as the remote would.  Returns True if anything was sent."""
        key = KEYS_BY_ID[key_id]
        code = key.code_for(self._deck)
        if code is None or not self.buttons[key_id].isEnabled():
            return False
        self.setFocus(Qt.OtherFocusReason)
        if key.confirm and not self._confirm(key):
            return False
        self.controller.send_remote(code)
        name = P.REMOTE_CODE_NAMES.get(code, key.label)
        self.keySent.emit(f"Sent {name}  (9F {code:02X})")
        if key.id == "vcr_dvd":
            other = Deck.DVD if self._deck is Deck.VCR else Deck.VCR
            self.deckToggleRequested.emit(other)
        return True

    def _keyboard_press(self, key_id: str) -> None:
        """A keyboard press, shown on the key as though it were clicked."""
        btn = self.buttons[key_id]
        if not btn.isEnabled():
            return
        btn.setDown(True)
        QTimer.singleShot(90, lambda: btn.setDown(False))
        self.press(key_id)

    def _confirm(self, key: HandsetKey) -> bool:
        deck = self._deck.value
        if key.id == "rec":
            title = "Start recording?"
            body = (
                f"This is the remote's REC key: the {deck} deck starts "
                "recording immediately. On a tape, that records over whatever "
                "is at the current position."
            )
        else:
            title = "Finalize this disc?"
            body = (
                "Finalizing makes the disc playable in other players. On DVD-R "
                "this cannot be undone and no further recording is possible."
            )
        answer = QMessageBox.warning(
            self, title, body,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes
