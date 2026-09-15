"""Main window and application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QIcon, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QSizePolicy, QSpinBox,
    QTextBrowser, QVBoxLayout, QWidget,
)

from .. import __version__
from .. import protocol as P
from ..device import DeviceController, LinkState
from ..macros import MacroLibrary
from ..protocol import Deck
from ..transport import SerialTransport, SimulatorTransport, list_serial_ports
from . import theme
from .handset import HandsetPanel
from .panels import (
    ConsolePanel, ElidedLabel, FlowLayout, MacroPanel, StatusPanel, button,
    card, dim, heading, scrollable,
)
from .quickkeys import QuickKeysBar

APP_NAME = "JVC SR-MV55 Control"
ORG_NAME = "jvcvcr"

SIMULATOR_PORT = "__simulator__"


def config_dir() -> Path:
    """Per-user directory for macros and settings."""
    if sys.platform == "win32":
        import os

        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    path = base / "jvcvcr"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Bridge(QObject):
    """Marshals device-thread callbacks onto the GUI thread.

    The controller invokes its callbacks from its worker thread.  Emitting a
    signal from that thread to slots on objects living in the GUI thread gives
    us a queued connection, which is the safe way across the boundary.
    """

    linkChanged = Signal(object, str)
    stateChanged = Signal(object)
    logged = Signal(object)
    responded = Signal(int, str)
    macroStep = Signal(int, object)
    macroFinished = Signal(bool, str)


class ConnectionBar(QWidget):
    connectRequested = Signal(str)
    disconnectRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        # A wrapping layout so the bar reflows onto a second row in a narrow
        # window rather than clipping the Connect button off the edge.
        layout = FlowLayout(self, spacing=8)
        layout.setContentsMargins(0, 0, 0, 0)

        self.port_label = dim("Port")
        layout.addWidget(self.port_label)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(200)
        self.port_combo.setMaximumWidth(420)
        self.port_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.port_combo)

        refresh = button("Refresh", tooltip="Re-scan for serial ports")
        refresh.clicked.connect(self.refresh_ports)
        layout.addWidget(refresh)

        self.connect_btn = button("Connect", role="primary")
        self.connect_btn.clicked.connect(self._toggle)
        layout.addWidget(self.connect_btn)

        self.led = QLabel("●")
        self.led.setProperty("role", "led")
        self.led.setProperty("state", "off")
        layout.addWidget(self.led)
        self.status = ElidedLabel("Disconnected")
        self.status.setProperty("role", "dim")
        # FlowLayout sizes each item by its hint, and an ignored horizontal
        # policy reports none -- so state the width the message needs here.
        self.status.setMinimumWidth(260)
        layout.addWidget(self.status)

        self._connected = False
        self.refresh_ports()

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        for device, label in list_serial_ports():
            self.port_combo.addItem(f"{device} — {label}", device)
        self.port_combo.addItem("Simulator (no hardware needed)", SIMULATOR_PORT)
        if current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def set_compact(self, compact: bool) -> None:
        """Fit the bar on one row in a narrow window.

        The "Port" caption and the status text are what push Connect onto a
        second row. Neither is lost: the LED carries the link state, and the
        same message is in the window's status bar.
        """
        self.port_label.setVisible(not compact)
        self.status.setVisible(not compact)
        self.port_combo.setMinimumWidth(130 if compact else 200)
        self.updateGeometry()

    def select_port(self, device: str) -> None:
        index = self.port_combo.findData(device)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)

    @property
    def port(self) -> str:
        return self.port_combo.currentData() or SIMULATOR_PORT

    def _toggle(self) -> None:
        if self._connected:
            self.disconnectRequested.emit()
        else:
            self.connectRequested.emit(self.port)

    def set_link(self, link: LinkState, message: str) -> None:
        self._connected = link is LinkState.CONNECTED
        self.connect_btn.setText("Disconnect" if self._connected else "Connect")
        self.port_combo.setEnabled(not self._connected)
        self.led.setProperty("state", {
            LinkState.CONNECTED: "on",
            LinkState.CONNECTING: "busy",
            LinkState.ERROR: "bad",
            LinkState.DISCONNECTED: "off",
        }[link])
        self.led.style().unpolish(self.led)
        self.led.style().polish(self.led)
        self.status.setText(message)


class SettingsDialog(QDialog):
    def __init__(self, controller: DeviceController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Settings")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        note = QLabel(
            "The manual requires at least 50 ms between commands. Raise the gap "
            "if the deck misses commands; lower it only if you know it copes."
        )
        note.setWordWrap(True)
        note.setProperty("role", "dim")
        layout.addWidget(note)

        gap_row = QHBoxLayout()
        gap_row.addWidget(QLabel("Command gap (ms)"))
        self.gap = QSpinBox()
        self.gap.setRange(50, 1000)
        self.gap.setValue(int(controller.command_gap * 1000))
        gap_row.addWidget(self.gap)
        layout.addLayout(gap_row)

        gap_note = QLabel(
            "The deck ignores a multi-byte command whose bytes arrive with no "
            "gap between them, so each byte is sent separately. Lower this "
            "only if multi-byte commands still work."
        )
        gap_note.setWordWrap(True)
        gap_note.setProperty("role", "dim")
        layout.addWidget(gap_note)

        byte_row = QHBoxLayout()
        byte_row.addWidget(QLabel("Gap between bytes (ms)"))
        self.byte_gap = QSpinBox()
        self.byte_gap.setRange(0, 500)
        self.byte_gap.setValue(int(controller.inter_byte_gap * 1000))
        byte_row.addWidget(self.byte_gap)
        layout.addLayout(byte_row)

        poll_row = QHBoxLayout()
        poll_row.addWidget(QLabel("Status poll interval (ms)"))
        self.poll = QSpinBox()
        self.poll.setRange(100, 5000)
        self.poll.setSingleStep(100)
        self.poll.setValue(int(controller.poll_interval * 1000))
        poll_row.addWidget(self.poll)
        layout.addLayout(poll_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self) -> None:
        self.controller.command_gap = self.gap.value() / 1000
        self.controller.poll_interval = self.poll.value() / 1000
        self.controller.inter_byte_gap = self.byte_gap.value() / 1000


HELP_HTML = f"""
<h2>Connecting to the deck</h2>
<p>Only the <b>SR-MV55U</b> has the serial port; the SR-MV45U does not.</p>

