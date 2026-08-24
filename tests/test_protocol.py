"""Protocol tests, checked against the worked examples printed in the manual."""

import pytest

from jvcvcr import protocol as P
from jvcvcr.protocol import Cmd, Deck


# -- manual p. 76: search and preset payloads -------------------------------

def test_chapter_search_matches_manual_example():
    # "When searching the 12th chapter" -> 80 30 31 32
    assert P.chapter_search(12) == bytes([0x80, 0x30, 0x31, 0x32])


def test_title_search_original_matches_manual_example():
    # "When searching the 45th title" -> 81 30 30 34 35
    assert P.title_search(45) == bytes([0x81, 0x30, 0x30, 0x34, 0x35])


def test_title_search_playlist_matches_manual_example():
    # "When searching the 28th play list" -> 81 38 30 32 38
    assert P.title_search(28, playlist=True) == bytes([0x81, 0x38, 0x30, 0x32, 0x38])


def test_date_preset_matches_manual_example():
    # "setting the date to January 17 2006" -> 8E 30 31 31 37 30 36
    assert P.date_preset(1, 17, 2006) == bytes(
        [0x8E, 0x30, 0x31, 0x31, 0x37, 0x30, 0x36]
    )


def test_clock_preset_matches_manual_example():
    # "12 hrs 34 min and 56 sec" -> 8F 31 32 33 34 35 36
    assert P.clock_preset(12, 34, 56) == bytes(
        [0x8F, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36]
    )


@pytest.mark.parametrize(
    "call, args",
    [
        (P.chapter_search, (0,)),
        (P.chapter_search, (1000,)),
        (P.title_search, (0,)),
        (P.date_preset, (13, 1, 2006)),
        (P.date_preset, (1, 32, 2006)),
        (P.clock_preset, (24, 0, 0)),
        (P.clock_preset, (0, 60, 0)),
        (P.clock_preset, (0, 0, 60)),
    ],
)
def test_out_of_range_values_are_rejected(call, args):
    with pytest.raises(ValueError):
        call(*args)


# -- manual pp. 78-80: select preset tables ---------------------------------

def test_select_preset_bytes():
    assert P.set_input("L-1 S-VIDEO") == bytes([0xB8, 0x30, 0x39])
    assert P.set_rec_mode("SP") == bytes([0xB8, 0x34, 0x31])
    assert P.set_audio_language("ENGLISH") == bytes([0xB8, 0x39, 0x11])
    assert P.set_subtitle("GERMAN") == bytes([0xB8, 0x3C, 0x12])
    assert P.set_subtitle("OFF") == bytes([0xB8, 0x3C, 0x10])


@pytest.mark.parametrize(
    "name, code",
    [
        ("XP", 0x30), ("SP", 0x31), ("LP", 0x32), ("EP", 0x33), ("DV", 0x38),
        # Spot checks across the free-rate range, from the manual's table.
        ("FR60", 0x81), ("FR100", 0x89), ("FR150", 0x93), ("FR200", 0x9D),
        ("FR265", 0xAA), ("FR300", 0xB1), ("FR360", 0xBD),
        ("FR420", 0xC9), ("FR480", 0xD5),
    ],
)
def test_record_mode_codes(name, code):
    assert P.REC_MODES[name] == code


def test_every_free_rate_step_is_present():
    # FR60 through FR360 in 5-minute steps, plus FR420 and FR480.
    expected = {f"FR{m}" for m in range(60, 361, 5)} | {"FR420", "FR480"}
    assert expected <= set(P.REC_MODES)


def test_unknown_names_are_rejected():
    with pytest.raises(ValueError):
        P.set_rec_mode("FR999")
    with pytest.raises(ValueError):
        P.set_input("HDMI")


# -- manual p. 82: select sense --------------------------------------------

def test_select_sense_matches_manual_example():
    # Example (392D311112): L-1 S-VIDEO, SP, ENGLISH audio, GERMAN subtitle.
    decoded = P.decode_select(bytes([0x39, 0x2D, 0x31, 0x11, 0x12]))
    assert decoded.input == "L-1 S-VIDEO"
    assert decoded.rec_mode == "SP"
    assert decoded.audio_language == "ENGLISH"
    assert decoded.subtitle == "GERMAN"


# -- manual pp. 81-84: sense decoding --------------------------------------

def test_chapter_sense_matches_manual_example():
    assert P.decode_chapter(b"012") == 12


def test_title_sense_matches_manual_examples():
    assert P.decode_title(bytes([0x30]) + b"045") == (45, False)
    assert P.decode_title(bytes([0x38]) + b"028") == (28, True)


def test_date_and_clock_sense_match_manual_examples():
    assert P.decode_date(b"011706") == (1, 17, 6)
    assert P.decode_clock(b"123456") == (12, 34, 56)


def test_unset_date_and_clock_decode_to_none():
    # "When the current date is not set, the value is fixed as '-' (0x2D)."
    assert P.decode_date(b"------") is None
    assert P.decode_clock(b"------") is None


def test_timecode_matches_manual_example():
    # "When remaining time is 1 hr 23 min 45 sec" -> 30 31 32 33 34 35 2D 2D
    assert P.decode_timecode(b"012345--") == "01:23:45"


# -- manual p. 83: status sense bit map ------------------------------------

def _status(b1=0x40, b2=0, b3=0, b4=0, b5=0):
    return bytes([b1, b2, b3, b4, b5])


def test_status_infers_deck_from_fixed_identity_bits():
    # VCR is 01 in bits 7/6, DVD is 11.
    assert P.decode_status(_status(b1=0x40)).deck is Deck.VCR
    assert P.decode_status(_status(b1=0xC0)).deck is Deck.DVD


