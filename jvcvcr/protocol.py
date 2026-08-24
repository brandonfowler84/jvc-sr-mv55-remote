"""Pure protocol definitions for the JVC SR-MV55U RS-232C interface.

Everything here is data or a pure function.  No I/O, no threads, no Qt --
this module is importable on its own by anyone who just wants to build or
parse bytes for the deck.

Source: JVC SR-MV45U / SR-MV55U user manual, pages 73-85.
Only the SR-MV55U has the serial port; the SR-MV45U does not.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, NamedTuple

# --------------------------------------------------------------------------
# Link settings (manual p. 73)
# --------------------------------------------------------------------------

BAUDRATE = 9600
BYTESIZE = 8
PARITY = "O"  # odd -- unusual, and the usual reason ad-hoc terminal tests fail
STOPBITS = 1

#: Manual says "a minimum interval of 50 mm/second is required between each
#: command", an obvious typo for 50 ms.  We default to a little more headroom.
MIN_COMMAND_GAP = 0.06

#: "It takes about 10 seconds for the communication to be established after
#: turning on the unit."
POWER_ON_SETTLE = 10.0


class Deck(Enum):
    """Which deck subsequent commands apply to."""

    VCR = "VCR"
    DVD = "DVD"


# --------------------------------------------------------------------------
# Opcodes
# --------------------------------------------------------------------------


class Cmd:
    """Command opcodes we send to the unit."""

    # -- system ------------------------------------------------------------
    CLEAR = 0x56  # clear the sticky Error state

    # -- deck targeting ----------------------------------------------------
    COMMAND_TARGET = 0xF0  # + 0x30 VCR / 0x38 DVD

    # -- transport ---------------------------------------------------------
    PLAY = 0x3A
    STOP = 0x3F
    STILL = 0x4F
    EJECT = 0xA3
    FF = 0xAB
    REW = 0xAC
    FWD_FIELD_STEP = 0xAD
    REV_FIELD_STEP = 0xAE
    VISS_FWD = 0xB0
    VISS_REV = 0xB1
    FWD_SHUTTLE = 0xB5  # + speed byte
    REV_SHUTTLE = 0xB6  # + speed byte

    # -- power -------------------------------------------------------------
    STANDBY_OFF = 0xA0  # power on
    STANDBY_ON = 0xA1  # power off

    # -- record ------------------------------------------------------------
    REC_REQUEST = 0xFA  # arm; cleared by STOP
    REC = 0xCA
    REC_PAUSE = 0xCB

    # -- search / preset ---------------------------------------------------
    CHAPTER_SEARCH = 0x80  # + 3 ASCII digits
    TITLE_SEARCH = 0x81  # + mode byte + 3 ASCII digits
    DATE_PRESET = 0x8E  # + 6 ASCII digits MMDDYY
    CLOCK_PRESET = 0x8F  # + 6 ASCII digits HHMMSS
    SELECT_PRESET = 0xB8  # + 2 bytes

    # -- disc --------------------------------------------------------------
    FINALIZE = 0x90
    CANCEL_FINALIZE = 0x91
    DISC_ERASE = 0x92

    # -- menus / navigation ------------------------------------------------
    TOP_MENU = 0x93
    MENU = 0x94
    NEXT_CHAPTER = 0x95
    PREV_CHAPTER = 0x96
    SETUP = 0x97  # + 0x30 close / 0x31 main menu / 0x32 DVD Navi
    SET = 0x98  # enter
    UP = 0x99
    DOWN = 0x9A
    RIGHT = 0x9B
    LEFT = 0x9C
    NEXT_TITLE = 0x9D
    PREV_TITLE = 0x9E

    REMOTE_DATA = 0x9F  # + remote key code, see REMOTE_CODES

    # -- sense (queries) ---------------------------------------------------
    CHAPTER_SENSE = 0x60
    TITLE_SENSE = 0x61
    SELECT_SENSE = 0xB9
    DATE_SENSE = 0xBE
    CLOCK_SENSE = 0xBF
    STATUS_SENSE = 0xD7
    TC_SENSE = 0xD8
    CTL_SENSE = 0xD9
    JVC_SENSE = 0xDD


class Resp:
    """Bytes the unit sends to us that are not sense replies."""

    COMPLETE = 0x01
    ERROR = 0x02
    CASSETTE_OUT = 0x03
    NOT_TARGET = 0x05
    ACK = 0x0A
    NAK = 0x0B


RESP_NAMES = {
    Resp.COMPLETE: "Complete",
    Resp.ERROR: "Error",
    Resp.CASSETTE_OUT: "Cassette Out",
    Resp.NOT_TARGET: "Not Target",
    Resp.ACK: "ACK",
    Resp.NAK: "NAK",
}

#: Number of data bytes that follow each opcode in a *command* we send.
COMMAND_PAYLOAD_LEN = {
    Cmd.COMMAND_TARGET: 1,
    Cmd.CHAPTER_SEARCH: 3,
    Cmd.TITLE_SEARCH: 4,
    Cmd.DATE_PRESET: 6,
    Cmd.CLOCK_PRESET: 6,
    Cmd.SETUP: 1,
    Cmd.REMOTE_DATA: 1,
    Cmd.FWD_SHUTTLE: 1,
    Cmd.REV_SHUTTLE: 1,
    Cmd.SELECT_PRESET: 2,
}

#: Number of bytes in a *sense reply*.
#:
#: No sense reply echoes its opcode -- the reply is these payload bytes and
#: nothing else.  Every sense table in the manual (pp. 81-85) puts the opcode
#: in an unnumbered leftmost *label* column, separate from the numbered
#: "1st Byte...Nth Byte" data columns, and the worked examples only ever show
#: the data bytes.  This holds for the bit-flag tables (D7 Status Sense, DD
#: JVC Status Sense) exactly as it does for the digit-format ones, and is
#: confirmed against a real SR-MV55U: D7 answers with 5 bytes beginning
#: "50 ...", DD with 4 bytes beginning "81 ...", neither carrying a leading
#: opcode.
#:
#: Because there is no opcode to anchor on, alignment is instead defended by
#: sending only one sense query at a time and structurally validating each
#: candidate reply -- see sense_payload_is_plausible.
SENSE_PAYLOAD_LEN = {
    Cmd.CHAPTER_SENSE: 3,
    Cmd.TITLE_SENSE: 4,
    Cmd.SELECT_SENSE: 5,
    Cmd.DATE_SENSE: 6,
    Cmd.CLOCK_SENSE: 6,
    Cmd.STATUS_SENSE: 5,
    Cmd.TC_SENSE: 8,
    Cmd.CTL_SENSE: 8,
    Cmd.JVC_SENSE: 4,
}

SENSE_NAMES = {
    Cmd.CHAPTER_SENSE: "Chapter Sense",
    Cmd.TITLE_SENSE: "Title Sense",
    Cmd.SELECT_SENSE: "Select Sense",
    Cmd.DATE_SENSE: "Date Sense",
    Cmd.CLOCK_SENSE: "Clock Sense",
    Cmd.STATUS_SENSE: "Status Sense",
    Cmd.TC_SENSE: "Remaining Time",
    Cmd.CTL_SENSE: "Counter",
    Cmd.JVC_SENSE: "JVC Status",
}

#: Human-readable names for the opcodes we send, for the traffic log.
COMMAND_NAMES = {
    Cmd.CLEAR: "Clear",
    Cmd.COMMAND_TARGET: "Command Target",
    Cmd.PLAY: "Play",
    Cmd.STOP: "Stop",
    Cmd.STILL: "Still",
    Cmd.EJECT: "Eject",
    Cmd.FF: "FF",
    Cmd.REW: "REW",
    Cmd.FWD_FIELD_STEP: "Fwd Field Step",
    Cmd.REV_FIELD_STEP: "Rev Field Step",
    Cmd.VISS_FWD: "VISS Fwd",
    Cmd.VISS_REV: "VISS Rev",
    Cmd.FWD_SHUTTLE: "Fwd Shuttle",
    Cmd.REV_SHUTTLE: "Rev Shuttle",
    Cmd.STANDBY_OFF: "Power On",
    Cmd.STANDBY_ON: "Power Off",
    Cmd.REC_REQUEST: "Rec Request",
    Cmd.REC: "Rec",
    Cmd.REC_PAUSE: "Rec Pause",
    Cmd.CHAPTER_SEARCH: "Chapter Search",
    Cmd.TITLE_SEARCH: "Title Search",
    Cmd.DATE_PRESET: "Date Preset",
    Cmd.CLOCK_PRESET: "Clock Preset",
    Cmd.SELECT_PRESET: "Select Preset",
    Cmd.FINALIZE: "Finalize",
    Cmd.CANCEL_FINALIZE: "Cancel Finalization",
    Cmd.DISC_ERASE: "Disc Erase",
    Cmd.TOP_MENU: "Top Menu",
    Cmd.MENU: "Menu",
    Cmd.NEXT_CHAPTER: "Next Chapter",
    Cmd.PREV_CHAPTER: "Prev Chapter",
    Cmd.SETUP: "Setup",
    Cmd.SET: "Set",
    Cmd.UP: "Up",
    Cmd.DOWN: "Down",
    Cmd.RIGHT: "Right",
    Cmd.LEFT: "Left",
    Cmd.NEXT_TITLE: "Next Title",
    Cmd.PREV_TITLE: "Prev Title",
    Cmd.REMOTE_DATA: "Remote Data",
    **SENSE_NAMES,
}


def describe(data: bytes) -> str:
    """Best-effort human label for a frame, used by the traffic log."""
    if not data:
        return ""
    op = data[0]
    if op in RESP_NAMES:
        return RESP_NAMES[op]
    name = COMMAND_NAMES.get(op)
    if name is None:
        return f"Unknown 0x{op:02X}"
    if op == Cmd.COMMAND_TARGET and len(data) > 1:
        return f"{name} -> {'DVD' if data[1] == 0x38 else 'VCR'}"
    if op == Cmd.REMOTE_DATA and len(data) > 1:
        key = REMOTE_CODE_NAMES.get(data[1])
        return f"{name}: {key}" if key else f"{name}: 0x{data[1]:02X}"
    if len(data) > 1:
        tail = " ".join(f"{b:02X}" for b in data[1:])
        return f"{name} [{tail}]"
    return name


# --------------------------------------------------------------------------
# Deck targeting
# --------------------------------------------------------------------------

TARGET_BYTE = {Deck.VCR: 0x30, Deck.DVD: 0x38}


def select_deck(deck: Deck) -> bytes:
    return bytes((Cmd.COMMAND_TARGET, TARGET_BYTE[deck]))


# --------------------------------------------------------------------------
# Remote Data (0x9F) key codes -- manual p. 77
# --------------------------------------------------------------------------


class RemoteKey(NamedTuple):
    code: int
    name: str
    deck: Deck | None  # None = works on both decks
    group: str


def _k(code: int, name: str, group: str, deck: Deck | None = None) -> RemoteKey:
    return RemoteKey(code, name, deck, group)


_VCR = Deck.VCR
_DVD = Deck.DVD

REMOTE_KEYS: tuple[RemoteKey, ...] = (
    # transport
    _k(0x03, "Stop / Clear", "Transport"),
    _k(0x04, "Eject", "Transport", _VCR),
    _k(0x0B, "Power On/Off", "Transport"),
    _k(0x0C, "Play / Select", "Transport"),
    _k(0x0D, "Pause", "Transport"),
    _k(0x14, "Next / Index +1", "Transport"),
    _k(0x15, "Previous / Index -1", "Transport"),
    _k(0xCC, "Rec", "Transport"),
    _k(0xCD, "Rec Pause", "Transport"),
    _k(0xDC, "Instant Replay", "Transport"),
    _k(0x96, "CM Skip", "Transport"),
    _k(0xCE, "Index +1", "Transport", _VCR),
    _k(0xCF, "Index -1", "Transport", _VCR),
    # slow / frame
    _k(0x06, "S.Fwd / Slow+", "Slow & Frame"),
    _k(0x07, "S.Rev / Slow-", "Slow & Frame"),
    _k(0x08, "Fwd Slow 1/6", "Slow & Frame", _VCR),
    _k(0xD0, "Rev Slow 1/6", "Slow & Frame", _VCR),
    _k(0xED, "Fwd Frame", "Slow & Frame"),
    _k(0xAF, "Rev Frame", "Slow & Frame"),
    _k(0xB0, "-Slow D", "Slow & Frame", _DVD),
    _k(0xB7, "-Slow C", "Slow & Frame"),
    _k(0xB6, "-Slow B", "Slow & Frame"),
    _k(0xB8, "+Slow C", "Slow & Frame"),
    _k(0xB9, "+Slow B", "Slow & Frame"),
    _k(0xBF, "+Slow D", "Slow & Frame", _DVD),
    # shuttle & jog
    _k(0xB1, "Shuttle -C", "Shuttle & Jog", _DVD),
    _k(0xB2, "Shuttle -B", "Shuttle & Jog"),
    _k(0xB3, "Shuttle -A", "Shuttle & Jog"),
    _k(0xB4, "Shuttle -2", "Shuttle & Jog"),
    _k(0xB5, "Shuttle -1", "Shuttle & Jog"),
    _k(0xBA, "Shuttle +1", "Shuttle & Jog"),
    _k(0xBB, "Shuttle +2", "Shuttle & Jog"),
    _k(0xBC, "Shuttle +A", "Shuttle & Jog"),
    _k(0xBD, "Shuttle +B", "Shuttle & Jog"),
    _k(0xBE, "Shuttle +C", "Shuttle & Jog", _DVD),
    _k(0xD8, "Jog -1", "Shuttle & Jog", _DVD),
    _k(0xD9, "Jog -1/2", "Shuttle & Jog", _DVD),
    _k(0xDA, "Jog +1/2", "Shuttle & Jog", _DVD),
    _k(0xDB, "Jog +1", "Shuttle & Jog", _DVD),
    # navigation
    _k(0x80, "Cursor Right", "Navigation"),
    _k(0x82, "Cursor Up", "Navigation"),
    _k(0x84, "Cursor Left", "Navigation"),
    _k(0x86, "Cursor Down", "Navigation"),
    _k(0x3C, "Enter", "Navigation"),
    _k(0x36, "Cancel", "Navigation"),
    _k(0x37, "Set Up", "Navigation"),
    _k(0x38, "Display", "Navigation"),
    _k(0xD4, "Return", "Navigation"),
    _k(0x81, "Menu", "Navigation", _DVD),
    _k(0xE0, "Top Menu / Navigation", "Navigation", _DVD),
    _k(0x90, "Memory / Mark", "Navigation", _DVD),
    # ten key
    _k(0x20, "Ten Key 0 / AUX / Space", "Ten Key", _DVD),
    _k(0x21, "Ten Key 1", "Ten Key", _DVD),
    _k(0x22, "Ten Key 2 / ABC", "Ten Key", _DVD),
    _k(0x23, "Ten Key 3 / DEF", "Ten Key", _DVD),
    _k(0x24, "Ten Key 4 / GHI", "Ten Key", _DVD),
    _k(0x25, "Ten Key 5 / JKL", "Ten Key", _DVD),
    _k(0x26, "Ten Key 6 / MNO", "Ten Key", _DVD),
    _k(0x27, "Ten Key 7 / PQRS", "Ten Key", _DVD),
    _k(0x28, "Ten Key 8 / TUV", "Ten Key", _DVD),
    _k(0x29, "Ten Key 9 / WXYZ", "Ten Key", _DVD),
    # picture / audio
    _k(0x17, "Audio", "Picture & Audio"),
    _k(0xC4, "Subtitle", "Picture & Audio", _DVD),
    _k(0xC0, "Angle / Live Check", "Picture & Audio", _DVD),
    _k(0x88, "TBC On/Off", "Picture & Audio", _VCR),
    _k(0x40, "Auto Tracking On/Off", "Picture & Audio", _VCR),
    _k(0x41, "Tracking +", "Picture & Audio", _VCR),
    _k(0x42, "Tracking -", "Picture & Audio", _VCR),
    _k(0x1E, "On Screen", "Picture & Audio", _VCR),
    _k(0x8E, "On Screen", "Picture & Audio"),
    _k(0x39, "Counter Reset", "Picture & Audio", _VCR),
    # input & mode
    _k(0x18, "Input -", "Input & Mode"),
    _k(0x19, "Input +", "Input & Mode"),
    _k(0xE1, "L-1 Y/C Input Select", "Input & Mode"),
    _k(0xE2, "L-1 Composite Input Select", "Input & Mode"),
    _k(0xEC, "F-1 Y/C Input Select", "Input & Mode"),
    _k(0xEE, "F-1 Composite Input Select", "Input & Mode"),
    _k(0xEA, "AUX (L-1) Select", "Input & Mode", _VCR),
    _k(0x97, "DV Input", "Input & Mode", _DVD),
    _k(0x31, "Remain / Rec Mode", "Input & Mode"),
    _k(0x7C, "SP", "Input & Mode", _VCR),
    _k(0x7D, "LP", "Input & Mode", _VCR),
    # disc & deck
    _k(0x3D, "Finalize", "Disc & Deck", _DVD),
    _k(0x87, "Open / Close", "Disc & Deck", _DVD),
    _k(0x43, "VCR Deck", "Disc & Deck", _DVD),
    _k(0x44, "DVD Deck", "Disc & Deck", _VCR),
    _k(0xD6, "VCR / DVD", "Disc & Deck"),
)

REMOTE_CODE_NAMES = {k.code: k.name for k in REMOTE_KEYS}
REMOTE_GROUPS = tuple(dict.fromkeys(k.group for k in REMOTE_KEYS))

#: The keys worth reaching for first.  Nearly everything else in the table
#: duplicates a direct opcode that the Transport, Record or DVD panels already
#: expose; these are the ones Remote Data is the *only* route to, which makes
#: them the real reason this tab exists.
COMMON_REMOTE_CODES: tuple[int, ...] = (
    0x41,  # Tracking +
    0x42,  # Tracking -
    0x40,  # Auto Tracking On/Off
    0x88,  # TBC On/Off
    0x39,  # Counter Reset
    0x96,  # CM Skip
    0xDC,  # Instant Replay
    0xCE,  # Index +1
    0xCF,  # Index -1
    0x38,  # Display
    0x8E,  # On Screen
    0x17,  # Audio
    0x31,  # Remain / Rec Mode
    0x7C,  # SP
    0x7D,  # LP
)

#: Label for the synthetic group the codes above are shown under.
COMMON_GROUP = "Most used"


def remote_keys_in(group: str) -> tuple[RemoteKey, ...]:
    """Keys in a group, including the synthetic COMMON_GROUP."""
    if group == COMMON_GROUP:
        by_code = {k.code: k for k in REMOTE_KEYS}
        return tuple(by_code[c] for c in COMMON_REMOTE_CODES if c in by_code)
    return tuple(k for k in REMOTE_KEYS if k.group == group)


def remote(code: int) -> bytes:
    """Build a Remote Data command for a wired-remote key code."""
    if not 0 <= code <= 0xFF:
        raise ValueError(f"remote code out of range: {code}")
    return bytes((Cmd.REMOTE_DATA, code))


# --------------------------------------------------------------------------
# Select Preset / Select Sense tables (0xB8 / 0xB9) -- manual pp. 78-80
# --------------------------------------------------------------------------

SEL_INPUT = 0x30
SEL_RECMODE = 0x34
SEL_AUDIO = 0x39
SEL_SUBTITLE = 0x3C

#: name -> second selector byte
INPUTS: dict[str, int] = {
    "L-1 VIDEO": 0x31,
    "L-1 S-VIDEO": 0x39,
    "F-1 VIDEO": 0x35,
    "F-1 S-VIDEO": 0x3D,
    "DV": 0x34,
}


def _build_rec_modes() -> dict[str, int]:
    modes = {"XP": 0x30, "SP": 0x31, "LP": 0x32, "EP": 0x33, "DV": 0x38}
    # Free-rate modes FR60..FR360 step 5 minutes, from 0x81 upwards, then the
    # three long modes the manual lists separately.
    code = 0x81
    for minutes in range(60, 361, 5):
        modes[f"FR{minutes}"] = code
        code += 1
    modes["FR420"] = 0xC9
    modes["FR480"] = 0xD5
    return modes


#: name -> second selector byte.  Includes every FR step from the manual.
REC_MODES: dict[str, int] = _build_rec_modes()

#: Approximate minutes each mode fits on a single-layer 4.7 GB disc.
REC_MODE_MINUTES: dict[str, int] = {
    "XP": 60,
    "SP": 120,
    "LP": 240,
    "EP": 360,
    **{name: int(name[2:]) for name in REC_MODES if name.startswith("FR")},
}

#: Audio-language / subtitle codes.  The second selector byte is shared between
#: the two categories; only the leading byte (0x39 vs 0x3C) differs.
LANGUAGES: dict[str, int] = {
    "ENGLISH": 0x11, "GERMAN": 0x12, "FRENCH": 0x13, "ITALIAN": 0x14,
    "SPANISH": 0x15, "DUTCH": 0x16, "SWEDISH": 0x17, "NORWEGIAN": 0x18,
    "FINNISH": 0x19, "DANISH": 0x1A, "JAPANESE": 0x1B,
    "AA": 0x1C, "AB": 0x1D, "AF": 0x1E, "AM": 0x1F, "AR": 0x20, "AS": 0x21,
    "AY": 0x22, "AZ": 0x23, "BA": 0x24, "BE": 0x25, "BG": 0x26, "BH": 0x27,
    "BI": 0x28, "BN": 0x29, "BO": 0x2A, "BR": 0x2B, "CA": 0x2C, "CO": 0x2D,
    "CS": 0x2E, "CY": 0x2F, "DZ": 0x30, "EL": 0x31, "EO": 0x32, "ET": 0x33,
    "EU": 0x34, "FA": 0x35, "FJ": 0x36, "FO": 0x37, "FY": 0x38, "GA": 0x39,
    "GD": 0x3A, "GL": 0x3B, "GN": 0x3C, "GU": 0x3D, "HA": 0x3E, "HI": 0x3F,
    "HR": 0x40, "HU": 0x41, "HY": 0x42, "IA": 0x43, "IE": 0x44, "IK": 0x45,
    "IN": 0x46, "IS": 0x47, "IW": 0x48, "JI": 0x49, "JW": 0x4A, "KA": 0x4B,
    "KK": 0x4C, "KL": 0x4D, "KM": 0x4E, "KN": 0x4F, "KO": 0x50, "KS": 0x51,
    "KU": 0x52, "KY": 0x53, "LA": 0x54, "LN": 0x55, "LO": 0x56, "LT": 0x57,
    "LV": 0x58, "MG": 0x59, "MI": 0x5A, "MK": 0x5B, "ML": 0x5C, "MN": 0x5D,
    "MO": 0x5E, "MR": 0x5F, "MS": 0x60, "MT": 0x61, "MY": 0x62, "NA": 0x63,
    "NE": 0x64, "OC": 0x65, "OM": 0x66, "OR": 0x67, "PA": 0x68, "PL": 0x69,
    "PS": 0x6A, "PT": 0x6B, "QU": 0x6C, "RM": 0x6D, "RN": 0x6E, "RO": 0x6F,
    "RU": 0x70, "RW": 0x71, "SA": 0x72, "SD": 0x73, "SG": 0x74, "SH": 0x75,
    "SI": 0x76, "SK": 0x77, "SL": 0x78, "SM": 0x79, "SN": 0x7A, "SO": 0x7B,
    "SQ": 0x7C, "SR": 0x7D, "SS": 0x7E, "ST": 0x7F, "SU": 0x80, "SW": 0x81,
    "TA": 0x82, "TE": 0x83, "TG": 0x84, "TH": 0x85, "TI": 0x86, "TK": 0x87,
    "TL": 0x88, "TN": 0x89, "TO": 0x8A, "TR": 0x8B, "TS": 0x8C, "TT": 0x8D,
    "TW": 0x8E, "UK": 0x8F, "UR": 0x90, "UZ": 0x91, "VI": 0x92, "VO": 0x93,
    "WO": 0x94, "XH": 0x95, "YO": 0x96, "ZH": 0x97, "ZU": 0x98,
}

#: Subtitles add an OFF value that audio languages do not have.
SUBTITLE_OFF = 0x10
SUBTITLES: dict[str, int] = {"OFF": SUBTITLE_OFF, **LANGUAGES}


def set_input(name: str) -> bytes:
    return bytes((Cmd.SELECT_PRESET, SEL_INPUT, _lookup(INPUTS, name, "input")))


def set_rec_mode(name: str) -> bytes:
    return bytes(
        (Cmd.SELECT_PRESET, SEL_RECMODE, _lookup(REC_MODES, name, "record mode"))
    )


def set_audio_language(name: str) -> bytes:
    return bytes(
        (Cmd.SELECT_PRESET, SEL_AUDIO, _lookup(LANGUAGES, name, "audio language"))
    )


def set_subtitle(name: str) -> bytes:
    return bytes(
        (Cmd.SELECT_PRESET, SEL_SUBTITLE, _lookup(SUBTITLES, name, "subtitle"))
    )


def _lookup(table: dict[str, int], name: str, what: str) -> int:
    try:
        return table[name.upper()]
    except KeyError:
        raise ValueError(f"unknown {what}: {name!r}") from None


def _reverse(table: dict[str, int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for name, code in table.items():
        out.setdefault(code, name)
    return out


_INPUTS_BY_CODE = _reverse(INPUTS)
_REC_MODES_BY_CODE = _reverse(REC_MODES)
_LANGUAGES_BY_CODE = _reverse(LANGUAGES)
_SUBTITLES_BY_CODE = _reverse(SUBTITLES)


# --------------------------------------------------------------------------
# Digit-payload encoders -- manual p. 76
# --------------------------------------------------------------------------


def _ascii_digits(value: int, width: int) -> bytes:
    """Encode an integer as `width` ASCII digit bytes (0x30-0x39)."""
    if value < 0 or value >= 10**width:
        raise ValueError(f"value {value} does not fit in {width} digits")
    return f"{value:0{width}d}".encode("ascii")


def chapter_search(chapter: int) -> bytes:
    """Search a chapter (DVD deck).  1-999."""
    if not 1 <= chapter <= 999:
        raise ValueError("chapter must be 1-999")
    return bytes((Cmd.CHAPTER_SEARCH,)) + _ascii_digits(chapter, 3)


TITLE_ORIGINAL = 0x30
TITLE_PLAYLIST = 0x38


def title_search(title: int, playlist: bool = False) -> bytes:
    """Search a title under ORIGINAL, or an entry in the PLAY LIST."""
    if not 1 <= title <= 999:
        raise ValueError("title must be 1-999")
    mode = TITLE_PLAYLIST if playlist else TITLE_ORIGINAL
    return bytes((Cmd.TITLE_SEARCH, mode)) + _ascii_digits(title, 3)


def date_preset(month: int, day: int, year: int) -> bytes:
    """Set the unit's date.  `year` may be 2-digit or 4-digit."""
    if not 1 <= month <= 12:
        raise ValueError("month must be 1-12")
    if not 1 <= day <= 31:
        raise ValueError("day must be 1-31")
    return (
        bytes((Cmd.DATE_PRESET,))
        + _ascii_digits(month, 2)
        + _ascii_digits(day, 2)
        + _ascii_digits(year % 100, 2)
    )


