"""Colours and stylesheet for the application.

Two palettes, one stylesheet.  Everything the interface colours comes from a
:class:`Palette`, so switching between dark and light is a matter of rebuilding
the stylesheet -- no restart, and no colour literals scattered through the
widget code.

Widgets that need a colour the stylesheet cannot reach (drawing into a table
cell, say) read it from :data:`ACTIVE` at paint time rather than capturing it
at construction, so they follow a theme change too.
"""

from __future__ import annotations

from dataclasses import dataclass

MONO = "Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace"


@dataclass(frozen=True)
class Palette:
    name: str

    bg: str            # window background
    bg_raised: str     # cards, menus, tab pane
    bg_input: str      # text fields, tables
    hover: str         # subtle hover fill

    border: str
    border_strong: str

    text: str
    text_dim: str
    text_faint: str

    accent: str        # primary colour, focus rings, active text
    accent_bg: str     # fill for checked/primary buttons
    accent_fg: str     # text drawn on accent_bg

    good: str
    warn: str
    bad: str
    rec: str

    error_bg: str      # error banner fill
    tx: str            # traffic log, transmitted
    rx: str            # traffic log, received

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


DARK = Palette(
    name="dark",
    bg="#15171c",
    bg_raised="#1d2027",
    bg_input="#111318",
    hover="#262a33",
    border="#2b3038",
    border_strong="#3a414d",
    text="#e6e9ef",
    text_dim="#8b93a3",
    text_faint="#5d6575",
    accent="#4f9dff",
    accent_bg="#2a5a99",
    accent_fg="#ffffff",
    good="#3fb96b",
    warn="#e3a33b",
    bad="#e5544b",
    rec="#e5544b",
    error_bg="#3a1c1a",
    tx="#4f9dff",
    rx="#3fb96b",
)

LIGHT = Palette(
    name="light",
    bg="#f2f4f7",
    bg_raised="#ffffff",
    bg_input="#ffffff",
    hover="#e9edf3",
    border="#dde2e9",
    border_strong="#c2cad4",
    text="#1b2230",
    text_dim="#5b6474",
    text_faint="#8b93a1",
    accent="#1f6feb",
    accent_bg="#1f6feb",
    accent_fg="#ffffff",
    good="#1a7f4b",
    warn="#9a6206",
    bad="#c62f26",
    rec="#c62f26",
    error_bg="#fdeceb",
    tx="#1f6feb",
    rx="#1a7f4b",
)

PALETTES = {p.name: p for p in (DARK, LIGHT)}

#: The palette currently in force. Read this rather than capturing colours.
ACTIVE: Palette = DARK


def set_active(palette: Palette) -> None:
    global ACTIVE
    ACTIVE = palette


