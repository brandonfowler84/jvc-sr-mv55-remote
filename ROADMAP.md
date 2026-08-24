# Roadmap

Where the project actually stands, and what is worth doing next.

---

## Shipped — v1.0

Working and verified against a real SR-MV55U.

**Protocol layer**
- Full command set from the manual (pp. 73–85): transport, record, DVD
  navigation, select preset with every FR60–FR480 free-rate step, the complete
  ~90-key wired-remote table, and all nine sense queries
- Encoders and decoders tested against the manual's own worked examples
- Pure data and functions — no I/O, no Qt — so it is usable from scripts

**Hardware findings folded back in** (see [PROTOCOL.md](PROTOCOL.md))
- Multi-byte commands need a gap between their bytes, or the deck ignores them
  entirely. This was breaking every multi-byte command; single-byte transport
  commands masked it.
- Sense replies carry no opcode. Framing is held together by single-flight
  queries, structural validation and a resync timeout instead.
- The VCR reports partial timecodes (hours and minutes only; all-filler with no
  media), which a strict decoder rejects.
- Some USB-serial cables reverse pins 2 and 3, needing a null-modem adapter —
  and a loopback test cannot detect it.

**Device layer**
- Threaded controller with command pacing, per-byte pacing, reply framing,
  status polling, and dropped-reply recovery
- Sticky Error detection and one-click Clear
- Auto-reconnect after an adapter is unplugged and returns
- Crash containment: an unexpected error surfaces instead of silently killing
  the worker

**Simulator** — a fake deck speaking the real protocol, so the whole app is
usable and testable with no hardware

**Interface**
- Status, transport, record (arm-gated), DVD navigation, remote keys, macros,
  raw console with decoded traffic log
- Configurable quick-keys strip for the keys nothing else can reach
- Dark and light themes, switching live
- Responsive: stacks vertically when docked narrow, with a forced compact mode
- Drawn transport icons, stable layout that never jumps as values change
- Controls disable when there is no link, rather than looking operable

**Diagnostics** — a scripted probe with controls either side and deck-state
checks, which is what found the byte-pacing bug

**Docs and packaging** — single-file exe, in-app getting-started and wiring
guides, README, PROTOCOL.md, MIT licence, 119 tests

---

## Worth doing next

### 1. Digitising workflow

The reason to have this deck under computer control at all, and the biggest
remaining gap.

- [ ] **Guided VHS→DVD dub**: rewind, arm the DVD deck, set record mode from
      tape length, start both decks, watch status until the tape end sensor
      trips, stop, finalize
- [ ] Tape-length-aware record mode suggestion (a T-120 at SP will not fit in
      XP; pick the FR rate that exactly fills a 4.7 GB disc)
- [ ] Unattended batch mode for a stack of tapes, with per-tape logging
- [ ] Capture-card trigger hooks: run ffmpeg or similar when playback starts and
      stops, for direct-to-file capture instead of dubbing to disc
- [ ] Blank and leader detection using the VISS and start/end sensors
- [ ] Per-tape session log: timestamps, counter positions, errors

### 2. Automation surface

- [ ] Headless CLI (`jvcvcr play --deck vcr`) over the same protocol layer
- [ ] Documented Python API for scripting
- [ ] Local HTTP/WebSocket server, so the deck can be driven from a phone, a
      stream deck, or Home Assistant
- [ ] Scheduled operations driven from the PC clock
- [ ] Macro triggers on device events (auto-finalize when a dub completes)

### 3. Deeper device integration

- [ ] Title and chapter browser driven by `60`/`61` polling
- [ ] Jog/shuttle wheel widget mapped to the `9F` jog detents
- [ ] Tracking and TBC as first-class controls rather than quick keys
- [ ] Disc management with progress tracking: register, format, erase, finalize
- [ ] Full audio-language and subtitle selection UI over the language tables
- [ ] Clock sync including DST handling

### 4. Polish and reach

- [ ] Screenshots in the README
- [ ] Macro import/export and a bundled library of useful ones
- [ ] Accessibility pass: full keyboard navigation, screen-reader labels
- [ ] macOS and Linux builds alongside Windows
- [ ] Protocol capture/replay for bug reports
- [ ] Device profiles for sibling JVC decks that share this protocol
      (SR-MV50, SR-VS30, HR-S9911U and similar)
- [ ] Localisation

---

## Known limitations

- **Not code-signed**, so Windows SmartScreen warns on first run.
- **`9F` jog and ten-key codes are untested** on hardware; they are DVD-deck
  keys and were never exercised.
- **DVD-deck features are lightly tested** — title/chapter search, menus and
  finalize work in the simulator and are correct per the manual, but have had
  far less real-hardware use than the VCR side.
- **The lower bound on inter-byte pacing is unknown.** 60 ms works; faster may
  too.