def clock_preset(hour: int, minute: int, second: int) -> bytes:
    """Set the unit's clock (24-hour)."""
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be 0-59")
    if not 0 <= second <= 59:
        raise ValueError("second must be 0-59")
    return (
        bytes((Cmd.CLOCK_PRESET,))
        + _ascii_digits(hour, 2)
        + _ascii_digits(minute, 2)
        + _ascii_digits(second, 2)
    )


def sync_clock(now: _dt.datetime | None = None) -> tuple[bytes, bytes]:
    """Return (date_preset, clock_preset) commands for the given time."""
    now = now or _dt.datetime.now()
    return (
        date_preset(now.month, now.day, now.year),
        clock_preset(now.hour, now.minute, now.second),
    )


# --------------------------------------------------------------------------
# Shuttle speeds -- manual p. 75, speed codes p. 84
# --------------------------------------------------------------------------


class ShuttleSpeed(NamedTuple):
    byte: int
    label: str
    decks: tuple[Deck, ...]


#: Shuttle steps in order, slowest to fastest.  The VCR deck omits a couple of
#: the intermediate slow and search steps that the DVD deck supports.
SHUTTLE_SPEEDS: tuple[ShuttleSpeed, ...] = (
    ShuttleSpeed(0x30, "Still", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x31, "Slow (slowest)", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x32, "Slow", (Deck.DVD,)),
    ShuttleSpeed(0x33, "Slow (fast)", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x35, "1x", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x36, "Search (fast)", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x37, "Search (faster)", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x38, "Search (fastest)", (Deck.VCR, Deck.DVD)),
    ShuttleSpeed(0x39, "Search (very fastest)", (Deck.DVD,)),
)