<h3>Cable</h3>
<p>The deck's <i>Serial Command</i> port is wired as <b>DCE</b> &mdash; pin 2 is
TxD out of the deck, pin 3 is RxD into it &mdash; so in principle a
<b>straight-through</b> DB9 cable is correct, and that is what the manual
specifies.</p>
<p>In practice, <b>many inexpensive USB-to-serial cables have pins 2 and 3
reversed internally</b>, whatever they are sold as. With one of those, a
straight-through connection will not work and adding a <b>null-modem
adapter</b> (male-to-female, which crosses 2 and 3) makes it work. Either can
be the right answer; it depends on your cable, not on the deck.</p>
<p><b>A loopback test cannot tell you which you have.</b> Bridging pins 2 and 3
at the connector only proves TX and RX reach each other, which succeeds with a
reversed cable too. Try straight through first; if the deck never answers, add
a null-modem adapter before concluding anything is faulty.</p>
<table cellpadding="4">
<tr><th align="left">Pin</th><th align="left">Signal</th></tr>
<tr><td>2</td><td>TxD (deck &rarr; PC)</td></tr>
<tr><td>3</td><td>RxD (PC &rarr; deck)</td></tr>
<tr><td>5</td><td>Ground</td></tr>
</table>
<p>No handshaking lines are connected.</p>

<h3>Port settings</h3>
<p>9600 baud, 8 data bits, <b>odd parity</b>, 1 stop bit, no flow control.
This program sets all of that for you. Odd parity is unusual and is the usual
reason a hand-rolled terminal test fails.</p>

<h3>Before it will respond</h3>
<ul>
<li>Wait about <b>10 seconds</b> after powering the deck on; the manual says
communication is not established before then.</li>
<li>Check the front panel for <b>LOCKED</b>. Mode Lock blocks some RS-232C
commands and survives an AC power cycle. Cancel it on the unit.</li>
<li>Install your adapter's driver (CH340, CP2102 and FTDI chips each need
their own on some Windows versions).</li>
</ul>

<h2>If something does not work</h2>

