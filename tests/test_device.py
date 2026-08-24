"""End-to-end tests: DeviceController talking to the simulator."""

import time

import pytest

from jvcvcr import protocol as P
from jvcvcr.device import DeviceController, Direction, LinkState
from jvcvcr.macros import Macro, MacroRunner, Step, parse_hex
from jvcvcr.protocol import Cmd, Deck, Resp
from jvcvcr.simulator import Simulator
from jvcvcr.transport import SimulatorTransport


def wait_for(predicate, timeout=4.0, interval=0.02):
    """Poll until `predicate` is true, or time out.

    Predicates routinely reach through state that has not arrived yet
    (``state.status.transport`` before the first poll lands), so an
    AttributeError is treated as "not ready" rather than a failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except AttributeError:
            pass
        time.sleep(interval)
    return False


@pytest.fixture
def rig():
    sim = Simulator()
    controller = DeviceController(command_gap=0.005, poll_interval=0.05)
    responses = []
    controller.on_response = lambda op, name: responses.append((op, name))
    controller.responses = responses
    controller.connect(SimulatorTransport(sim))
    assert controller.link is LinkState.CONNECTED
    try:
        yield controller, sim
    finally:
        controller.disconnect()
        sim.shutdown()


# -- link ------------------------------------------------------------------

def test_connects_and_asserts_deck_target(rig):
    controller, sim = rig
    assert wait_for(lambda: sim.target is Deck.VCR)
    assert controller.connected


def test_status_polling_populates_state(rig):
    controller, sim = rig
    assert wait_for(lambda: controller.state.status is not None)
    assert wait_for(lambda: controller.state.counter is not None)
    assert controller.state.status.deck is Deck.VCR


def test_disconnect_stops_the_worker(rig):
    controller, _sim = rig
    controller.disconnect()
    assert controller.link is LinkState.DISCONNECTED
    assert not controller.connected


# -- transport commands ----------------------------------------------------

def test_play_reaches_the_simulator_and_comes_back_as_status(rig):
    controller, sim = rig
    controller.send_command("play")
    assert wait_for(lambda: sim.decks[Deck.VCR].playing)
    assert wait_for(lambda: controller.state.status
                    and controller.state.status.transport == "Play")


def test_stop_clears_playback(rig):
    controller, sim = rig
    controller.send_command("play")
    assert wait_for(lambda: sim.decks[Deck.VCR].playing)
    controller.send_command("stop")
    assert wait_for(lambda: sim.decks[Deck.VCR].stopped)
    assert wait_for(lambda: controller.state.status.transport == "Stop")


def test_deck_switching_targets_the_other_deck(rig):
    controller, sim = rig
    controller.set_deck(Deck.DVD)
    assert wait_for(lambda: sim.target is Deck.DVD)
    assert controller.state.deck is Deck.DVD
    assert wait_for(lambda: controller.state.status
                    and controller.state.status.deck is Deck.DVD)


def test_counter_advances_during_playback(rig):
    controller, sim = rig
    controller.send_command("play")
    assert wait_for(lambda: sim.decks[Deck.VCR].counter_seconds > 0.5, timeout=3)
    assert wait_for(lambda: controller.state.counter not in (None, "00:00:00"),
                    timeout=3)


# -- record gating ---------------------------------------------------------

def test_record_without_arming_raises_the_sticky_error(rig):
    controller, sim = rig
    controller.send(bytes([Cmd.REC]))
    assert wait_for(lambda: controller.state.error_latched)
    assert sim.decks[Deck.VCR].command_error
    assert not sim.decks[Deck.VCR].recording


def test_clear_releases_the_sticky_error(rig):
    controller, sim = rig
    controller.send(bytes([Cmd.REC]))
    assert wait_for(lambda: sim.decks[Deck.VCR].command_error)
    controller.send_command("clear")
    assert wait_for(lambda: not sim.decks[Deck.VCR].command_error)
    assert wait_for(lambda: not controller.state.error_latched)


def test_arm_then_record_works(rig):
    controller, sim = rig
    controller.send_command("rec_request")
    assert wait_for(lambda: sim.decks[Deck.VCR].rec_requested)
    assert controller.state.armed
    controller.send_command("rec")
    assert wait_for(lambda: sim.decks[Deck.VCR].recording)
    assert wait_for(lambda: controller.state.status.transport == "Recording")


def test_stop_disarms_the_record_request(rig):
    controller, sim = rig
    controller.send_command("rec_request")
    assert wait_for(lambda: sim.decks[Deck.VCR].rec_requested)
    controller.send_command("stop")
    assert wait_for(lambda: not sim.decks[Deck.VCR].rec_requested)
    assert not controller.state.armed


# -- framing ---------------------------------------------------------------

def test_sense_replies_are_framed_and_validated(rig):
    controller, _sim = rig
    assert wait_for(lambda: controller.sense_replies_ok)


def test_unknown_opcode_yields_nak(rig):
    controller, _sim = rig
    controller.send(bytes([0x77]))
    assert wait_for(lambda: any(op == Resp.NAK for op, _ in controller.responses))


def test_defined_command_yields_ack(rig):
    controller, _sim = rig
    controller.send_command("still")
    assert wait_for(lambda: any(op == Resp.ACK for op, _ in controller.responses))


def test_search_completion_yields_complete(rig):
    controller, sim = rig
    controller.set_deck(Deck.DVD)
    assert wait_for(lambda: sim.target is Deck.DVD)
    controller.send(P.chapter_search(12))
    assert wait_for(lambda: any(op == Resp.COMPLETE for op, _ in controller.responses))
    assert sim.decks[Deck.DVD].chapter == 12


def test_eject_on_the_vcr_emits_cassette_out(rig):
    controller, sim = rig
    controller.send_command("eject")
    assert wait_for(
        lambda: any(op == Resp.CASSETTE_OUT for op, _ in controller.responses)
    )
    assert not sim.decks[Deck.VCR].media_present


def test_payload_split_across_reads_is_reassembled(rig):
    """The simulator's own parser must tolerate a fragmented command."""
    controller, sim = rig
    controller.set_deck(Deck.DVD)
    assert wait_for(lambda: sim.target is Deck.DVD)
    sim.feed(bytes([Cmd.CHAPTER_SEARCH, 0x30]))
    sim.feed(bytes([0x34]))
    sim.feed(bytes([0x37]))
    assert wait_for(lambda: sim.decks[Deck.DVD].chapter == 47)