def shuttle(forward: bool, speed_byte: int) -> bytes:
    op = Cmd.FWD_SHUTTLE if forward else Cmd.REV_SHUTTLE
    if not 0x30 <= speed_byte <= 0x39:
        raise ValueError("shuttle speed byte must be 0x30-0x39")
    return bytes((op, speed_byte))


def shuttle_speeds_for(deck: Deck) -> tuple[ShuttleSpeed, ...]:
    return tuple(s for s in SHUTTLE_SPEEDS if deck in s.decks)


# --------------------------------------------------------------------------
# Setup screen (0x97)
# --------------------------------------------------------------------------

SETUP_CLOSE = 0x30
SETUP_MAIN_MENU = 0x31
SETUP_DVD_NAVI = 0x32


def setup(which: int) -> bytes:
    return bytes((Cmd.SETUP, which))


# --------------------------------------------------------------------------
# Sense decoders
# --------------------------------------------------------------------------

#: SPEED CODE nibble (D7 byte 5, bits 3..0) -> label, per deck (manual p. 84).
_SPEED_CODES_DVD = {
    0b0000: "Still",
    0b0001: "Slow (slowest)",
    0b0010: "Slow",
    0b0011: "Slow (fast)",
    0b0101: "1x",
    0b0110: "Search (fast)",
    0b0111: "Search (faster)",
    0b1000: "Search (fastest)",
    0b1001: "Search (very fastest)",
}
_SPEED_CODES_VCR = {
    0b0000: "Still",
    0b0001: "Slow",
    0b0101: "1x",
    0b0111: "Search (fast)",
    0b1000: "Search (fastest)",
}

