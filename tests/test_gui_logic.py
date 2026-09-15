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


@pytest.fixture
def transport_panel(qapp):
    from jvcvcr.gui.panels import TransportPanel

    controller = FakeController()
    return TransportPanel(controller), controller


# -- shuttle ---------------------------------------------------------------

def test_shuttle_centre_is_still_and_extremes_are_fastest(transport_panel):
    panel, _ = transport_panel
    panel.set_deck(Deck.DVD)
    speeds = P.shuttle_speeds_for(Deck.DVD)

    assert panel._speed_at(0).label == "Still"
    assert panel._speed_at(len(speeds) - 1) is speeds[-1]
    assert panel._speed_at(-(len(speeds) - 1)) is speeds[-1]


def test_shuttle_can_reach_the_slow_speeds(transport_panel):
    """Slow motion sits between Still and 1x and must be selectable."""
    panel, controller = transport_panel
    panel.set_deck(Deck.DVD)

    slow_labels = {
        panel._speed_at(offset).label
        for offset in range(0, panel.shuttle.maximum() + 1)
    }
    assert "Slow" in slow_labels
    assert "1x" in slow_labels
    assert any(label.startswith("Search") for label in slow_labels)


def test_shuttle_sign_selects_direction(transport_panel):
    panel, controller = transport_panel
    panel.set_deck(Deck.VCR)

    panel.shuttle.setValue(3)
    panel._shuttle_apply()
    assert controller.sent[-1][0] == Cmd.FWD_SHUTTLE

    panel.shuttle.setValue(-3)
    panel._shuttle_apply()
    assert controller.sent[-1][0] == Cmd.REV_SHUTTLE


def test_shuttle_only_offers_speeds_the_deck_supports(transport_panel):
    panel, _ = transport_panel

    panel.set_deck(Deck.VCR)
    vcr_bytes = {
        panel._speed_at(o).byte for o in range(0, panel.shuttle.maximum() + 1)
    }
    panel.set_deck(Deck.DVD)
    dvd_bytes = {
        panel._speed_at(o).byte for o in range(0, panel.shuttle.maximum() + 1)
    }
    # 0x32 and 0x39 are DVD-only in the manual's B5/B6 tables.
    assert 0x32 not in vcr_bytes and 0x32 in dvd_bytes
    assert 0x39 not in vcr_bytes and 0x39 in dvd_bytes


def test_reset_returns_to_one_x(transport_panel):
    panel, controller = transport_panel
    panel.set_deck(Deck.VCR)
    panel.shuttle.setValue(panel.shuttle.maximum())
    panel._shuttle_reset()
    assert panel._speed_at(panel.shuttle.value()).label == "1x"
    assert controller.sent[-1] == P.shuttle(True, 0x35)


# -- deck-aware enabling ---------------------------------------------------

def test_vcr_only_buttons_disable_on_the_dvd_deck(transport_panel):
    panel, _ = transport_panel

    panel.set_deck(Deck.VCR)
    assert all(btn.isEnabled() for btn in panel.viss_buttons)

    panel.set_deck(Deck.DVD)
    assert not any(btn.isEnabled() for btn in panel.viss_buttons)


# -- record gating ---------------------------------------------------------

def test_record_button_is_disabled_until_armed(qapp):
    from dataclasses import replace

    from jvcvcr.device import DeviceState
    from jvcvcr.gui.panels import RecordPanel

    panel = RecordPanel(FakeController())
    state = DeviceState()

    panel.on_state(state)
    assert not panel.rec_btn.isEnabled()

    panel.on_state(replace(state, armed=True))
    assert panel.rec_btn.isEnabled()
    assert panel.arm_btn.isChecked()


def test_record_mode_hint_reports_disc_capacity(qapp):
    from jvcvcr.gui.panels import RecordPanel

    panel = RecordPanel(FakeController())
    panel._update_mode_hint("SP")
    assert "2h" in panel.mode_hint.text()
    panel._update_mode_hint("FR90")
    assert "1h 30m" in panel.mode_hint.text()


# -- remote browser --------------------------------------------------------

def _remote_buttons(panel):
    """The key buttons currently on show, as {label: button}."""
    out = {}
    for i in range(panel._keys.count()):
        widget = panel._keys.itemAt(i).widget()
        if widget is not None:
            out[widget.text()] = widget
    return out


def test_remote_browser_opens_on_the_most_used_keys(qapp):
    """The full table is ~85 keys. Landing on the curated set means the keys
    with no direct-opcode equivalent are reachable without hunting."""
    from jvcvcr.gui.panels import RemotePanel

    panel = RemotePanel(FakeController())
    panel.set_deck(Deck.VCR)

    labels = _remote_buttons(panel)
    assert any("Tracking +" in text for text in labels)
    assert any("Counter Reset" in text for text in labels)
    # A whole category's worth, not the entire table.
    assert len(labels) < 20