<h3>Nothing responds at all</h3>
<ol>
<li>Open <b>Tools &rarr; Console</b> and send <code>D7</code>. A healthy deck
answers with five status bytes. Note there is <b>no <code>D7</code> in the
reply</b> &mdash; sense replies carry no opcode.</li>
<li>Try a null-modem adapter, per the cable note above.</li>
<li>Reseat the connector and screw it down; a 20-year-old port that has never
been used may just need the contact wiped.</li>
<li>Run <b>Device &rarr; Diagnose Remote Data&hellip;</b>, which sends a set of
probes with known-good controls either side and reports what the results rule
in and out.</li>
</ol>

<h3>"Write timed out" or "Send failed"</h3>
<p>The port opened but the adapter refuses to transmit. Our cable has no
handshake lines, and some USB-serial chipsets gate their transmitter on
RTS/DTR. The app forces both high on connect, which fixes it for most
adapters. If it persists, check the port's Advanced settings in Device Manager
for a flow-control option, or try a different adapter.</p>

<h3>Some buttons work and others do nothing</h3>
<p>If the working ones are Play, Stop, Rewind and Eject, this is the
<b>byte pacing</b> quirk below. Those are single-byte commands; everything
else is not.</p>

<h2>Quirks of this deck</h2>
<p>These are not in the manual. They were established against real hardware.</p>

<h3>Multi-byte commands need a gap between their bytes</h3>
<p>The deck silently ignores a multi-byte command whose bytes arrive
back-to-back &mdash; no reply, not even a NAK. The same bytes written
separately, tens of milliseconds apart, work. This affects deck targeting,
input and record-mode selection, searches, shuttle speeds and every handset
key, while single-byte transport commands are unaffected.</p>
<p>The app spaces every byte automatically (<b>File &rarr; Settings &rarr; Gap
between bytes</b>, 60&nbsp;ms by default).</p>

<h3>Sense replies carry no opcode</h3>
<p>A reply to <code>D7</code> is five status bytes with no leading
<code>D7</code>; <code>D9</code> returns eight ASCII bytes with no leading
<code>D9</code>. Worth knowing when reading the traffic log.</p>

<h3>The VCR reports partial times</h3>
<p>On the VCR deck, remaining time is hours and minutes only, with the seconds
field filled with <code>-</code>. With no tape loaded, every field is filler.
Both are normal.</p>

<h2>Recording</h2>
<p>Press <b>REC</b> on the handset. The app asks you to confirm, then the deck
records straight away &mdash; the remote's REC key does not need the Rec
Request that a serial Record command does. <b>REMAIN / REC MODE</b> changes the
recording mode; the status card shows which one is set.</p>

<h2>The handset</h2>
<p>The handset sends the same codes as the RM-SSR005U remote. Many keys only
change something on the <b>video output</b> &mdash; menus, Display, On Screen,
Subtitle, Angle &mdash; so they look like they do nothing if you are watching
this window rather than the TV or capture feed. The status bar confirms each
key as it goes out.</p>
<p>Keys the selected deck cannot use are greyed out. F1&ndash;F3, TV/VCR and
TV VOL are TV and cable-box keys, which the deck has no code for. Tracking, TBC
and Counter Reset are not on this remote, so they sit under the status card
instead.</p>

<p style="color:{theme.ACTIVE.text_faint}">Protocol from the SR-MV45U/SR-MV55U
user manual, pages 73&ndash;85, plus behaviour established against real
hardware. See PROTOCOL.md in the project folder for the full write-up.</p>
"""


GETTING_STARTED_HTML = f"""
<h2>Getting started</h2>
<p>This app drives a <b>JVC SR-MV55U</b> DVD/VCR deck over its serial port.
Here is the short version of getting from nothing to working.</p>

<h3>1. Have a look without any hardware</h3>
<p>Pick <b>Simulator (no hardware needed)</b> in the port list and press
<b>Connect</b>. A built-in fake deck responds just like the real one, so you can
try every control, watch the traffic log and get a feel for the layout before
wiring anything up. Nothing you do there touches real equipment.</p>

<h3>2. What you need for the real thing</h3>
<ul>
<li>An <b>SR-MV55U</b>. The SR-MV45U looks identical and shares a manual, but
has no serial port.</li>
<li>A <b>USB-to-serial adapter</b> and a <b>DB9 cable</b>. Genuine FTDI-based
adapters tend to give the least trouble.</li>
<li>Possibly a <b>null-modem adapter</b> &mdash; whether you need one depends on
how your particular cable is wired internally. Read
<i>Help &rarr; Wiring and troubleshooting</i> before buying anything.</li>
</ul>