def build_stylesheet(p: Palette) -> str:
    """The whole application stylesheet, for one palette."""
    return f"""
QWidget {{
    background: {p.bg};
    color: {p.text};
    font-size: 13px;
}}

QMainWindow, QDialog {{ background: {p.bg}; }}

QLabel {{ background: transparent; }}
QLabel[role="heading"] {{
    font-size: 11px;
    font-weight: 600;
    color: {p.text_faint};
    letter-spacing: 1px;
}}
QLabel[role="dim"] {{ color: {p.text_dim}; }}
QLabel[role="warn"] {{ color: {p.warn}; }}
QLabel[role="value"] {{ font-family: {MONO}; font-size: 14px; }}
QLabel[role="bigvalue"] {{
    font-family: {MONO};
    font-size: 28px;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="transport"] {{ font-size: 18px; font-weight: 600; }}
/* Transport state colours the label without touching its metrics, so the
   text never shifts as the deck changes mode. */
QLabel[role="transport"][state="idle"]  {{ color: {p.text_dim}; }}
QLabel[role="transport"][state="stop"]  {{ color: {p.text}; }}
QLabel[role="transport"][state="play"]  {{ color: {p.accent}; }}
QLabel[role="transport"][state="rec"]   {{ color: {p.rec}; }}

QLabel[role="led"] {{ font-size: 16px; }}
QLabel[role="led"][state="off"]  {{ color: {p.text_faint}; }}
QLabel[role="led"][state="on"]   {{ color: {p.good}; }}
QLabel[role="led"][state="busy"] {{ color: {p.warn}; }}
QLabel[role="led"][state="bad"]  {{ color: {p.bad}; }}

QFrame[role="card"] {{
    background: {p.bg_raised};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QFrame[role="separator"] {{ background: {p.border}; max-height: 1px; border: none; }}

QWidget[role="banner"] {{ background: transparent; border: none; }}
QWidget[role="banner"][state="error"] {{
    background: {p.error_bg};
    border: 1px solid {p.bad};
    border-radius: 7px;
}}
QLabel[role="banner-text"] {{ color: {p.bad}; font-weight: 600; }}

QPushButton {{
    background: {p.bg_raised};
    border: 1px solid {p.border_strong};
    border-radius: 7px;
    padding: 7px 12px;
    color: {p.text};
}}
QPushButton:hover:enabled {{ background: {p.hover}; border-color: {p.accent}; }}
QPushButton:pressed:enabled {{ background: {p.accent_bg}; color: {p.accent_fg}; }}
QPushButton:disabled {{ color: {p.text_faint}; border-color: {p.border}; }}
QPushButton:focus {{ border-color: {p.accent}; }}
QPushButton:checked {{
    background: {p.accent_bg};
    border-color: {p.accent};
    color: {p.accent_fg};
}}
QPushButton[role="transport"] {{ font-size: 16px; min-height: 34px; padding: 2px; }}
QPushButton[role="compact"] {{ padding: 4px 8px; font-size: 12px; }}
QPushButton[role="primary"] {{
    background: {p.accent_bg};
    border-color: {p.accent};
    color: {p.accent_fg};
    font-weight: 600;
}}
QPushButton[role="primary"]:hover:enabled {{ background: {p.accent}; }}
QPushButton[role="danger"] {{ border-color: {p.bad}; color: {p.bad}; }}
QPushButton[role="danger"]:hover:enabled {{
    background: {p.error_bg}; border-color: {p.bad};
}}
QPushButton[role="record"] {{
    border-color: {p.rec};
    color: {p.rec};
    font-weight: 600;
    min-height: 38px;
}}
QPushButton[role="record"]:hover:enabled {{ background: {p.error_bg}; }}
QPushButton[role="deck"] {{ font-size: 14px; font-weight: 600; min-height: 30px; }}

QComboBox, QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {p.bg_input};
    border: 1px solid {p.border_strong};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {p.accent_bg};
    selection-color: {p.accent_fg};
}}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {p.accent}; }}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled {{ color: {p.text_faint}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {p.bg_raised};
    border: 1px solid {p.border_strong};
    selection-background-color: {p.accent_bg};
    selection-color: {p.accent_fg};
    outline: none;
}}

QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 10px;
    top: -1px;
    background: {p.bg_raised};
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    padding: 8px 14px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QTabBar::tab:hover {{ color: {p.text}; }}
QTabBar::tab:selected {{
    background: {p.bg_raised};
    color: {p.text};
    border-color: {p.border};
    border-bottom-color: {p.bg_raised};
}}

QTableWidget, QListWidget, QTreeWidget {{
    background: {p.bg_input};
    border: 1px solid {p.border};
    border-radius: 8px;
    gridline-color: {p.border};
    outline: none;
}}
QTableWidget::item, QListWidget::item {{ padding: 3px 6px; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {p.accent_bg};
    color: {p.accent_fg};
}}
QHeaderView::section {{
    background: {p.bg_raised};
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 6px;
    font-weight: 600;
}}

QGroupBox {{
    border: 1px solid {p.border};
    border-radius: 9px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: {p.text_faint};
    font-size: 11px;
    letter-spacing: 1px;
}}

QSlider::groove:horizontal {{
    height: 5px;
    background: {p.border};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {p.accent_bg}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {p.accent};
    width: 15px;
    margin: -6px 0;
    border-radius: 7px;
}}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p.border_strong};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; }}
QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    border-radius: 5px;
    min-width: 30px;
}}

QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {p.border_strong};
    border-radius: 4px;
    background: {p.bg_input};
}}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QCheckBox:disabled {{ color: {p.text_faint}; }}

QMenuBar {{
    background: {p.bg_raised};
    border-bottom: 1px solid {p.border};
    padding: 2px;
}}
QMenuBar::item {{ background: transparent; padding: 5px 11px; border-radius: 5px; }}
QMenuBar::item:selected {{ background: {p.accent_bg}; color: {p.accent_fg}; }}
QMenu {{
    background: {p.bg_raised};
    border: 1px solid {p.border_strong};
    border-radius: 7px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {p.accent_bg}; color: {p.accent_fg}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}

QStatusBar {{
    background: {p.bg_raised};
    border-top: 1px solid {p.border};
    color: {p.text_dim};
}}
QToolTip {{
    background: {p.bg_raised};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 5px;
    border-radius: 5px;
}}
QSplitter::handle {{ background: transparent; width: 8px; height: 8px; }}
"""


#: Convenience for the default theme at import time.
STYLESHEET = build_stylesheet(DARK)
