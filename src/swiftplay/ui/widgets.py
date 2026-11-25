"""Widgets reutilizables estilo YouTube / YT Music sin cards, grillas y sliders."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from swiftplay.models import MediaItem, MusicAlbumInfo, format_duration, upgrade_thumb_url
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon


class LoadingShimmerBar(QWidget):
    """Barra fina animada para indicar carga asíncrona sin bloquear la UI."""

    def __init__(self, accent: str = "#8B5CF6", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._accent = QColor(accent)
        self._pos = -0.35
        self._active = False
        self.setFixedHeight(3)
        self.setVisible(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(-0.35)
        self._anim.setEndValue(1.35)
        self._anim.setDuration(1050)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_step)

    def _on_step(self, val: object) -> None:
        self._pos = float(val) if isinstance(val, (int, float)) else 0.0
        self.update()

    def set_accent(self, accent: str) -> None:
        self._accent = QColor(accent)
        self.update()

    def set_active(self, active: bool) -> None:
        want = bool(active)
        if want == self._active:
            return
        self._active = want
        if want:
            self.setVisible(True)
            if self._anim.state() != QVariantAnimation.State.Running:
                self._anim.start()
        else:
            self._anim.stop()
            self.setVisible(False)

    def start(self) -> None:
        self.set_active(True)

    def stop(self) -> None:
        self.set_active(False)

    def hideEvent(self, event) -> None:
        if self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._active and self._anim.state() != QVariantAnimation.State.Running:
            self._anim.start()

    def paintEvent(self, _event) -> None:
        if not self.isVisible():
            return
        w = float(max(1, self.width()))
        h = float(max(1, self.height()))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor(255, 255, 255, 16))
        bar_w = max(48.0, w * 0.34)
        cx = self._pos * w
        x0 = cx - bar_w * 0.5
        x1 = cx + bar_w * 0.5
        grad = QLinearGradient(x0, 0.0, x1, 0.0)
        edge = QColor(self._accent)
        edge.setAlpha(0)
        mid = QColor(self._accent)
        mid.setAlpha(235)
        grad.setColorAt(0.0, edge)
        grad.setColorAt(0.5, mid)
        grad.setColorAt(1.0, edge)
        p.fillRect(QRectF(max(0.0, x0), 0.0, min(w, x1) - max(0.0, x0), h), grad)
        p.end()


class ClickableSlider(QSlider):
    """QSlider que salta al punto de clic y opcionalmente ajusta volumen (±5% / Shift ±1%)."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        volume_wheel: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._volume_wheel = bool(volume_wheel)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._volume_wheel:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta != 0:
                step = 1 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 5
                new_val = self.value() + (step if delta > 0 else -step)
                self.setValue(max(self.minimum(), min(self.maximum(), new_val)))
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                opt,
                QStyle.SubControl.SC_SliderGroove,
                self,
            )
            if self.orientation() == Qt.Orientation.Horizontal and groove.width() > 0:
                pos = event.position().x() - groove.x()
                ratio = max(0.0, min(1.0, pos / groove.width()))
                val = self.minimum() + round(ratio * (self.maximum() - self.minimum()))
                self.setValue(val)
                self.sliderMoved.emit(val)
        super().mousePressEvent(event)


