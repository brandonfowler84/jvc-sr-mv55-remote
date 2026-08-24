# JVC SR-MV55U Computer Control — Design Plan

A cross-platform desktop application for controlling a JVC SR-MV55U DVD/VCR combo
deck over its RS-232C serial port.

Protocol source: *JVC SR-MV45U / SR-MV55U User Manual*, pages 73–85
("RS-232C INTERFACE (SR-MV55U ONLY)"). The SR-MV45U does **not** have this port.

---

## 1. Hardware and link settings

### Wiring

| DB9 pin | Signal | Direction |
|---|---|---|
| 2 | TxD | VCR → PC |
| 3 | RxD | PC → VCR |
| 5 | GND | — |

The VCR is wired as **DCE**, so a correctly wired **straight-through** DB9
cable connects it directly to a standard USB-to-serial adapter (which presents
as DTE). No hardware flow control lines are wired.

In practice many inexpensive USB-serial cables reverse pins 2 and 3 internally,
and those need a null-modem adapter to work — confirmed on this project's own
hardware. A loopback test cannot distinguish the two cases.

### Serial parameters

| Setting | Value |
|---|---|
| Baud | 9600 |
| Data bits | 8 |
| Parity | **Odd** |
| Stop bits | 1 |
| Flow control | None |

Odd parity is unusual and is the single most common reason a hand-rolled
terminal test fails against this unit.

### Timing constraints (from the manual's NOTES)

- Communication is not established until **~10 seconds after the unit powers on**.
- A **minimum 50 ms interval** is required between commands. The manual's phrase
  is "50 mm/second", an obvious typo for 50 ms; we use 60 ms of headroom by
  default and make it configurable.

### Undocumented: inter-byte timing

Established against real hardware, not from the manual: the deck **ignores a
multi-byte command whose bytes arrive back-to-back**. `F0 30` sent as a single
write draws no reply whatsoever; the same bytes written separately, tens of
milliseconds apart, are acknowledged. Single-byte commands are unaffected.

The controller therefore writes each byte of a multi-byte command separately,
spaced by `inter_byte_gap` (60 ms by default, configurable, 0 to disable).

This one behaviour accounted for every multi-byte command appearing inert —
deck targeting, select preset, searches, shuttle, and the whole of Remote
Data — while transport control worked flawlessly, because transport commands
happen to be single-byte.

### Unit-side prerequisites

- **Mode Lock must be OFF.** Manual p. 65: "During Mode Lock, certain commands of
  the RS-232C control cannot be used." Mode Lock survives an AC power cycle, so a
  unit that ignores commands should be checked for a `LOCKED` front-panel display.

---

## 2. Protocol model

### Frame format

There is no packet framing, checksum, or terminator. A command is a **single
opcode byte**, optionally followed by a **fixed number of data bytes** determined
by the opcode. The receiver's parser is therefore a state machine keyed on the
opcode, with a per-opcode expected payload length.

### Command classes

**Deck targeting** — every transport/status command applies to whichever deck is
currently selected. This must be established before anything else:

- `F0 30` → target VCR deck
- `F0 38` → target DVD deck

The application always tracks the selected deck and re-asserts it on connect.

**Operation commands** (opcode + optional payload):

| Opcode | Function | Payload |
|---|---|---|
| `3A` | Play | — |
| `3F` | Stop (also clears Resume and Rec Request) | — |
| `4F` | Still | — |
| `80` | Chapter search (DVD) | 3 ASCII digits |
| `81` | Title search (DVD) | 1 mode byte (`30` original / `38` playlist) + 3 ASCII digits |
| `8E` | Date preset | 6 ASCII digits `MMDDYY` |
| `8F` | Clock preset | 6 ASCII digits `HHMMSS` |
| `90` / `91` | Finalize / cancel finalize (DVD) | — |
| `92` | Erase rewritable disc (DVD) | — |
| `93` / `94` | Top menu / disc menu (DVD) | — |
| `95` / `96` | Next / previous chapter (DVD) | — |
| `97` | Main Menu / DVD Navi screen | 1 byte: `30` close, `31` main menu, `32` navi |
| `98`–`9C` | Enter, up, down, right, left | — |
| `9D` / `9E` | Next / previous title (DVD) | — |
| `9F` | **Remote Data** — issue any wired-remote key code | 1 byte, see below |
| `A0` / `A1` | Power on / off | — |
| `A3` | Eject (DVD: open/close tray; VCR: eject cassette) | — |
| `AB` / `AC` | FF / REW (during playback: search) | — |
| `AD` / `AE` | Frame step forward / reverse | — |
| `B0` / `B1` | VISS forward / reverse (VCR) | — |
| `B5` / `B6` | Forward / reverse shuttle | 1 speed byte `30`–`39` |
| `B8` | Select preset: input, rec mode, audio language, subtitle | 2 bytes |
| `CA` / `CB` | Record / record pause (requires Rec Request first) | — |
| `FA` | Rec Request (record arming; cleared by Stop) | — |