#: DD byte 2, bits 3..0 (DVD deck) -- manual p. 85.  The manual prints DVD+R and
#: DVD+RW with the same 0000 pattern as plain DVD, so 0 is inherently ambiguous.
DISC_TYPES = {
    0b0000: "DVD / DVD+R / DVD+RW",
    0b0001: "DVD-RAM",
    0b0010: "DVD-R",
    0b0011: "DVD-RW",
    0b0110: "VCD",
    0b0111: "CD",
    0b1111: "No disc",
}


@dataclass(frozen=True)
class Status:
    """Decoded D7 Status Sense (5 bytes)."""

    deck: Deck
    raw: bytes

    playing: bool = False
    stopped: bool = False
    recording: bool = False
    paused: bool = False
    standby: bool = False
    fast_forward: bool = False
    rewinding: bool = False
    ejecting: bool = False
    fwd_shuttle: bool = False
    rev_shuttle: bool = False

    media_missing: bool = False
    record_forbidden: bool = False
    command_error: bool = False
    abnormality: bool = False
    video_ee: bool = False
    audio_ee: bool = False
    start_sensor: bool = False
    end_sensor: bool = False
    repeat_playback: bool = False

    speed_code: int = 0
    speed_label: str = "Still"

    @property
    def transport(self) -> str:
        """One-line summary of what the deck is doing."""
        if self.standby:
            return "Standby"
        if self.ejecting:
            return "Ejecting"
        if self.recording:
            return "Rec Pause" if self.paused else "Recording"
        if self.fast_forward:
            return "Fast Forward"
        if self.rewinding:
            return "Rewind"
        if self.fwd_shuttle:
            return f"Forward {self.speed_label}"
        if self.rev_shuttle:
            return f"Reverse {self.speed_label}"
        if self.playing:
            if self.paused or self.speed_code == 0:
                return "Still"
            return "Play" if self.speed_label == "1x" else f"Play {self.speed_label}"
        if self.stopped:
            return "Stop"
        return "Unknown"


