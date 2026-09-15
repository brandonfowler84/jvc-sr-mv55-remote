# JVC SR-MV55U RS-232C — practical notes

What the manual says, plus what it doesn't. Everything marked **undocumented**
was established against a real SR-MV55U and is not in the manual at all; those
are the things most likely to cost you a weekend.

The manual's own reference is pages 73–85 of the SR-MV45U/SR-MV55U user manual.
Only the **SR-MV55U** has the serial port.

---

## Link settings

| Setting | Value |
|---|---|
| Baud | 9600 |
| Data bits | 8 |
| Parity | **Odd** |
| Stop bits | 1 |
| Flow control | None |

DB9 pins 2 (TxD, deck→PC), 3 (RxD, PC→deck), 5 (GND). The deck is wired as
**DCE**, so a correctly wired straight-through cable is what the manual calls
for. Be aware that many cheap USB-serial cables reverse pins 2 and 3
internally, in which case a null-modem adapter is needed to cancel that out —
and a loopback test will not reveal which kind you have (see the table at the
end).

Odd parity is unusual and is the most common reason an ad-hoc terminal test
appears to fail.

---

## Undocumented: multi-byte commands need a gap between their bytes

**The single most important thing on this page.**

The deck silently ignores a multi-byte command whose bytes arrive back-to-back
at 9600 baud. It does not NAK. It does not reply at all.

```
F0 30   sent as one write        -> no response whatsoever
F0  30  sent as two writes ~60ms apart -> ACK
```

Single-byte commands are unaffected, which makes this genuinely hard to
diagnose: Play, Stop, Rewind and Eject all work perfectly while *every*
multi-byte command fails silently. It presents as "some buttons don't work"
rather than as a timing problem.

Affected: deck targeting (`F0`), Select Preset (`B8`), chapter/title search
(`80`/`81`), date and clock preset (`8E`/`8F`), shuttle (`B5`/`B6`), the setup
screen (`97`) and **all of Remote Data (`9F`)** — i.e. the entire wired-remote
key table.

**Write each byte separately with a delay between them.** 60 ms is known to
work; the lower bound has not been established.

---

## Undocumented: sense replies carry no opcode

Reply framing is easy to get wrong because the manual's tables *look* like the
opcode is part of the response. It isn't — in every sense table the opcode sits
in an unnumbered leftmost label column, separate from the numbered
"1st Byte…Nth Byte" data columns, and the worked examples only ever show data
bytes.

Confirmed on hardware for all of them, including the two bit-flag replies:

```
D7  ->  50 00 00 80 05      (5 bytes, no D7)
DD  ->  81 22 80 C0         (4 bytes, no DD)
D9  ->  30 30 30 32 32 31 2D 2D   ("000221--", no D9)
```

So a reply cannot be identified from its own bytes. Three things make framing
reliable:

1. **Only one sense query in flight at a time.** With several outstanding there
   is no way to know which reply you are looking at.
2. **Validate each candidate structurally** before accepting it — fixed filler
   bytes, ASCII-digit ranges, and for `D7`/`DD` the bits the manual documents as
   permanently 0 or 1. Reply lengths are fixed and known.
3. **Time out and resynchronise** if a reply never completes, discarding any
   partial bytes so they can't misalign the next one.

Conveniently, no sense reply's first byte can collide with a response opcode:
responses are `01`–`0B`, status/JVC replies have bit 6 or 7 set, and the
digit-format replies start `2D`–`3F`. An ACK arriving mid-stream is therefore
always distinguishable.

---

## Undocumented: the VCR reports partial timecodes

`D8` (remaining time) and `D9` (counter) return eight bytes: three two-digit
fields plus a two-byte frame field fixed at `--`. **Any field may be `--`.**

- The **VCR deck** reports hours and minutes only, so seconds read `--`:
  `0044----` is 44 minutes.
- With **no media loaded** the deck fills *every* field: `--------`.

A decoder that demands six digits rejects both, which looks like the deck never
answering. The manual does say TC Sense is "hours and minutes for VCR" (p. 84),
but it is easy to miss.

---

## Command shape

A command is one opcode byte plus a fixed number of payload bytes. No framing,
no checksum, no terminator.

**Deck targeting comes first** — every transport and status command applies to
whichever deck was last selected:

- `F0 30` → VCR deck
- `F0 38` → DVD deck

**Responses:** `01` Complete · `02` Error (sticky; clear with `56`) · `03`
Cassette Out · `05` Not Target · `0A` ACK · `0B` NAK.

An ACK means "a defined command arrived", not "it did something". `9F` is
acknowledged whether or not the key code has any effect.

**Timing:** at least 50 ms between commands (manual p. 73 says "50 mm/second",
an obvious typo for ms), and about 10 seconds after power-on before the deck
talks at all.

**Mode Lock** blocks a subset of commands and survives an AC power cycle. If the
front panel reads `LOCKED`, cancel it on the unit.

---

## Remote Data (`9F`)

`9F` + one key code reaches roughly 90 wired-remote keys — the only route to
tracking, TBC, counter reset, CM skip, instant replay, VHS index search,
ten-key entry, jog detents and the VCR's SP/LP toggle. Everything else in that
table duplicates a direct opcode.

It works, but note:

- It is **two bytes**, so the pacing rule above applies. Without it, nothing on
  the entire remote table does anything.
- Many keys only affect the **on-screen display over the video output** —
  Audio, Display, On Screen, Subtitle, Angle, Set Up. They will appear dead if
  you are watching the app rather than the TV.
- Some keys are deck-specific; the manual's table marks which.
- **REC (`9F CC`) needs no Rec Request.** The `CA` Rec opcode is refused until
  `FA` has armed the deck, but the remote's REC key records immediately.
  Confirmed on a real SR-MV55U. Anything that wants recording gated behind a
  deliberate step has to do the gating itself.

---

## Things that look like faults but aren't

| Symptom | Cause |
|---|---|
| Port opens, every write times out | Adapter waiting on RTS/CTS or DSR/DTR that a 3-wire cable never asserts. Force RTS and DTR high after opening. |
| Nothing works at all, both directions | Some cheap cables have pins 2 and 3 reversed internally. A loopback test still passes — it only proves TX and RX reach each other. A null-modem adapter fixes it. |
| Single-byte commands work, everything else silently fails | Byte pacing (above). |
| A sense field stays blank forever | Partial-timecode or all-filler reply being rejected (above). |