**Remote Data (`9F`)** is the escape hatch and covers roughly 90 keys that the
direct opcodes do not expose: manual tracking ±, auto-tracking toggle, TBC on/off,
ten-key digits, jog and shuttle detents, S-VHS/composite/DV input selection,
CM skip, instant replay, counter reset, SP/LP speed, on-screen display, angle,
audio channel, subtitle, and the DVD/VCR deck keys. Prolonged (held) keypresses
are not supported for VCR-deck operations.

**Select preset (`B8`)** carries a 2-byte selector:

- Input: `30 31` L-1 video, `30 39` L-1 S-video, `30 35` F-1 video, `30 3D` F-1
  S-video, `30 34` DV
- Record mode: `34` + one of `30`–`33` (XP/SP/LP/EP), `38` (DV), or `81`–`D5`
  covering every FR60…FR480 free-rate step
- Audio language: `39` + language code
- Subtitle: `3C` + language code (`10` = off)

### Response classes

**System / unsolicited:**

| Byte | Meaning |
|---|---|
| `01` | Complete — a long operation (e.g. Title Search) finished |
| `02` | Error — invalid command in context; unit refuses further commands until cleared |
| `03` | Cassette Out — emitted by the VCR deck after an eject |
| `05` | Not Target — the requested operation could not be completed |
| `0A` | ACK — a defined command was received |
| `0B` | NAK — an undefined or nonexistent command was received |
| `56` | Clear — *sent by us* to clear the Error state |

The Error state is sticky and important: once `02` is raised, the unit stops
accepting commands but still answers Status Sense. The application surfaces this
prominently with a one-click Clear.

**Sense (query) commands** — the reply is the payload bytes *only*; the opcode
is **not** echoed (see "Reply framing" above):

| Opcode | Query | Reply payload |
|---|---|---|
| `60` | Chapter sense (DVD) | 3 ASCII digits |
| `61` | Title/track sense (DVD) | mode byte + 3 ASCII digits |
| `B9` | Select sense | 5 bytes: input, `2D`, rec mode, audio lang, subtitle |
| `BE` | Date sense | 6 ASCII digits `MMDDYY` (`2D` fill when unset) |
| `BF` | Clock sense | 6 ASCII digits `HHMMSS` (`2D` fill when unset) |
| `D7` | **Status sense** | 5 bytes of bit flags |
| `D8` | Remaining time in current rec mode | 8 ASCII bytes `HHMMSS--` |
| `D9` | CTL / lapse counter | 8 ASCII bytes `HHMMSS--` |
| `DD` | JVC status sense | 4 bytes of bit flags |

**`D7` Status Sense bit map** (differs between VCR and DVD deck):

- Byte 1 — b7/b6 fixed deck identity (VCR `01`, DVD `11`), b4 record forbidden,
  b3 media not inserted, b0 RS-232C command error
- Byte 2 — b7 video EE, b6 audio EE, b3 unit abnormality, b1 start sensor (VCR),
  b0 end sensor (VCR)
- Byte 3 — b2 repeat playback (DVD)
- Byte 4 — b7 play, b6 FF (VCR), b5 REW (VCR), b4 stop, b3 standby,
  b2 cassette eject (VCR), b1 recording
- Byte 5 — b7 pause, b5 forward shuttle, b4 reverse shuttle,
  b3..b0 SPEED CODE (still / slow ×3 / 1× / search ×4)

**`DD` JVC Status Sense**: byte 1 b4 = playing an EP-recorded tape (VCR);
byte 2 b3..b0 = disc type (DVD-RAM/-R/-RW/+R/+RW/VCD/CD/no disc);
byte 2 b0 and byte 4 b3 = dubbing in progress.

### Reply framing