def decode_status(payload: bytes, deck: Deck | None = None) -> Status:
    """Decode the 5 payload bytes of a D7 Status Sense reply.

    If `deck` is omitted it is inferred from byte 1 bits 7/6, which the manual
    fixes at 01 for the VCR deck and 11 for the DVD deck.
    """
    if len(payload) != 5:
        raise ValueError(f"status payload must be 5 bytes, got {len(payload)}")
    b1, b2, b3, b4, b5 = payload

    if deck is None:
        deck = Deck.DVD if b1 & 0x80 else Deck.VCR

    is_vcr = deck is Deck.VCR
    speed = b5 & 0x0F
    table = _SPEED_CODES_VCR if is_vcr else _SPEED_CODES_DVD

    return Status(
        deck=deck,
        raw=bytes(payload),
        record_forbidden=bool(b1 & 0x10),
        media_missing=bool(b1 & 0x08),
        command_error=bool(b1 & 0x01),
        video_ee=bool(b2 & 0x80),
        audio_ee=bool(b2 & 0x40),
        abnormality=bool(b2 & 0x08),
        start_sensor=bool(b2 & 0x02) and is_vcr,
        end_sensor=bool(b2 & 0x01) and is_vcr,
        repeat_playback=bool(b3 & 0x04) and not is_vcr,
        playing=bool(b4 & 0x80),
        fast_forward=bool(b4 & 0x40) and is_vcr,
        rewinding=bool(b4 & 0x20) and is_vcr,
        stopped=bool(b4 & 0x10),
        standby=bool(b4 & 0x08),
        ejecting=bool(b4 & 0x04) and is_vcr,
        recording=bool(b4 & 0x02),
        paused=bool(b5 & 0x80),
        fwd_shuttle=bool(b5 & 0x20),
        rev_shuttle=bool(b5 & 0x10),
        speed_code=speed,
        speed_label=table.get(speed, f"Speed code {speed:04b}"),
    )


