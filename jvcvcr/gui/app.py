"""Main window and application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QIcon, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QSpinBox, QSplitter, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from .. import __version__
from .. import protocol as P
from ..device import DeviceController, LinkState, LogEntry
from ..macros import MacroLibrary
from ..protocol import Deck
from ..transport import SerialTransport, SimulatorTransport, list_serial_ports
from . import theme
from .panels import (
    ConsolePanel, DeckSelector, DvdPanel, ElidedLabel, FlowLayout, MacroPanel,
    RecordPanel, RemotePanel, StatusPanel, button, dim, heading, scrollable,
)

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

        layout.addWidget(dim("Port"))
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
<li>Open the <b>Console</b> tab and send <code>D7</code>. A healthy deck
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
input and record-mode selection, searches, shuttle speeds and the whole Remote
tab, while single-byte transport commands are unaffected.</p>
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
<p>The deck will not record until it receives a Rec Request. The Record tab's
<b>Arm</b> button sends it; Stop clears it again. This mirrors the deck rather
than hiding it, so what you see matches what the hardware is doing.</p>

<h2>Remote keys</h2>
<p>The <b>Remote</b> tab sends wired-remote key codes. Many of them only change
something on the <b>video output</b> &mdash; Audio, Display, On Screen,
Subtitle, Angle. Those will look like they do nothing if you are watching this
window rather than the TV or capture feed. Keys such as Tracking, TBC and
Counter Reset have visible effects on the deck or in the status panel.</p>
<p>Keys belonging to the other deck are hidden unless you tick <i>Include other
deck's keys</i>, and are labelled when shown.</p>

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
<p>Then use the transport buttons, or the keyboard: <b>Space</b> play,
<b>S</b> stop, <b>J</b>/<b>L</b> rewind and fast forward, <b>K</b> still,
<b>,</b> and <b>.</b> to step a frame. Full list under
<i>Help &rarr; Keyboard shortcuts</i>.</p>

<h3>5. Recording</h3>
<p>The deck refuses to record until it has been armed. In the <b>Record</b> tab
choose your input and record mode, press <b>Arm</b>, then <b>Record</b>. Stop
disarms it again. That is the deck's own behaviour, not something this app
imposes.</p>

<h3>6. Make it yours</h3>
<ul>
<li><b>View &rarr; Configure quick keys</b> &mdash; choose which remote keys sit
under the transport buttons. Tracking, TBC and counter reset are there by
default because nothing else in the app can reach them.</li>
<li><b>View &rarr; Dark / Light theme</b>.</li>
<li><b>View &rarr; Compact layout</b>, or simply make the window narrow &mdash;
it rearranges itself to sit in a strip beside a capture window.</li>
<li><b>Macros</b> tab &mdash; save a sequence of commands with delays, handy for
repeatable VHS&rarr;DVD runs.</li>
</ul>

<h3>If something misbehaves</h3>
<p>The <b>Console</b> tab shows every byte in and out with a plain-English
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
        self.resize(1180, 780)
        # Small enough to sit in a strip beside a capture window.
        self.setMinimumSize(340, 480)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._compact_forced = False
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

        left = QVBoxLayout()
        left.setSpacing(10)
        self.status_panel = StatusPanel()
        # Merged into the status card's header row, which saves a whole row
        # and keeps "which deck" next to "what is it doing".
        self.deck_selector = self.status_panel.deck_selector
        left.addWidget(self.status_panel)
        from .panels import TransportPanel

        self.transport_panel = TransportPanel(self.controller)
        left.addWidget(self.transport_panel)
        left.addStretch(1)

        left_widget = QWidget()
        left_widget.setLayout(left)
        # A width range rather than a fixed width, so the column can give way
        # when the window is docked narrow instead of forcing a scrollbar.
        left_widget.setMinimumWidth(300)
        left_widget.setMaximumWidth(460)
        self.left_pane = scrollable(left_widget)
        self.left_pane.setMinimumWidth(300)

        self.tabs = QTabWidget()
        # With many tabs in a narrow window, elide the labels and offer
        # scroll arrows rather than letting the bar overflow.
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setDocumentMode(True)
        self.record_panel = RecordPanel(self.controller)
        self.dvd_panel = DvdPanel(self.controller)
        self.remote_panel = RemotePanel(self.controller)
        self.macro_panel = MacroPanel(self.controller, self.library)
        self.console_panel = ConsolePanel(self.controller)
        # Each tab scrolls independently: a short window should let you reach
        # every control by scrolling, never squash one into illegibility.
        # The minimum heights are what the scroll areas scroll *to* -- below
        # them the content would start clipping its own controls instead.
        self.record_panel.setMinimumHeight(420)
        self.dvd_panel.setMinimumHeight(520)
        self.remote_panel.setMinimumHeight(320)
        self.macro_panel.setMinimumHeight(360)
        self.console_panel.setMinimumHeight(400)
        for panel, label in (
            (self.record_panel, "Record"),
            (self.dvd_panel, "DVD"),
            (self.remote_panel, "Remote"),
            (self.macro_panel, "Macros"),
            (self.console_panel, "Console"),
        ):
            self.tabs.addTab(scrollable(panel), label)
        self.tabs.setMinimumWidth(260)
        # Without an explicit minimum, the tallest page's own minimum (~300px)
        # becomes the tab area's floor, which in stacked mode would squeeze
        # the transport controls out of view. Every page scrolls internally,
        # so a small floor is safe.
        self.tabs.setMinimumHeight(150)

        self.body = QSplitter(Qt.Horizontal)
        self.body.addWidget(self.left_pane)
        self.body.addWidget(self.tabs)
        self.body.setStretchFactor(0, 0)
        self.body.setStretchFactor(1, 1)
        self.body.setChildrenCollapsible(False)
        outer.addWidget(self.body, 1)

        self.setCentralWidget(central)

        self.statusBar().showMessage("Not connected")
        self._build_menu()

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
        quick = QAction("Configure &quick keys...", self)
        quick.setStatusTip(
            "Choose which remote keys sit beside the transport controls"
        )
        quick.triggered.connect(self._configure_quick_keys)
        view_menu.addAction(quick)

        self.compact_action = QAction("&Compact layout", self)
        self.compact_action.setCheckable(True)
        self.compact_action.setStatusTip(
            "Use the dense layout at any width, not only when docked narrow"
        )
        self.compact_action.toggled.connect(self._set_compact_forced)
        view_menu.addAction(self.compact_action)

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
        for panel in (self.transport_panel, self.record_panel, self.dvd_panel,
                      self.remote_panel):
            panel.setEnabled(connected)
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
        self.transport_panel.set_deck(deck)
        self.transport_panel.quick_keys.set_deck(deck)
        self.dvd_panel.set_deck(deck)
        self.remote_panel.set_deck(deck)

    def _on_state(self, state) -> None:
        self.status_panel.on_state(state)
        self.record_panel.on_state(state)
        self.dvd_panel.on_state(state)
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

        bar = self.transport_panel.quick_keys
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
        for panel in (self.console_panel, self.transport_panel,
                      self.dvd_panel, self.record_panel):
            panel.refresh_theme()

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
                "seconds, and that Mode Lock is off. Check the Console tab's "
                "traffic log for what's actually coming back."
            )
        QMessageBox.information(self, "Protocol probe", text)

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
            "<p>They apply to whichever deck is currently selected.</p>",
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

    def _set_compact_forced(self, forced: bool) -> None:
        self._compact_forced = forced
        self.settings.setValue("compact", forced)
        self._apply_responsive_layout(force=True)

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
        self.transport_panel.quick_keys.set_codes(
            parse_codes(self.settings.value("quick_keys"))
        )
        if str(self.settings.value("compact", "false")).lower() in ("true", "1"):
            self.compact_action.setChecked(True)
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

    #: Below this width the side-by-side layout stops making sense and the
    #: two panes stack vertically instead.
    NARROW_WIDTH = 760

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self, force: bool = False) -> None:
        """Stack the panes vertically when the window is too narrow to sit
        them side by side -- the shape it takes when pinned beside a capture.

        `force` re-applies even when the orientation has not changed, which
        the Compact layout toggle needs.
        """
        narrow = self.width() < self.NARROW_WIDTH or self._compact_forced
        for panel in (self.status_panel, self.transport_panel):
            panel.set_compact(narrow)
        wanted = Qt.Vertical if narrow else Qt.Horizontal
        if self.body.orientation() == wanted and not force:
            return

        self.body.setOrientation(wanted)
        if narrow:
            # Stacked: the status/transport column no longer competes for
            # width, so let it use the full span.
            self.left_pane.setMaximumWidth(16777215)
            self.left_pane.widget().setMaximumWidth(16777215)
        else:
            self.left_pane.setMaximumWidth(460)
            self.left_pane.widget().setMaximumWidth(460)
        # Deferred: the splitter has not been re-laid-out for its new
        # orientation yet, and sizes set against the old geometry get
        # rescaled into something arbitrary.
        QTimer.singleShot(0, self._rebalance_panes)

    def _rebalance_panes(self) -> None:
        """Give each pane a sensible share after an orientation change."""
        if self.body.orientation() == Qt.Vertical:
            total = self.body.height()
            # Enough height for the transport buttons where possible -- they
            # are the point of the app, and having to scroll to reach Play
            # would be absurd -- but capped so the tabs keep a usable share.
            wanted = self.left_pane.widget().sizeHint().height() + 8
            # Leave the tabs a usable strip, but otherwise favour the deck
            # controls -- on a tall narrow window everything fits; on a short
            # one the transport buttons are what should stay reachable.
            top = min(wanted, max(int(total * 0.55), total - 170))
            self.body.setSizes([top, max(150, total - top)])
        else:
            total = self.body.width()
            self.body.setSizes([380, max(320, total - 380)])

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