# -- logging ---------------------------------------------------------------

def test_traffic_is_logged_in_both_directions(rig):
    controller, _sim = rig
    entries = []
    controller.on_log = entries.append
    controller.send_command("play")
    assert wait_for(lambda: any(e.direction is Direction.TX for e in entries))
    assert wait_for(lambda: any(e.direction is Direction.RX for e in entries))
    tx = next(e for e in entries if e.direction is Direction.TX)
    assert tx.hex and tx.label


# -- macros ----------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("3A", b"\x3a"),
        ("b8 34 31", b"\xb8\x34\x31"),
        ("0xB8,0x34", b"\xb8\x34"),
        ("B83431", b"\xb8\x34\x31"),
    ],
)
def test_hex_parsing(text, expected):
    assert parse_hex(text) == expected


@pytest.mark.parametrize("bad", ["", "zz", "1FF 00"])
def test_bad_hex_is_rejected(bad):
    with pytest.raises(ValueError):
        parse_hex(bad)


def test_macro_steps_encode_to_the_right_bytes():
    assert Step("deck", "DVD").to_bytes() == bytes([0xF0, 0x38])
    assert Step("command", "play").to_bytes() == bytes([0x3A])
    assert Step("remote", 0x41).to_bytes() == bytes([0x9F, 0x41])
    assert Step("raw", "B8 34 31").to_bytes() == bytes([0xB8, 0x34, 0x31])
    assert Step("wait", 1.5).to_bytes() is None
    with pytest.raises(ValueError):
        Step("command", "nope").to_bytes()


def test_macro_runner_executes_every_step(rig):
    controller, sim = rig
    macro = Macro(
        name="test",
        steps=[
            Step("deck", "DVD"),
            Step("command", "rec_request"),
            Step("wait", 0.05),
            Step("command", "rec"),
        ],
    )
    finished = []
    runner = MacroRunner(controller)
    runner.on_finish = lambda ok, msg: finished.append((ok, msg))
    runner.run(macro)
    assert wait_for(lambda: finished, timeout=6)
    assert finished[0][0] is True
    assert wait_for(lambda: sim.decks[Deck.DVD].recording)