@dataclass(frozen=True)
class JvcStatus:
    """Decoded DD JVC Status Sense (4 bytes)."""

    deck: Deck
    raw: bytes
    dubbing: bool = False
    ep_tape: bool = False  # VCR: playing an EP-recorded tape
    disc_type: str | None = None  # DVD only


def decode_jvc_status(payload: bytes, deck: Deck) -> JvcStatus:
    if len(payload) != 4:
        raise ValueError(f"JVC status payload must be 4 bytes, got {len(payload)}")
    b1, b2, b3, b4 = payload
    # The manual marks dubbing in two places; either being set means dubbing.
    dubbing = bool(b3 & 0x01) or bool(b4 & 0x08)
    return JvcStatus(
        deck=deck,
        raw=bytes(payload),
        dubbing=dubbing,
        ep_tape=bool(b1 & 0x10) if deck is Deck.VCR else False,
        disc_type=DISC_TYPES.get(b2 & 0x0F, f"Unknown ({b2 & 0x0F:04b})")
        if deck is Deck.DVD
        else None,
    )


@dataclass(frozen=True)
class SelectStatus:
    """Decoded B9 Select Sense (5 bytes).

    The reply carries only the *second* byte of each two-byte selector used by
    the B8 Select Preset command; byte 2 of the reply is a fixed 0x2D filler.
    """

    raw: bytes
    input: str | None = None
    rec_mode: str | None = None
    audio_language: str | None = None
    subtitle: str | None = None


