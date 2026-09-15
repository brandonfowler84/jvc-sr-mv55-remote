"""A fake SR-MV55U that speaks the real RS-232C protocol.

This exists so the application can be developed, demonstrated and tested with
no hardware attached, and so contributors who do not own an SR-MV55U can still
work on the project.  It models transport state, deck targeting, the record
arming handshake and the sticky Error state -- enough to exercise every code
path in the GUI.

It is a simulation of the *protocol*, not of the deck's internals; it does not
attempt to reproduce real-world timing beyond a rough approximation.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as P
from .protocol import Cmd, Deck, Resp


@dataclass
class DeckState:
    """Everything the simulator tracks for one of the two decks."""

    deck: Deck
    media_present: bool = True
    record_forbidden: bool = False

    playing: bool = False
    stopped: bool = True
    recording: bool = False
    paused: bool = False
    standby: bool = False
    fast_forward: bool = False
    rewinding: bool = False
    fwd_shuttle: bool = False
    rev_shuttle: bool = False
    speed_code: int = 0b0101  # 1x

    rec_requested: bool = False
    command_error: bool = False

    counter_seconds: float = 0.0
    remaining_seconds: int = 2 * 3600

    chapter: int = 1
    title: int = 1
    playlist: bool = False
    disc_type: int = 0b0011  # DVD-RW

    input_code: int = P.INPUTS["L-1 VIDEO"]
    rec_mode_code: int = P.REC_MODES["SP"]
    audio_code: int = P.LANGUAGES["ENGLISH"]
    subtitle_code: int = P.SUBTITLE_OFF

    def all_stop(self) -> None:
        self.playing = False
        self.recording = False
        self.paused = False
        self.fast_forward = False
        self.rewinding = False
        self.fwd_shuttle = False
        self.rev_shuttle = False
        self.stopped = True
        self.speed_code = 0b0101

    def status_bytes(self) -> bytes:
        is_vcr = self.deck is Deck.VCR
        b1 = 0x40 if is_vcr else 0xC0
        if self.record_forbidden:
            b1 |= 0x10
        if not self.media_present:
            b1 |= 0x08
        if self.command_error:
            b1 |= 0x01

        b2 = 0
        if self.playing or self.recording:
            b2 |= 0xC0  # video + audio EE
        if is_vcr and self.counter_seconds <= 0:
            b2 |= 0x02  # start sensor
        if is_vcr and self.counter_seconds >= self.remaining_seconds:
            b2 |= 0x01  # end sensor

        b3 = 0

        b4 = 0
        if self.playing:
            b4 |= 0x80
        if self.fast_forward and is_vcr:
            b4 |= 0x40
        if self.rewinding and is_vcr:
            b4 |= 0x20
        if self.stopped:
            b4 |= 0x10
        if self.standby:
            b4 |= 0x08
        if self.recording:
            b4 |= 0x02

        b5 = self.speed_code & 0x0F
        if self.paused:
            b5 |= 0x80
        if self.fwd_shuttle:
            b5 |= 0x20
        if self.rev_shuttle:
            b5 |= 0x10

        return bytes((b1, b2, b3, b4, b5))

    def jvc_bytes(self) -> bytes:
        b1 = 0x81
        b2 = 0x22 if self.deck is Deck.VCR else (0x20 | (self.disc_type & 0x0F))
        b3 = 0x80
        b4 = 0xC0
        return bytes((b1, b2, b3, b4))


class Simulator:
    """Consumes command bytes, produces reply bytes.

    Call :meth:`feed` with bytes written by the host; replies are handed to the
    ``on_output`` callback.  A background thread advances the transport counters
    so status polling shows movement.
    """

    def __init__(self) -> None:
        self.on_output: Callable[[bytes], None] | None = None
        self.decks = {Deck.VCR: DeckState(Deck.VCR), Deck.DVD: DeckState(Deck.DVD)}
        self.decks[Deck.DVD].remaining_seconds = 4 * 3600
        self.target = Deck.VCR
        self.date: tuple[int, int, int] | None = None
        self.clock_base: tuple[int, int, int] | None = None

        self._buf = bytearray()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._tick_loop, name="sim-tick", daemon=True
        )
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------

    def shutdown(self) -> None:
        self._stop.set()

    @property
    def state(self) -> DeckState:
        return self.decks[self.target]

    # -- host -> unit ------------------------------------------------------

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)
            self._drain()

    def _drain(self) -> None:
        while self._buf:
            op = self._buf[0]
            need = P.COMMAND_PAYLOAD_LEN.get(op, 0)
            if len(self._buf) < 1 + need:
                return  # wait for the rest of the payload
            frame = bytes(self._buf[: 1 + need])
            del self._buf[: 1 + need]
            self._handle(frame)

    def _emit(self, data: bytes) -> None:
        if self.on_output and data:
            self.on_output(data)

    def _handle(self, frame: bytes) -> None:
        op, payload = frame[0], frame[1:]

        # Sense commands are always answered, even while in the Error state --
        # the manual is explicit that Status Sense still works after an Error.
        handler = self._SENSE.get(op)
        if handler is not None:
            # Sense replies carry the payload only, with no opcode echoed
            # ahead of it -- see protocol.SENSE_PAYLOAD_LEN. A real
            # SR-MV55U was observed doing exactly this for every sense
            # command, including the two bit-flag ones. The simulator must
            # match, or it stops being a faithful stand-in for the deck
            # precisely where the reader's framing is most delicate.
            self._emit(handler(self))
            return

        if op == Cmd.CLEAR:
            self.state.command_error = False
            self._emit(bytes((Resp.ACK,)))
            return

        if op not in P.COMMAND_NAMES:
            self._emit(bytes((Resp.NAK,)))
            return

        if self.state.command_error:
            # Sticky Error: commands are refused until 0x56 clears it.
            self._emit(bytes((Resp.ERROR,)))
            return

        self._emit(bytes((Resp.ACK,)))
        completed = self._apply(op, payload)
        if completed:
            self._emit(bytes((Resp.COMPLETE,)))

    def _apply(self, op: int, payload: bytes) -> bool:
        """Mutate state for an operation command.  Returns True if it should
        also emit a Complete."""
        st = self.state

        if op == Cmd.COMMAND_TARGET:
            self.target = Deck.DVD if payload and payload[0] == 0x38 else Deck.VCR
            return False

        if op == Cmd.PLAY:
            if not st.media_present:
                self._emit(bytes((Resp.NOT_TARGET,)))
                return False
            st.all_stop()
            st.stopped = False
            st.playing = True
            return False

        if op == Cmd.STOP:
            st.all_stop()
            st.rec_requested = False
            return False

        if op == Cmd.STILL:
            st.paused = True
            st.playing = True
            st.stopped = False
            st.speed_code = 0
            return False

        if op in (Cmd.FF, Cmd.REW):
            st.all_stop()
            st.stopped = False
            if self.target is Deck.VCR:
                st.fast_forward = op == Cmd.FF
                st.rewinding = op == Cmd.REW
            else:
                st.playing = True
                st.fwd_shuttle = op == Cmd.FF
                st.rev_shuttle = op == Cmd.REW
                st.speed_code = 0b0111
            return False

        if op in (Cmd.FWD_FIELD_STEP, Cmd.REV_FIELD_STEP):
            step = 1 / 30 if op == Cmd.FWD_FIELD_STEP else -1 / 30
            st.counter_seconds = max(0.0, st.counter_seconds + step)
            return False

        if op in (Cmd.FWD_SHUTTLE, Cmd.REV_SHUTTLE):
            speed_byte = payload[0] if payload else 0x35
            st.all_stop()
            st.stopped = False
            st.playing = True
            st.speed_code = speed_byte - 0x30
            st.paused = st.speed_code == 0
            st.fwd_shuttle = op == Cmd.FWD_SHUTTLE and st.speed_code != 0
            st.rev_shuttle = op == Cmd.REV_SHUTTLE and st.speed_code != 0
            return False

        if op in (Cmd.VISS_FWD, Cmd.VISS_REV):
            st.counter_seconds = max(
                0.0, st.counter_seconds + (300 if op == Cmd.VISS_FWD else -300)
            )
            return True

        if op == Cmd.EJECT:
            st.all_stop()
            st.media_present = not st.media_present
            if self.target is Deck.VCR and not st.media_present:
                self._emit(bytes((Resp.CASSETTE_OUT,)))
            return False

        if op == Cmd.STANDBY_ON:
            for d in self.decks.values():
                d.all_stop()
                d.standby = True
            return False

        if op == Cmd.STANDBY_OFF:
            for d in self.decks.values():
                d.standby = False
            return False

        if op == Cmd.REC_REQUEST:
            st.rec_requested = True
            return False

        if op in (Cmd.REC, Cmd.REC_PAUSE):
            if not st.rec_requested:
                # Recording without arming is exactly the kind of thing the
                # real unit refuses, and it is worth surfacing in the GUI.
                st.command_error = True
                self._emit(bytes((Resp.ERROR,)))
                return False
            if st.record_forbidden or not st.media_present:
                self._emit(bytes((Resp.NOT_TARGET,)))
                return False
            st.all_stop()
            st.stopped = False
            st.recording = True
            st.paused = op == Cmd.REC_PAUSE
            return False

        if op == Cmd.CHAPTER_SEARCH:
            st.chapter = int(payload.decode("ascii", "replace") or 1)
            return True

        if op == Cmd.TITLE_SEARCH:
            st.playlist = payload[0] == P.TITLE_PLAYLIST
            st.title = int(payload[1:].decode("ascii", "replace") or 1)
            return True

        if op == Cmd.DATE_PRESET:
            text = payload.decode("ascii", "replace")
            self.date = (int(text[0:2]), int(text[2:4]), int(text[4:6]))
            return False

        if op == Cmd.CLOCK_PRESET:
            text = payload.decode("ascii", "replace")
            self.clock_base = (int(text[0:2]), int(text[2:4]), int(text[4:6]))
            return False

        if op == Cmd.SELECT_PRESET:
            if len(payload) == 2:
                kind, value = payload
                if kind == P.SEL_INPUT:
                    st.input_code = value
                elif kind == P.SEL_RECMODE:
                    st.rec_mode_code = value
                elif kind == P.SEL_AUDIO:
                    st.audio_code = value
                elif kind == P.SEL_SUBTITLE:
                    st.subtitle_code = value
            return False

        if op in (Cmd.FINALIZE, Cmd.CANCEL_FINALIZE, Cmd.DISC_ERASE):
            if self.target is not Deck.DVD or not st.media_present:
                self._emit(bytes((Resp.NOT_TARGET,)))
                return False
            if op == Cmd.DISC_ERASE:
                st.counter_seconds = 0.0
                st.title = st.chapter = 1
            return True

        if op == Cmd.NEXT_CHAPTER:
            st.chapter += 1
            return False
        if op == Cmd.PREV_CHAPTER:
            st.chapter = max(1, st.chapter - 1)
            return False
        if op == Cmd.NEXT_TITLE:
            st.title += 1
            return False
        if op == Cmd.PREV_TITLE:
            st.title = max(1, st.title - 1)
            return False

        if op == Cmd.REMOTE_DATA and payload:
            # The remote's transport keys do what their direct opcodes do, so
            # the handset visibly drives the simulator.
            if payload[0] == _REMOTE_REC:
                # Unlike the CA Rec opcode, the remote's REC key records with
                # no Rec Request first -- confirmed on a real SR-MV55U.
                if st.record_forbidden or not st.media_present:
                    self._emit(bytes((Resp.NOT_TARGET,)))
                    return False
                st.all_stop()
                st.stopped = False
                st.recording = True
                return False
            equivalent = self._REMOTE_EQUIVALENTS.get(payload[0])
            if equivalent is not None:
                if self.target is Deck.VCR and equivalent in _DVD_ONLY_OPS:
                    return False
                return self._apply(equivalent, b"")
            return False

        # Menus, cursor keys, setup and the remaining remote keys have no
        # simulated effect beyond the ACK already sent.
        return False

    #: Remote Data key code -> the direct opcode with the same effect.
    _REMOTE_EQUIVALENTS = {
        0x0C: Cmd.PLAY,
        0x03: Cmd.STOP,
        0x0D: Cmd.STILL,
        0x04: Cmd.EJECT,
        0x14: Cmd.NEXT_CHAPTER,
        0x15: Cmd.PREV_CHAPTER,
    }

    # -- sense replies -----------------------------------------------------

    def _sense_status(self) -> bytes:
        return self.state.status_bytes()

    def _sense_jvc(self) -> bytes:
        return self.state.jvc_bytes()

    def _sense_select(self) -> bytes:
        st = self.state
        return bytes((st.input_code, 0x2D, st.rec_mode_code, st.audio_code,
                      st.subtitle_code))

    def _sense_chapter(self) -> bytes:
        return f"{self.state.chapter % 1000:03d}".encode("ascii")

    def _sense_title(self) -> bytes:
        mode = P.TITLE_PLAYLIST if self.state.playlist else P.TITLE_ORIGINAL
        return bytes((mode,)) + f"{self.state.title % 1000:03d}".encode("ascii")

    def _sense_date(self) -> bytes:
        if self.date is None:
            return b"------"
        m, d, y = self.date
        return f"{m:02d}{d:02d}{y:02d}".encode("ascii")

    def _sense_clock(self) -> bytes:
        if self.clock_base is None:
            return b"------"
        h, m, s = self.clock_base
        return f"{h:02d}{m:02d}{s:02d}".encode("ascii")

    def _sense_ctl(self) -> bytes:
        return _hms(self.state.counter_seconds)

    def _sense_tc(self) -> bytes:
        left = max(0, self.state.remaining_seconds - int(self.state.counter_seconds))
        # Manual p. 84: TC Data Sense reports hours, minutes and seconds on
        # the DVD deck, but hours and minutes only on the VCR deck, where
        # the seconds field is '-' filler. Confirmed on real hardware, whose
        # VCR deck answers e.g. "0044----".
        return _hms(left, with_seconds=self.target is Deck.DVD)

    _SENSE = {
        Cmd.STATUS_SENSE: _sense_status,
        Cmd.JVC_SENSE: _sense_jvc,
        Cmd.SELECT_SENSE: _sense_select,
        Cmd.CHAPTER_SENSE: _sense_chapter,
        Cmd.TITLE_SENSE: _sense_title,
        Cmd.DATE_SENSE: _sense_date,
        Cmd.CLOCK_SENSE: _sense_clock,
        Cmd.CTL_SENSE: _sense_ctl,
        Cmd.TC_SENSE: _sense_tc,
    }

    # -- time base ---------------------------------------------------------

    _SPEED_FACTOR = {
        0b0000: 0.0, 0b0001: 0.1, 0b0010: 0.25, 0b0011: 0.5,
        0b0101: 1.0, 0b0110: 3.0, 0b0111: 7.0, 0b1000: 15.0, 0b1001: 30.0,
    }

    def _tick_loop(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(0.1):
            now = time.monotonic()
            dt = now - last
            last = now
            with self._lock:
                for st in self.decks.values():
                    if st.standby or st.paused:
                        continue
                    factor = self._SPEED_FACTOR.get(st.speed_code, 1.0)
                    if st.fast_forward:
                        st.counter_seconds += dt * 20
                    elif st.rewinding:
                        st.counter_seconds -= dt * 20
                    elif st.rev_shuttle:
                        st.counter_seconds -= dt * factor
                    elif st.playing or st.recording:
                        st.counter_seconds += dt * factor
                    else:
                        continue
                    st.counter_seconds = max(0.0, st.counter_seconds)
                    if st.counter_seconds >= st.remaining_seconds:
                        st.counter_seconds = float(st.remaining_seconds)
                        st.all_stop()


#: Opcodes whose remote-key equivalents only mean something on the DVD deck;
#: on the VCR, Next/Previous step the index instead, which is not modelled.
_DVD_ONLY_OPS = frozenset((Cmd.NEXT_CHAPTER, Cmd.PREV_CHAPTER))

#: The remote's REC key (Remote Data code).
_REMOTE_REC = 0xCC


def _hms(seconds: float, with_seconds: bool = True) -> bytes:
    """Encode seconds as an 8-byte timecode payload.

    The trailing two bytes are the frame field, always '-' filler. When
    `with_seconds` is False the seconds field is filler too, as the VCR
    deck's TC Data Sense reply uses.
    """
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    text = f"{h % 100:02d}{m:02d}".encode("ascii")
    text += f"{s:02d}".encode("ascii") if with_seconds else b"--"
    return text + b"--"