<h3>3. Connect</h3>
<ol>
<li>Power the deck on and give it <b>ten seconds</b>.</li>
<li>Pick your COM port from the list at the top &mdash; press <b>Refresh</b> if
it is not listed &mdash; then press <b>Connect</b>.</li>
<li>The dot beside the button turns green and the status card starts showing the
deck's real state: transport mode, counter, what media is loaded.</li>
</ol>
<p>If the status card stays blank, go to
<i>Help &rarr; Wiring and troubleshooting</i>. It covers every failure this
project actually ran into, in the order worth checking them.</p>

<h3>4. Driving the deck</h3>
<p>Choose <b>VCR</b> or <b>DVD</b> in the status card first &mdash; every
command applies to whichever deck is selected, and forgetting that is the most
common cause of "why did nothing happen".</p>
<p>Then use the handset exactly as you would the real remote. The keyboard
works too: <b>Space</b> play, <b>S</b> stop, <b>J</b>/<b>L</b> rewind and fast
forward, <b>K</b> still, and &mdash; after clicking the handset &mdash; the
arrow keys, <b>Enter</b> and <b>0</b>&ndash;<b>9</b>. Full list under
<i>Help &rarr; Keyboard shortcuts</i>.</p>

<h3>5. Recording</h3>
<p>Choose the input with <b>INPUT +/&minus;</b> and the mode with <b>REMAIN /
REC MODE</b>, then press <b>REC</b>. The app asks you to confirm before the
deck starts.</p>

<h3>6. Make it yours</h3>
<ul>
<li><b>View &rarr; Configure extra keys</b> &mdash; choose the keys under the
status card. Tracking, TBC and counter reset are there by default because the
remote has no keys for them.</li>
<li><b>View &rarr; Dark / Light theme</b>.</li>
<li>Make the window narrow to dock it beside a capture window &mdash; the status
stays pinned at the top and the handset scrolls underneath.</li>
<li><b>Tools &rarr; Macros</b> &mdash; save a sequence of commands with delays,
handy for repeatable VHS&rarr;DVD runs.</li>
</ul>

<h3>If something misbehaves</h3>
<p><b>Tools &rarr; Console</b> shows every byte in and out with a plain-English
decode, and <b>Device &rarr; Diagnose Remote Data&hellip;</b> runs a scripted set
of probes and reports what the results rule in and out. Between them they will
usually find the problem without guesswork.</p>