def decode_select(payload: bytes) -> SelectStatus:
    if len(payload) != 5:
        raise ValueError(f"select payload must be 5 bytes, got {len(payload)}")
    inp, _fill, mode, audio, sub = payload
    return SelectStatus(
        raw=bytes(payload),
        input=_INPUTS_BY_CODE.get(inp),
        rec_mode=_REC_MODES_BY_CODE.get(mode),
        audio_language=_LANGUAGES_BY_CODE.get(audio),
        subtitle=_SUBTITLES_BY_CODE.get(sub),
    )


def _digits(payload: Iterable[int]) -> str | None:
    """Decode ASCII digit bytes; returns None if the field is unset ('-')."""
    out = []
    for b in payload:
        if b == 0x2D:  # '-', the manual's "not set" filler
            return None
        if not 0x30 <= b <= 0x39:
            return None
        out.append(chr(b))
    return "".join(out)


def decode_date(payload: bytes) -> tuple[int, int, int] | None:
    """Decode BE Date Sense -> (month, day, year_2digit), or None if unset."""
    text = _digits(payload)
    if text is None or len(text) != 6:
        return None
    return int(text[0:2]), int(text[2:4]), int(text[4:6])


def decode_clock(payload: bytes) -> tuple[int, int, int] | None:
    """Decode BF Clock Sense -> (hour, minute, second), or None if unset."""
    text = _digits(payload)
    if text is None or len(text) != 6:
        return None
    return int(text[0:2]), int(text[2:4]), int(text[4:6])


def decode_timecode(payload: bytes) -> str | None:
    """Decode D8/D9 (8 bytes, HHMMSS then two '-' frame bytes).

    Returns 'HH:MM:SS', or 'HH:MM' when the seconds field is filler.

    The seconds field is genuinely optional: manual p. 84 says TC Data Sense
    reports "hours, minutes and seconds for DVD, and hours and minutes for
    VCR", with unused fields fixed as '-' (0x2D). A real SR-MV55U's VCR deck
    answers D8 with e.g. "0044----" (00 hours, 44 minutes, seconds and frame
    both filler), so requiring all six digits -- as an earlier version did --
    rejected every VCR remaining-time reply outright.
    """
    if len(payload) < 6:
        return None
    hours = _digits(payload[0:2])
    minutes = _digits(payload[2:4])
    if hours is None or minutes is None:
        return None
    seconds = _digits(payload[4:6])
    if seconds is None:
        return f"{hours}:{minutes}"
    return f"{hours}:{minutes}:{seconds}"


def decode_chapter(payload: bytes) -> int | None:
    text = _digits(payload)
    return int(text) if text and len(text) == 3 else None


def decode_title(payload: bytes) -> tuple[int, bool] | None:
    """Decode 61 Title Sense -> (title_number, is_playlist).

    Rejects anything where the mode byte isn't exactly ORIGINAL or PLAYLIST,
    rather than silently treating an unrecognised byte as "not playlist" --
    sense replies carry no opcode, so this check is part of this frame's only
    defence against reading a misaligned window of bytes as a genuine reply.
    """
    if len(payload) != 4:
        return None
    if payload[0] not in (TITLE_ORIGINAL, TITLE_PLAYLIST):
        return None
    text = _digits(payload[1:])
    if text is None:
        return None
    return int(text), payload[0] == TITLE_PLAYLIST


# --------------------------------------------------------------------------
# Reply validation
# --------------------------------------------------------------------------
#
# Sense replies carry no opcode and no checksum (see SENSE_PAYLOAD_LEN), so
# the reader cannot confirm from the bytes alone that a candidate window is
# actually the start of a reply rather than a misaligned view of one.  These
# checks close most of that gap by testing each reply against whatever
# structure its format guarantees: fixed filler bytes, ASCII-digit ranges,
# and -- for the two bit-flag replies -- the bits the manual documents as
# permanently 0 or 1.


def _status_bits_plausible(payload: bytes) -> bool:
    """Check D7 Status Sense against its documented fixed bits (p. 83)."""
    if len(payload) != 5:
        return False
    b1, b2, b3, b4, b5 = payload
    # Byte 1: bit6 always 1 (deck identity is 01 for VCR, 11 for DVD);
    # bits 5, 2, 1 always 0.
    if not b1 & 0x40 or b1 & 0x26:
        return False
    # Byte 2: bits 5, 4, 2 always 0.
    if b2 & 0x34:
        return False
    # Byte 3: everything fixed 0 except bit2 (DVD repeat playback).
    if b3 & 0xFB:
        return False
    # Byte 4: bit0 always 0.  Byte 5: bit6 always 0.
    return not b4 & 0x01 and not b5 & 0x40


def _jvc_bits_plausible(payload: bytes) -> bool:
    """Check DD JVC Status Sense against its documented fixed bits (p. 85)."""
    if len(payload) != 4:
        return False
    b1, b2, b3, b4 = payload
    # Byte 1: 1000 ---1, where bit4 is the VCR's EP-tape flag.
    if b1 & 0xEF != 0x81:
        return False
    # Byte 2: high nibble fixed 0010; low nibble is the DVD disc type.
    if b2 & 0xF0 != 0x20:
        return False
    # Byte 3: 1000 000-, bit0 is a dubbing flag.
    if b3 & 0xFE != 0x80:
        return False
    # Byte 4: 1100 -000, bit3 is a dubbing flag.
    return b4 & 0xF7 == 0xC0