def test_status_transport_flags():
    playing = P.decode_status(_status(b4=0x80, b5=0x05))
    assert playing.playing and playing.transport == "Play"

    stopped = P.decode_status(_status(b4=0x10))
    assert stopped.stopped and stopped.transport == "Stop"

    recording = P.decode_status(_status(b4=0x02))
    assert recording.recording and recording.transport == "Recording"

    rec_paused = P.decode_status(_status(b4=0x02, b5=0x80))
    assert rec_paused.transport == "Rec Pause"

    standby = P.decode_status(_status(b4=0x08))
    assert standby.standby and standby.transport == "Standby"


def test_vcr_only_flags_are_suppressed_on_the_dvd_deck():
    # Bit 6 of byte 4 is "During FF" on the VCR and fixed 0 on the DVD deck.
    dvd = P.decode_status(_status(b1=0xC0, b2=0x03, b4=0x40))
    assert not dvd.fast_forward
    assert not dvd.start_sensor and not dvd.end_sensor

    vcr = P.decode_status(_status(b1=0x40, b2=0x03, b4=0x40))
    assert vcr.fast_forward
    assert vcr.start_sensor and vcr.end_sensor


def test_status_condition_flags():
    st = P.decode_status(_status(b1=0x40 | 0x10 | 0x08 | 0x01, b2=0x08))
    assert st.record_forbidden
    assert st.media_missing
    assert st.command_error
    assert st.abnormality


def test_speed_codes_differ_between_decks():
    # 0b0110 is "search (fast)" on DVD; the VCR table has no such entry.
    dvd = P.decode_status(_status(b1=0xC0, b4=0x80, b5=0x20 | 0b0110))
    assert dvd.speed_label == "Search (fast)"
    assert dvd.transport == "Forward Search (fast)"

    vcr = P.decode_status(_status(b1=0x40, b4=0x80, b5=0x20 | 0b0111))
    assert vcr.speed_label == "Search (fast)"


def test_status_rejects_wrong_length():
    with pytest.raises(ValueError):
        P.decode_status(b"\x40\x00\x00")


# -- manual p. 85: JVC status sense ----------------------------------------

def test_jvc_status_disc_type():
    jvc = P.decode_jvc_status(bytes([0x81, 0x20 | 0b0011, 0x80, 0xC0]), Deck.DVD)
    assert jvc.disc_type == "DVD-RW"

    none = P.decode_jvc_status(bytes([0x81, 0x20 | 0b1111, 0x80, 0xC0]), Deck.DVD)
    assert none.disc_type == "No disc"


def test_jvc_status_dubbing_and_ep_tape():
    dubbing = P.decode_jvc_status(bytes([0x81, 0x22, 0x81, 0xC0]), Deck.VCR)
    assert dubbing.dubbing

    also_dubbing = P.decode_jvc_status(bytes([0x81, 0x22, 0x80, 0xC8]), Deck.VCR)
    assert also_dubbing.dubbing

    ep = P.decode_jvc_status(bytes([0x91, 0x22, 0x80, 0xC0]), Deck.VCR)
    assert ep.ep_tape
    # bit4 of byte 1 is fixed 0 on the DVD deck, so it must not be reported.
    assert not P.decode_jvc_status(bytes([0x91, 0x20, 0x80, 0xC0]), Deck.DVD).ep_tape


# -- deck targeting and remote data ----------------------------------------

def test_deck_targeting_bytes():
    assert P.select_deck(Deck.VCR) == bytes([0xF0, 0x30])
    assert P.select_deck(Deck.DVD) == bytes([0xF0, 0x38])


def test_remote_codes_are_unique_per_deck_scope():
    # The manual lists 0x1E and 0x8E both as "ON SCREEN"; every other code
    # must appear exactly once.
    codes = [k.code for k in P.REMOTE_KEYS]
    assert len(codes) == len(set(codes))


def test_remote_command_bytes():
    assert P.remote(0x41) == bytes([0x9F, 0x41])  # Tracking +
    with pytest.raises(ValueError):
        P.remote(0x100)


def test_shuttle_speeds_are_deck_aware():
    vcr = {s.byte for s in P.shuttle_speeds_for(Deck.VCR)}
    dvd = {s.byte for s in P.shuttle_speeds_for(Deck.DVD)}
    # 0x32 and 0x39 are DVD-only per the manual's B5/B6 tables.
    assert 0x32 not in vcr and 0x32 in dvd
    assert 0x39 not in vcr and 0x39 in dvd
    assert P.shuttle(True, 0x36) == bytes([0xB5, 0x36])
    assert P.shuttle(False, 0x36) == bytes([0xB6, 0x36])
    with pytest.raises(ValueError):
        P.shuttle(True, 0x40)


# -- framing tables --------------------------------------------------------

def test_sense_payload_lengths_cover_every_sense_command():
    senses = {
        Cmd.CHAPTER_SENSE, Cmd.TITLE_SENSE, Cmd.SELECT_SENSE, Cmd.DATE_SENSE,
        Cmd.CLOCK_SENSE, Cmd.STATUS_SENSE, Cmd.TC_SENSE, Cmd.CTL_SENSE,
        Cmd.JVC_SENSE,
    }
    assert senses == set(P.SENSE_PAYLOAD_LEN)


def test_response_opcodes_do_not_collide_with_sense_opcodes():
    # The reader frames on the first byte, so these two sets must be disjoint.
    assert not set(P.RESP_NAMES) & set(P.SENSE_PAYLOAD_LEN)


def test_describe_labels_frames():
    assert P.describe(bytes([0x0A])) == "ACK"
    assert P.describe(bytes([0xF0, 0x38])) == "Command Target -> DVD"
    assert P.describe(bytes([0x9F, 0x41])) == "Remote Data: Tracking +"
    assert P.describe(bytes([0x3A])) == "Play"
