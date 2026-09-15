"""Tests for GUI logic that has a real behavioural contract.

These construct widgets, so they need a QApplication but no visible window.
Skipped entirely if PySide6 or a usable display is unavailable.
"""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from jvcvcr import protocol as P  # noqa: E402
from jvcvcr.protocol import Cmd, Deck  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeController:
    """Records what the panel would send, without touching a port."""

    command_gap = 0.06
    connected = True

    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, data, **_kwargs):
        self.sent.append(bytes(data))

    def send_command(self, key):
        self.sent.append(P.SIMPLE_BY_KEY[key].payload)

    def send_remote(self, code):
        self.sent.append(P.remote(code))

    def query(self, opcode):
        self.sent.append(bytes((opcode,)))


# -- console ---------------------------------------------------------------

def test_console_filter_reveals_previously_hidden_history(qapp):
    """Toggling the poll filter must re-show traffic, not just future traffic."""
    import time

    from jvcvcr.device import Direction, LogEntry
    from jvcvcr.gui.panels import ConsolePanel

    panel = ConsolePanel(FakeController())
    panel.hide_polls.setChecked(True)

    poll = LogEntry(time.time(), Direction.TX, bytes([Cmd.STATUS_SENSE]),
                    "Status", Cmd.STATUS_SENSE)
    play = LogEntry(time.time(), Direction.TX, bytes([Cmd.PLAY]), "Play")
    panel.append(poll)
    panel.append(play)
    assert panel.table.rowCount() == 1

    panel.hide_polls.setChecked(False)
    assert panel.table.rowCount() == 2


def test_console_filter_hides_sense_replies_not_just_queries(qapp):
    """Regression: the filter used to key off a frame's first byte, but a
    sense reply carries no opcode -- so every reply leaked through while the
    queries were hidden, burying real traffic in polling noise."""
    import time

    from jvcvcr.device import Direction, LogEntry
    from jvcvcr.gui.panels import ConsolePanel

    panel = ConsolePanel(FakeController())
    panel.hide_polls.setChecked(True)

    # A real Status Sense reply: five payload bytes, no leading D7.
    reply = LogEntry(time.time(), Direction.RX, bytes([0x50, 0x00, 0x00, 0x80, 0x05]),
                     "Status Sense", Cmd.STATUS_SENSE)
    ack = LogEntry(time.time(), Direction.RX, bytes([0x0A]), "ACK")
    panel.append(reply)
    panel.append(ack)

    # Only the ACK -- the thing you actually want to see -- should show.
    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 3).text() == "ACK"


def test_console_rejects_invalid_hex_without_sending(qapp, monkeypatch):
    from jvcvcr.gui import panels
    from jvcvcr.gui.panels import ConsolePanel

    controller = FakeController()
    panel = ConsolePanel(controller)
    monkeypatch.setattr(panels.QMessageBox, "warning",
                        lambda *a, **k: None)

    panel.hex_entry.setText("nonsense")
    panel._send_raw()
    assert controller.sent == []

    panel.hex_entry.setText("3A")
    panel._send_raw()
    assert controller.sent == [bytes([Cmd.PLAY])]


# -- macro step building ---------------------------------------------------

def test_macro_panel_validates_steps(qapp, tmp_path):
    from jvcvcr.macros import MacroLibrary
    from jvcvcr.gui.panels import MacroPanel

    library = MacroLibrary(tmp_path / "macros.json")
    library.load()
    panel = MacroPanel(FakeController(), library)

    assert panel._build_step("wait", "2.5").value == 2.5
    assert panel._build_step("deck", "dvd").value == "DVD"
    assert panel._build_step("remote", "41").value == 0x41

    for kind, bad in (
        ("wait", "soon"), ("wait", "-1"), ("deck", "BETA"),
        ("command", "explode"), ("remote", "41 42"), ("raw", "zz"),
    ):
        with pytest.raises(ValueError):
            panel._build_step(kind, bad)


# -- handset ---------------------------------------------------------------

@pytest.fixture
def handset(qapp):
    from jvcvcr.gui.handset import HandsetPanel

    controller = FakeController()
    return HandsetPanel(controller), controller


def test_handset_codes_match_the_remote_table():
    """Every code a handset key sends must be a real remote key, scoped to a
    deck it actually works on -- a typo here would send a silent no-op."""
    from jvcvcr.gui.handset import KEYS

    by_code = {k.code: k for k in P.REMOTE_KEYS}
    for key in KEYS:
        for deck, code in ((Deck.VCR, key.vcr), (Deck.DVD, key.dvd)):
            if code is None:
                continue
            assert code in by_code, f"{key.id}: 0x{code:02X} is not a remote key"
            assert by_code[code].deck in (None, deck), (
                f"{key.id}: 0x{code:02X} does not work on the {deck.value} deck"
            )
        # A both-decks code must not be left off one deck by mistake.
        if key.dvd is not None and by_code[key.dvd].deck is None:
            assert key.vcr is not None, f"{key.id} is needlessly DVD-only"


def test_handset_key_sends_its_remote_code(handset):
    panel, controller = handset
    announced = []
    panel.keySent.connect(announced.append)
    panel.buttons["play"].click()
    assert controller.sent == [P.remote(0x0C)]
    assert announced and "9F 0C" in announced[0]


def test_handset_on_screen_follows_the_deck(handset):
    panel, controller = handset
    panel.set_deck(Deck.VCR)
    panel.press("on_screen")
    panel.set_deck(Deck.DVD)
    panel.press("on_screen")
    assert controller.sent == [P.remote(0x1E), P.remote(0x8E)]


def test_handset_greys_out_dvd_keys_on_the_vcr(handset):
    panel, controller = handset
    panel.set_deck(Deck.VCR)
    assert not panel.buttons["n5"].isEnabled()
    assert not panel.buttons["top_menu"].isEnabled()
    assert panel.buttons["enter"].isEnabled()
    assert panel.press("n5") is False
    assert controller.sent == []

    panel.set_deck(Deck.DVD)
    assert panel.buttons["n5"].isEnabled()
    panel.press("n5")
    assert controller.sent == [P.remote(0x25)]


def test_handset_tv_keys_are_dead_on_both_decks(handset):
    panel, _ = handset
    for deck in Deck:
        panel.set_deck(deck)
        for key_id in ("f1", "f2", "f3", "tv_vcr", "vol_up", "vol_down"):
            assert not panel.buttons[key_id].isEnabled()


def test_handset_rec_asks_first(handset, monkeypatch):
    from jvcvcr.gui import handset as hs

    panel, controller = handset
    answer = [hs.QMessageBox.Cancel]
    monkeypatch.setattr(hs.QMessageBox, "warning", lambda *a, **k: answer[0])

    panel.press("rec")
    assert controller.sent == []

    answer[0] = hs.QMessageBox.Yes
    panel.press("rec")
    assert controller.sent == [P.remote(0xCC)]


def test_handset_deck_key_asks_the_app_to_follow(handset):
    panel, controller = handset
    panel.set_deck(Deck.VCR)
    seen = []
    panel.deckToggleRequested.connect(seen.append)

    panel.press("vcr_dvd")
    assert controller.sent == [P.remote(0xD6)]
    assert seen == [Deck.DVD]


def test_handset_keyboard_map_points_at_real_keys():
    from jvcvcr.gui.handset import KEYBOARD, KEYS_BY_ID

    assert set(KEYBOARD.values()) <= set(KEYS_BY_ID)
    assert KEYBOARD["Backspace"] == "return"
