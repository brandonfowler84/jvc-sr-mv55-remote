"""Byte-level transports: a real serial port, and the in-process simulator."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from . import protocol as P


class TransportError(RuntimeError):
    pass


class Transport(ABC):
    """Minimal byte pipe.  `read` must block for at most `timeout` seconds."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, timeout: float) -> bytes:
        """Return whatever bytes are available, or b'' if none arrive in time."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @property
    def description(self) -> str:
        return self.__class__.__name__


# --------------------------------------------------------------------------


class SerialTransport(Transport):
    """A real RS-232 link, 9600 8-O-1, no flow control."""

    def __init__(self, port: str, baudrate: int = P.BAUDRATE):
        self.port = port
        self.baudrate = baudrate
        self._serial = None

    def open(self) -> None:
        import serial  # imported lazily so protocol.py stays dependency-free

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_ODD,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.05,
                write_timeout=2.0,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
            # Our cable only wires TxD/RxD/GND (manual p. 73); RTS/CTS and
            # DTR/DSR are not connected to the deck at all.  rtscts=False and
            # dsrdtr=False above only disable *software* handshaking though --
            # several common USB-serial chipsets (Prolific, some CH340
            # clones) still gate their physical transmitter on RTS/DTR being
            # asserted, and leave it deasserted by default.  With those pins
            # floating, every write blocks until write_timeout and raises
            # SerialTimeoutException.  Forcing both high here is the standard
            # fix and is harmless for adapters that don't need it.
            try:
                self._serial.rts = True
                self._serial.dtr = True
            except Exception:
                pass  # not every backend supports setting these; ignore
        except Exception as exc:  # pyserial raises several unrelated types
            raise TransportError(f"could not open {self.port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def write(self, data: bytes) -> None:
        import serial

        if self._serial is None:
            raise TransportError("port is not open")
        try:
            self._serial.write(data)
            self._serial.flush()
        except serial.SerialTimeoutException as exc:
            raise TransportError(
                f"write timed out on {self.port}: {exc}. This usually means "
                "the adapter is waiting on a hardware flow-control signal "
                "(RTS/CTS or DSR/DTR) that our 3-wire cable never asserts. "
                "Try a different USB-serial adapter or driver, or check its "
                "port settings for a flow-control / RTS option to disable."
            ) from exc
        except Exception as exc:
            raise TransportError(f"write failed: {exc}") from exc

    def read(self, timeout: float) -> bytes:
        if self._serial is None:
            raise TransportError("port is not open")
        try:
            # Only touch pyserial's `.timeout` property when the caller
            # actually wants a different value than what's already set --
            # not unconditionally on every call. On Windows, that setter
            # issues a real SetCommTimeouts() syscall, and the worker loop
            # calls read() roughly 50 times a second with the *same* value
            # every time. Reissuing that syscall that often proved to be a
            # real source of multi-second stalls against a real adapter: the
            # entire worker loop (including the timeout-recovery check that
            # runs once per iteration) would go silent for seconds at a
            # time, since nothing else in the loop runs until read() itself
            # returns. Since ~50 syscalls/sec was pure waste to begin with
            # (the value never changes call to call in practice), only
            # reconfiguring on an actual change removes that entirely.
            if self._serial.timeout != timeout:
                self._serial.timeout = timeout
            first = self._serial.read(1)
            if not first:
                return b""
            waiting = self._serial.in_waiting
            if waiting:
                return first + self._serial.read(waiting)
            return first
        except Exception as exc:
            raise TransportError(f"read failed: {exc}") from exc

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def description(self) -> str:
        return f"{self.port} @ {self.baudrate} 8-O-1"


def list_serial_ports() -> list[tuple[str, str]]:
    """Return [(device, human description)] for every serial port found."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        label = p.description or p.device
        if p.manufacturer and p.manufacturer not in label:
            label = f"{label} ({p.manufacturer})"
        out.append((p.device, label))
    return out


# --------------------------------------------------------------------------


class SimulatorTransport(Transport):
    """Wraps a `Simulator` so the whole application runs without hardware."""

    def __init__(self, simulator=None):
        from .simulator import Simulator

        self.sim = simulator or Simulator()
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._data_ready = threading.Event()
        self._open = False

    def open(self) -> None:
        self._open = True
        self.sim.on_output = self._collect

    def close(self) -> None:
        self._open = False
        self.sim.on_output = None
        self.sim.shutdown()

    def _collect(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)
        self._data_ready.set()

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportError("simulator is not open")
        self.sim.feed(data)

    def read(self, timeout: float) -> bytes:
        if not self._open:
            raise TransportError("simulator is not open")
        self._data_ready.wait(timeout)
        with self._lock:
            out = bytes(self._buf)
            self._buf.clear()
            self._data_ready.clear()
        return out

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return "Simulator (no hardware)"
