"""Threaded controller that owns the link to the deck.

The GUI never touches a serial port directly.  It creates a
:class:`DeviceController`, registers callbacks, and calls high-level methods.
All I/O happens on a worker thread; callbacks are invoked from that thread, so
GUI code must marshal them onto the UI thread (the Qt layer does this with
signals).
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable

from . import protocol as P
from .protocol import Cmd, Deck, Resp
from .transport import Transport, TransportError


class LinkState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class Direction(Enum):
    TX = "TX"
    RX = "RX"


@dataclass(frozen=True)
class LogEntry:
    timestamp: float
    direction: Direction
    data: bytes
    label: str
    #: The sense command this frame belongs to, for a query or its reply;
    #: None for anything else. Carried explicitly because a sense *reply*
    #: does not begin with its opcode (see protocol.SENSE_PAYLOAD_LEN), so
    #: there is no way to recognise one from its bytes alone.
    sense_opcode: int | None = None

    @property
    def hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)


@dataclass
class DeviceState:
    """Everything we currently believe about the unit."""

    deck: Deck = Deck.VCR
    status: P.Status | None = None
    jvc: P.JvcStatus | None = None
    select: P.SelectStatus | None = None
    counter: str | None = None
    remaining: str | None = None
    chapter: int | None = None
    title: int | None = None
    title_is_playlist: bool = False
    date: tuple[int, int, int] | None = None
    clock: tuple[int, int, int] | None = None
    armed: bool = False
    last_response: str = ""
    error_latched: bool = False


#: Sense commands the poller cycles through, and how often each is worth asking
#: for.  Status is the one that matters moment to moment; the rest change
#: slowly and would only waste bandwidth on a 9600 baud link.
_POLL_PLAN: tuple[tuple[int, int], ...] = (
    (Cmd.STATUS_SENSE, 1),
    (Cmd.CTL_SENSE, 2),
    (Cmd.JVC_SENSE, 4),
    (Cmd.TC_SENSE, 8),
    (Cmd.SELECT_SENSE, 16),
)


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    data: bytes = field(compare=False)
    is_poll: bool = field(default=False, compare=False)
    #: False sends the frame as one write, bytes back-to-back. Only the
    #: diagnostics use that, to demonstrate what the deck does with it.
    pace: bool = field(default=True, compare=False)


class DeviceController:
    """Owns the transport, the worker thread, and the decoded device state."""

    PRIORITY_USER = 0
    PRIORITY_POLL = 10

    def __init__(
        self,
        *,
        command_gap: float = P.MIN_COMMAND_GAP,
        poll_interval: float = 0.5,
        sense_timeout: float = 0.75,
        inter_byte_gap: float = 0.06,
    ) -> None:
        self.command_gap = command_gap
        self.poll_interval = poll_interval
        #: Gap inserted between the bytes of a multi-byte command.
        #:
        #: A real SR-MV55U does not accept a multi-byte command whose bytes
        #: arrive back-to-back: "F0 30" sent as a single write draws no reply
        #: at all, while the same two bytes written separately are both
        #: acknowledged. Because single-byte commands were unaffected, this
        #: presented as "some buttons do nothing" when in fact *every*
        #: multi-byte command was being dropped -- deck targeting, input and
        #: record-mode selection, searches, shuttle and all of Remote Data.
        #: Set to 0 to send frames unpaced.
        self.inter_byte_gap = inter_byte_gap
        #: How long to wait for a sense reply before giving up on it,
        #: re-learning the echo/no-echo framing guess, and moving on. A
        #: healthy reply at 9600 baud comes back in single-digit
        #: milliseconds, so this is a dropped-reply/stuck-framing recovery
        #: net, not a normal-path timing budget -- kept well under
        #: poll_interval so recovery doesn't itself feel like a stall.
        self.sense_timeout = sense_timeout

        self.state = DeviceState()
        self.link = LinkState.DISCONNECTED

        # Callbacks -- all invoked from the worker thread.
        self.on_link_change: Callable[[LinkState, str], None] | None = None
        self.on_state_change: Callable[[DeviceState], None] | None = None
        self.on_log: Callable[[LogEntry], None] | None = None
        self.on_response: Callable[[int, str], None] | None = None

        #: Diagnostic for the Probe Protocol action: True once any sense
        #: reply has been received and structurally validated, confirming
        #: the link and framing are working end to end.
        self._sense_replies_ok = False

        self._transport: Transport | None = None
        self._queue: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rx = bytearray()
        #: Sense opcodes we've transmitted and are still waiting to hear back
        #: about, oldest first.  Only ever one entry on real hardware -- see
        #: _enqueue_poll -- but kept as a list so a manually-typed Console
        #: query can queue up behind an in-flight poll without being lost.
        self._pending_sense: list[int] = []
        self._pending_since: float = 0.0
        #: Sense opcodes that are due to be sent once the link is free.
        #: Kept separate from _pending_sense so "what's due" and "what's
        #: currently awaiting a reply" can't be conflated.
        self._poll_queue: list[int] = []
        self._polling = True
        self._poll_tick = 0

    # -- connection --------------------------------------------------------

    def connect(self, transport: Transport) -> None:
        self.disconnect()
        self._transport = transport
        self._stop.clear()
        self._set_link(LinkState.CONNECTING, f"Opening {transport.description}")
        try:
            transport.open()
        except TransportError as exc:
            self._transport = None
            self._set_link(LinkState.ERROR, str(exc))
            return
        self._rx.clear()
        self._pending_sense.clear()
        self._poll_queue.clear()
        self._drain_queue()
        self._thread = threading.Thread(
            target=self._worker, name="jvc-device", daemon=True
        )
        self._thread.start()
        self._set_link(LinkState.CONNECTED, f"Connected: {transport.description}")
        # Re-assert deck targeting so our idea of the target matches the unit's.
        self.set_deck(self.state.deck)
        self.refresh_all()

    def disconnect(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        self._drain_queue()
        if self.link is not LinkState.ERROR:
            self._set_link(LinkState.DISCONNECTED, "Disconnected")

    @property
    def connected(self) -> bool:
        return self.link is LinkState.CONNECTED

    def set_polling(self, enabled: bool) -> None:
        self._polling = enabled

    # -- sending -----------------------------------------------------------

    def send(self, data: bytes, *, priority: int | None = None,
             is_poll: bool = False, pace: bool = True) -> None:
        """Queue a raw frame.  Never blocks on the port."""
        if not data:
            return
        if not self.connected:
            return
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        pri = self.PRIORITY_POLL if is_poll else self.PRIORITY_USER
        if priority is not None:
            pri = priority
        self._queue.put(_Job(pri, seq, bytes(data), is_poll, pace))

    def send_command(self, key: str) -> None:
        """Send one of the named simple commands from the protocol catalogue."""
        cmd = P.SIMPLE_BY_KEY.get(key)
        if cmd is None:
            raise KeyError(f"unknown command {key!r}")
        self.send(cmd.payload)
        if cmd.payload[0] == Cmd.REC_REQUEST:
            self._update(armed=True)
        elif cmd.payload[0] == Cmd.STOP:
            self._update(armed=False)
        elif cmd.payload[0] == Cmd.CLEAR:
            self._update(error_latched=False)

    def set_deck(self, deck: Deck) -> None:
        self.send(P.select_deck(deck))
        # Values below describe the previously targeted deck, so drop them
        # rather than showing the wrong deck's data until the next poll.
        self._update(
            deck=deck, armed=False, status=None, jvc=None, select=None,
            counter=None, remaining=None, chapter=None, title=None,
        )

    def send_remote(self, code: int) -> None:
        self.send(P.remote(code))

    def query(self, opcode: int) -> None:
        """Send a sense command."""
        self.send(bytes((opcode,)))

    @property
    def sense_replies_ok(self) -> bool:
        """True once a sense reply has arrived and structurally validated."""
        return self._sense_replies_ok

    def probe_protocol(self) -> None:
        """Send a Status Sense and see whether a valid reply comes back."""
        self._sense_replies_ok = False
        self.query(Cmd.STATUS_SENSE)

    def refresh_all(self) -> None:
        """Queue every sense query once, on connect or when the deck changes.

        The regular poller deliberately asks for slow-moving values only
        occasionally, which would otherwise leave fields blank for seconds
        after connecting.  These go through the same _poll_queue as regular
        polling (rather than being sent directly) so they're still drained
        one at a time -- see _enqueue_poll for why that matters.
        """
        opcodes = [
            Cmd.STATUS_SENSE, Cmd.SELECT_SENSE, Cmd.CTL_SENSE, Cmd.TC_SENSE,
            Cmd.JVC_SENSE, Cmd.DATE_SENSE, Cmd.CLOCK_SENSE,
        ]
        if self.state.deck is Deck.DVD:
            opcodes += [Cmd.CHAPTER_SENSE, Cmd.TITLE_SENSE]
        for opcode in opcodes:
            if opcode not in self._poll_queue:
                self._poll_queue.append(opcode)

    # -- worker ------------------------------------------------------------

    def _worker(self) -> None:
        last_send = 0.0
        last_poll = 0.0
        while not self._stop.is_set():
            transport = self._transport
            if transport is None:
                break

            try:
                # 1. read anything waiting
                try:
                    chunk = transport.read(0.02)
                except TransportError as exc:
                    self._set_link(LinkState.ERROR, f"Link lost: {exc}")
                    break
                if chunk:
                    self._feed(chunk)

                now = time.monotonic()

                # 2. decide what's due to be polled, and try to keep the
                #    single in-flight sense slot full so queued items (e.g.
                #    from refresh_all) drain as fast as replies arrive
                #    rather than waiting on the next poll_interval tick.
                if self._polling and now - last_poll >= self.poll_interval:
                    last_poll = now
                    self._refill_poll_queue()
                self._drain_poll_queue()

                # 3. send at most one frame, honouring the inter-command gap.
                #    Note there is deliberately no `continue` on an empty
                #    queue: an idle queue is the *normal* state while a sense
                #    reply is outstanding, and skipping the rest of the loop
                #    would skip step 4 -- which is exactly what previously
                #    stopped stale-reply timeouts from ever firing while
                #    idle, so status updates only resumed when the user
                #    pressed a button and put a job in the queue.
                job = None
                if now - last_send >= self.command_gap:
                    try:
                        job = self._queue.get_nowait()
                    except queue.Empty:
                        job = None
                if job is not None:
                    try:
                        self._write_frame(transport, job.data, job.pace)
                    except TransportError as exc:
                        self._set_link(LinkState.ERROR, f"Send failed: {exc}")
                        break
                    last_send = time.monotonic()
                    op = job.data[0]
                    # Poll-sourced sense queries already reserved their slot
                    # in _pending_sense back in _drain_poll_queue, at the
                    # moment they were chosen -- not here, at the moment
                    # they're actually sent, which can lag behind by up to
                    # command_gap. Only a directly-issued query (Console tab,
                    # probe_protocol) needs booking at this point.
                    if op in P.SENSE_PAYLOAD_LEN and not job.is_poll:
                        self._pending_sense.append(op)
                        if len(self._pending_sense) == 1:
                            self._pending_since = last_send
                    self._log(
                        Direction.TX, job.data,
                        sense_opcode=op if op in P.SENSE_PAYLOAD_LEN else None,
                    )

                # 4. give up on a sense reply that never showed up.
                self._expire_stale_pending(now)
            except Exception as exc:
                # Anything unexpected here (a malformed reply tripping a
                # decoder in a way _handle_sense's `except ValueError` didn't
                # anticipate, for instance) must not silently kill this
                # thread -- that previously left the app looking frozen with
                # no error shown at all, since nothing outside the thread
                # could tell it had died. Surface it and stop cleanly instead.
                import traceback

                self._set_link(
                    LinkState.ERROR,
                    f"Internal error in device worker: {exc!r}",
                )
                self._log(
                    Direction.RX, b"",
                    label=f"WORKER CRASHED: {exc!r}\n{traceback.format_exc()}",
                )
                break

    def _expire_stale_pending(self, now: float) -> None:
        """Drop a pending sense query that's waited past sense_timeout.

        Without this, a single dropped reply (real hardware can occasionally
        miss one, e.g. mid-warm-up or under Mode Lock) would leave
        _pending_sense permanently non-empty, which -- since _try_frame only
        ever matches against pending_sense[0] and _drain_poll_queue refuses
        to send while anything is pending -- would silently wedge the reader
        forever.  A factored-out method so tests can exercise it directly
        rather than waiting out a real multi-second timeout.
        """
        if self._pending_sense and now - self._pending_since > self.sense_timeout:
            missed = self._pending_sense.pop(0)
            self._pending_since = now
            self._log(
                Direction.RX, b"",
                label=f"No reply to {P.SENSE_NAMES.get(missed, hex(missed))} "
                      f"(timed out)",
            )
            # Clear the buffer, but surface anything recognisable in it first.
            # A response byte can end up stranded behind a reply that never
            # completed -- an ACK for an unrelated command, say -- and simply
            # discarding the buffer would silently destroy it, making that
            # command look like it was ignored by the deck. Draining byte by
            # byte both reports it and leaves the buffer clean, so the next
            # reply (which has no opcode to anchor on) still starts aligned.
            while self._rx:
                op = self._rx[0]
                frame = bytes(self._rx[:1])
                del self._rx[:1]
                if op in P.RESP_NAMES:
                    self._log(Direction.RX, frame)
                    self._handle_response(op)
                else:
                    self._log(Direction.RX, frame,
                              label=f"Unexpected 0x{op:02X}")

    def _write_frame(self, transport: Transport, data: bytes,
                     pace: bool = True) -> None:
        """Write a frame, spacing its bytes unless told otherwise.

        See the note on `inter_byte_gap`: this deck drops a multi-byte
        command whose bytes arrive with no gap between them, so each byte
        goes out as its own write. The brief sleeps happen on the worker
        thread and only delay reading by a few tens of milliseconds; the
        port buffers anything that arrives meanwhile.
        """
        if not pace or len(data) < 2 or self.inter_byte_gap <= 0:
            transport.write(data)
            return
        for index, value in enumerate(data):
            if index:
                time.sleep(self.inter_byte_gap)
            transport.write(bytes([value]))

    def _refill_poll_queue(self) -> None:
        """Add whichever sense opcodes are due to _poll_queue.

        Does not send anything -- see _drain_poll_queue for that.  Kept
        separate so refilling happens on the slow poll_interval cadence
        while draining can happen as fast as replies arrive.
        """
        self._poll_tick += 1
        for opcode, divisor in _POLL_PLAN:
            if self._poll_tick % divisor == 0 and opcode not in self._poll_queue:
                self._poll_queue.append(opcode)
        if self.state.deck is Deck.DVD and self._poll_tick % 4 == 0:
            for opcode in (Cmd.CHAPTER_SENSE, Cmd.TITLE_SENSE):
                if opcode not in self._poll_queue:
                    self._poll_queue.append(opcode)

    def _drain_poll_queue(self) -> None:
        """Send the next queued sense opcode, if the link is free for one.

        This protocol has no frame delimiters or checksum -- the reader has
        to infer where one reply ends and the next begins, purely from which
        opcode it's currently expecting.  That inference only holds when at
        most one sense reply is outstanding at a time: firing several
        different sense queries close together lets a coincidental byte
        value inside one payload get mistaken for the opcode of a *different*
        pending reply, permanently desyncing the reader (this happened in
        practice against real hardware).  So however many opcodes are due,
        only ever let one be in flight.
        """
        if self._pending_sense or not self._poll_queue:
            return
        opcode = self._poll_queue.pop(0)
        # Reserve the pending slot right now, synchronously -- not when the
        # job is actually transmitted in step 3, which is throttled by
        # command_gap (default 60ms) and can lag well behind how often this
        # method itself gets called (every loop iteration, ~20-50ms). Without
        # this, a second call here could pop and send *another* opcode
        # during that gap, before the first one was ever marked pending --
        # silently recreating the exact multi-in-flight desync this
        # single-flight design exists to prevent.
        self._pending_sense.append(opcode)
        self._pending_since = time.monotonic()
        self.send(bytes((opcode,)), is_poll=True)

    # -- receive framing ---------------------------------------------------

    def _feed(self, chunk: bytes) -> None:
        self._rx.extend(chunk)
        while self._rx:
            consumed = self._try_frame()
            if consumed == 0:
                return

    def _try_frame(self) -> int:
        """Pull one complete frame off the RX buffer.  Returns bytes consumed.

        No sense reply carries its opcode or a checksum (see
        protocol.SENSE_PAYLOAD_LEN), so alignment can't be confirmed from the
        bytes alone.  Three things make that safe:

        * only one sense query is ever in flight (see _drain_poll_queue), so
          the expected reply -- and therefore its length -- is unambiguous;
        * a candidate window is only accepted if it structurally validates as
          a reply of that type (see protocol.sense_payload_is_plausible);
        * anything else falls through to the single-byte response check, and
          failing that is dropped one byte at a time so the window slides
          rather than swallowing possibly-real data.

        Trying the expected sense reply *before* the response check is
        deliberate and safe: no sense reply's first byte can collide with a
        response opcode.  Status and JVC Status both have bit6-or-bit7 set in
        their first byte, and the digit-format replies start with 0x2D-0x3F,
        while every response byte is 0x01-0x0B.  So an ACK arriving mid-reply
        fails validation, gets consumed as a response, and the real reply
        validates on the following pass.
        """
        if self._pending_sense:
            expected = self._pending_sense[0]
            payload_len = P.SENSE_PAYLOAD_LEN[expected]
            if len(self._rx) >= payload_len:
                candidate = bytes(self._rx[:payload_len])
                if P.sense_payload_is_plausible(expected, candidate):
                    del self._rx[:payload_len]
                    # describe() labels a frame by reading its first byte as
                    # an opcode, which is meaningless for a reply that has
                    # none -- and mislabelling these as "Unknown 0xNN" is
                    # what made them so hard to tell from noise while
                    # debugging against real hardware. Label explicitly.
                    name = P.SENSE_NAMES.get(expected, f"0x{expected:02X}")
                    tail = " ".join(f"{b:02X}" for b in candidate)
                    self._log(Direction.RX, candidate, label=f"{name} [{tail}]",
                              sense_opcode=expected)
                    self._pending_sense.pop(0)
                    self._sense_replies_ok = True
                    self._handle_sense(expected, candidate)
                    return payload_len

        op = self._rx[0]

        # Single-byte system responses can arrive unsolicited at any time
        # (e.g. Complete/Error/Cassette Out from an earlier operation).
        if op in P.RESP_NAMES:
            frame = bytes(self._rx[:1])
            del self._rx[:1]
            self._log(Direction.RX, frame)
            self._handle_response(op)
            return 1

        if self._pending_sense:
            # Still short of a full reply, and this isn't a response byte --
            # wait for more rather than nibbling away at what may turn out
            # to be a genuine reply. sense_timeout recovers if it never
            # completes.
            if len(self._rx) < P.SENSE_PAYLOAD_LEN[self._pending_sense[0]]:
                return 0

        frame = bytes(self._rx[:1])
        del self._rx[:1]
        self._log(Direction.RX, frame, label=f"Unexpected 0x{frame[0]:02X}")
        return 1

    def _handle_response(self, op: int) -> None:
        name = P.RESP_NAMES.get(op, f"0x{op:02X}")
        if op == Resp.ERROR:
            self._update(last_response=name, error_latched=True)
        elif op == Resp.CASSETTE_OUT:
            self._update(last_response=name)
        else:
            self._update(last_response=name)
        if self.on_response:
            self.on_response(op, name)

    def _handle_sense(self, op: int, payload: bytes) -> None:
        try:
            if op == Cmd.STATUS_SENSE:
                status = P.decode_status(payload, self.state.deck)
                self._update(status=status, error_latched=status.command_error)
            elif op == Cmd.JVC_SENSE:
                self._update(jvc=P.decode_jvc_status(payload, self.state.deck))
            elif op == Cmd.SELECT_SENSE:
                self._update(select=P.decode_select(payload))
            elif op == Cmd.CTL_SENSE:
                self._update(counter=P.decode_timecode(payload))
            elif op == Cmd.TC_SENSE:
                self._update(remaining=P.decode_timecode(payload))
            elif op == Cmd.CHAPTER_SENSE:
                self._update(chapter=P.decode_chapter(payload))
            elif op == Cmd.TITLE_SENSE:
                decoded = P.decode_title(payload)
                if decoded:
                    self._update(title=decoded[0], title_is_playlist=decoded[1])
            elif op == Cmd.DATE_SENSE:
                self._update(date=P.decode_date(payload))
            elif op == Cmd.CLOCK_SENSE:
                self._update(clock=P.decode_clock(payload))
        except ValueError:
            # A malformed reply should not take the worker thread down.
            pass

    # -- plumbing ----------------------------------------------------------

    def _log(self, direction: Direction, data: bytes, label: str | None = None,
             sense_opcode: int | None = None) -> None:
        if self.on_log:
            self.on_log(
                LogEntry(time.time(), direction, data,
                         label or P.describe(data), sense_opcode)
            )

    def _update(self, **changes) -> None:
        self.state = replace(self.state, **changes)
        if self.on_state_change:
            self.on_state_change(self.state)

    def _set_link(self, link: LinkState, message: str) -> None:
        self.link = link
        if self.on_link_change:
            self.on_link_change(link, message)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
