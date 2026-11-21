"""Íconos vectoriales nítidos dibujados con QPainter según el color del tema."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


@lru_cache(maxsize=160)
def render_icon(name: str, color: str = "#EFEFF1", size: int = 20) -> QIcon:
    # Renderizar a 2x para HiDPI nítido
    px_size = size * 2
    pm = QPixmap(px_size, px_size)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(px_size)
    c = QColor(color)
    pen = QPen(c, max(2.2, s * 0.085), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if name == "play":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(s * 0.30, s * 0.20)
        path.lineTo(s * 0.80, s * 0.50)
        path.lineTo(s * 0.30, s * 0.80)
        path.closeSubpath()
        p.drawPath(path)

    elif name == "pause":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        w = s * 0.17
        h = s * 0.56
        y = s * 0.22
        p.drawRoundedRect(QRectF(s * 0.27, y, w, h), 3, 3)
        p.drawRoundedRect(QRectF(s * 0.56, y, w, h), 3, 3)

    elif name == "next":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(s * 0.22, s * 0.24)
        path.lineTo(s * 0.62, s * 0.50)
        path.lineTo(s * 0.22, s * 0.76)
        path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(s * 0.68, s * 0.24, s * 0.11, s * 0.52), 2, 2)

    elif name == "prev":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.21, s * 0.24, s * 0.11, s * 0.52), 2, 2)
        path = QPainterPath()
        path.moveTo(s * 0.78, s * 0.24)
        path.lineTo(s * 0.38, s * 0.50)
        path.lineTo(s * 0.78, s * 0.76)
        path.closeSubpath()
        p.drawPath(path)

    elif name == "search":
        p.drawEllipse(QRectF(s * 0.18, s * 0.18, s * 0.48, s * 0.48))
        p.drawLine(QPointF(s * 0.60, s * 0.60), QPointF(s * 0.82, s * 0.82))

    elif name == "video":
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.24, s * 0.72, s * 0.52), s * 0.12, s * 0.12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(s * 0.43, s * 0.37)
        path.lineTo(s * 0.63, s * 0.50)
        path.lineTo(s * 0.43, s * 0.63)
        path.closeSubpath()
        p.drawPath(path)

    elif name == "music":
        p.drawLine(QPointF(s * 0.38, s * 0.68), QPointF(s * 0.38, s * 0.24))
        p.drawLine(QPointF(s * 0.38, s * 0.24), QPointF(s * 0.76, s * 0.18))
        p.drawLine(QPointF(s * 0.76, s * 0.18), QPointF(s * 0.76, s * 0.62))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(s * 0.20, s * 0.60, s * 0.22, s * 0.19))
        p.drawEllipse(QRectF(s * 0.58, s * 0.54, s * 0.22, s * 0.19))

    elif name in ("bookmark", "bookmark_filled"):
        path = QPainterPath()
        path.moveTo(s * 0.28, s * 0.18)
        path.lineTo(s * 0.72, s * 0.18)
        path.lineTo(s * 0.72, s * 0.82)
        path.lineTo(s * 0.50, s * 0.65)
        path.lineTo(s * 0.28, s * 0.82)
        path.closeSubpath()
        if name == "bookmark_filled":
            p.setBrush(c)
        p.drawPath(path)

    elif name in ("heart", "heart_filled"):
        path = QPainterPath()
        path.moveTo(s * 0.50, s * 0.78)
        path.cubicTo(s * 0.16, s * 0.56, s * 0.14, s * 0.32, s * 0.32, s * 0.24)
        path.cubicTo(s * 0.42, s * 0.20, s * 0.48, s * 0.27, s * 0.50, s * 0.33)
        path.cubicTo(s * 0.52, s * 0.27, s * 0.58, s * 0.20, s * 0.68, s * 0.24)
        path.cubicTo(s * 0.86, s * 0.32, s * 0.84, s * 0.56, s * 0.50, s * 0.78)
        if name == "heart_filled":
            p.setBrush(c)
        p.drawPath(path)

    elif name == "download":
        p.drawLine(QPointF(s * 0.50, s * 0.18), QPointF(s * 0.50, s * 0.64))
        p.drawLine(QPointF(s * 0.33, s * 0.48), QPointF(s * 0.50, s * 0.65))
        p.drawLine(QPointF(s * 0.67, s * 0.48), QPointF(s * 0.50, s * 0.65))
        p.drawLine(QPointF(s * 0.22, s * 0.78), QPointF(s * 0.78, s * 0.78))

    elif name == "queue":
        for y in (0.28, 0.50, 0.72):
            p.drawLine(QPointF(s * 0.20, s * y), QPointF(s * 0.80, s * y))

    elif name == "audio_only":
        # Auriculares
        p.drawArc(QRectF(s * 0.20, s * 0.20, s * 0.60, s * 0.58), 0, 180 * 16)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.50, s * 0.16, s * 0.26), 4, 4)
        p.drawRoundedRect(QRectF(s * 0.68, s * 0.50, s * 0.16, s * 0.26), 4, 4)

    elif name == "fullscreen":
        # 4 esquinas hacia afuera
        p.drawLine(QPointF(s * 0.20, s * 0.36), QPointF(s * 0.20, s * 0.20))
        p.drawLine(QPointF(s * 0.20, s * 0.20), QPointF(s * 0.36, s * 0.20))
        p.drawLine(QPointF(s * 0.64, s * 0.20), QPointF(s * 0.80, s * 0.20))
        p.drawLine(QPointF(s * 0.80, s * 0.20), QPointF(s * 0.80, s * 0.36))
        p.drawLine(QPointF(s * 0.80, s * 0.64), QPointF(s * 0.80, s * 0.80))
        p.drawLine(QPointF(s * 0.80, s * 0.80), QPointF(s * 0.64, s * 0.80))
        p.drawLine(QPointF(s * 0.36, s * 0.80), QPointF(s * 0.20, s * 0.80))
        p.drawLine(QPointF(s * 0.20, s * 0.80), QPointF(s * 0.20, s * 0.64))

    elif name == "exit_fullscreen":
        p.drawLine(QPointF(s * 0.36, s * 0.20), QPointF(s * 0.36, s * 0.36))
        p.drawLine(QPointF(s * 0.36, s * 0.36), QPointF(s * 0.20, s * 0.36))
        p.drawLine(QPointF(s * 0.64, s * 0.20), QPointF(s * 0.64, s * 0.36))
        p.drawLine(QPointF(s * 0.64, s * 0.36), QPointF(s * 0.80, s * 0.36))
        p.drawLine(QPointF(s * 0.64, s * 0.80), QPointF(s * 0.64, s * 0.64))
        p.drawLine(QPointF(s * 0.64, s * 0.64), QPointF(s * 0.80, s * 0.64))
        p.drawLine(QPointF(s * 0.36, s * 0.80), QPointF(s * 0.36, s * 0.64))
        p.drawLine(QPointF(s * 0.36, s * 0.64), QPointF(s * 0.20, s * 0.64))

    elif name == "mini_bar":
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.20, s * 0.68, s * 0.60), 4, 4)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.42, s * 0.50, s * 0.34, s * 0.22), 2, 2)

    elif name == "settings":
        p.drawEllipse(QRectF(s * 0.36, s * 0.36, s * 0.28, s * 0.28))
        for dx, dy in (
            (0.50, 0.16),
            (0.50, 0.84),
            (0.16, 0.50),
            (0.84, 0.50),
            (0.26, 0.26),
            (0.74, 0.74),
            (0.74, 0.26),
            (0.26, 0.74),
        ):
            p.drawPoint(QPointF(s * dx, s * dy))

    elif name == "volume":
        path = QPainterPath()
        path.moveTo(s * 0.18, s * 0.40)
        path.lineTo(s * 0.34, s * 0.40)
        path.lineTo(s * 0.52, s * 0.24)
        path.lineTo(s * 0.52, s * 0.76)
        path.lineTo(s * 0.34, s * 0.60)
        path.lineTo(s * 0.18, s * 0.60)
        path.closeSubpath()
        p.setBrush(c)
        p.drawPath(path)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(s * 0.54, s * 0.34, s * 0.18, s * 0.32), -45 * 16, 90 * 16)
        p.drawArc(QRectF(s * 0.60, s * 0.24, s * 0.24, s * 0.52), -45 * 16, 90 * 16)

    elif name == "mute":
        path = QPainterPath()
        path.moveTo(s * 0.18, s * 0.40)
        path.lineTo(s * 0.34, s * 0.40)
        path.lineTo(s * 0.52, s * 0.24)
        path.lineTo(s * 0.52, s * 0.76)
        path.lineTo(s * 0.34, s * 0.60)
        path.lineTo(s * 0.18, s * 0.60)
        path.closeSubpath()
        p.setBrush(c)
        p.drawPath(path)
        p.drawLine(QPointF(s * 0.64, s * 0.38), QPointF(s * 0.84, s * 0.62))
        p.drawLine(QPointF(s * 0.84, s * 0.38), QPointF(s * 0.64, s * 0.62))

    elif name == "shuffle":
        p.drawLine(QPointF(s * 0.18, s * 0.32), QPointF(s * 0.42, s * 0.32))
        p.drawLine(QPointF(s * 0.42, s * 0.32), QPointF(s * 0.62, s * 0.68))
        p.drawLine(QPointF(s * 0.62, s * 0.68), QPointF(s * 0.82, s * 0.68))
        p.drawLine(QPointF(s * 0.18, s * 0.68), QPointF(s * 0.42, s * 0.68))
        p.drawLine(QPointF(s * 0.42, s * 0.68), QPointF(s * 0.62, s * 0.32))
        p.drawLine(QPointF(s * 0.62, s * 0.32), QPointF(s * 0.82, s * 0.32))

    elif name == "trash":
        p.drawLine(QPointF(s * 0.22, s * 0.28), QPointF(s * 0.78, s * 0.28))
        p.drawLine(QPointF(s * 0.40, s * 0.18), QPointF(s * 0.60, s * 0.18))
        p.drawRoundedRect(QRectF(s * 0.28, s * 0.28, s * 0.44, s * 0.54), 3, 3)

    elif name == "folder":
        path = QPainterPath()
        path.moveTo(s * 0.16, s * 0.26)
        path.lineTo(s * 0.42, s * 0.26)
        path.lineTo(s * 0.50, s * 0.34)
        path.lineTo(s * 0.84, s * 0.34)
        path.lineTo(s * 0.84, s * 0.76)
        path.lineTo(s * 0.16, s * 0.76)
        path.closeSubpath()
        p.drawPath(path)

    elif name == "check":
        p.drawLine(QPointF(s * 0.22, s * 0.52), QPointF(s * 0.42, s * 0.72))
        p.drawLine(QPointF(s * 0.42, s * 0.72), QPointF(s * 0.78, s * 0.30))

    elif name == "close":
        p.drawLine(QPointF(s * 0.24, s * 0.24), QPointF(s * 0.76, s * 0.76))
        p.drawLine(QPointF(s * 0.76, s * 0.24), QPointF(s * 0.24, s * 0.76))

    elif name == "back":
        p.drawLine(QPointF(s * 0.76, s * 0.50), QPointF(s * 0.24, s * 0.50))
        p.drawLine(QPointF(s * 0.46, s * 0.28), QPointF(s * 0.24, s * 0.50))
        p.drawLine(QPointF(s * 0.46, s * 0.72), QPointF(s * 0.24, s * 0.50))

    elif name == "pin":
        p.drawLine(QPointF(s * 0.50, s * 0.56), QPointF(s * 0.50, s * 0.84))
        p.drawRoundedRect(QRectF(s * 0.30, s * 0.20, s * 0.40, s * 0.36), 4, 4)

    elif name == "plus":
        p.drawLine(QPointF(s * 0.50, s * 0.22), QPointF(s * 0.50, s * 0.78))
        p.drawLine(QPointF(s * 0.22, s * 0.50), QPointF(s * 0.78, s * 0.50))

    elif name == "refresh":
        p.drawArc(QRectF(s * 0.20, s * 0.20, s * 0.60, s * 0.60), 40 * 16, 280 * 16)
        p.drawLine(QPointF(s * 0.68, s * 0.20), QPointF(s * 0.78, s * 0.34))
        p.drawLine(QPointF(s * 0.62, s * 0.36), QPointF(s * 0.78, s * 0.34))

    elif name == "user":
        p.drawEllipse(QRectF(s * 0.34, s * 0.18, s * 0.32, s * 0.32))
        p.drawArc(QRectF(s * 0.20, s * 0.54, s * 0.60, s * 0.44), 10 * 16, 160 * 16)

    elif name == "help":
        p.drawEllipse(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68))
        p.drawArc(QRectF(s * 0.37, s * 0.30, s * 0.26, s * 0.22), 180 * 16, -210 * 16)
        p.drawLine(QPointF(s * 0.50, s * 0.51), QPointF(s * 0.50, s * 0.60))
        p.drawPoint(QPointF(s * 0.50, s * 0.72))

    elif name == "sidebar":
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.20, s * 0.72, s * 0.60), 4, 4)
        p.drawLine(QPointF(s * 0.38, s * 0.20), QPointF(s * 0.38, s * 0.80))

    elif name == "theater":
        p.drawRoundedRect(QRectF(s * 0.12, s * 0.24, s * 0.76, s * 0.52), 4, 4)
        p.drawLine(QPointF(s * 0.24, s * 0.64), QPointF(s * 0.76, s * 0.64))

    elif name == "history":
        p.drawArc(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), 135 * 16, -280 * 16)
        p.drawLine(QPointF(s * 0.18, s * 0.22), QPointF(s * 0.18, s * 0.40))
        p.drawLine(QPointF(s * 0.18, s * 0.40), QPointF(s * 0.36, s * 0.40))
        p.drawLine(QPointF(s * 0.50, s * 0.50), QPointF(s * 0.50, s * 0.33))
        p.drawLine(QPointF(s * 0.50, s * 0.50), QPointF(s * 0.64, s * 0.56))

    elif name == "more":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        r = s * 0.068
        for y in (0.26, 0.50, 0.74):
            p.drawEllipse(QPointF(s * 0.50, s * y), r, r)

    elif name == "chevron_left":
        p.drawLine(QPointF(s * 0.62, s * 0.22), QPointF(s * 0.34, s * 0.50))
        p.drawLine(QPointF(s * 0.34, s * 0.50), QPointF(s * 0.62, s * 0.78))

    elif name == "chevron_right":
        p.drawLine(QPointF(s * 0.38, s * 0.22), QPointF(s * 0.66, s * 0.50))
        p.drawLine(QPointF(s * 0.66, s * 0.50), QPointF(s * 0.38, s * 0.78))

    elif name == "chevron_down":
        p.drawLine(QPointF(s * 0.22, s * 0.38), QPointF(s * 0.50, s * 0.66))
        p.drawLine(QPointF(s * 0.50, s * 0.66), QPointF(s * 0.78, s * 0.38))

    elif name == "live":
        p.drawArc(QRectF(s * 0.14, s * 0.18, s * 0.72, s * 0.64), 135 * 16, 90 * 16)
        p.drawArc(QRectF(s * 0.14, s * 0.18, s * 0.72, s * 0.64), -45 * 16, 90 * 16)
        p.drawArc(QRectF(s * 0.28, s * 0.30, s * 0.44, s * 0.40), 135 * 16, 90 * 16)
        p.drawArc(QRectF(s * 0.28, s * 0.30, s * 0.44, s * 0.40), -45 * 16, 90 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(s * 0.41, s * 0.41, s * 0.18, s * 0.18))

    elif name == "discord":
        # Auriculares con micrófono integrado (voz en tiempo real)
        p.drawArc(QRectF(s * 0.18, s * 0.16, s * 0.64, s * 0.60), 0, 180 * 16)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.15, s * 0.46, s * 0.15, s * 0.24), 4, 4)
        p.drawRoundedRect(QRectF(s * 0.70, s * 0.46, s * 0.15, s * 0.24), 4, 4)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(s * 0.42, s * 0.56, s * 0.36, s * 0.26), -90 * 16, -90 * 16)
        p.setBrush(c)
        p.drawEllipse(QRectF(s * 0.41, s * 0.77, s * 0.12, s * 0.10))

    elif name in ("mic", "mic_off"):
        p.drawRoundedRect(QRectF(s * 0.38, s * 0.16, s * 0.24, s * 0.38), s * 0.12, s * 0.12)
        p.drawArc(QRectF(s * 0.26, s * 0.30, s * 0.48, s * 0.38), 180 * 16, 180 * 16)
        p.drawLine(QPointF(s * 0.50, s * 0.68), QPointF(s * 0.50, s * 0.82))
        p.drawLine(QPointF(s * 0.35, s * 0.82), QPointF(s * 0.65, s * 0.82))
        if name == "mic_off":
            p.drawLine(QPointF(s * 0.20, s * 0.20), QPointF(s * 0.80, s * 0.80))

    elif name in ("headphones", "headphones_off"):
        p.drawArc(QRectF(s * 0.20, s * 0.20, s * 0.60, s * 0.58), 0, 180 * 16)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.50, s * 0.16, s * 0.26), 4, 4)
        p.drawRoundedRect(QRectF(s * 0.68, s * 0.50, s * 0.16, s * 0.26), 4, 4)
        if name == "headphones_off":
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(s * 0.20, s * 0.20), QPointF(s * 0.80, s * 0.80))

    elif name == "phone_off":
        p.drawArc(QRectF(s * 0.14, s * 0.40, s * 0.72, s * 0.34), 20 * 16, 140 * 16)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.15, s * 0.52, s * 0.16, s * 0.15), 3, 3)
        p.drawRoundedRect(QRectF(s * 0.69, s * 0.52, s * 0.16, s * 0.15), 3, 3)

    elif name == "shield":
        path = QPainterPath()
        path.moveTo(s * 0.50, s * 0.15)
        path.lineTo(s * 0.80, s * 0.26)
        path.lineTo(s * 0.78, s * 0.54)
        path.quadTo(s * 0.70, s * 0.76, s * 0.50, s * 0.85)
        path.quadTo(s * 0.30, s * 0.76, s * 0.22, s * 0.54)
        path.lineTo(s * 0.20, s * 0.26)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(s * 0.37, s * 0.50), QPointF(s * 0.47, s * 0.60))
        p.drawLine(QPointF(s * 0.47, s * 0.60), QPointF(s * 0.65, s * 0.40))

    p.end()
    return QIcon(pm)