def test_macro_can_be_cancelled(rig):
    controller, _sim = rig
    macro = Macro(name="slow", steps=[Step("wait", 30.0), Step("command", "play")])
    finished = []
    runner = MacroRunner(controller)
    runner.on_finish = lambda ok, msg: finished.append((ok, msg))
    runner.run(macro)
    time.sleep(0.1)
    runner.cancel()
    assert wait_for(lambda: finished, timeout=3)
    assert finished[0] == (False, "Cancelled")


def test_macro_library_round_trips(tmp_path):
    from jvcvcr.macros import MacroLibrary

    path = tmp_path / "macros.json"
    library = MacroLibrary(path)
    library.load()  # seeds the defaults
    assert library.macros
    library.replace(Macro(name="Custom", steps=[Step("command", "stop")]))
    library.save()

    reloaded = MacroLibrary(path)
    reloaded.load()
    assert reloaded.find("Custom") is not None
    assert reloaded.find("Custom").steps[0].kind == "command"


# -- framing robustness -----------------------------------------------------
#
# Regression tests for a real-hardware bug: the original reader recognised a
# "new frame" against the *entire* sense-opcode table, and its no-echo
# fallback blindly consumed N bytes off the front of the buffer whenever
# anything was pending, with no check that those bytes were actually a frame
# boundary. Status/JVC-sense payload bytes are arbitrary bit patterns (not
# restricted to a safe range), so a byte that happened to equal an unrelated
# sense opcode -- or simply whatever was sitting at the buffer head once
# multiple queries were in flight at once -- got misread as a fresh frame,
# permanently desyncing every byte after it. Confirmed against a real
# SR-MV55U: polling several different sense types close together produced
# exactly this cascade of garbage in the traffic log.
#
# These construct a bare DeviceController and feed it bytes directly via
# _feed(), bypassing the transport entirely, so the exact byte sequence that
# caused the corruption can be reproduced deterministically.


def _controller_awaiting(*opcodes):
    """A controller primed as if it just sent queries for `opcodes` in order."""
    controller = DeviceController()
    controller.link = LinkState.CONNECTED
    controller._pending_sense = list(opcodes)
    controller._pending_since = time.monotonic()
    return controller


#: A real VCR-deck Status Sense payload, captured from an SR-MV55U: playing,
#: 1x, record-inhibited. Note it carries no opcode -- that is the real format.
REAL_STATUS_VCR = bytes([0x50, 0x00, 0x00, 0x80, 0x05])
#: Likewise a real VCR-deck JVC Status Sense payload.
REAL_JVC_VCR = bytes([0x81, 0x22, 0x80, 0xC0])


def test_status_sense_reply_carries_no_opcode_and_is_accepted():
    """Regression: an earlier version required D7 to echo its opcode, which
    real hardware never does -- so no status ever decoded and the UI sat on
    "Waiting for status..." indefinitely."""
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.status)

    controller._feed(REAL_STATUS_VCR)
    assert controller._pending_sense == []
    assert decoded[-1] is not None
    assert decoded[-1].raw == REAL_STATUS_VCR
    assert decoded[-1].playing
    assert decoded[-1].record_forbidden


def test_jvc_status_reply_carries_no_opcode_and_is_accepted():
    controller = _controller_awaiting(Cmd.JVC_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.jvc)

    controller._feed(REAL_JVC_VCR)
    assert controller._pending_sense == []
    assert decoded[-1] is not None


def test_bit_flag_replies_are_validated_against_their_fixed_bits():
    """Status and JVC Status have no digits to check, so their documented
    always-0/always-1 bits are the only thing standing between a misaligned
    window and it being accepted as real deck state."""
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.status)

    # bit6 of byte 1 is fixed 1 on both decks; 0x00 violates that.
    controller._feed(bytes([0x00, 0x00, 0x00, 0x80, 0x05]))
    assert decoded == []
    assert controller._pending_sense == [Cmd.STATUS_SENSE]

    # Byte 3 is fixed 0 apart from bit2; 0x10 violates that.
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.status)
    controller._feed(bytes([0x50, 0x00, 0x10, 0x80, 0x05]))
    assert decoded == []


