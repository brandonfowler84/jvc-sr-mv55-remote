"""Transport icons, drawn rather than typed.

Unicode has glyphs for all of these -- ▶ ■ ⏏ and friends -- but relying on them
looks wrong in practice: each comes from whichever installed font happens to
carry it, so they arrive at different weights, sizes and baselines, and some
(the eject symbol especially) get claimed by the emoji font and render in
colour. Drawing them gives one consistent set that also takes the current
theme's colour and scales cleanly.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

#: Nominal drawing box. Shapes are defined in 0..1 and scaled to the pixmap.
_PAD = 0.0


def _tri(painter: QPainter, size: float, points) -> None:
    painter.drawPolygon(QPolygonF([QPointF(x * size, y * size) for x, y in points]))


def _rect(painter: QPainter, size: float, x, y, w, h, radius=0.04) -> None:
    painter.drawRoundedRect(
        QRectF(x * size, y * size, w * size, h * size),
        radius * size, radius * size,
    )


def _play(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.30, 0.18), (0.30, 0.82), (0.80, 0.50)])


def _stop(p: QPainter, s: float) -> None:
    _rect(p, s, 0.26, 0.26, 0.48, 0.48, radius=0.05)


def _pause(p: QPainter, s: float) -> None:
    _rect(p, s, 0.30, 0.20, 0.13, 0.60, radius=0.04)
    _rect(p, s, 0.57, 0.20, 0.13, 0.60, radius=0.04)


def _fast_forward(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.12, 0.24), (0.12, 0.76), (0.49, 0.50)])
    _tri(p, s, [(0.51, 0.24), (0.51, 0.76), (0.88, 0.50)])


def _rewind(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.88, 0.24), (0.88, 0.76), (0.51, 0.50)])
    _tri(p, s, [(0.49, 0.24), (0.49, 0.76), (0.12, 0.50)])


def _step_forward(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.20, 0.22), (0.20, 0.78), (0.63, 0.50)])
    _rect(p, s, 0.68, 0.22, 0.12, 0.56)


def _step_back(p: QPainter, s: float) -> None:
    _rect(p, s, 0.20, 0.22, 0.12, 0.56)
    _tri(p, s, [(0.80, 0.22), (0.80, 0.78), (0.37, 0.50)])


def _eject(p: QPainter, s: float) -> None:
    # A triangle over a bar: the standard eject mark, but drawn so it matches
    # the weight of the other icons instead of arriving from the emoji font.
    _tri(p, s, [(0.50, 0.20), (0.18, 0.58), (0.82, 0.58)])
    _rect(p, s, 0.18, 0.66, 0.64, 0.14, radius=0.04)


def _record(p: QPainter, s: float) -> None:
    p.drawEllipse(QRectF(0.26 * s, 0.26 * s, 0.48 * s, 0.48 * s))


def _up(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.50, 0.26), (0.22, 0.70), (0.78, 0.70)])


def _down(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.50, 0.74), (0.22, 0.30), (0.78, 0.30)])


def _left(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.26, 0.50), (0.70, 0.22), (0.70, 0.78)])


def _right(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.74, 0.50), (0.30, 0.22), (0.30, 0.78)])


def _skip_forward(p: QPainter, s: float) -> None:
    _tri(p, s, [(0.14, 0.24), (0.14, 0.76), (0.56, 0.50)])
    _rect(p, s, 0.62, 0.24, 0.12, 0.52)


def _skip_back(p: QPainter, s: float) -> None:
    _rect(p, s, 0.26, 0.24, 0.12, 0.52)
    _tri(p, s, [(0.86, 0.24), (0.86, 0.76), (0.44, 0.50)])


def _curved_arrow(p: QPainter, s: float, head_right: bool) -> None:
    """An arc over the top with the arrowhead dropping off one end.

    The remote's Instant Replay and CM Skip keys: back and forward jumps.
    Drawn with a stroke rather than a fill, so it borrows the brush colour
    for the pen and puts the brush back for the head.
    """
    colour = p.brush().color()
    p.setPen(QPen(colour, 0.10 * s))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(0.22 * s, 0.30 * s, 0.56 * s, 0.50 * s), 0, 180 * 16)
    p.setPen(Qt.NoPen)
    p.setBrush(colour)
    x = 0.78 if head_right else 0.22
    _tri(p, s, [(x - 0.13, 0.50), (x + 0.13, 0.50), (x, 0.74)])


def _replay(p: QPainter, s: float) -> None:
    _curved_arrow(p, s, head_right=False)


def _skip_ahead(p: QPainter, s: float) -> None:
    _curved_arrow(p, s, head_right=True)


SHAPES = {
    "replay": _replay,
    "skip_ahead": _skip_ahead,
    "play": _play,
    "stop": _stop,
    "pause": _pause,
    "ff": _fast_forward,
    "rew": _rewind,
    "step_fwd": _step_forward,
    "step_rev": _step_back,
    "eject": _eject,
    "record": _record,
    "up": _up,
    "down": _down,
    "left": _left,
    "right": _right,
    "skip_fwd": _skip_forward,
    "skip_rev": _skip_back,
}


def icon(name: str, colour: str, size: int = 18) -> QIcon:
    """Draw one icon in the given colour.

    Rendered at 2x and handed to Qt as a high-DPI pixmap so it stays crisp on
    scaled displays.
    """
    shape = SHAPES.get(name)
    if shape is None:
        return QIcon()

    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(colour))
    shape(painter, float(size * scale))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return QIcon(pixmap)