def _timecode_plausible(payload: bytes) -> bool:
    """Check a D8/D9 reply: three HH/MM/SS fields then a fixed '--' frame.

    Each field is either two ASCII digits or '--' filler.  Filler is normal,
    not a malformed reply: the VCR deck always fills the seconds field that
    way, and with no media loaded the deck fills *every* field, since it has
    no tape or disc to measure.  Rejecting the all-filler form (as an earlier
    version did) left the reader waiting out a timeout on every poll, which
    also stalled anything queued behind it.
    """
    if len(payload) != 8:
        return False
    # The frame field is fixed as '-' (0x2D) on both decks -- a strong anchor.
    if payload[6:8] != b"--":
        return False
    return all(
        _digits(payload[i:i + 2]) is not None or payload[i:i + 2] == b"--"
        for i in (0, 2, 4)
    )


def _select_plausible(payload: bytes) -> bool:
    """Check a B9 reply: input code, a fixed 0x2D, then three selector bytes."""
    if len(payload) != 5:
        return False
    return payload[1] == 0x2D and 0x30 <= payload[0] <= 0x3F


def _date_or_clock_plausible(payload: bytes) -> bool:
    if len(payload) != 6:
        return False
    # Wholly unset reads as all '-' (0x2D); otherwise all six must be digits.
    return _digits(payload) is not None or all(b == 0x2D for b in payload)


def sense_payload_is_plausible(opcode: int, payload: bytes) -> bool:
    """Does `payload` look like a genuine reply to `opcode`?

    Used by the reader to decide whether a candidate window of bytes is
    really a reply, since no sense reply carries an opcode to anchor on.
    Every sense command has some checkable structure, so a `False` here is
    a reliable signal that the reader is misaligned rather than merely
    unlucky.
    """
    if opcode == Cmd.STATUS_SENSE:
        return _status_bits_plausible(payload)
    if opcode == Cmd.JVC_SENSE:
        return _jvc_bits_plausible(payload)
    if opcode in (Cmd.TC_SENSE, Cmd.CTL_SENSE):
        return _timecode_plausible(payload)
    if opcode == Cmd.SELECT_SENSE:
        return _select_plausible(payload)
    if opcode in (Cmd.DATE_SENSE, Cmd.CLOCK_SENSE):
        return _date_or_clock_plausible(payload)
    if opcode == Cmd.CHAPTER_SENSE:
        return decode_chapter(payload) is not None
    if opcode == Cmd.TITLE_SENSE:
        return decode_title(payload) is not None
    return False


# --------------------------------------------------------------------------
# Convenience: a small named catalogue of simple no-payload commands, used to
# drive the GUI and macro system without hardcoding opcodes in either.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimpleCommand:
    key: str
    label: str
    payload: bytes
    decks: tuple[Deck, ...] = (Deck.VCR, Deck.DVD)
    destructive: bool = False
    tooltip: str = ""


def _s(key, label, op, decks=(Deck.VCR, Deck.DVD), destructive=False, tooltip=""):
    return SimpleCommand(key, label, bytes((op,)), tuple(decks), destructive, tooltip)


SIMPLE_COMMANDS: tuple[SimpleCommand, ...] = (
    _s("play", "Play", Cmd.PLAY),
    _s("stop", "Stop", Cmd.STOP, tooltip="Also clears Resume and Rec Request"),
    _s("still", "Still", Cmd.STILL),
    _s("ff", "Fast Forward", Cmd.FF),
    _s("rew", "Rewind", Cmd.REW),
    _s("step_fwd", "Frame +", Cmd.FWD_FIELD_STEP, tooltip="Works in Still mode"),
    _s("step_rev", "Frame -", Cmd.REV_FIELD_STEP, tooltip="Works in Still mode"),
    _s("eject", "Eject", Cmd.EJECT),
    _s("viss_fwd", "VISS Fwd", Cmd.VISS_FWD, (Deck.VCR,)),
    _s("viss_rev", "VISS Rev", Cmd.VISS_REV, (Deck.VCR,)),
    _s("power_on", "Power On", Cmd.STANDBY_OFF),
    _s("power_off", "Power Off", Cmd.STANDBY_ON),
    _s("rec_request", "Arm (Rec Request)", Cmd.REC_REQUEST),
    _s("rec", "Record", Cmd.REC, destructive=True),
    _s("rec_pause", "Record Pause", Cmd.REC_PAUSE),
    _s("clear", "Clear Error", Cmd.CLEAR),
    _s("top_menu", "Top Menu", Cmd.TOP_MENU, (Deck.DVD,)),
    _s("menu", "Disc Menu", Cmd.MENU, (Deck.DVD,)),
    _s("next_chapter", "Next Chapter", Cmd.NEXT_CHAPTER, (Deck.DVD,)),
    _s("prev_chapter", "Prev Chapter", Cmd.PREV_CHAPTER, (Deck.DVD,)),
    _s("next_title", "Next Title", Cmd.NEXT_TITLE, (Deck.DVD,)),
    _s("prev_title", "Prev Title", Cmd.PREV_TITLE, (Deck.DVD,)),
    _s("up", "Up", Cmd.UP),
    _s("down", "Down", Cmd.DOWN),
    _s("left", "Left", Cmd.LEFT),
    _s("right", "Right", Cmd.RIGHT),
    _s("set", "Enter", Cmd.SET),
    _s("finalize", "Finalize Disc", Cmd.FINALIZE, (Deck.DVD,), destructive=True),
    _s("cancel_finalize", "Cancel Finalization", Cmd.CANCEL_FINALIZE, (Deck.DVD,)),
    _s("erase", "Erase Disc", Cmd.DISC_ERASE, (Deck.DVD,), destructive=True),
)

SIMPLE_BY_KEY = {c.key: c for c in SIMPLE_COMMANDS}