def test_an_ack_arriving_before_a_sense_reply_is_not_swallowed_by_it():
    """A response byte can land between the query and its reply. It must be
    consumed as a response, with the reply still recognised afterwards --
    not absorbed into the reply's payload window."""
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    responses = []
    decoded = []
    controller.on_response = lambda op, name: responses.append(name)
    controller.on_state_change = lambda s: decoded.append(s.status)

    controller._feed(bytes([Resp.ACK]) + REAL_STATUS_VCR)

    assert responses == ["ACK"]
    assert controller._pending_sense == []
    assert decoded[-1] is not None
    assert decoded[-1].raw == REAL_STATUS_VCR


def test_response_bytes_are_recognised_even_with_a_sense_query_pending():
    """ACK/NAK/Error/etc. can arrive at any time and must not be swallowed
    by whatever sense reply happens to be outstanding."""
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    responses = []
    controller.on_response = lambda op, name: responses.append((op, name))

    controller._feed(bytes([Resp.ACK]))
    assert responses == [(Resp.ACK, "ACK")]
    assert controller._pending_sense == [Cmd.STATUS_SENSE]  # untouched


def test_ctl_sense_reply_carries_no_opcode_and_is_accepted():
    """CTL Sense (D9) replies with 8 raw bytes and no leading D9 -- confirmed
    against real hardware. The trailing '--' is the fixed frame field."""
    controller = _controller_awaiting(Cmd.CTL_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.counter)

    controller._feed(b"012345--")
    assert controller._pending_sense == []
    assert decoded[-1] == "01:23:45"


def test_vcr_remaining_time_reply_without_seconds_is_accepted():
    """Regression: the VCR deck's TC Data Sense reports hours and minutes
    only, leaving the seconds field as '-' filler (manual p. 84) -- e.g.
    "0044----". An earlier decoder demanded all six digits, so every VCR
    remaining-time reply was rejected as malformed and the field stayed
    blank forever."""
    controller = _controller_awaiting(Cmd.TC_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.remaining)

    controller._feed(b"0044----")
    assert controller._pending_sense == []
    assert decoded[-1] == "00:44"


def test_no_echo_payload_validator_rejects_a_misaligned_window():
    """The structural validator is the only defence a no-echo reply has
    against a misaligned buffer window; it must reject bytes that don't fit
    the expected shape rather than accept them and slide on."""
    controller = _controller_awaiting(Cmd.TITLE_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.title)

    # Mode byte must be 0x30 (ORIGINAL) or 0x38 (PLAYLIST); 0xFF is neither,
    # for any 4-byte window this can slide to (only 1 byte is dropped per
    # rejection, so a single 0xFF keeps re-invalidating each new window
    # until it's finally the one being dropped).
    controller._feed(bytes([0xFF, 0xFF, 0xFF, 0xFF]))
    assert controller._pending_sense == [Cmd.TITLE_SENSE]
    assert decoded == []

    # A fresh controller for the acceptance half, so this phase isn't
    # entangled with whatever partial window the rejections above left
    # buffered -- keeps each half independently easy to reason about.
    controller = _controller_awaiting(Cmd.TITLE_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.title)
    controller._feed(bytes([0x30, 0x30, 0x34, 0x35]))
    assert controller._pending_sense == []
    assert decoded[-1] == 45


def test_no_echo_payload_validator_accepts_the_unset_date_filler():
    """Date/Clock Sense use 0x2D-filled bytes to mean 'not set', which the
    validator must recognise as valid rather than reject as malformed."""
    controller = _controller_awaiting(Cmd.DATE_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.date)

    controller._feed(b"------")
    assert controller._pending_sense == []
    assert decoded[-1] is None  # "unset", successfully decoded as such


def test_only_one_sense_query_is_ever_in_flight_against_the_simulator():
    """Integration check: refresh_all() queues many sense types at once on
    connect, but the drain logic must only ever let one be outstanding."""
    sim = Simulator()
    controller = DeviceController(command_gap=0.005, poll_interval=0.05)
    max_pending = 0

    def watch(_state):
        nonlocal max_pending
        max_pending = max(max_pending, len(controller._pending_sense))

    controller.on_state_change = watch
    try:
        controller.connect(SimulatorTransport(sim))
        controller.refresh_all()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            max_pending = max(max_pending, len(controller._pending_sense))
            time.sleep(0.005)
    finally:
        controller.disconnect()
        sim.shutdown()

    assert max_pending <= 1