class SectionTitleWidget(QLabel):
    """Encabezado limpio para secciones del feed de YouTube Music."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setStyleSheet(
            "font-size: 16px; font-weight: 700; padding-top: 8px; padding-bottom: 4px;"
        )


class ElidedLabel(QLabel):
    """Etiqueta que recorta con '...' al ancho disponible sin forzar scroll horizontal."""

    def __init__(
        self, text: str = "", max_lines: int = 1, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._full_text = str(text or "")
        self._max_lines = max(1, int(max_lines))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        if self._full_text:
            self.setToolTip(self._full_text)

    def text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self.update()

    def minimumSizeHint(self) -> QSize:
        self.ensurePolished()
        fm = self.fontMetrics()
        h = (fm.lineSpacing() * 2 + 2) if self._max_lines >= 2 else (fm.height() + 2)
        return QSize(0, h)

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        fm = self.fontMetrics()
        if self._max_lines >= 2:
            return QSize(180, fm.lineSpacing() * 2 + 2)
        return QSize(120, fm.height() + 2)

    def paintEvent(self, _event) -> None:
        w = max(0, self.width())
        h = max(0, self.height())
        if w <= 0 or h <= 0 or not self._full_text:
            return

        painter = QPainter(self)
        painter.setFont(self.font())
        fm = QFontMetrics(self.font())
        align_h = self.alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask
        if not align_h:
            align_h = Qt.AlignmentFlag.AlignLeft

        if self._max_lines == 1:
            elided = fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, w)
            self.style().drawItemText(
                painter,
                self.rect(),
                int(align_h | Qt.AlignmentFlag.AlignVCenter),
                self.palette(),
                self.isEnabled(),
                elided,
                QPalette.ColorRole.WindowText,
            )
            painter.end()
            return

        layout = QTextLayout(self._full_text, self.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)
        layout.beginLayout()
        line1 = layout.createLine()
        if line1.isValid():
            line1.setLineWidth(float(w))
        line2 = layout.createLine()
        layout.endLayout()

        line_h = fm.lineSpacing()
        has_second = line2.isValid() and line1.textLength() < len(self._full_text)
        y0 = 0

        if line1.isValid():
            start1 = line1.textStart()
            end1 = start1 + line1.textLength()
            t1 = self._full_text[start1:end1].rstrip("\n")
            self.style().drawItemText(
                painter,
                QRect(0, y0, w, line_h),
                int(align_h | Qt.AlignmentFlag.AlignTop),
                self.palette(),
                self.isEnabled(),
                t1,
                QPalette.ColorRole.WindowText,
            )
            if has_second:
                rest = self._full_text[end1:].lstrip()
                t2 = fm.elidedText(rest, Qt.TextElideMode.ElideRight, w)
                self.style().drawItemText(
                    painter,
                    QRect(0, y0 + line_h, w, max(line_h, h - y0 - line_h)),
                    int(align_h | Qt.AlignmentFlag.AlignTop),
                    self.palette(),
                    self.isEnabled(),
                    t2,
                    QPalette.ColorRole.WindowText,
                )
        painter.end()


class RoundedThumbWidget(QWidget):
    """Miniatura con esquinas redondeadas, badge, progreso, hover zoom, fade-in y crossfade."""

    def __init__(
        self,
        radius: int = 8,
        duration_text: str = "",
        progress_ratio: float = 0.0,
        show_play_on_hover: bool = False,
        force_play_overlay: bool = False,
        bg_color: str = "#121418",
        enable_hover_zoom: bool = False,
        enable_crossfade: bool = False,
        enable_fade_in: bool = True,
        ambient_shadow: bool = False,
        accent_color: str = "#CFD5DF",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radius = max(0, int(radius))
        self._duration_text = duration_text.strip()
        self._progress_ratio = max(0.0, min(1.0, float(progress_ratio)))
        self._show_play_on_hover = show_play_on_hover
        self._force_play_overlay = force_play_overlay
        self._bg_color = bg_color
        self._accent_color = accent_color
        self._enable_hover_zoom = bool(enable_hover_zoom)
        self._enable_crossfade = bool(enable_crossfade)
        self._enable_fade_in = bool(enable_fade_in)
        self._ambient_shadow = bool(ambient_shadow)
        self._hovered = False
        self._hover_t = 0.0
        self._hover_anim: QVariantAnimation | None = None
        self._pixmap: QPixmap | None = None
        self._prev_pixmap: QPixmap | None = None
        self._fade_t = 1.0
        self._fade_anim: QVariantAnimation | None = None
        self._appear_t = 1.0
        self._appear_anim: QVariantAnimation | None = None
        self._enter_t = 1.0
        self._enter_anim: QVariantAnimation | None = None
        self._press_t = 0.0
        self._press_anim: QVariantAnimation | None = None
        self._playing_bars = False
        self._bars_phase = 0
        self._bars_timer: QTimer | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def _on_appear_val(self, val: object) -> None:
        self._appear_t = float(val)
        self.update()

    def _on_enter_val(self, val: object) -> None:
        self._enter_t = float(val)
        self.update()

    def play_entrance(self) -> None:
        if self._enter_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(220)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.valueChanged.connect(self._on_enter_val)
            self._enter_anim = anim
        self._enter_t = 0.0
        self._enter_anim.stop()
        self._enter_anim.start()
        self.update()

    def _on_press_val(self, val: object) -> None:
        self._press_t = float(val)
        self.update()

    def trigger_press_feedback(self) -> None:
        if self._press_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(110)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.valueChanged.connect(self._on_press_val)
            self._press_anim = anim
        self._press_t = 1.0
        self._press_anim.stop()
        self._press_anim.start()

    def _on_bars_tick(self) -> None:
        self._bars_phase = (self._bars_phase + 1) % 6
        self.update()

    def set_playing_bars(self, active: bool) -> None:
        active = bool(active)
        if self._playing_bars == active:
            return
        self._playing_bars = active
        if active:
            if self._bars_timer is None:
                timer = QTimer(self)
                timer.setInterval(85)
                timer.timeout.connect(self._on_bars_tick)
                self._bars_timer = timer
            if self.isVisible():
                self._bars_timer.start()
        else:
            if self._bars_timer is not None:
                self._bars_timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._playing_bars and self._bars_timer is not None:
            self._bars_timer.start()

    def hideEvent(self, event) -> None:
        if self._bars_timer is not None:
            self._bars_timer.stop()
        super().hideEvent(event)

    def _on_hover_val(self, val: object) -> None:
        self._hover_t = float(val)
        self.update()

    def _start_hover_anim(self, target: float) -> None:
        if self._hover_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(180)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(self._on_hover_val)
            self._hover_anim = anim
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(float(target))
        self._hover_anim.start()

    def _on_fade_val(self, val: object) -> None:
        self._fade_t = float(val)
        self.update()

    def _on_fade_finished(self) -> None:
        self._prev_pixmap = None
        self._fade_t = 1.0
        self.update()

    def set_pixmap(self, pix: QPixmap | None) -> None:
        if (
            self._enable_crossfade
            and pix is not None
            and not pix.isNull()
            and self._pixmap is not None
            and not self._pixmap.isNull()
            and self._pixmap.cacheKey() != pix.cacheKey()
        ):
            if self._appear_anim is not None:
                self._appear_anim.stop()
            self._appear_t = 1.0
            self._prev_pixmap = self._pixmap
            self._pixmap = pix
            self._fade_t = 0.0
            if self._fade_anim is None:
                anim = QVariantAnimation(self)
                anim.setDuration(220)
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                anim.valueChanged.connect(self._on_fade_val)
                anim.finished.connect(self._on_fade_finished)
                self._fade_anim = anim
            self._fade_anim.stop()
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
            self.update()
            return

        was_empty = self._pixmap is None or self._pixmap.isNull()
        if self._fade_anim is not None:
            self._fade_anim.stop()
        self._prev_pixmap = None
        self._fade_t = 1.0
        self._pixmap = pix

        if (
            was_empty
            and self._enable_fade_in
            and pix is not None
            and not pix.isNull()
        ):
            if self._appear_anim is None:
                a_anim = QVariantAnimation(self)
                a_anim.setDuration(160)
                a_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                a_anim.setStartValue(0.0)
                a_anim.setEndValue(1.0)
                a_anim.valueChanged.connect(self._on_appear_val)
                self._appear_anim = a_anim
            self._appear_t = 0.0
            self._appear_anim.stop()
            self._appear_anim.start()
        else:
            if self._appear_anim is not None:
                self._appear_anim.stop()
            self._appear_t = 1.0
        self.update()

    def clear(self) -> None:
        if self._fade_anim is not None:
            self._fade_anim.stop()
        if self._appear_anim is not None:
            self._appear_anim.stop()
        self._prev_pixmap = None
        self._fade_t = 1.0
        self._appear_t = 1.0
        self._pixmap = None
        self.update()

    def set_duration_text(self, text: str) -> None:
        self._duration_text = (text or "").strip()
        self.update()

    def set_progress_ratio(self, ratio: float) -> None:
        self._progress_ratio = max(0.0, min(1.0, float(ratio)))
        self.update()

    def set_force_play_overlay(self, active: bool) -> None:
        if self._force_play_overlay != bool(active):
            self._force_play_overlay = bool(active)
            self.update()

    def set_bg_color(self, bg_color: str) -> None:
        self._bg_color = bg_color
        self.update()

    def set_accent_color(self, accent_color: str) -> None:
        self._accent_color = accent_color
        self.update()

    def enterEvent(self, event) -> None:
        if self._show_play_on_hover or self._enable_hover_zoom:
            self._hovered = True
            self._start_hover_anim(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._show_play_on_hover or self._enable_hover_zoom:
            self._hovered = False
            self._start_hover_anim(0.0)
        super().leaveEvent(event)

    def _draw_pixmap_cover(
        self, painter: QPainter, pix: QPixmap, w: int, h: int, zoom: float = 1.0
    ) -> None:
        pw = pix.width()
        ph = pix.height()
        if pw <= 0 or ph <= 0:
            return
        if pw == w and ph == h and zoom == 1.0:
            painter.drawPixmap(0, 0, pix)
            return
        scale = max(w / pw, h / ph) * zoom
        dw = pw * scale
        dh = ph * scale
        dx = (w - dw) / 2.0
        dy = (h - dh) / 2.0
        painter.drawPixmap(
            QRectF(dx, dy, dw, dh),
            pix,
            QRectF(0.0, 0.0, float(pw), float(ph)),
        )

    def paintEvent(self, _event) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = QRectF(0.0, 0.0, float(w), float(h))
        clip = QPainterPath()
        clip.addRoundedRect(rect, float(self._radius), float(self._radius))
        painter.setClipPath(clip)

        painter.fillRect(rect, QColor(self._bg_color))

        zoom = (
            1.0 + 0.05 * self._hover_t
            if (self._enable_hover_zoom and self._hover_t > 0.001)
            else 1.0
        )
        if self._enter_t < 1.0:
            zoom *= 0.94 + 0.06 * self._enter_t
        if self._press_t > 0.001:
            zoom *= 1.0 - 0.03 * self._press_t

        old_op = painter.opacity()
        vis_op = (0.35 + 0.65 * self._enter_t) if self._enter_t < 1.0 else 1.0
        if self._appear_t < 1.0:
            vis_op *= self._appear_t
        if vis_op < 0.999:
            painter.setOpacity(old_op * vis_op)

        if (
            self._prev_pixmap is not None
            and not self._prev_pixmap.isNull()
            and self._fade_t < 1.0
        ):
            self._draw_pixmap_cover(painter, self._prev_pixmap, w, h, zoom=1.0)
            if self._pixmap is not None and not self._pixmap.isNull():
                painter.setOpacity(old_op * vis_op * self._fade_t)
                self._draw_pixmap_cover(painter, self._pixmap, w, h, zoom=zoom)
        elif self._pixmap is not None and not self._pixmap.isNull():
            self._draw_pixmap_cover(painter, self._pixmap, w, h, zoom=zoom)
        painter.setOpacity(old_op)

        ov_t = 1.0 if self._force_play_overlay else (
            self._hover_t if (self._show_play_on_hover and self._hover_t > 0.01) else 0.0
        )
        if ov_t > 0.0:
            painter.fillRect(rect, QColor(11, 12, 14, int(125 * ov_t)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, int(255 * ov_t)))
            if self._force_play_overlay and self._playing_bars:
                heights = (
                    (0.45, 0.85, 0.60),
                    (0.75, 0.50, 0.90),
                    (0.90, 0.70, 0.45),
                    (0.55, 0.95, 0.70),
                    (0.40, 0.65, 0.85),
                    (0.80, 0.45, 0.55),
                )[self._bars_phase]
                bw = max(2.5, min(w, h) * 0.09)
                gap = bw * 0.8
                total_w = bw * 3 + gap * 2
                max_h = min(w, h) * 0.44
                x0 = (w - total_w) / 2.0
                base_y = (h + max_h) / 2.0
                for idx_b, ratio_b in enumerate(heights):
                    bh = max(4.0, max_h * ratio_b)
                    bx = x0 + idx_b * (bw + gap)
                    painter.drawRoundedRect(QRectF(bx, base_y - bh, bw, bh), 1.5, 1.5)
            else:
                tri = max(12.0, min(36.0, min(w, h) * 0.24)) * (0.86 + 0.14 * ov_t)
                cx = w / 2.0
                cy = h / 2.0
                play_path = QPainterPath()
                play_path.moveTo(cx - tri * 0.36, cy - tri * 0.50)
                play_path.lineTo(cx + tri * 0.52, cy)
                play_path.lineTo(cx - tri * 0.36, cy + tri * 0.50)
                play_path.closeSubpath()
                painter.drawPath(play_path)

        if self._duration_text:
            is_live_badge = self._duration_text.upper() in ("EN VIVO", "EN DIRECTO", "LIVE")
            is_off_badge = self._duration_text.upper() == "OFFLINE"
            font = QFont(self.font())
            font.setPixelSize(11)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(self._duration_text)
            th = fm.height()
            pad_x = 6
            pad_y = 2
            dot_extra = 10 if is_live_badge else 0
            bw = tw + pad_x * 2 + dot_extra
            bh = th + pad_y * 2
            margin = 7
            y_extra = 4 if self._progress_ratio > 0.0 else 0
            bx = max(4.0, float(w - bw - margin))
            by = max(4.0, float(h - bh - margin - y_extra))
            badge_rect = QRectF(bx, by, float(bw), float(bh))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                QColor(200, 24, 24, 232) if is_live_badge else QColor(11, 12, 14, 218)
            )
            painter.drawRoundedRect(badge_rect, 4.0, 4.0)
            if is_live_badge:
                dot_r = 3.0
                cx = bx + pad_x + dot_r
                cy = by + bh / 2.0
                painter.setBrush(QColor("#FFFFFF"))
                painter.drawEllipse(QRectF(cx - dot_r, cy - dot_r, dot_r * 2.0, dot_r * 2.0))
                txt_rect = QRectF(bx + dot_extra, by, float(bw - dot_extra), float(bh))
            else:
                txt_rect = badge_rect
            painter.setPen(
                QColor("#FFFFFF")
                if is_live_badge
                else QColor("#9299A8" if is_off_badge else "#F1F3F6")
            )
            painter.drawText(txt_rect, Qt.AlignmentFlag.AlignCenter, self._duration_text)

        if self._progress_ratio > 0.0:
            bar_h = 3.0
            bar_y = float(h) - bar_h
            painter.fillRect(QRectF(0.0, bar_y, float(w), bar_h), QColor(255, 255, 255, 58))
            painter.fillRect(
                QRectF(0.0, bar_y, max(4.0, float(w) * self._progress_ratio), bar_h),
                QColor(self._accent_color),
            )

        if self._enable_hover_zoom and self._hover_t > 0.01:
            sheen = QLinearGradient(0.0, 0.0, float(w) * 0.65, float(h) * 0.65)
            s_col = QColor(255, 255, 255, int(26 * self._hover_t))
            s_zero = QColor(255, 255, 255, 0)
            sheen.setColorAt(0.0, s_col)
            sheen.setColorAt(0.55, s_zero)
            painter.fillRect(rect, sheen)

        if self._ambient_shadow:
            painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                rect.adjusted(0.5, 0.5, -0.5, -0.5),
                float(self._radius),
                float(self._radius),
            )
        elif self._enable_hover_zoom and self._hover_t > 0.01:
            glow_pen = QColor(self._accent_color)
            glow_pen.setAlpha(int(175 * self._hover_t))
            painter.setPen(QPen(glow_pen, 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                rect.adjusted(0.7, 0.7, -0.7, -0.7),
                float(self._radius),
                float(self._radius),
            )

        painter.end()


class ResponsiveGridWidget(QWidget):
    """Grilla responsiva multi-columna estilo YouTube que recalcula columnas al redimensionar."""

    def __init__(
        self,
        min_col_width: int = 270,
        max_cols: int = 4,
        spacing: int = 16,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._min_col_width = max(160, int(min_col_width))
        self._max_cols = max(1, int(max_cols))
        self._spacing = max(8, int(spacing))
        self._widgets: list[QWidget] = []
        self._last_cols = 0
        self._in_batch_layout = False

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 4, 4, 16)
        self._grid.setHorizontalSpacing(self._spacing)
        self._grid.setVerticalSpacing(22)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def clear_widgets(self) -> None:
        for w in self._widgets:
            rel = getattr(w, "release_resources", None)
            if callable(rel):
                rel()
            w.hide()
            self._grid.removeWidget(w)
            w.deleteLater()
        self._widgets.clear()
        self._last_cols = 0
        self.updateGeometry()

    def set_items_widgets(
        self, widgets: list[QWidget], hint_width: int = 0
    ) -> None:
        self._in_batch_layout = True
        self.setUpdatesEnabled(False)
        try:
            self.clear_widgets()
            self._widgets = list(widgets)
            self.relayout(force_regrid=True, hint_width=hint_width)
            self._grid.activate()
            self.adjustSize()
        finally:
            self._in_batch_layout = False
            self.setUpdatesEnabled(True)

    def append_widgets(
        self, widgets: list[QWidget], hint_width: int = 0
    ) -> None:
        if not widgets:
            return
        if not self._widgets:
            self.set_items_widgets(widgets, hint_width=hint_width)
            return
        self._in_batch_layout = True
        self.setUpdatesEnabled(False)
        try:
            start_idx = len(self._widgets)
            cols = self._last_cols if self._last_cols > 0 else 1
            m = self._grid.contentsMargins()
            ref_w = max(self.width(), int(hint_width or 0))
            avail_w = max(ref_w, self._min_col_width + m.left() + m.right())
            inner_w = max(180, avail_w - m.left() - m.right() - 2)
            calc_cols = max(
                1,
                min(
                    self._max_cols,
                    (inner_w + self._spacing) // (self._min_col_width + self._spacing),
                ),
            )
            if calc_cols != cols:
                self._widgets.extend(widgets)
                self.relayout(force_regrid=True, hint_width=hint_width)
            else:
                col_w = max(160, (inner_w - self._spacing * (cols - 1)) // cols)
                for offset, w in enumerate(widgets):
                    set_w = getattr(w, "set_tile_width", None)
                    if callable(set_w):
                        set_w(col_w)
                    self._widgets.append(w)
                    r, c = divmod(start_idx + offset, cols)
                    self._grid.addWidget(w, r, c, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self._grid.activate()
            self.adjustSize()
            self.updateGeometry()
        finally:
            self._in_batch_layout = False
            self.setUpdatesEnabled(True)

    def relayout(self, force_regrid: bool = False, hint_width: int = 0) -> None:
        if not self._widgets:
            return
        m = self._grid.contentsMargins()
        ref_w = max(self.width(), int(hint_width or 0))
        avail_w = max(ref_w, self._min_col_width + m.left() + m.right())
        inner_w = max(180, avail_w - m.left() - m.right() - 2)
        cols = max(
            1,
            min(
                self._max_cols,
                (inner_w + self._spacing) // (self._min_col_width + self._spacing),
            ),
        )
        col_w = max(160, (inner_w - self._spacing * (cols - 1)) // cols)

        for w in self._widgets:
            set_w = getattr(w, "set_tile_width", None)
            if callable(set_w):
                set_w(col_w)

        if force_regrid or cols != self._last_cols:
            for w in self._widgets:
                self._grid.removeWidget(w)
            for c in range(self._max_cols):
                self._grid.setColumnStretch(c, 1 if c < cols else 0)
            for idx, w in enumerate(self._widgets):
                r, c = divmod(idx, cols)
                self._grid.addWidget(w, r, c, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self._last_cols = cols

        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._in_batch_layout or not self.isVisible():
            return
        if event.size().width() == event.oldSize().width():
            return
        self.relayout(force_regrid=False)


class VideoGridCard(QFrame):
    """Elemento de video en grilla estilo YouTube (sin borde ni fondo de card)."""

    play_requested = Signal(object)       # MediaItem
    channel_clicked = Signal(object)      # MediaItem
    watch_later_toggled = Signal(object)  # MediaItem
    download_requested = Signal(object)   # MediaItem
    remove_requested = Signal(object)     # MediaItem
    hover_entered = Signal(object)        # MediaItem
    hover_left = Signal(object)           # MediaItem

    THUMB_FETCH_SIZE = QSize(320, 180)
    AVATAR_SIZE = QSize(34, 34)

    def __init__(
        self,
        item: MediaItem,
        in_watch_later: bool,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        show_remove: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self._in_watch_later = in_watch_later
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._show_remove = show_remove
        self._thumb_url = item.normalized_thumb()
        self._avatar_url = item.channel_avatar_url or ""
        self._has_avatar_pix = False
        self._thumb_done = False
        self._avatar_done = not bool(self._avatar_url)
        self._thumb_connected = False
        self._offscreen_unloaded = False
        self._card_hovered = False
        self._card_hover_t = 0.0
        self._card_hover_anim: QVariantAnimation | None = None
        self._has_resume = item.resume_pos >= 3.0
        self.setObjectName("YtVideoTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        ratio = 0.0
        if item.resume_pos >= 3.0 and item.duration_sec > 0:
            ratio = max(0.02, min(1.0, item.resume_pos / item.duration_sec))

        self.thumb_widget = RoundedThumbWidget(
            radius=12,
            duration_text=("EN VIVO" if item.is_live else (item.duration or "")),
            progress_ratio=ratio,
            bg_color=palette.surface1,
            enable_hover_zoom=True,
        )
        self.thumb_widget.setFixedSize(272, 153)
        root.addWidget(self.thumb_widget)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 0)
        meta_row.setSpacing(10)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(self.AVATAR_SIZE)
        self.avatar_label.setScaledContents(True)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        initial = (item.author or "Y").strip()[:1].upper()
        self.avatar_label.setText(initial)
        meta_row.addWidget(self.avatar_label, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_lbl = ElidedLabel(item.title, max_lines=2, parent=self)
        self.title_lbl.setStyleSheet("font-size: 13.5px; font-weight: 600;")
        text_col.addWidget(self.title_lbl)

        self.author_lbl = ElidedLabel(item.author or "YouTube", max_lines=1, parent=self)
        self.author_lbl.setObjectName("MutedLabel")
        text_col.addWidget(self.author_lbl)

        sub_text = ""
        if item.resume_pos >= 3.0:
            res_str = format_duration(item.resume_pos)
            if res_str:
                sub_text = f"Continuar en {res_str}"
        elif item.published_text:
            sub_text = item.published_text

        self.sub_meta_lbl = ElidedLabel(sub_text, max_lines=1, parent=self)
        text_col.addWidget(self.sub_meta_lbl)

        meta_row.addLayout(text_col, 1)

        self.more_btn = QPushButton(self)
        self.more_btn.setObjectName("TileMoreBtn")
        self.more_btn.setFixedSize(28, 28)
        self.more_btn.setToolTip("Opciones")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.clicked.connect(self._open_context_menu)
        meta_row.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignTop)

        root.addLayout(meta_row)
        if not sub_text:
            self.sub_meta_lbl.hide()
        self.set_tile_width(272)
        self.update_theme(palette)
        self.set_watch_later(in_watch_later)

        if self.item.kind == "video" and (self.item.channel_id or self.item.video_id):
            self.avatar_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.author_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            tip_ch = f"Ver canal: {item.author or 'YouTube'}"
            self.avatar_label.setToolTip(tip_ch)
            self.author_lbl.setToolTip(tip_ch)
            self.avatar_label.installEventFilter(self)
            self.author_lbl.installEventFilter(self)

        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_FETCH_SIZE)
        if cached is not None:
            self.thumb_widget.set_pixmap(cached)
            self._thumb_done = True
        else:
            self._thumb_done = not bool(self._thumb_url)

        if self._avatar_url:
            av_cached = thumb_cache.get_or_fetch(
                self._avatar_url, self.AVATAR_SIZE, circular=True
            )
            if av_cached is not None:
                self._set_avatar_pixmap(av_cached)
                self._avatar_done = True

        if not (self._thumb_done and self._avatar_done):
            thumb_cache.ready.connect(self._on_thumb_ready)
            self._thumb_connected = True

    def _disconnect_thumb_cache(self) -> None:
        if self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_thumb_ready)
            except (RuntimeError, TypeError):
                pass

    def release_resources(self) -> None:
        self._disconnect_thumb_cache()
        if self._card_hover_anim is not None:
            self._card_hover_anim.stop()
        self.thumb_widget.clear()
        self.avatar_label.clear()

    def set_offscreen_unloaded(self, unloaded: bool) -> None:
        want = bool(unloaded)
        if want == self._offscreen_unloaded:
            return
        self._offscreen_unloaded = want
        if want:
            self._disconnect_thumb_cache()
            self.thumb_widget.clear()
            self._thumb_done = False
            return
        if self._thumb_url:
            cached = self._thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_FETCH_SIZE)
            if cached is not None:
                self.thumb_widget.set_pixmap(cached)
                self._thumb_done = True
            elif not self._thumb_connected:
                self._thumb_cache.ready.connect(self._on_thumb_ready)
                self._thumb_connected = True

    def set_tile_width(self, width: int) -> None:
        w = max(160, int(width))
        h = max(90, int(round(w * 9 / 16)))
        self.thumb_widget.setFixedSize(w, h)
        self.setFixedWidth(w)
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            hint_h = lay.sizeHint().height()
        else:
            hint_h = h + 100
        self.setMinimumHeight(max(h + 100, hint_h))
        self.updateGeometry()

    def _open_context_menu(self) -> None:
        menu = QMenu(self)
        try:
            if self.item.kind == "twitch":
                fav_label = (
                    "Quitar de Favoritos"
                    if self._in_watch_later
                    else "Guardar en Favoritos"
                )
                act_fav = menu.addAction(fav_label)
                act_fav.triggered.connect(lambda: self.watch_later_toggled.emit(self.item))
            else:
                if self.item.kind == "video" and (self.item.channel_id or self.item.video_id):
                    act_ch = menu.addAction("Ir al canal")
                    act_ch.triggered.connect(lambda: self.channel_clicked.emit(self.item))
                wl_label = (
                    "Quitar de Ver más tarde"
                    if self._in_watch_later
                    else "Guardar en Ver más tarde"
                )
                act_wl = menu.addAction(wl_label)
                act_wl.triggered.connect(lambda: self.watch_later_toggled.emit(self.item))

                act_dl = menu.addAction("Descargar video")
                act_dl.triggered.connect(lambda: self.download_requested.emit(self.item))

            if self._show_remove:
                menu.addSeparator()
                act_rm = menu.addAction("Quitar del historial")
                act_rm.triggered.connect(lambda: self.remove_requested.emit(self.item))

            pos = self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft())
            menu.exec(pos)
        finally:
            menu.deleteLater()

    def contextMenuEvent(self, _event) -> None:
        self._open_context_menu()

    def _set_avatar_pixmap(self, pix: QPixmap) -> None:
        self._has_avatar_pix = True
        self.avatar_label.setText("")
        self.avatar_label.setPixmap(pix)
        self.avatar_label.setStyleSheet("background-color: transparent; border: none;")

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == self.THUMB_FETCH_SIZE:
            self.thumb_widget.set_pixmap(pix)
            self._thumb_done = True
        elif (
            self._avatar_url
            and url == self._avatar_url
            and pix.size() == self.AVATAR_SIZE
        ):
            self._set_avatar_pixmap(pix)
            self._avatar_done = True
        if self._thumb_done and self._avatar_done:
            self._disconnect_thumb_cache()

    def set_watch_later(self, active: bool) -> None:
        self._in_watch_later = active

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_widget.set_bg_color(palette.surface1)
        self.thumb_widget.set_accent_color(palette.accent)
        if not self._has_avatar_pix:
            self.avatar_label.setStyleSheet(
                f"background-color: {palette.surface2}; color: {palette.text2}; "
                f"border-radius: 17px; border: 1px solid {palette.border}; "
                "font-size: 13px; font-weight: 700;"
            )
        if self._has_resume:
            self.sub_meta_lbl.setStyleSheet(f"color: {palette.accent}; font-size: 11.5px; font-weight: 600;")
        else:
            self.sub_meta_lbl.setStyleSheet(f"color: {palette.text2}; font-size: 11.5px;")
        more_color = palette.text if self._card_hovered else palette.text2
        self.more_btn.setIcon(render_icon("more", more_color, 16))
        self.update()

    def _on_card_hover_val(self, val: object) -> None:
        self._card_hover_t = float(val)
        self.update()

    def _start_card_hover_anim(self, target: float) -> None:
        if self._card_hover_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(150)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(self._on_card_hover_val)
            self._card_hover_anim = anim
        self._card_hover_anim.stop()
        self._card_hover_anim.setStartValue(self._card_hover_t)
        self._card_hover_anim.setEndValue(float(target))
        self._card_hover_anim.start()

    def enterEvent(self, event) -> None:
        self._card_hovered = True
        self._start_card_hover_anim(1.0)
        self.more_btn.setIcon(render_icon("more", self._palette.text, 16))
        if self.item.kind != "twitch":
            self.hover_entered.emit(self.item)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._card_hovered = False
        self._start_card_hover_anim(0.0)
        self.more_btn.setIcon(render_icon("more", self._palette.text2, 16))
        if self.item.kind != "twitch":
            self.hover_left.emit(self.item)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._card_hover_t > 0.01:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            bg = QColor(self._palette.surface2)
            bg.setAlpha(int(95 * self._card_hover_t))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(self.rect()), 12.0, 12.0)
            p.end()
        super().paintEvent(event)

    def eventFilter(self, obj, event) -> bool:
        try:
            etype = event.type()
        except RuntimeError:
            return False
        if (
            self.item.kind == "video"
            and (obj is self.avatar_label or obj is self.author_lbl)
        ):
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.channel_clicked.emit(self.item)
                return True
            if etype == QEvent.Type.Enter and obj is self.author_lbl:
                self.author_lbl.setStyleSheet(f"color: {self._palette.text};")
            elif etype == QEvent.Type.Leave and obj is self.author_lbl:
                self.author_lbl.setStyleSheet("")
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.thumb_widget.trigger_press_feedback()
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)


class MusicTileWidget(QFrame):
    """Tile cuadrado estilo YouTube Music para estanterías horizontales (sin card)."""

    play_requested = Signal(object)      # MediaItem
    my_music_toggled = Signal(object)    # MediaItem
    enqueue_requested = Signal(object)   # MediaItem
    download_requested = Signal(object)  # MediaItem
    artist_clicked = Signal(object)      # MediaItem

    TILE_SIZE = 172
    THUMB_FETCH_SIZE = QSize(180, 180)

    def __init__(
        self,
        item: MediaItem,
        in_my_music: bool,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self._in_my_music = in_my_music
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._thumb_url = upgrade_thumb_url(item.normalized_thumb(), 360)
        self._thumb_connected = False
        self._card_hover_t = 0.0
        self._card_hover_anim: QVariantAnimation | None = None
        self.setObjectName("YtMusicTile")
        self.setFixedWidth(self.TILE_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)

        self.thumb_widget = RoundedThumbWidget(
            radius=10,
            show_play_on_hover=True,
            bg_color=palette.surface1,
            enable_hover_zoom=True,
        )
        self.thumb_widget.setFixedSize(self.TILE_SIZE, self.TILE_SIZE)
        root.addWidget(self.thumb_widget)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(4)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_lbl = ElidedLabel(item.title, max_lines=2)
        self.title_lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        text_col.addWidget(self.title_lbl)

        sub_text = item.author or "YouTube Music"
        if item.duration:
            sub_text = f"{sub_text}  •  {item.duration}"
        self.artist_lbl = ElidedLabel(sub_text, max_lines=1)
        self.artist_lbl.setObjectName("MutedLabel")
        if item.author or item.channel_id:
            self.artist_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            self.artist_lbl.setToolTip(
                f"Ver perfil de {item.author}" if item.author else "Ver perfil del artista"
            )
            self.artist_lbl.installEventFilter(self)
        text_col.addWidget(self.artist_lbl)

        bottom_row.addLayout(text_col, 1)

        self.more_btn = QPushButton()
        self.more_btn.setObjectName("TileMoreBtn")
        self.more_btn.setFixedSize(24, 24)
        self.more_btn.setToolTip("Opciones")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.clicked.connect(self._open_context_menu)
        bottom_row.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignTop)

        root.addLayout(bottom_row)
        self.update_theme(palette)

        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_FETCH_SIZE)
        if cached is not None:
            self.thumb_widget.set_pixmap(cached)
        elif self._thumb_url:
            thumb_cache.ready.connect(self._on_thumb_ready)
            self._thumb_connected = True

    def _disconnect_thumb_cache(self) -> None:
        if self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_thumb_ready)
            except (RuntimeError, TypeError):
                pass

    def release_resources(self) -> None:
        self._disconnect_thumb_cache()
        if self._card_hover_anim is not None:
            self._card_hover_anim.stop()
        try:
            self.artist_lbl.removeEventFilter(self)
        except Exception:
            pass
        self.thumb_widget.clear()

    def _open_context_menu(self) -> None:
        menu = QMenu(self)
        try:
            if self.item.author or self.item.channel_id:
                act_art = menu.addAction("Ver perfil del artista")
                act_art.triggered.connect(lambda: self.artist_clicked.emit(self.item))

            mm_label = (
                "Quitar de Mi música"
                if self._in_my_music
                else "Guardar en Mi música"
            )
            act_mm = menu.addAction(mm_label)
            act_mm.triggered.connect(lambda: self.my_music_toggled.emit(self.item))

            act_q = menu.addAction("Agregar a la cola")
            act_q.triggered.connect(lambda: self.enqueue_requested.emit(self.item))

            act_dl = menu.addAction("Descargar canción")
            act_dl.triggered.connect(lambda: self.download_requested.emit(self.item))

            pos = self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft())
            menu.exec(pos)
        finally:
            menu.deleteLater()

    def contextMenuEvent(self, _event) -> None:
        self._open_context_menu()

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == self.THUMB_FETCH_SIZE:
            self.thumb_widget.set_pixmap(pix)
            self._disconnect_thumb_cache()

    def set_my_music(self, active: bool) -> None:
        self._in_my_music = active

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_widget.set_bg_color(palette.surface1)
        self.thumb_widget.set_accent_color(palette.accent)
        self.more_btn.setIcon(render_icon("more", palette.text2, 15))
        self.update()

    def _on_card_hover_val(self, val: object) -> None:
        self._card_hover_t = float(val)
        self.update()

    def _start_card_hover_anim(self, target: float) -> None:
        if self._card_hover_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(150)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(self._on_card_hover_val)
            self._card_hover_anim = anim
        self._card_hover_anim.stop()
        self._card_hover_anim.setStartValue(self._card_hover_t)
        self._card_hover_anim.setEndValue(float(target))
        self._card_hover_anim.start()

    def enterEvent(self, event) -> None:
        self._start_card_hover_anim(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._start_card_hover_anim(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._card_hover_t > 0.01:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            bg = QColor(self._palette.surface2)
            bg.setAlpha(int(95 * self._card_hover_t))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(self.rect()), 10.0, 10.0)
            p.end()
        super().paintEvent(event)

    def eventFilter(self, obj, event) -> bool:
        try:
            if obj is not getattr(self, "artist_lbl", None) or not (
                self.item.author or self.item.channel_id
            ):
                return False
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.artist_clicked.emit(self.item)
                return True
            if etype == QEvent.Type.Enter:
                self.artist_lbl.setStyleSheet(f"color: {self._palette.text}; text-decoration: underline;")
            elif etype == QEvent.Type.Leave:
                self.artist_lbl.setStyleSheet("")
        except Exception:
            return False
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.thumb_widget.trigger_press_feedback()
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)


class MusicShelfWidget(QWidget):
    """Estantería horizontal desplazable de YT Music con botones < y >."""

    def __init__(
        self,
        title: str,
        tiles: list[MusicTileWidget],
        palette: ThemePalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._tiles = list(tiles)
        self._scroll_anim: QPropertyAnimation | None = None
        self._scroll_target: int | None = None
        self.setMinimumWidth(0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 12)
        root.setSpacing(10)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 4, 0)
        hdr.setSpacing(8)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("font-size: 20px; font-weight: 700;")
        hdr.addWidget(self.title_lbl)
        hdr.addStretch()

        self.prev_btn = QPushButton()
        self.prev_btn.setObjectName("ShelfNavBtn")
        self.prev_btn.setFixedSize(30, 30)
        self.prev_btn.setToolTip("Desplazar a la izquierda")
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.clicked.connect(lambda: self._scroll_by(-540))
        hdr.addWidget(self.prev_btn)

        self.next_btn = QPushButton()
        self.next_btn.setObjectName("ShelfNavBtn")
        self.next_btn.setFixedSize(30, 30)
        self.next_btn.setToolTip("Desplazar a la derecha")
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.clicked.connect(lambda: self._scroll_by(540))
        hdr.addWidget(self.next_btn)

        root.addLayout(hdr)

        self.scroll = QScrollArea()
        self.scroll.setMinimumWidth(0)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(252)

        container = QWidget()
        row_lay = QHBoxLayout(container)
        row_lay.setContentsMargins(0, 2, 0, 2)
        row_lay.setSpacing(16)
        for tile in self._tiles:
            row_lay.addWidget(tile, 0, Qt.AlignmentFlag.AlignTop)
        row_lay.addStretch()

        self.scroll.setWidget(container)
        root.addWidget(self.scroll)
        self.update_theme(palette)

    def _scroll_by(self, delta: int) -> None:
        bar = self.scroll.horizontalScrollBar()
        base = (
            self._scroll_target
            if (
                self._scroll_anim is not None
                and self._scroll_anim.state() == QPropertyAnimation.State.Running
                and self._scroll_target is not None
            )
            else bar.value()
        )
        target = max(bar.minimum(), min(bar.maximum(), base + int(delta)))
        self._scroll_target = target
        if self._scroll_anim is None:
            anim = QPropertyAnimation(bar, b"value", self)
            anim.setDuration(260)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._scroll_anim = anim
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.start()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.prev_btn.setIcon(render_icon("chevron_left", palette.text, 15))
        self.next_btn.setIcon(render_icon("chevron_right", palette.text, 15))
        for tile in self._tiles:
            tile.update_theme(palette)

    def release_resources(self) -> None:
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
        for tile in self._tiles:
            tile.release_resources()


class VideoCardWidget(QFrame):
    """Tarjeta horizontal compacta para un video de YouTube con avatar de canal."""

    play_requested = Signal(object)          # MediaItem
    watch_later_toggled = Signal(object)     # MediaItem
    download_requested = Signal(object)      # MediaItem
    remove_requested = Signal(object)        # MediaItem (opcional para historial)

    THUMB_SIZE = QSize(160, 90)
    AVATAR_SIZE = QSize(22, 22)

    def __init__(
        self,
        item: MediaItem,
        in_watch_later: bool,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        show_remove: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self._in_watch_later = in_watch_later
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._thumb_url = item.normalized_thumb()
        self._avatar_url = item.channel_avatar_url or ""
        self._has_avatar_pix = False
        self._thumb_done = False
        self._avatar_done = not bool(self._avatar_url)
        self._thumb_connected = False
        self.setObjectName("MediaCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(12)

        thumb_col = QVBoxLayout()
        thumb_col.setContentsMargins(0, 0, 0, 0)
        thumb_col.setSpacing(3)

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(self.THUMB_SIZE)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(
            f"background-color: {palette.base}; border-radius: 6px; border: 1px solid {palette.border};"
        )
        thumb_col.addWidget(self.thumb_label)

        self.history_progress: QProgressBar | None = None
        if item.resume_pos >= 3.0 and item.duration_sec > 0:
            self.history_progress = QProgressBar()
            self.history_progress.setObjectName("HistoryProgress")
            self.history_progress.setFixedSize(self.THUMB_SIZE.width(), 3)
            self.history_progress.setRange(0, 1000)
            ratio_val = min(1000, int((item.resume_pos / item.duration_sec) * 1000))
            self.history_progress.setValue(max(10, ratio_val))
            self.history_progress.setTextVisible(False)
            thumb_col.addWidget(self.history_progress)

        root.addLayout(thumb_col)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 2, 0, 2)
        info_col.setSpacing(6)

        self.title_lbl = QLabel(item.title)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumHeight(42)
        self.title_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        info_col.addWidget(self.title_lbl)

        channel_row = QHBoxLayout()
        channel_row.setContentsMargins(0, 0, 0, 0)
        channel_row.setSpacing(7)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(self.AVATAR_SIZE)
        self.avatar_label.setScaledContents(True)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        initial = (item.author or "Y").strip()[:1].upper()
        self.avatar_label.setText(initial)
        channel_row.addWidget(self.avatar_label)

        meta_parts = [p for p in (item.author, item.duration) if p]
        if item.resume_pos >= 3.0:
            res_str = format_duration(item.resume_pos)
            if res_str:
                meta_parts.append(f"Continuar en {res_str}")
        self.meta_lbl = QLabel("  •  ".join(meta_parts))
        self.meta_lbl.setObjectName("MutedLabel")
        channel_row.addWidget(self.meta_lbl, 1)
        info_col.addLayout(channel_row)

        info_col.addStretch()
        root.addLayout(info_col, 1)

        actions_col = QVBoxLayout()
        actions_col.setSpacing(6)
        actions_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.wl_btn = QPushButton()
        self.wl_btn.setObjectName("IconBtn")
        self.wl_btn.setFixedSize(34, 34)
        self.wl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wl_btn.clicked.connect(lambda: self.watch_later_toggled.emit(self.item))
        actions_col.addWidget(self.wl_btn)

        self.dl_btn = QPushButton()
        self.dl_btn.setObjectName("IconBtn")
        self.dl_btn.setFixedSize(34, 34)
        self.dl_btn.setToolTip("Descargar video")
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.clicked.connect(lambda: self.download_requested.emit(self.item))
        actions_col.addWidget(self.dl_btn)

        self.remove_btn: QPushButton | None = None
        if show_remove:
            self.remove_btn = QPushButton()
            self.remove_btn.setObjectName("IconBtn")
            self.remove_btn.setFixedSize(34, 34)
            self.remove_btn.setToolTip("Quitar del historial")
            self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.item))
            actions_col.addWidget(self.remove_btn)

        root.addLayout(actions_col)
        self.update_theme(palette)
        self.set_watch_later(in_watch_later)

        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_SIZE)
        if cached is not None:
            self.thumb_label.setPixmap(cached)
            self._thumb_done = True
        else:
            self._thumb_done = not bool(self._thumb_url)

        if self._avatar_url:
            av_cached = thumb_cache.get_or_fetch(
                self._avatar_url, self.AVATAR_SIZE, circular=True
            )
            if av_cached is not None:
                self._set_avatar_pixmap(av_cached)
                self._avatar_done = True

        if not (self._thumb_done and self._avatar_done):
            thumb_cache.ready.connect(self._on_thumb_ready)
            self._thumb_connected = True

    def _disconnect_thumb_cache(self) -> None:
        if self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_thumb_ready)
            except (RuntimeError, TypeError):
                pass

    def release_resources(self) -> None:
        self._disconnect_thumb_cache()
        self.thumb_label.clear()
        self.avatar_label.clear()

    def _set_avatar_pixmap(self, pix: QPixmap) -> None:
        self._has_avatar_pix = True
        self.avatar_label.setText("")
        self.avatar_label.setPixmap(pix)
        self.avatar_label.setStyleSheet("background-color: transparent; border: none;")

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == self.THUMB_SIZE:
            self.thumb_label.setPixmap(pix)
            self._thumb_done = True
        elif (
            self._avatar_url
            and url == self._avatar_url
            and pix.size() == self.AVATAR_SIZE
        ):
            self._set_avatar_pixmap(pix)
            self._avatar_done = True
        if self._thumb_done and self._avatar_done:
            self._disconnect_thumb_cache()

    def set_watch_later(self, active: bool) -> None:
        self._in_watch_later = active
        self.wl_btn.setProperty("active", "true" if active else "false")
        self.wl_btn.style().unpolish(self.wl_btn)
        self.wl_btn.style().polish(self.wl_btn)
        if active:
            self.wl_btn.setIcon(render_icon("bookmark_filled", self._palette.accent, 18))
            self.wl_btn.setToolTip("Quitar de Ver más tarde")
        else:
            self.wl_btn.setIcon(render_icon("bookmark", self._palette.text2, 18))
            self.wl_btn.setToolTip("Guardar en Ver más tarde")

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_label.setStyleSheet(
            f"background-color: {palette.base}; border-radius: 6px; border: 1px solid {palette.border};"
        )
        if not self._has_avatar_pix:
            self.avatar_label.setStyleSheet(
                f"background-color: {palette.surface2}; color: {palette.text2}; "
                f"border-radius: 11px; border: 1px solid {palette.border}; "
                "font-size: 11px; font-weight: 700;"
            )
        self.dl_btn.setIcon(render_icon("download", palette.text2, 18))
        if self.remove_btn is not None:
            self.remove_btn.setIcon(render_icon("close", palette.text2, 17))
        self.set_watch_later(self._in_watch_later)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)


class MusicRowWidget(QFrame):
    """Fila limpia estilo YouTube Music (sin borde de card) para listas, búsqueda y cola."""

    play_requested = Signal(object)      # MediaItem
    my_music_toggled = Signal(object)    # MediaItem
    enqueue_requested = Signal(object)   # MediaItem
    download_requested = Signal(object)  # MediaItem
    remove_requested = Signal(object)    # MediaItem
    artist_clicked = Signal(object)      # MediaItem

    THUMB_SIZE = QSize(44, 44)

    def __init__(
        self,
        item: MediaItem,
        in_my_music: bool,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        show_enqueue: bool = True,
        show_remove: bool = False,
        is_current: bool = False,
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self._in_my_music = in_my_music
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._is_current = is_current
        self._compact = compact
        self._thumb_url = item.normalized_thumb()
        self._thumb_connected = False
        self._hover_t = 0.0
        self._hover_anim: QVariantAnimation | None = None
        self.setObjectName("MusicRow")
        self.setProperty("current", "true" if is_current else "false")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(58 if compact else 60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QHBoxLayout(self)
        root.setContentsMargins(8 if compact else 10, 6, 6 if compact else 10, 6)
        root.setSpacing(8 if compact else 10)

        self.thumb_label = RoundedThumbWidget(
            radius=6,
            force_play_overlay=is_current,
            bg_color=palette.surface1,
        )
        self.thumb_label.setFixedSize(self.THUMB_SIZE)
        root.addWidget(self.thumb_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_lbl = ElidedLabel(item.title, max_lines=1)
        self.title_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {palette.accent if is_current else palette.text};"
        )
        text_col.addWidget(self.title_lbl)

        self.artist_lbl = ElidedLabel(item.author or "YouTube Music", max_lines=1)
        self.artist_lbl.setObjectName("MutedLabel")
        self.artist_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.artist_lbl.setToolTip(f"Ver perfil de {item.author}" if item.author else "Ver perfil del artista")
        self.artist_lbl.installEventFilter(self)
        text_col.addWidget(self.artist_lbl)
        root.addLayout(text_col, 1)

        self.dur_lbl = QLabel(item.duration)
        self.dur_lbl.setObjectName("MutedLabel")
        self.dur_lbl.setFixedWidth(38)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.dur_lbl)

        self.fav_btn: QPushButton | None = None
        self.queue_btn: QPushButton | None = None
        self.dl_btn: QPushButton | None = None
        self.more_btn: QPushButton | None = None

        if compact:
            self.more_btn = QPushButton()
            self.more_btn.setObjectName("TileMoreBtn")
            self.more_btn.setFixedSize(26, 26)
            self.more_btn.setToolTip("Opciones")
            self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.more_btn.clicked.connect(self._open_compact_menu)
            root.addWidget(self.more_btn)
        else:
            self.fav_btn = QPushButton()
            self.fav_btn.setObjectName("IconBtn")
            self.fav_btn.setFixedSize(32, 32)
            self.fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.fav_btn.clicked.connect(lambda: self.my_music_toggled.emit(self.item))
            root.addWidget(self.fav_btn)

            if show_enqueue:
                self.queue_btn = QPushButton()
                self.queue_btn.setObjectName("IconBtn")
                self.queue_btn.setFixedSize(32, 32)
                self.queue_btn.setToolTip("Agregar a la cola")
                self.queue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                self.queue_btn.clicked.connect(lambda: self.enqueue_requested.emit(self.item))
                root.addWidget(self.queue_btn)

            self.dl_btn = QPushButton()
            self.dl_btn.setObjectName("IconBtn")
            self.dl_btn.setFixedSize(32, 32)
            self.dl_btn.setToolTip("Descargar canción")
            self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.dl_btn.clicked.connect(lambda: self.download_requested.emit(self.item))
            root.addWidget(self.dl_btn)

        self.remove_btn: QPushButton | None = None
        if show_remove:
            self.remove_btn = QPushButton()
            self.remove_btn.setObjectName("IconBtn")
            self.remove_btn.setFixedSize(26 if compact else 32, 26 if compact else 32)
            self.remove_btn.setToolTip("Quitar")
            self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.item))
            root.addWidget(self.remove_btn)

        self.update_theme(palette)
        self.set_my_music(in_my_music)

        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_SIZE)
        if cached is not None:
            self.thumb_label.set_pixmap(cached)
        elif self._thumb_url:
            thumb_cache.ready.connect(self._on_thumb_ready)
            self._thumb_connected = True

    def _disconnect_thumb_cache(self) -> None:
        if self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_thumb_ready)
            except (RuntimeError, TypeError):
                pass

    def release_resources(self) -> None:
        self._disconnect_thumb_cache()
        if self._hover_anim is not None:
            self._hover_anim.stop()
        try:
            self.artist_lbl.removeEventFilter(self)
        except Exception:
            pass
        self.thumb_label.set_playing_bars(False)
        self.thumb_label.clear()

    def _open_compact_menu(self) -> None:
        menu = QMenu(self)
        try:
            if self.item.author or self.item.channel_id:
                act_art = menu.addAction("Ver perfil del artista")
                act_art.triggered.connect(lambda: self.artist_clicked.emit(self.item))

            mm_label = (
                "Quitar de Mi música"
                if self._in_my_music
                else "Guardar en Mi música"
            )
            act_mm = menu.addAction(mm_label)
            act_mm.triggered.connect(lambda: self.my_music_toggled.emit(self.item))

            if self.more_btn is None and self.queue_btn is not None:
                act_q = menu.addAction("Agregar a la cola")
                act_q.triggered.connect(lambda: self.enqueue_requested.emit(self.item))

            act_dl = menu.addAction("Descargar canción")
            act_dl.triggered.connect(lambda: self.download_requested.emit(self.item))

            if self.more_btn is not None:
                pos = self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft())
                menu.exec(pos)
            else:
                menu.exec(self.mapToGlobal(QPoint(self.width() - 24, self.height() // 2)))
        finally:
            menu.deleteLater()

    def contextMenuEvent(self, _event) -> None:
        self._open_compact_menu()

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == self.THUMB_SIZE:
            self.thumb_label.set_pixmap(pix)
            self._disconnect_thumb_cache()

    def set_my_music(self, active: bool) -> None:
        self._in_my_music = active
        if self.fav_btn is not None:
            if active:
                self.fav_btn.setIcon(render_icon("heart_filled", self._palette.accent, 17))
                self.fav_btn.setToolTip("Quitar de Mi música")
            else:
                self.fav_btn.setIcon(render_icon("heart", self._palette.text2, 17))
                self.fav_btn.setToolTip("Guardar en Mi música")

    def set_current(self, is_current: bool) -> None:
        self._is_current = is_current
        self.setProperty("current", "true" if is_current else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.thumb_label.set_force_play_overlay(is_current)
        if not is_current:
            self.thumb_label.set_playing_bars(False)
        color = self._palette.accent if is_current else self._palette.text
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {color};")
        self.update()

    def set_playing_indicator(self, playing: bool) -> None:
        self.thumb_label.set_playing_bars(self._is_current and bool(playing))

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_label.set_bg_color(palette.surface1)
        color = palette.accent if self._is_current else palette.text
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {color};")
        if self.dl_btn is not None:
            self.dl_btn.setIcon(render_icon("download", palette.text2, 17))
        if self.queue_btn is not None:
            self.queue_btn.setIcon(render_icon("plus", palette.text2, 17))
        if self.more_btn is not None:
            self.more_btn.setIcon(render_icon("more", palette.text2, 16))
        if self.remove_btn is not None:
            self.remove_btn.setIcon(render_icon("close", palette.text2, 15))
        self.set_my_music(self._in_my_music)

    def _on_hover_val(self, val: object) -> None:
        self._hover_t = float(val)
        self.update()

    def _start_hover_anim(self, target: float) -> None:
        if self._hover_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(140)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(self._on_hover_val)
            self._hover_anim = anim
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(float(target))
        self._hover_anim.start()

    def enterEvent(self, event) -> None:
        self._start_hover_anim(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._start_hover_anim(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        alpha = (26 if self._is_current else 0) + int(18 * self._hover_t)
        if alpha > 0 or self._is_current:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            if alpha > 0:
                painter.setBrush(QColor(255, 255, 255, min(48, alpha)))
                painter.drawRoundedRect(QRectF(self.rect()), 8.0, 8.0)
            if self._is_current:
                painter.setBrush(QColor(self._palette.accent))
                pill_h = max(12.0, float(self.height() - 24))
                painter.drawRoundedRect(QRectF(2.0, 12.0, 3.0, pill_h), 1.5, 1.5)
            painter.end()
        super().paintEvent(event)

    def eventFilter(self, obj, event) -> bool:
        try:
            if obj is not getattr(self, "artist_lbl", None) or not (
                self.item.author or self.item.channel_id
            ):
                return False
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.artist_clicked.emit(self.item)
                return True
            if etype == QEvent.Type.Enter:
                self.artist_lbl.setStyleSheet(f"color: {self._palette.text}; text-decoration: underline;")
            elif etype == QEvent.Type.Leave:
                self.artist_lbl.setStyleSheet("")
        except Exception:
            return False
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)


class MusicAlbumCard(QFrame):
    """Tarjeta cuadrada compacta de álbum o sencillo para el perfil de artista."""

    clicked = Signal(object)  # MusicAlbumInfo

    CARD_W = 184
    THUMB_SIZE = 168

    def __init__(
        self,
        album: MusicAlbumInfo,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.album = album
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._thumb_url = upgrade_thumb_url(album.thumb_url, 360)
        self._thumb_connected = False
        self._hover_t = 0.0
        self._hover_anim: QVariantAnimation | None = None
        self.setObjectName("YtMusicTile")
        self.setFixedSize(self.CARD_W, 238)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.thumb_widget = RoundedThumbWidget(
            radius=10,
            show_play_on_hover=True,
            bg_color=palette.surface1,
            enable_hover_zoom=True,
        )
        self.thumb_widget.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        root.addWidget(self.thumb_widget)

        self.title_lbl = ElidedLabel(album.title, max_lines=2)
        self.title_lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        root.addWidget(self.title_lbl)

        meta_parts = [p for p in (album.year, album.album_type) if p]
        self.meta_lbl = ElidedLabel("  •  ".join(meta_parts), max_lines=1)
        self.meta_lbl.setObjectName("MutedLabel")
        root.addWidget(self.meta_lbl)
        root.addStretch()

        if self._thumb_url:
            cached = thumb_cache.get_or_fetch(
                self._thumb_url, QSize(self.THUMB_SIZE, self.THUMB_SIZE)
            )
            if cached is not None:
                self.thumb_widget.set_pixmap(cached)
            else:
                thumb_cache.ready.connect(self._on_thumb_ready)
                self._thumb_connected = True

    def _disconnect_thumb_cache(self) -> None:
        if self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_thumb_ready)
            except (RuntimeError, TypeError):
                pass

    def release_resources(self) -> None:
        self._disconnect_thumb_cache()
        if self._hover_anim is not None:
            self._hover_anim.stop()
        self.thumb_widget.clear()

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == QSize(self.THUMB_SIZE, self.THUMB_SIZE):
            self.thumb_widget.set_pixmap(pix)
            self._disconnect_thumb_cache()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_widget.set_bg_color(palette.surface1)
        self.thumb_widget.set_accent_color(palette.accent)
        self.update()

    def _on_hover_val(self, val: object) -> None:
        self._hover_t = float(val)
        self.update()

    def _start_hover_anim(self, target: float) -> None:
        if self._hover_anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(150)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(self._on_hover_val)
            self._hover_anim = anim
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(float(target))
        self._hover_anim.start()

    def enterEvent(self, event) -> None:
        self._start_hover_anim(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._start_hover_anim(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._hover_t > 0.01:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            bg = QColor(self._palette.surface2)
            bg.setAlpha(int(95 * self._hover_t))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(self.rect()), 10.0, 10.0)
            p.end()
        super().paintEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.thumb_widget.trigger_press_feedback()
            self.clicked.emit(self.album)
        super().mousePressEvent(event)


class ContinueWatchingBanner(QFrame):
    """Franja minimalista superior para retomar el último video guardado."""

    resume_requested = Signal(object, float)  # MediaItem, pos_sec
    dismissed = Signal()

    THUMB_SIZE = QSize(96, 54)

    def __init__(
        self,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._item: MediaItem | None = None
        self._position: float = 0.0
        self._thumb_url: str = ""
        self._dismiss_anim: QPropertyAnimation | None = None

        self.setObjectName("ContinueBanner")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 12, 10)
        lay.setSpacing(14)

        self.thumb_widget = RoundedThumbWidget(
            radius=8,
            bg_color=palette.surface1,
        )
        self.thumb_widget.setFixedSize(self.THUMB_SIZE)
        lay.addWidget(self.thumb_widget)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)

        self.tag_lbl = QLabel("CONTINUAR VIENDO")
        info_col.addWidget(self.tag_lbl)

        self.title_lbl = ElidedLabel("", max_lines=1)
        self.title_lbl.setStyleSheet("font-size: 13.5px; font-weight: 600;")
        info_col.addWidget(self.title_lbl)

        self.sub_lbl = ElidedLabel("", max_lines=1)
        self.sub_lbl.setObjectName("MutedLabel")
        info_col.addWidget(self.sub_lbl)
        lay.addLayout(info_col, 1)

        self.resume_btn = QPushButton(" Continuar")
        self.resume_btn.setObjectName("AccentBtn")
        self.resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resume_btn.clicked.connect(self._emit_resume)
        lay.addWidget(self.resume_btn)

        self.close_btn = QPushButton()
        self.close_btn.setObjectName("IconBtn")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setToolTip("Ocultar")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.dismiss_animated)
        lay.addWidget(self.close_btn)

        self._thumb_cache.ready.connect(self._on_thumb_ready)
        self.update_theme(palette)

    def _emit_resume(self) -> None:
        if self._item is not None:
            self.resume_requested.emit(self._item, self._position)

    def set_item(
        self, item: MediaItem, position: float = 0.0, duration: float = 0.0
    ) -> None:
        if self._dismiss_anim is not None:
            self._dismiss_anim.stop()
        self.setMaximumHeight(16777215)
        self._item = item
        self._position = max(0.0, float(position))
        eff_dur = max(float(duration or 0.0), float(item.duration_sec or 0))
        ratio = (
            max(0.02, min(1.0, self._position / eff_dur))
            if (eff_dur > 0 and self._position >= 3.0)
            else 0.0
        )
        self.thumb_widget.set_progress_ratio(ratio)
        self.title_lbl.setText(item.title)
        pos_str = format_duration(self._position) if self._position >= 3.0 else ""
        author = item.author or "YouTube"
        self.sub_lbl.setText(f"{author}  ·  En {pos_str}" if pos_str else author)

        self._thumb_url = item.normalized_thumb()
        pix = self._thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_SIZE)
        if pix is not None:
            self.thumb_widget.set_pixmap(pix)
        else:
            self.thumb_widget.clear()
        self.setVisible(True)

    def dismiss_animated(self) -> None:
        self.dismissed.emit()
        if not self.isVisible():
            return
        if self._dismiss_anim is None:
            anim = QPropertyAnimation(self, b"maximumHeight", self)
            anim.setDuration(160)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(self._on_dismiss_done)
            self._dismiss_anim = anim
        self._dismiss_anim.stop()
        self._dismiss_anim.setStartValue(max(0, self.height()))
        self._dismiss_anim.setEndValue(0)
        self._dismiss_anim.start()

    def _on_dismiss_done(self) -> None:
        self.hide()
        self.setMaximumHeight(16777215)

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if self._thumb_url and url == self._thumb_url and pix.size() == self.THUMB_SIZE:
            self.thumb_widget.set_pixmap(pix)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.tag_lbl.setStyleSheet(
            f"font-size: 9.5px; font-weight: 700; letter-spacing: 1px; color: {palette.accent};"
        )
        self.thumb_widget.set_bg_color(palette.surface1)
        self.resume_btn.setIcon(render_icon("play", palette.text, 14))
        self.close_btn.setIcon(render_icon("close", palette.text2, 14))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._emit_resume()
        super().mousePressEvent(event)


class AnimatedToastWidget(QWidget):
    """Notificación flotante pintada con entrada y salida suave (0 MB extra en GPU)."""

    def __init__(self, palette: ThemePalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._message = ""
        self._alpha = 0.0
        self._fading_out = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.valueChanged.connect(self._on_fade_val)
        self._fade_anim.finished.connect(self._on_fade_finished)

        self._pos_anim = QPropertyAnimation(self, b"pos", self)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_fade_out)
        self.hide()

    def _on_fade_val(self, val: object) -> None:
        self._alpha = float(val)
        self.update()

    def _on_fade_finished(self) -> None:
        if self._fading_out:
            self._fading_out = False
            self.hide()

    def _start_fade_out(self) -> None:
        if not self.isVisible() or self._fading_out:
            return
        self._fading_out = True
        self._fade_anim.stop()
        self._fade_anim.setDuration(190)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_anim.setStartValue(self._alpha)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

        self._pos_anim.stop()
        self._pos_anim.setDuration(190)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(QPoint(self.x(), self.y() + 8))
        self._pos_anim.start()

    def _target_rect_size(self) -> QSize:
        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.Medium)
        fm = QFontMetrics(font)
        w = min(460, max(120, fm.horizontalAdvance(self._message) + 32))
        h = max(34, fm.height() + 16)
        return QSize(w, h)

    def show_message(
        self,
        message: str,
        container_size: QSize,
        bottom_offset: int,
        duration_ms: int = 3200,
    ) -> None:
        self._message = str(message or "")
        sz = self._target_rect_size()
        self.resize(sz)
        margin = 18
        x = max(margin, container_size.width() - sz.width() - margin)
        y = max(margin, container_size.height() - sz.height() - bottom_offset)

        self._fading_out = False
        self._pos_anim.stop()
        self._fade_anim.stop()
        self.move(x, y + 10)
        self.show()
        self.raise_()

        self._pos_anim.setDuration(170)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pos_anim.setStartValue(QPoint(x, y + 10))
        self._pos_anim.setEndValue(QPoint(x, y))
        self._pos_anim.start()

        self._fade_anim.setDuration(170)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.setStartValue(max(0.1, self._alpha))
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

        self._hold_timer.start(duration_ms)

    def reposition(self, container_size: QSize, bottom_offset: int) -> None:
        if not self.isVisible():
            return
        self._pos_anim.stop()
        margin = 18
        x = max(margin, container_size.width() - self.width() - margin)
        y = max(margin, container_size.height() - self.height() - bottom_offset)
        self.move(x, y)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.update()

    def paintEvent(self, _event) -> None:
        if self._alpha <= 0.01 or not self._message:
            return
        w = float(self.width())
        h = float(self.height())
        if w <= 0 or h <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg = QColor(self._palette.surface2)
        bg.setAlpha(int(242 * self._alpha))
        border = QColor(self._palette.accent)
        border.setAlpha(int(175 * self._alpha))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 10.0, 10.0)

        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        txt_col = QColor(self._palette.text)
        txt_col.setAlpha(int(255 * self._alpha))
        painter.setPen(txt_col)
        fm = QFontMetrics(font)
        elided = fm.elidedText(self._message, Qt.TextElideMode.ElideRight, int(w - 24))
        painter.drawText(QRectF(12.0, 0.0, w - 24.0, h), Qt.AlignmentFlag.AlignCenter, elided)
        painter.end()
