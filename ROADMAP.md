# Roadmap

Where the project actually stands, and what is worth doing next.

The guiding rule since v1.1: **the remote plus live status is the product.**
New work should either make that screen better or live behind a menu.
Anything that would put a second panel back on the main screen needs a very
good reason.

---

## Shipped — v1.1

- **Handset** — the main screen is now a copy of the RM-SSR005U remote beside
  the live status card: same keys, places and colours, sending the codes the
  real remote sends. Deck-scoped keys grey out, arrows repeat while held, and
  arrows/Enter/Backspace/0–9 work from the keyboard. REC and FINALIZE confirm
  first
- **Not on the remote** — a pinned strip for the VCR keys the handset lacks:
  tracking ±, auto tracking, TBC, counter reset
- **One screen** — the transport card and the Record, DVD, Remote and Screen
  tabs are gone; Console, Macros and clock sync moved to a Tools menu
- **Responsive** — below 720 px the window becomes one column with the status
  pinned on top and the handset scrolling; usable down to 360 px wide
- **Hardware finding** — the remote's REC key (`9F CC`) records without a Rec
  Request, unlike the `CA` opcode. Documented in [PROTOCOL.md](PROTOCOL.md)
- Simulator honours the remote's transport and REC keys
- README screenshots

## Shipped — v1.0

Working and verified against a real SR-MV55U.

- Full protocol layer from the manual (pp. 73–85), pure and scriptable, tested
  against the manual's own worked examples
- Hardware findings folded back in: multi-byte commands need a gap between
  their bytes; sense replies carry no opcode; the VCR reports partial
  timecodes; some USB-serial cables reverse pins 2 and 3
- Threaded device controller with pacing, reply framing, status polling,
  dropped-reply recovery, sticky-error handling and auto-reconnect
- Simulator speaking the real protocol
- Diagnostics probe, raw console, macros, dark and light themes
- Single-file Windows executable, in-app guides, MIT licence

---

## Worth doing next

### 1. Verify the handset on hardware

Cheap, and it closes the known gaps below.

- [ ] Number keys (`9F 20`–`29`) on the DVD deck
- [ ] Whether the VCR/DVD key (`9F D6`) moves the serial Command Target
- [ ] Finalize (`9F 3D`): does the deck show its own confirmation?

### 2. Digitising workflow

The reason to put this deck under computer control at all.

- [ ] **Guided VHS→DVD dub**: rewind, set record mode from tape length, start
      both decks, stop when the tape end sensor trips, finalize
- [ ] Record-mode picker for the FR60–FR480 free-rate steps, behind a menu —
      the remote can only reach them by cycling REC MODE
- [ ] Capture-card hooks: run ffmpeg or similar when playback starts and stops
- [ ] Per-tape session log: timestamps, counter positions, errors

### 3. Reach

- [ ] Local web server so the same handset works from a phone on the network
- [ ] Headless CLI (`jvcvcr play --deck vcr`) over the protocol layer
- [ ] macOS and Linux builds
- [ ] Profiles for sibling JVC decks that share the protocol, if any owners
      turn up to test them

### 4. Polish

- [ ] Accessibility pass: screen-reader labels for the handset keys
- [ ] Protocol capture/replay for bug reports

---

## Known limitations

- **Not code-signed**, so Windows SmartScreen warns on first run.
- **Handset number keys are untested on hardware** (`9F 20`–`29`); they are
  DVD-deck keys and have not been exercised.
- **VCR/DVD key targeting is unverified.** The app re-sends Command Target
  after the key, so it stays correct either way, but what `9F D6` does to the
  serial target on its own is unknown.
- **No direct free-rate mode control** in the interface. The protocol layer
  supports every FR step, and the Console or a macro can send `B8 34 xx`.
- **DVD-deck features are lightly tested** compared with the VCR side.
- **The lower bound on inter-byte pacing is unknown.** 60 ms works; faster may
  too.