def test_dropped_reply_times_out_and_unblocks_the_queue():
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    controller.sense_timeout = 0.05
    entries = []
    controller.on_log = entries.append

    time.sleep(0.1)
    controller._expire_stale_pending(time.monotonic())

    assert controller._pending_sense == []
    assert "timed out" in entries[-1].label


def test_timeout_clears_partial_bytes_so_the_next_reply_starts_clean():
    """A half-arrived reply left in the buffer would misalign the *next*
    one, and with no opcode to resynchronise on that error compounds. Giving
    up on a query must therefore discard its partial bytes too."""
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    controller.sense_timeout = 0.05
    controller._feed(bytes([0x50, 0x00]))  # only 2 of 5 bytes arrive
    assert bytes(controller._rx) == bytes([0x50, 0x00])

    time.sleep(0.1)
    controller._expire_stale_pending(time.monotonic())

    assert controller._pending_sense == []
    assert bytes(controller._rx) == b""

    # A fresh query's reply is now read correctly rather than being offset
    # by the two stale bytes.
    controller._pending_sense = [Cmd.STATUS_SENSE]
    controller._pending_since = time.monotonic()
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.status)
    controller._feed(REAL_STATUS_VCR)
    assert decoded[-1] is not None
    assert decoded[-1].raw == REAL_STATUS_VCR


def test_leading_noise_is_dropped_until_a_valid_reply_aligns():
    """With no opcode to anchor on, the reader slides forward one byte at a
    time through anything that doesn't validate, rather than consuming a
    misaligned window -- and still recognises the real reply when it lands."""
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.status)

    # Bytes that cannot start a valid status reply (bit6 of byte 1 is fixed
    # 1, so 0x00/0x20 are impossible) and are not response opcodes either.
    controller._feed(bytes([0x00, 0x20, 0x00, 0x20]))
    assert all(status is None for status in decoded)
    assert controller._pending_sense == [Cmd.STATUS_SENSE]

    controller._feed(REAL_STATUS_VCR)
    assert controller._pending_sense == []
    assert decoded[-1] is not None
    assert decoded[-1].raw == REAL_STATUS_VCR


def test_drain_poll_queue_refuses_to_overlap_a_pending_query():
    controller = _controller_awaiting(Cmd.STATUS_SENSE)
    controller.link = LinkState.CONNECTED
    controller._poll_queue = [Cmd.CTL_SENSE]

    controller._drain_poll_queue()

    # The already-pending query must not be joined by a second one.
    assert controller._pending_sense == [Cmd.STATUS_SENSE]
    assert controller._poll_queue == [Cmd.CTL_SENSE]  # left queued, not sent