<p style="color:{theme.ACTIVE.text_faint}">You can reopen this page any time
from the Help menu.</p>
"""


class HtmlDialog(QDialog):
    """A scrollable page of help text."""

    def __init__(self, title: str, html: str, parent=None,
                 size: tuple[int, int] = (720, 760)) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(html)
        browser.setOpenExternalLinks(True)
        layout.addWidget(browser)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)
        layout.addWidget(self.buttons)


class HelpDialog(HtmlDialog):
    def __init__(self, parent=None) -> None:
        super().__init__("Wiring and troubleshooting", HELP_HTML, parent)


class GettingStartedDialog(HtmlDialog):
    """Shown automatically the first time the app is run."""

    def __init__(self, parent=None) -> None:
        super().__init__("Getting started", GETTING_STARTED_HTML, parent,
                         size=(700, 720))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  {__version__}")
        self.resize(780, 940)
        # The handset's own width plus margins: narrow enough to dock beside
        # a capture window.
        self.setMinimumSize(360, 480)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._auto_reconnect = False
        self._last_port: str | None = None
        self.controller = DeviceController()
        self.library = MacroLibrary(config_dir() / "macros.json")
        self.library.load()

        self.bridge = Bridge()
        self.controller.on_link_change = self.bridge.linkChanged.emit
        self.controller.on_state_change = self.bridge.stateChanged.emit
        self.controller.on_log = self.bridge.logged.emit
        self.controller.on_response = self.bridge.responded.emit

        self._build_ui()
        self._wire_signals()
        self._restore_settings()
        self._shortcuts()

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(3000)
        self._reconnect_timer.timeout.connect(self._try_auto_reconnect)
        self._reconnect_timer.start()

        # After the window is up, so the guide appears over a drawn interface.
        QTimer.singleShot(300, self._maybe_show_getting_started)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        self.connection = ConnectionBar()
        outer.addWidget(self.connection)

        self._outer = outer

        self.status_panel = StatusPanel()
        # Merged into the status card's header row, which saves a whole row
        # and keeps "which deck" next to "what is it doing".
        self.deck_selector = self.status_panel.deck_selector
        # The VCR keys this remote has no key for. Tracking above all: it is
        # what you fight most while capturing old tapes.
        extras = card()
        extras.setToolTip("VCR keys the RM-SSR005U remote has no key for")
        self._extras_layout = QVBoxLayout(extras)
        extras_layout = self._extras_layout
        extras_layout.setContentsMargins(16, 12, 16, 12)
        extras_layout.setSpacing(8)
        self._extras_heading = heading("Not on the remote")
        extras_layout.addWidget(self._extras_heading)
        self.quick_keys = QuickKeysBar(self.controller)
        extras_layout.addWidget(self.quick_keys)
        extras.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        # Status and extra keys stay outside the scroll area in both layouts:
        # however far the handset is scrolled, the deck state and the
        # tracking keys stay in view.
        self.side = QWidget()
        side_layout = QVBoxLayout(self.side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(10)
        side_layout.addWidget(self.status_panel)
        side_layout.addWidget(extras)
        side_layout.addStretch(1)

        self.handset_panel = HandsetPanel(self.controller)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        handset_row = QHBoxLayout()
        handset_row.addStretch(1)
        handset_row.addWidget(self.handset_panel)
        handset_row.addStretch(1)
        body_layout.addLayout(handset_row)
        body_layout.addStretch(1)

        # Two columns when wide, one when narrow -- see _apply_layout.
        self.content = QBoxLayout(QBoxLayout.LeftToRight)
        self.content.addWidget(self.side)
        # Scrolls rather than squashing on a short screen: the handset is
        # tall, and a key cut off the bottom is worse than a scrollbar.
        self.content.addWidget(scrollable(body), 1)
        outer.addLayout(self.content, 1)
        self._narrow: bool | None = None
        self._apply_layout(narrow=False)

        # Built once and kept for the whole session, so the traffic log and a
        # running macro survive their window being closed; the Tools menu
        # only shows them.
        self.console_panel = ConsolePanel(self.controller)
        self.console_window = self._tool_window(
            self.console_panel, "Console", (860, 620))
        self.macro_panel = MacroPanel(self.controller, self.library)
        self.macro_window = self._tool_window(
            self.macro_panel, "Macros", (760, 520))

        self.setCentralWidget(central)

        self.statusBar().showMessage("Not connected")
        self._build_menu()
        # So the handset's keyboard keys work without clicking it first.
        self.handset_panel.setFocus()

    #: Below this window width the status column and the handset no longer fit
    #: side by side, and the window becomes a single column.
    NARROW_WIDTH = 720

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_layout(self.width() < self.NARROW_WIDTH)

    def _apply_layout(self, narrow: bool) -> None:
        """One column when narrow, two when wide.

        Narrow is the shape the window takes docked beside a capture. The
        status card drops to its one-line form and, with the extra keys, is
        pinned above the handset, which scrolls in whatever height is left.
        """
        if narrow == self._narrow:
            return
        self._narrow = narrow
        # Everything pinned above the handset gives up height when narrow,
        # because every pixel it keeps is a row of handset keys scrolled away.
        self.status_panel.set_compact(narrow)
        self.connection.set_compact(narrow)
        self._extras_heading.setVisible(not narrow)
        if narrow:
            self._extras_layout.setContentsMargins(10, 8, 10, 8)
        else:
            self._extras_layout.setContentsMargins(16, 12, 16, 12)
        if narrow:
            self.content.setDirection(QBoxLayout.TopToBottom)
            self.content.setSpacing(8)
            self._outer.setContentsMargins(8, 8, 8, 8)
            self._outer.setSpacing(8)
            self.side.setMinimumWidth(0)
            self.side.setMaximumWidth(16777215)
            self.side.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        else:
            self.content.setDirection(QBoxLayout.LeftToRight)
            self.content.setSpacing(16)
            self._outer.setContentsMargins(14, 12, 14, 12)
            self._outer.setSpacing(12)
            self.side.setFixedWidth(360)
            self.side.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

    def _tool_window(self, panel: QWidget, title: str, size) -> QDialog:
        window = QDialog(self)
        window.setWindowTitle(title)
        window.resize(*size)
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)
        return window

    @staticmethod
    def _show_window(window: QDialog) -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        settings_action = QAction("&Settings...", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        device_menu = self.menuBar().addMenu("&Device")
        probe = QAction("&Probe protocol", self)
        probe.setStatusTip(
            "Send a Status Sense and report how the unit frames its reply"
        )
        probe.triggered.connect(self._probe)
        device_menu.addAction(probe)

        diagnose = QAction("&Diagnose Remote Data...", self)
        diagnose.setStatusTip(
            "Work out why Remote Data (9F) commands do nothing on this deck"
        )
        diagnose.triggered.connect(self._diagnose)
        device_menu.addAction(diagnose)

        self.poll_action = QAction("Poll &status continuously", self)
        self.poll_action.setCheckable(True)
        self.poll_action.setChecked(True)
        self.poll_action.toggled.connect(self.controller.set_polling)
        device_menu.addAction(self.poll_action)

        device_menu.addSeparator()
        power_on = QAction("Power &on", self)
        power_on.triggered.connect(lambda: self.controller.send_command("power_on"))
        device_menu.addAction(power_on)
        power_off = QAction("Power o&ff", self)
        power_off.triggered.connect(lambda: self.controller.send_command("power_off"))
        device_menu.addAction(power_off)

        view_menu = self.menuBar().addMenu("&View")
        self.theme_actions: dict[str, QAction] = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for name, label in (("dark", "&Dark theme"), ("light", "&Light theme")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setActionGroup(theme_group)
            action.triggered.connect(lambda _=False, n=name: self.set_theme(n))
            view_menu.addAction(action)
            self.theme_actions[name] = action

        view_menu.addSeparator()
        quick = QAction("Configure &extra keys...", self)
        quick.setStatusTip("Choose which keys sit under the status card")
        quick.triggered.connect(self._configure_quick_keys)
        view_menu.addAction(quick)

        tools_menu = self.menuBar().addMenu("&Tools")
        console = QAction("&Console", self)
        console.setShortcut(QKeySequence("Ctrl+L"))
        console.setStatusTip("Every byte in and out, with its meaning")
        console.triggered.connect(lambda: self._show_window(self.console_window))
        tools_menu.addAction(console)
        macros = QAction("&Macros", self)
        macros.setStatusTip("Named sequences of commands with delays")
        macros.triggered.connect(lambda: self._show_window(self.macro_window))
        tools_menu.addAction(macros)
        tools_menu.addSeparator()
        clock = QAction("&Sync deck clock from this PC", self)
        clock.triggered.connect(self._sync_clock)
        tools_menu.addAction(clock)

        help_menu = self.menuBar().addMenu("&Help")
        started = QAction("&Getting started", self)
        started.triggered.connect(
            lambda: GettingStartedDialog(self).exec()
        )
        help_menu.addAction(started)
        help_menu.addSeparator()

        wiring = QAction("&Wiring and troubleshooting", self)
        wiring.triggered.connect(lambda: HelpDialog(self).exec())
        help_menu.addAction(wiring)
        shortcuts = QAction("&Keyboard shortcuts", self)
        shortcuts.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts)
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _wire_signals(self) -> None:
        self.connection.connectRequested.connect(self._connect)
        self.connection.disconnectRequested.connect(self._disconnect)

        self.bridge.linkChanged.connect(self._on_link)
        self.bridge.stateChanged.connect(self._on_state)
        self.bridge.logged.connect(self.console_panel.append)
        self.bridge.responded.connect(self._on_response)

        self.deck_selector.deckChanged.connect(self._on_deck)
        self.handset_panel.deckToggleRequested.connect(self._on_handset_deck)
        self.handset_panel.keySent.connect(
            lambda text: self.statusBar().showMessage(text, 4000))

        self.status_panel.clearErrorRequested.connect(
            lambda: self.controller.send_command("clear")
        )

        runner = self.macro_panel.runner
        runner.on_step = self.bridge.macroStep.emit
        runner.on_finish = self.bridge.macroFinished.emit
        self.bridge.macroStep.connect(self.macro_panel.on_macro_step)
        self.bridge.macroFinished.connect(self.macro_panel.on_macro_finish)

    def _shortcuts(self) -> None:
        """Keyboard control for the transport, the way a deck should feel."""
        bindings = (
            ("Space", "play"),
            ("K", "still"),
            ("S", "stop"),
            ("J", "rew"),
            ("L", "ff"),
            (",", "step_rev"),
            (".", "step_fwd"),
        )
        for key, command in bindings:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(
                lambda c=command: self.controller.send_command(c)
            )

    # -- connection --------------------------------------------------------

    def _connect(self, port: str, auto: bool = False) -> None:
        if port == SIMULATOR_PORT:
            transport = SimulatorTransport()
        else:
            transport = SerialTransport(port)
        self.controller.connect(transport)
        if self.controller.link is LinkState.CONNECTED:
            self.settings.setValue("port", port)
            self._last_port = port
            self._auto_reconnect = True
        elif not auto:
            # A connect the user asked for and that failed is worth a dialog;
            # a failed background retry is not.
            QMessageBox.critical(
                self, "Could not connect",
                f"{port} did not open.\n\nCheck the adapter is plugged in "
                "and no other program is using the port."
            )

    def _disconnect(self) -> None:
        # An explicit disconnect means stop trying to get back on.
        self._auto_reconnect = False
        self.controller.disconnect()

    def _try_auto_reconnect(self) -> None:
        """Reconnect after an unplug, once the port comes back.

        USB-serial adapters get knocked out mid-session; without this the app
        sits in an error state until noticed, which on a long capture could be
        a long time.
        """
        if not self._auto_reconnect or self.controller.connected:
            return
        port = self._last_port
        if not port:
            return
        if port != SIMULATOR_PORT:
            if port not in {device for device, _label in list_serial_ports()}:
                return  # adapter still missing; wait for it
        self.statusBar().showMessage(f"Reconnecting to {port}...", 3000)
        self._connect(port, auto=True)

    def _on_link(self, link: LinkState, message: str) -> None:
        self.connection.set_link(link, message)
        connected = link is LinkState.CONNECTED
        # Controls that need a live link go dead when there isn't one, rather
        # than looking operable and silently doing nothing.
        for widget in (self.handset_panel, self.quick_keys):
            widget.setEnabled(connected)
        self.deck_selector.setEnabled(connected)
        self.console_panel.set_link_enabled(connected)
        if not connected:
            self.status_panel.set_offline(message if link is LinkState.ERROR
                                          else "Not connected")
        # Deliberately not a modal dialog: losing the link mid-capture should
        # not steal focus or block the window. The bar, the LED and the status
        # line all carry it already.
        self.statusBar().showMessage(message, 0 if connected else 15000)

    # -- device state ------------------------------------------------------

    def _on_deck(self, deck: Deck) -> None:
        self.controller.set_deck(deck)
        self.controller.refresh_all()
        self.quick_keys.set_deck(deck)
        self.handset_panel.set_deck(deck)

    def _on_handset_deck(self, deck: Deck) -> None:
        """The handset's VCR/DVD key switched the unit; follow it, so the
        app's command target and the deck the keys are greyed for agree
        with what the key just did."""
        self.deck_selector.set_deck(deck)
        self._on_deck(deck)

    def _on_state(self, state) -> None:
        self.status_panel.on_state(state)
        self.deck_selector.set_deck(state.deck)

    def _on_response(self, opcode: int, name: str) -> None:
        if opcode in (P.Resp.ERROR, P.Resp.NAK, P.Resp.NOT_TARGET):
            self.statusBar().showMessage(f"Unit replied: {name}", 6000)

    def _probe(self) -> None:
        if not self.controller.connected:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the deck first.")
            return
        self.controller.probe_protocol()
        QTimer.singleShot(1200, self._probe_result)

    def _configure_quick_keys(self) -> None:
        from .quickkeys import QuickKeysDialog

        bar = self.quick_keys
        dialog = QuickKeysDialog(bar.codes, self)
        if dialog.exec() == QDialog.Accepted:
            from .quickkeys import format_codes

            codes = dialog.selected_codes()
            bar.set_codes(codes)
            bar.set_deck(self.deck_selector.deck)
            self.settings.setValue("quick_keys", format_codes(codes))

    def set_theme(self, name: str) -> None:
        """Switch palettes live and remember the choice."""
        palette = theme.PALETTES.get(name, theme.DARK)
        theme.set_active(palette)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_stylesheet(palette))
        action = self.theme_actions.get(palette.name)
        if action is not None and not action.isChecked():
            action.setChecked(True)
        self.settings.setValue("theme", palette.name)
        # Widgets that paint with colours the stylesheet cannot reach --
        # drawn icons and table text -- have to be redrawn by hand.
        self.console_panel.refresh_theme()

    def _diagnose(self) -> None:
        from .diagnostics import DiagnosticsDialog

        DiagnosticsDialog(self.controller, self.bridge, self).exec()

    def _probe_result(self) -> None:
        if self.controller.sense_replies_ok:
            text = (
                "Status Sense replied with a valid 5-byte status, so the "
                "link and reply framing are both working correctly."
            )
        else:
            text = (
                "No confirmed reply to Status Sense yet.\n\n"
                "Check the cable, that the deck has been on for at least ten "
                "seconds, and that Mode Lock is off. Open Tools → Console and check the "
                "traffic log for what's actually coming back."
            )
        QMessageBox.information(self, "Protocol probe", text)

    def _sync_clock(self) -> None:
        if not self.controller.connected:
            QMessageBox.information(self, "Not connected",
                                    "Connect to the deck first.")
            return
        date_cmd, clock_cmd = P.sync_clock()
        self.controller.send(date_cmd)
        self.controller.send(clock_cmd)
        self.statusBar().showMessage("Deck clock and date set from this PC", 5000)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.controller, self)
        if dialog.exec() == QDialog.Accepted:
            dialog.apply()
            self.settings.setValue("gap_ms", dialog.gap.value())
            self.settings.setValue("poll_ms", dialog.poll.value())
            self.settings.setValue("byte_gap_ms", dialog.byte_gap.value())

    def _show_shortcuts(self) -> None:
        from .panels import SHORTCUT_HINTS

        rows = "".join(
            f"<tr><td><b>{key}</b></td><td>&nbsp;&nbsp;</td>"
            f"<td>{P.SIMPLE_BY_KEY[command].label}</td></tr>"
            for command, key in SHORTCUT_HINTS.items()
        )
        QMessageBox.information(
            self, "Keyboard shortcuts",
            "<p>These work whenever the window has focus:</p>"
            f"<table>{rows}</table>"
            "<p>They apply to whichever deck is currently selected.</p>"
            "<p>After clicking the handset, the arrow keys, Enter, Backspace "
            "(Return) and 0&ndash;9 press its keys.</p>",
        )

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>Serial control for the JVC SR-MV55U DVD/VCR deck.</p>"
            "<p>Protocol implemented from the SR-MV45U/SR-MV55U user manual, "
            "pages 73&ndash;85.</p>"
            "<p>MIT licensed. Not affiliated with JVC.</p>",
        )

    # -- persistence -------------------------------------------------------

    def _maybe_show_getting_started(self) -> None:
        """Open the guide on first run only.

        Shared with people who have never seen the app, so the first launch
        should say what to do rather than presenting an inert control panel.
        """
        if self.settings.value("seen_getting_started"):
            return
        self.settings.setValue("seen_getting_started", True)
        GettingStartedDialog(self).exec()

    def _restore_settings(self) -> None:
        from .quickkeys import parse_codes

        self.set_theme(str(self.settings.value("theme", "dark")))
        self.quick_keys.set_codes(
            parse_codes(self.settings.value("quick_keys"))
        )
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        port = self.settings.value("port")
        if port:
            self.connection.select_port(str(port))
        gap = self.settings.value("gap_ms")
        if gap:
            self.controller.command_gap = int(gap) / 1000
        byte_gap = self.settings.value("byte_gap_ms")
        if byte_gap is not None:
            self.controller.inter_byte_gap = int(byte_gap) / 1000
        poll = self.settings.value("poll_ms")
        if poll:
            self.controller.poll_interval = int(poll) / 1000

    def closeEvent(self, event) -> None:
        if self.macro_panel.runner.running:
            answer = QMessageBox.question(
                self, "Macro still running",
                "A macro is part-way through. Quitting now leaves the deck "
                "wherever that macro got to.\n\nQuit anyway?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.settings.setValue("geometry", self.saveGeometry())
        self.macro_panel.runner.cancel()
        self.controller.disconnect()
        super().closeEvent(event)


def _icon() -> "QIcon | None":
    """The app icon, whether running from source or from a bundled exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    for name in ("icon.ico", "icon.png"):
        candidate = base / name
        if candidate.exists():
            return QIcon(str(candidate))
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    icon = _icon()
    if icon is not None:
        app.setWindowIcon(icon)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