def test_remote_browser_shows_one_category_at_a_time(qapp):
    from jvcvcr.gui.panels import RemotePanel

    panel = RemotePanel(FakeController())
    panel.set_deck(Deck.VCR)

    panel._pick_group("Transport")
    labels = _remote_buttons(panel)
    assert any("Stop / Clear" in text for text in labels)
    # Tracking lives under Picture & Audio, so it must not be here.
    assert not any("Tracking" in text for text in labels)


def test_remote_browser_hides_other_decks_keys(qapp):
    from jvcvcr.gui.panels import RemotePanel

    panel = RemotePanel(FakeController())
    panel.set_deck(Deck.VCR)
    panel._pick_group("Picture & Audio")

    assert any("TBC" in text for text in _remote_buttons(panel))     # VCR only
    assert not any("Subtitle" in text for text in _remote_buttons(panel))  # DVD

    panel.show_all.setChecked(True)
    shown = _remote_buttons(panel)
    assert any("Subtitle" in text for text in shown)
    # Flagged so it is obvious it will not act on the current deck.
    assert any("Subtitle" in text and "DVD" in text for text in shown)


def test_remote_browser_search_spans_every_category(qapp):
    """Search must ignore the selected category, or a key you can name is
    still buried behind knowing which group it is filed under."""
    from jvcvcr.gui.panels import RemotePanel

    panel = RemotePanel(FakeController())
    panel.set_deck(Deck.VCR)
    panel._pick_group("Transport")

    panel.search.setText("tracking")  # Tracking is not in Transport
    labels = _remote_buttons(panel)
    assert any("Tracking +" in text for text in labels)
    assert not any("Instant Replay" in text for text in labels)

    panel.search.setText("88")  # by hex code
    assert any("TBC" in text for text in _remote_buttons(panel))


def test_remote_key_button_sends_immediately(qapp):
    """A remote should not require select-then-send."""
    from jvcvcr.gui.panels import RemotePanel

    controller = FakeController()
    panel = RemotePanel(controller)
    panel.set_deck(Deck.VCR)

    buttons = _remote_buttons(panel)
    target = next(b for text, b in buttons.items() if "Counter Reset" in text)
    target.click()

    assert controller.sent == [P.remote(0x39)]


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


# -- screen / on-screen display -------------------------------------------

@pytest.fixture
def screen_panel(qapp):
    from jvcvcr.gui.panels import ScreenPanel

    controller = FakeController()
    return ScreenPanel(controller), controller


def test_clear_overlay_uses_the_vcr_on_screen_key(screen_panel):
    panel, controller = screen_panel
    panel.set_deck(Deck.VCR)
    panel._clear_now()
    assert controller.sent == [P.remote(0x1E)]


def test_clear_overlay_presses_twice_on_the_dvd_deck(screen_panel):
    # The DVD deck cycles indicators -> on-screen bar -> nothing, so a single
    # press swaps one overlay for another rather than clearing it.
    panel, controller = screen_panel
    panel.set_deck(Deck.DVD)
    panel._clear_now()
    assert controller.sent == [P.remote(0x8E), P.remote(0x8E)]


def test_setup_navigation_is_vcr_only(screen_panel):
    panel, _ = screen_panel
    panel.set_deck(Deck.DVD)
    assert not panel.superimpose_btn.isEnabled()
    panel.set_deck(Deck.VCR)
    assert panel.superimpose_btn.isEnabled()


@pytest.mark.parametrize("row, downs", [
    ("VIDEO CALIBRATION", 1),
    ("SUPERIMPOSE", 4),
])
def test_setup_navigation_walks_to_the_right_menu_row(screen_panel, row, downs):
    from jvcvcr.gui import panels as pnl

    panel, _ = screen_panel
    steps = []
    panel.runner.run = lambda macro: steps.extend(macro.steps)
    panel._goto(row)

    kinds = [(s.kind, s.value) for s in steps]
    # Set Up, then one Down onto FUNCTION SET, Enter, then `downs` more.
    assert kinds[0] == ("remote", pnl._K_SETUP)
    remotes = [v for k, v in kinds if k == "remote"]
    assert remotes[:3] == [pnl._K_SETUP, pnl._K_DOWN, pnl._K_ENTER]
    assert remotes[3:] == [pnl._K_DOWN] * downs
    # Waits sit where the menu needs time to draw, not between every key.
    assert [k for k, _ in kinds].count("wait") == 2


def test_setup_navigation_never_guesses_the_value(screen_panel):
    # SUPERIMPOSE is a three-way the deck will not report, so the panel must
    # stop at the row and leave the value to the operator.
    from jvcvcr.gui import panels as pnl

    panel, _ = screen_panel
    steps = []
    panel.runner.run = lambda macro: steps.extend(macro.steps)
    panel._goto("SUPERIMPOSE")

    values = [s.value for s in steps if s.kind == "remote"]
    assert pnl._K_RIGHT not in values
    assert pnl._K_LEFT not in values


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