def test_worker_survives_an_unexpected_decoder_exception(rig, monkeypatch):
    """An exception the inner `except ValueError` in _handle_sense doesn't
    anticipate must not silently kill the worker thread -- previously it
    did, leaving the app looking permanently frozen with no error shown."""
    controller, sim = rig
    link_events = []
    controller.on_link_change = lambda link, msg: link_events.append((link, msg))
    log_entries = []
    controller.on_log = log_entries.append

    monkeypatch.setattr(
        controller, "_handle_sense",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    controller.send_command("still")  # any command that provokes a poll reply
    # Wait on the *last* thing the crash handler does, not the first. The link
    # state is set before the crash is logged, so waiting on the link alone
    # can return in between the two and miss the log entry.
    assert wait_for(lambda: any("WORKER CRASHED" in e.label for e in log_entries),
                    timeout=8)

    assert controller.link is LinkState.ERROR
    assert any("Internal error" in msg for _link, msg in link_events)
    # The thread must actually be gone, not just reporting an error while
    # still silently running.
    assert wait_for(lambda: controller._thread is None or
                    not controller._thread.is_alive(), timeout=2)


def test_drain_poll_queue_reserves_its_slot_before_transmission():
    """Regression: the pending slot must be reserved the moment an opcode
    is chosen to be sent, not when it's actually written to the wire.

    Actual transmission is throttled by command_gap (60ms default), but
    _drain_poll_queue is called on every worker-loop iteration (roughly
    every 20-50ms) -- faster than that throttle. If reservation happened at
    transmit time instead, several back-to-back loop iterations could each
    pop and hand off a *different* opcode before any of them were marked
    pending, silently recreating the multi-in-flight desync this whole
    design exists to prevent. This is exactly what happened against real
    hardware: F0/D7/B9/D9/D8/DD/BE/BF all went out within under a second.
    """
    controller = DeviceController()
    controller.link = LinkState.CONNECTED
    controller._poll_queue = [Cmd.STATUS_SENSE, Cmd.CTL_SENSE, Cmd.JVC_SENSE]

    # Three calls in a row, as fast successive loop iterations would make,
    # well before anything could realistically have been transmitted yet.
    controller._drain_poll_queue()
    controller._drain_poll_queue()
    controller._drain_poll_queue()

    assert controller._pending_sense == [Cmd.STATUS_SENSE]
    assert controller._poll_queue == [Cmd.CTL_SENSE, Cmd.JVC_SENSE]


def test_all_filler_timecode_reply_is_accepted():
    """With no media loaded the deck has nothing to measure and fills every
    field of a D8/D9 reply with '-'. Regression: rejecting that as malformed
    made every Remaining Time poll wait out a timeout, which also stalled
    whatever was queued behind it."""
    controller = _controller_awaiting(Cmd.TC_SENSE)
    decoded = []
    controller.on_state_change = lambda s: decoded.append(s.remaining)

    controller._feed(b"--------")
    assert controller._pending_sense == []
    # Accepted as a frame, but carries no usable value.
    assert decoded[-1] is None


def test_timeout_surfaces_a_response_stranded_behind_a_missing_reply():
    """An ACK sitting behind an incomplete reply must still be reported, not
    thrown away with the buffer -- otherwise the command that earned it looks
    like the deck ignored it entirely."""
    controller = _controller_awaiting(Cmd.TC_SENSE)
    controller.sense_timeout = 0.05
    responses = []
    controller.on_response = lambda op, name: responses.append(name)

    # A partial reply that never completes, with an ACK arriving behind it.
    controller._feed(bytes([0x2D, 0x2D]) + bytes([Resp.ACK]))
    assert responses == []  # stuck behind the incomplete reply for now

    time.sleep(0.1)
    controller._expire_stale_pending(time.monotonic())

    assert responses == ["ACK"]
    assert bytes(controller._rx) == b""


class _RecordingTransport:
    """Captures each individual write, so byte pacing can be observed."""

    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))


def test_multibyte_commands_are_sent_one_byte_per_write():
    """Regression: a real SR-MV55U silently drops a multi-byte command whose
    bytes arrive back-to-back -- "F0 30" as a single write draws no reply at
    all, while the same bytes written separately are acknowledged. Since
    single-byte commands were unaffected, this looked like "some buttons do
    nothing" when in fact every multi-byte command was being lost."""
    controller = DeviceController(inter_byte_gap=0.001)
    transport = _RecordingTransport()

    controller._write_frame(transport, P.select_deck(Deck.DVD))

    assert transport.writes == [b"\xf0", b"\x38"]


def test_single_byte_commands_are_sent_as_one_write():
    controller = DeviceController(inter_byte_gap=0.001)
    transport = _RecordingTransport()

    controller._write_frame(transport, bytes([Cmd.PLAY]))

    assert transport.writes == [b"\x3a"]


def test_pacing_can_be_disabled_for_a_single_frame():
    """The diagnostics need to send an unpaced frame deliberately, to show
    what the deck does with one."""
    controller = DeviceController(inter_byte_gap=0.001)
    transport = _RecordingTransport()

    controller._write_frame(transport, P.select_deck(Deck.DVD), pace=False)

    assert transport.writes == [b"\xf0\x38"]


def test_zero_gap_disables_pacing_entirely():
    controller = DeviceController(inter_byte_gap=0.0)
    transport = _RecordingTransport()

    controller._write_frame(transport, P.set_rec_mode("SP"))

    assert transport.writes == [bytes([0xB8, 0x34, 0x31])]
