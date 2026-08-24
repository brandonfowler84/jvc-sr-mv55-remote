"""Named command sequences, stored as JSON so users can share them."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import protocol as P
from .protocol import Deck


@dataclass
class Step:
    """One action in a macro.

    kind:
      ``deck``    -- target the VCR or DVD deck; ``value`` is "VCR" or "DVD"
      ``command`` -- a named simple command; ``value`` is its key
      ``remote``  -- a wired-remote key code; ``value`` is the code as an int
      ``raw``     -- literal bytes; ``value`` is a hex string like "B8 34 31"
      ``wait``    -- pause; ``value`` is seconds as a float
    """

    kind: str
    value: str | int | float

    def describe(self) -> str:
        if self.kind == "deck":
            return f"Target {self.value} deck"
        if self.kind == "command":
            cmd = P.SIMPLE_BY_KEY.get(str(self.value))
            return cmd.label if cmd else f"Command {self.value}"
        if self.kind == "remote":
            code = int(self.value)
            name = P.REMOTE_CODE_NAMES.get(code, f"0x{code:02X}")
            return f"Remote: {name}"
        if self.kind == "raw":
            return f"Raw: {self.value}"
        if self.kind == "wait":
            return f"Wait {float(self.value):g}s"
        return f"{self.kind}: {self.value}"

    def to_bytes(self) -> bytes | None:
        """Bytes to transmit, or None for non-transmitting steps (wait)."""
        if self.kind == "deck":
            return P.select_deck(Deck.DVD if str(self.value).upper() == "DVD"
                                 else Deck.VCR)
        if self.kind == "command":
            cmd = P.SIMPLE_BY_KEY.get(str(self.value))
            if cmd is None:
                raise ValueError(f"unknown command {self.value!r}")
            return cmd.payload
        if self.kind == "remote":
            return P.remote(int(self.value))
        if self.kind == "raw":
            return parse_hex(str(self.value))
        if self.kind == "wait":
            return None
        raise ValueError(f"unknown step kind {self.kind!r}")


def parse_hex(text: str) -> bytes:
    """Parse '3A' / '3a b8 34' / '0x3A,0xB8' into bytes."""
    cleaned = text.replace("0x", " ").replace("0X", " ").replace(",", " ")
    cleaned = cleaned.replace("-", " ").strip()
    if not cleaned:
        raise ValueError("no bytes given")
    parts = cleaned.split()
    if len(parts) == 1 and len(parts[0]) > 2 and len(parts[0]) % 2 == 0:
        parts = [parts[0][i:i + 2] for i in range(0, len(parts[0]), 2)]
    out = bytearray()
    for part in parts:
        try:
            value = int(part, 16)
        except ValueError:
            raise ValueError(f"{part!r} is not a hex byte") from None
        if not 0 <= value <= 0xFF:
            raise ValueError(f"{part!r} is out of byte range")
        out.append(value)
    return bytes(out)


@dataclass
class Macro:
    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [asdict(s) for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Macro":
        return cls(
            name=data.get("name", "Untitled"),
            description=data.get("description", ""),
            steps=[Step(s["kind"], s["value"]) for s in data.get("steps", [])],
        )


class MacroLibrary:
    """A collection of macros backed by a single JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.macros: list[Macro] = []

    def load(self) -> None:
        if not self.path.exists():
            self.macros = list(default_macros())
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.macros = [Macro.from_dict(m) for m in data.get("macros", [])]
        except (OSError, ValueError, KeyError):
            self.macros = list(default_macros())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "macros": [m.to_dict() for m in self.macros]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def find(self, name: str) -> Macro | None:
        return next((m for m in self.macros if m.name == name), None)

    def replace(self, macro: Macro) -> None:
        for i, existing in enumerate(self.macros):
            if existing.name == macro.name:
                self.macros[i] = macro
                return
        self.macros.append(macro)

    def remove(self, name: str) -> None:
        self.macros = [m for m in self.macros if m.name != name]


class MacroRunner:
    """Executes a macro on a background thread, one step at a time."""

    def __init__(self, controller):
        self.controller = controller
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.on_step: Callable[[int, Step], None] | None = None
        self.on_finish: Callable[[bool, str], None] | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self, macro: Macro) -> None:
        if self.running:
            raise RuntimeError("a macro is already running")
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(macro,), name="macro-runner", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, macro: Macro) -> None:
        try:
            for index, step in enumerate(macro.steps):
                if self._cancel.is_set():
                    self._finish(False, "Cancelled")
                    return
                if self.on_step:
                    self.on_step(index, step)
                if step.kind == "wait":
                    if self._cancel.wait(float(step.value)):
                        self._finish(False, "Cancelled")
                        return
                    continue
                if step.kind == "deck":
                    # Route through the controller so its tracked deck (and
                    # therefore the GUI) stays in step with the unit.
                    self.controller.set_deck(
                        Deck.DVD if str(step.value).upper() == "DVD" else Deck.VCR
                    )
                else:
                    data = step.to_bytes()
                    if data:
                        self.controller.send(data)
                # Leave room for the inter-command gap the worker enforces.
                if self._cancel.wait(max(0.1, self.controller.command_gap * 2)):
                    self._finish(False, "Cancelled")
                    return
            self._finish(True, "Finished")
        except Exception as exc:  # a bad step should not kill the thread quietly
            self._finish(False, str(exc))

    def _finish(self, ok: bool, message: str) -> None:
        if self.on_finish:
            self.on_finish(ok, message)


def default_macros() -> list[Macro]:
    """A small starter library, mostly aimed at VHS-to-DVD transfer."""
    return [
        Macro(
            name="Rewind tape",
            description="Rewind the VCR deck to the beginning.",
            steps=[
                Step("deck", "VCR"),
                Step("command", "stop"),
                Step("command", "rew"),
            ],
        ),
        Macro(
            name="Dub VHS to DVD (SP)",
            description=(
                "Rewind the tape, arm the DVD deck in SP, start recording, then "
                "start playback. Watch the status panel and stop both decks when "
                "the tape ends."
            ),
            steps=[
                Step("deck", "VCR"),
                Step("command", "stop"),
                Step("command", "rew"),
                Step("wait", 5.0),
                Step("deck", "DVD"),
                Step("command", "stop"),
                Step("raw", "B8 34 31"),  # Select Preset: record mode SP
                Step("raw", "B8 30 31"),  # Select Preset: input L-1 VIDEO
                Step("command", "rec_request"),
                Step("wait", 1.0),
                Step("command", "rec"),
                Step("wait", 2.0),
                Step("deck", "VCR"),
                Step("command", "play"),
            ],
        ),
        Macro(
            name="Stop everything",
            description="Stop both decks and clear any latched error.",
            steps=[
                Step("deck", "VCR"),
                Step("command", "stop"),
                Step("deck", "DVD"),
                Step("command", "stop"),
                Step("command", "clear"),
            ],
        ),
        Macro(
            name="Finalize disc",
            description="Stop the DVD deck and finalize the disc for playback "
                        "in other players. This cannot be undone on DVD-R.",
            steps=[
                Step("deck", "DVD"),
                Step("command", "stop"),
                Step("wait", 1.0),
                Step("command", "finalize"),
            ],
        ),
    ]
