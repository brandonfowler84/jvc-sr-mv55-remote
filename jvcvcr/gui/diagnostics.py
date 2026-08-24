"""A scripted probe for working out why a command does nothing.

Built for one specific mystery: the deck accepts Remote Data (0x9F) commands
without acknowledging them and without acting on them, while direct opcodes
like Eject (0xA3) work normally.  Rather than guess, this sends a series of
probes with known-good controls either side, captures every byte that comes
back with timings, and reports what each result rules in or out.

Two details matter for the results to mean anything:

* Replies are assigned to probes by timestamp over the whole run, and
  anything that lands between probes is reported separately rather than
  silently credited to a neighbour.  An earlier version cleared its capture
  buffer per probe, which quietly mis-attributed late replies and produced
  self-contradictory results.
* Probe key codes are chosen to be undefined as *commands* (see the table on
  manual p. 73).  0x38, for instance, is both the DISPLAY key code and the
  standalone "DVD Selection" command, so an ACK for it proves nothing -- and
  sending it silently switches the deck.

Everything here is harmless: no recording, no disc operations, nothing that
touches media.  The most invasive probe asks the deck to start playing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
)

from .. import protocol as P
from ..device import Direction, LogEntry
from . import theme

#: Counter Reset. Chosen as the main probe key code because 0x39 is *not* a
#: command opcode, so any reply is unambiguous, and because its effect is
#: objectively checkable -- the counter should go to zero.
KEY_COUNTER_RESET = 0x39
#: PLAY. Also undefined as a command opcode, and plainly visible on the deck.
KEY_PLAY = 0x0C
#: Undefined as both a command and a remote key: a clean flush byte.
FILLER = 0x77


@dataclass
class Probe:
    """One step: send some frames, wait, then judge what came back."""

    title: str
    #: Each entry is transmitted as a separate write, spaced by the
    #: controller's inter-command gap. One entry = one frame.
    frames: list[bytes]
    question: str
    #: What this probe is for, so the verdict doesn't depend on list order:
    #: "control", "nak", "remote" or "flush".
    kind: str = "remote"
    #: Seconds to listen after the last frame goes out.
    listen: float = 1.2
    #: False sends each frame as one unpaced write, bytes back-to-back.
    pace: bool = True
    t0: float = 0.0
    t1: float = 0.0
    replies: list[LogEntry] = field(default_factory=list)

    @property
    def sent_hex(self) -> str:
        return "  ".join(" ".join(f"{b:02X}" for b in f) for f in self.frames)


def _state_check(note: str) -> Probe:
    """A probe that asks whether the deck is idle or mid-command.

    Status Sense is the ideal question because its two possible outcomes are
    unmistakable.  If the deck is idle it answers with five status bytes.  If
    it is part-way through a command and waiting for another byte, it eats
    the 0xD7 as that byte and says nothing.  No other command distinguishes
    those two states -- a plain ACK looks identical either way, which is what
    made the earlier runs of this tool impossible to interpret.
    """
    return Probe(
        kind="state",
        title=f"State check: {note}",
        frames=[],
        question=(
            "A status reply means the deck is idle. Silence means it is still "
            "waiting for a byte, and swallowed this query as that byte."
        ),
        listen=1.3,
    )


def build_probes() -> list[Probe]:
    return [
        Probe(
            kind="control",
            title="Control: a normal one-byte command",
            frames=[bytes([P.Cmd.STILL])],
            question=(
                "Expect an ACK (0A). If this alone fails, the link is not "
                "working and nothing below means anything."
            ),
        ),
        Probe(
            kind="nak",
            title="Control: an undefined command",
            frames=[bytes([FILLER])],
            question=(
                "0x77 is not a real command. A NAK (0B) proves this deck "
                "rejects what it doesn't recognise, which makes silence "
                "elsewhere meaningful."
            ),
        ),
        Probe(
            kind="unpaced",
            title="Two-byte command, bytes back-to-back (no gap)",
            frames=[P.select_deck(P.Deck.VCR)],
            pace=False,
            question=(
                "Command Target (F0 30) must work -- it is how the deck is "
                "told which deck to target. Sent as one write, with no gap "
                "between the two bytes."
            ),
        ),
        _state_check("after the unpaced two-byte command"),
        Probe(
            kind="paced",
            title="The same two-byte command, bytes spaced apart",
            frames=[P.select_deck(P.Deck.VCR)],
            question=(
                "Identical bytes, but with a gap between them. If this is "
                "acknowledged and the one above was not, the deck simply "
                "cannot take multi-byte commands at full speed -- which would "
                "break every multi-byte command, not just Remote Data."
            ),
        ),
        _state_check("baseline, before touching Remote Data"),
        Probe(
            title="Remote Data, normal two-byte form",
            frames=[bytes([P.Cmd.REMOTE_DATA, KEY_COUNTER_RESET])],
            question=(
                "Counter Reset -- exactly what a Remote tab button sends. "
                "0x39 is not a command on its own, so a NAK here would mean "
                "the deck read it as one rather than as a key code."
            ),
        ),
        _state_check("after a two-byte Remote Data"),
        Probe(
            title="Remote Data, key code sent as a separate write",
            frames=[bytes([P.Cmd.REMOTE_DATA]), bytes([KEY_COUNTER_RESET])],
            question=(
                "The same two bytes with the inter-command gap between them."
            ),
        ),
        _state_check("after a split-write Remote Data"),
        Probe(
            title="Remote Data, PLAY (watch the deck itself)",
            frames=[bytes([P.Cmd.REMOTE_DATA, KEY_PLAY])],
            question=(
                "If the deck starts playing, Remote Data works and the other "
                "codes were doing something you could not see."
            ),
            listen=1.8,
        ),
        _state_check("after Remote Data PLAY"),
        Probe(
            kind="control",
            title="Control: a normal command, again",
            frames=[bytes([P.Cmd.STOP])],
            question="Confirms the deck is still responding normally.",
        ),
    ]


class DiagnosticsDialog(QDialog):
    """Runs the probe sequence and reports what each result implies."""

    def __init__(self, controller, bridge, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.bridge = bridge
        self.setWindowTitle("Diagnose Remote Data")
        self.resize(720, 640)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Sends a series of harmless probes and reports exactly what the "
            "deck says back, with timings.\n\nPut a tape in, leave the deck "
            "powered on and stopped, and select the VCR deck first. Watch the "
            "deck itself during the run as well as this report."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.report = QTextBrowser()
        self.report.setStyleSheet(f"font-family: {theme.MONO}; font-size: 12px;")
        layout.addWidget(self.report, 1)

        self.run_btn = QPushButton("Run diagnostics")
        self.run_btn.setProperty("role", "primary")
        self.run_btn.clicked.connect(self.start)
        layout.addWidget(self.run_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._probes: list[Probe] = []
        self._index = 0
        #: Every RX frame for the whole run, assigned to probes afterwards.
        self._all: list[LogEntry] = []
        self._running = False
        self._counter_before: str | None = None
        self._counter_after: str | None = None

    # -- capture -----------------------------------------------------------

    def _on_log(self, entry: LogEntry) -> None:
        # Empty-data entries are the reader's own "timed out" notices, which
        # are exactly what a swallowed state check looks like -- keep them.
        if self._running and entry.direction is Direction.RX:
            self._all.append(entry)

    # -- sequencing --------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if not self.controller.connected:
            self.report.setPlainText("Not connected. Connect to the deck first.")
            return

        self._probes = build_probes()
        self._index = 0
        self._all = []
        self._counter_before = self._counter_after = None
        self._running = True
        self.run_btn.setEnabled(False)
        self.report.clear()

        # Polling would bury the replies we care about, and its own queries
        # would interleave with the probes.
        self.controller.set_polling(False)
        self.bridge.logged.connect(self._on_log)

        self._append("Running. Polling paused for the duration.")
        QTimer.singleShot(400, self._next)

    def _next(self) -> None:
        if self._index >= len(self._probes):
            self._start_effect_check()
            return

        probe = self._probes[self._index]
        probe.t0 = time.time()
        if probe.kind == "state":
            # Routed through query() so the reader tracks it as an expected
            # sense reply and reports a timeout if none arrives.
            self.controller.query(P.Cmd.STATUS_SENSE)
        for frame in probe.frames:
            self.controller.send(frame, pace=probe.pace)

        # Allow for the frames actually leaving (one per inter-command gap)
        # before the listening window closes.
        send_time = len(probe.frames) * max(self.controller.command_gap, 0.06)
        QTimer.singleShot(int((send_time + probe.listen) * 1000), self._collect)

    def _collect(self) -> None:
        self._probes[self._index].t1 = time.time()
        self._index += 1
        QTimer.singleShot(350, self._next)

    # -- objective effect check -------------------------------------------

    def _start_effect_check(self) -> None:
        """Ask the deck what its counter is, reset it, then ask again.

        Independent of ACKs entirely: if Counter Reset works, the value
        changes. This is the closest thing to ground truth available.
        """
        self._append("\n--- Effect check: does Counter Reset actually reset?")
        self.controller.query(P.Cmd.CTL_SENSE)
        QTimer.singleShot(700, self._effect_ensure_nonzero)

    def _effect_ensure_nonzero(self) -> None:
        """A counter already at zero can't show a reset, so advance it first."""
        counter = self.controller.state.counter
        if counter is None:
            self._finish()  # no tape; the verdict explains
            return
        if counter != "00:00:00":
            self._effect_capture_before()
            return
        self._append("    counter is already 00:00:00 -- playing briefly so a "
                     "reset would be measurable")
        self.controller.send_command("play")
        QTimer.singleShot(2600, self._effect_after_play)

    def _effect_after_play(self) -> None:
        self.controller.send_command("stop")
        QTimer.singleShot(700, lambda: (self.controller.query(P.Cmd.CTL_SENSE),
                                        QTimer.singleShot(700,
                                                          self._effect_capture_before)))

    def _effect_capture_before(self) -> None:
        self._counter_before = self.controller.state.counter
        self.controller.send(bytes([P.Cmd.REMOTE_DATA, KEY_COUNTER_RESET]))
        QTimer.singleShot(900, self._effect_step3)

    def _effect_step3(self) -> None:
        self.controller.query(P.Cmd.CTL_SENSE)
        QTimer.singleShot(700, self._effect_done)

    def _effect_done(self) -> None:
        self._counter_after = self.controller.state.counter
        self._finish()

    # -- reporting ---------------------------------------------------------

    def _finish(self) -> None:
        self._running = False
        try:
            self.bridge.logged.disconnect(self._on_log)
        except (RuntimeError, TypeError):
            pass
        self.controller.set_polling(True)
        self.run_btn.setEnabled(True)

        # Assign replies by timestamp only now that every window is closed,
        # so a late reply is attributed to the probe it actually belongs to
        # -- or reported as arriving between probes, never silently moved.
        for probe in self._probes:
            probe.replies = [e for e in self._all if probe.t0 <= e.timestamp < probe.t1]
        for probe in self._probes:
            self._report_probe(probe)

        claimed = {id(e) for p in self._probes for e in p.replies}
        strays = [e for e in self._all
                  if id(e) not in claimed and e.timestamp < self._probes[-1].t1]
        if strays:
            self._append("\n--- Arrived between probes (not attributable)")
            for entry in strays:
                self._append(f"    {entry.hex:<26} {entry.label}")

        self._append("\n" + "=" * 64)
        self._append(self._verdict())

    def _append(self, text: str) -> None:
        self.report.append(text)
        bar = self.report.verticalScrollBar()
        bar.setValue(bar.maximum())

    @staticmethod
    def _deck_was_idle(probe: Probe) -> bool:
        """True if a state check got a real status reply back."""
        return any(e.sense_opcode == P.Cmd.STATUS_SENSE and e.data
                   for e in probe.replies)

    def _report_probe(self, probe: Probe) -> None:
        self._append(f"\n--- {probe.title}")
        self._append(f"    sent:  {probe.sent_hex or 'D7  (Status Sense)'}")
        if not probe.replies:
            self._append("    got:   (nothing)")
        for entry in probe.replies:
            offset = int((entry.timestamp - probe.t0) * 1000)
            shown = entry.hex or "--"
            self._append(f"    got:   +{offset:>4}ms  {shown:<20} {entry.label}")
        if probe.kind == "state":
            verdict = ("DECK IS IDLE" if self._deck_was_idle(probe)
                       else "DECK IS WAITING FOR A BYTE")
            self._append(f"    ==>    {verdict}")
        self._append(f"    note:  {probe.question}")

    def _of_kind(self, kind: str) -> list[Probe]:
        return [p for p in self._probes if p.kind == kind]

    @staticmethod
    def _has(probe: Probe, opcode: int) -> bool:
        return any(e.data and e.data[0] == opcode for e in probe.replies)

    def _verdict(self) -> str:
        controls = self._of_kind("control")
        control_ok = any(self._has(p, P.Resp.ACK) for p in controls)
        naks_garbage = any(self._has(p, P.Resp.NAK) for p in self._of_kind("nak"))
        remote = self._of_kind("remote")
        remote_acks = [p for p in remote if self._has(p, P.Resp.ACK)]
        remote_naks = [p for p in remote if self._has(p, P.Resp.NAK)]

        lines = ["VERDICT", ""]

        if not control_ok:
            return "\n".join(lines + [
                "The deck did not answer even a plain Still command, so the "
                "link itself is the problem. Nothing else here is reliable."
            ])

        lines.append("The link is fine: normal commands are acknowledged.")

        # Reported before anything else: byte pacing is the most fundamental
        # result here, and it stays true whether or not the later checks pass.
        unpaced_ok = any(self._has(p, P.Resp.ACK)
                         for p in self._of_kind("unpaced"))
        paced_ok = any(self._has(p, P.Resp.ACK) for p in self._of_kind("paced"))
        if paced_ok and not unpaced_ok:
            lines.append(
                "\nBYTE PACING IS REQUIRED, AND CONFIRMED.\n"
                "The same two-byte command was ignored with its bytes sent "
                "back-to-back, and acknowledged with a gap between them. This "
                "deck cannot take a multi-byte command at full line rate.\n\n"
                "That affects every multi-byte command -- deck targeting, "
                "input and record-mode selection, searches, shuttle speeds "
                "and all of Remote Data. Single-byte commands (play, stop, "
                "rewind, eject) were never affected. The app spaces these "
                "bytes automatically; the gap is adjustable in Settings."
            )
        elif unpaced_ok:
            lines.append(
                "\nThis deck accepts multi-byte commands with no gap between "
                "their bytes, so byte pacing is not needed here (it does no "
                "harm). You can set the gap to 0 in Settings for more speed."
            )

        inconclusive = (
            self._counter_before in (None, "00:00:00")
            or self._counter_after is None
        )
        counter_worked = (
            not inconclusive
            and self._counter_before != self._counter_after
            and self._counter_after == "00:00:00"
        )
        lines.append(
            f"\nCounter before Remote Data reset: {self._counter_before or '--'}"
            f"\nCounter after:                    {self._counter_after or '--'}"
        )
        if inconclusive:
            lines.append(
                "  Inconclusive -- the counter could not be read, or was "
                "already at zero. Load a tape, select the VCR deck, and run "
                "this again."
            )
        elif counter_worked:
            lines.append(
                "\nThe counter reset. Remote Data DOES work on this deck -- the "
                "keys that appeared dead were simply ones whose effect is only "
                "visible on the video output."
            )
            return "\n".join(lines)

        if not inconclusive:
            lines.append("  The counter did not reset.")

        two_byte_ok = paced_ok or unpaced_ok
        states = self._of_kind("state")
        baseline_idle = self._deck_was_idle(states[0]) if states else False
        stuck_after = [s for s in states[1:] if not self._deck_was_idle(s)]

        if two_byte_ok:
            lines.append(
                "\nTwo-byte commands work: Command Target (F0 30) was "
                "acknowledged from a single write, so byte timing is fine and "
                "nothing below is a framing artefact."
            )

        if baseline_idle and stuck_after:
            lines.append(
                "\nTHIS IS THE KEY RESULT: the deck answered a status query "
                "before Remote Data, then stopped answering after one. It is "
                "sitting mid-command, waiting for a byte that -- by the "
                "protocol -- it should already have received.\n\n"
                "So 0x9F is not being ignored. The deck starts the command "
                "and then expects MORE than the one key-code byte the manual "
                "documents. Everything on the Remote tab therefore leaves the "
                "deck half-way through a command, which also explains why the "
                "very next command sent afterwards behaves strangely."
            )
        elif baseline_idle and not stuck_after:
            lines.append(
                "\nThe deck stayed idle throughout: every Remote Data command "
                "was consumed completely. It accepts them and declines to act."
            )
            if remote_naks:
                lines.append(
                    "A NAK to a Remote Data probe means the key code was read "
                    "as a command in its own right, so 0x9F is not being "
                    "treated as a two-byte command at all."
                )
            elif not remote_acks and naks_garbage:
                lines.append(
                    "The deck does NAK commands it doesn't know, so silence "
                    "in response to 0x9F means it recognises the command and "
                    "is deliberately ignoring it."
                )

        lines.append(
            "\nWORTH KNOWING -- the deck's remote code.\n"
            "Remote Data emulates the wired remote, and this deck ships set to "
            "remote code 3 of 4 (manual p. 55; hold PLAY on the unit for 5+ "
            "seconds while it is off to see the current code). If the code "
            "table on p. 77 is written for a different code set, the deck "
            "would ignore every key exactly like this. Changing the code needs "
            "the IR remote."
        )
        return "\n".join(lines)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._running:
            self._running = False
            try:
                self.bridge.logged.disconnect(self._on_log)
            except (RuntimeError, TypeError):
                pass
            self.controller.set_polling(True)
        super().closeEvent(event)