**No sense reply echoes its opcode.** The reply is its payload bytes and
nothing else. In every sense table the opcode sits in an unnumbered leftmost
*label* column, separate from the numbered "1st Byte…Nth Byte" data columns,
and the worked examples only ever show data bytes. Confirmed against real
hardware for all nine, including the two bit-flag replies:

```
D7  ->  50 00 00 80 05          (5 bytes, no D7)
DD  ->  81 22 80 C0             (4 bytes, no DD)
D9  ->  30 30 30 32 32 31 2D 2D (no D9)
```

This was misread during development, and the resulting reader — which required
an echo and dropped anything else as noise — silently discarded every reply.
Worth recording as a warning: the tables genuinely look as though the opcode is
part of the response.

Since a reply cannot be identified from its own bytes, three things make
framing reliable:

1. **One sense query in flight at a time.** With several outstanding there is
   no way to know which reply is being read.
2. **Structural validation** of each candidate before accepting it — fixed
   filler bytes, ASCII-digit ranges, and for `D7`/`DD` the bits the manual
   documents as permanently 0 or 1.
3. **Timeout and resynchronise** if a reply never completes, discarding partial
   bytes so they cannot misalign the next one.

Helpfully, no sense reply's first byte can collide with a response opcode:
responses are `01`–`0B`, the bit-flag replies always have bit 6 or 7 set, and
the digit-format replies start `2D`–`3F`. An ACK arriving mid-stream is
therefore always distinguishable from reply data.

---

## 3. Software architecture

Three layers, strictly separated so the protocol is reusable outside this GUI
(scripts, home automation, batch digitising rigs).

```
jvcvcr/
  protocol.py    pure data + pure functions. No I/O, no threads, no Qt.
  transport.py   serial port abstraction + the in-process simulator backend
  device.py      threaded device controller: pacing, framing, polling, events
  simulator.py   a fake SR-MV55 that speaks the real protocol
  macros.py      named command sequences, JSON on disk
  gui/           PySide6 views; talks to device.py only through signals
```

**`protocol.py`** — every opcode, the complete `9F` remote-code table, the full
record-mode and language tables, encoders that build byte strings for the
digit-payload commands, and decoders that turn each sense reply into a typed
result. Fully unit-testable with no hardware.

**`device.py`** — owns a background reader thread and a writer queue. It enforces
the inter-command gap, correlates ACK/NAK with the command that caused it, frames
incoming sense replies, polls status on an interval, and emits state-change
events. Nothing in the GUI ever blocks on the port.

**`simulator.py`** — a fake deck that maintains plausible transport state, honours
deck targeting, answers all sense commands, emits ACK/NAK, and models the sticky
Error state. It makes the entire application testable with no hardware, which
matters both for development before the cable arrives and for anyone who wants to
contribute without owning an SR-MV55.

---

## 4. Interface design

Single window. A persistent connection bar (port, connect/disconnect, link state)
and a persistent **deck selector** at the top, because every command is
deck-scoped and confusing the two decks is the most likely user error.

- **Transport** — play, stop, still, FF/REW, frame step, eject, plus a shuttle
  speed control spanning still → slow ×3 → 1× → search ×4.
- **Status** — live decode of `D7`/`D9`/`DD`/`D8`: current mode, lapse counter,
  remaining time, media present, record-inhibit, disc type, and a prominent
  error banner with a one-click Clear.
- **Record** — deliberately gated. Choose rec mode and input, press **Arm**
  (`FA`), and only then does **Record** become available. Destructive DVD
  actions (erase, finalize) sit behind explicit confirmation.
- **DVD** — title and chapter search, menu keys, D-pad, next/previous.
- **Remote** — the full `9F` code table, searchable, as a fallback for anything
  the dedicated panels do not cover.
- **Console** — raw hex send plus a colour-coded TX/RX traffic log. This is what
  makes the app debuggable when a real unit behaves unexpectedly.
- **Macros** — named command sequences with delays, stored as shareable JSON.
  The obvious use is repeatable VHS→DVD dubbing runs.

---

## 5. Distribution

- Single-file `.exe` built with PyInstaller; no installer, no Python required.
- Also runs from source on Windows, macOS, and Linux via `pip install -e .`.
- MIT licence, so it can be freely shared and modified.
- README covers wiring, the odd-parity setting, USB-adapter driver notes
  (CH340/CP2102/FTDI), Mode Lock, and the 10-second warm-up.
