"""Overlays flotantes translúcidos para video: HUD de volumen y barra fullscreen auto-ocultable."""

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
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import VALID_QUALITIES, VALID_SPEEDS
from swiftplay.models import MediaItem, format_duration
from swiftplay.themes import ThemePalette
from swiftplay.ui.icons import render_icon
from swiftplay.ui.widgets import ClickableSlider


class VolumeHudOverlay(QWidget):
    """HUD flotante sutil con el porcentaje de volumen al usar la rueda sobre el video."""

    def __init__(self, palette: ThemePalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(210, 46)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName("VolumeHudFrame")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        self.icon_lbl = QLabel()
        self.icon_lbl.setFixedSize(20, 20)
        lay.addWidget(self.icon_lbl)

        self.bar = QProgressBar()
        self.bar.setObjectName("HudProgress")
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar, 1)

        self.pct_lbl = QLabel("80%")
        self.pct_lbl.setFixedWidth(38)
        self.pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.pct_lbl.setStyleSheet("font-size: 12px; font-weight: 700;")
        lay.addWidget(self.pct_lbl)

        outer.addWidget(frame)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._start_fade_out)

        self._fading_out = False
        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.finished.connect(self._on_anim_finished)
        self.setWindowOpacity(0.0)

    def _start_fade_out(self) -> None:
        if not self.isVisible() or self._fading_out:
            return
        cur = float(self.windowOpacity())
        self._opacity_anim.stop()
        self._fading_out = True
        self._opacity_anim.setDuration(max(70, int(180 * cur)))
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._opacity_anim.setStartValue(cur)
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.start()

    def _on_anim_finished(self) -> None:
        if self._fading_out:
            self._fading_out = False
            self.hide()

    def show_volume(self, percent: int, anchor_rect: QRect) -> None:
        pct = max(0, min(100, int(percent)))
        icon_name = "mute" if pct == 0 else "volume"
        self.icon_lbl.setPixmap(
            render_icon(icon_name, self._palette.accent, 18).pixmap(QSize(18, 18))
        )
        self.bar.setValue(pct)
        self.pct_lbl.setText(f"{pct}%")

        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.top() + 26
        self.move(x, y)
        if not self.isVisible():
            self._opacity_anim.stop()
            self._fading_out = False
            self.setWindowOpacity(0.0)
            self.show()
            self.raise_()
            self._opacity_anim.setDuration(120)
            self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._opacity_anim.setStartValue(0.0)
            self._opacity_anim.setEndValue(1.0)
            self._opacity_anim.start()
        elif self._fading_out or self.windowOpacity() < 0.99:
            cur = float(self.windowOpacity())
            self._opacity_anim.stop()
            self._fading_out = False
            self.raise_()
            self._opacity_anim.setDuration(max(50, int(120 * (1.0 - cur))))
            self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._opacity_anim.setStartValue(cur)
            self._opacity_anim.setEndValue(1.0)
            self._opacity_anim.start()
        else:
            self.raise_()
        self._hide_timer.start(900)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette


class PlaybackFeedbackHud(QWidget):
    """HUD central flotante estilo YouTube para feedback de Play/Pause/Fullscreen/Seek."""

    def __init__(self, palette: ThemePalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._icon_name = "play"
        self._text = ""
        self._t = 0.0
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(82, 82)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(340)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_val)
        self._anim.finished.connect(self.hide)

    def _on_val(self, val: object) -> None:
        self._t = float(val)
        # Opacidad rápida de entrada (0..0.28) y fade-out suave (0.28..1.0)
        if self._t < 0.28:
            op = self._t / 0.28
        else:
            op = max(0.0, 1.0 - (self._t - 0.28) / 0.72)
        self.setWindowOpacity(op)
        self.update()

    def show_action(self, icon_name: str, anchor_rect: QRect, text: str = "") -> None:
        self._icon_name = icon_name
        self._text = text
        self._t = 0.0
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.center().y() - self.height() // 2
        self.move(x, y)
        self.setWindowOpacity(0.15)
        if not self.isVisible():
            self.show()
        self.raise_()
        self._anim.stop()
        self._anim.start()

    def paintEvent(self, _event) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        scale = 0.84 + 0.21 * self._t
        d = min(w, h) * 0.78 * scale
        cx = w / 2.0
        cy = h / 2.0
        circle = QRectF(cx - d / 2.0, cy - d / 2.0, d, d)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        painter.setPen(QColor(207, 213, 223, 72))
        painter.setBrush(QColor(14, 16, 20, 215))
        painter.drawEllipse(circle)

        if self._text:
            font = QFont(self.font())
            font.setPixelSize(15)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QColor(self._palette.text))
            painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, self._text)
        else:
            ico_px = max(20, int(28 * scale))
            pm = render_icon(self._icon_name, self._palette.text, 28).pixmap(QSize(56, 56))
            painter.drawPixmap(
                QRectF(cx - ico_px / 2.0, cy - ico_px / 2.0, float(ico_px), float(ico_px)),
                pm,
                QRectF(pm.rect()),
            )

        painter.end()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette


class FullscreenOverlayBar(QWidget):
    """Barra flotante semitransparente que se oculta en 0.5 s sin mover el mouse."""

    play_pause_requested = Signal()
    seek_requested = Signal(float)
    volume_changed = Signal(int)
    speed_changed = Signal(float)
    quality_changed = Signal(str)
    audio_only_toggled = Signal(bool)
    watch_later_toggled = Signal()
    download_requested = Signal()
    exit_fullscreen_requested = Signal()
    theater_requested = Signal()
    wheel_scrolled = Signal(int)
    reload_live_requested = Signal()

    def __init__(
        self,
        palette: ThemePalette,
        initial_quality: str = "720p",
        initial_speed: float = 1.0,
        initial_volume: int = 80,
        initial_audio_only: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._duration = 0.0
        self._seeking = False
        self._is_paused = True
        self._in_watch_later = False
        self._is_live_mode = False
        self._is_twitch = False
        self._prev_volume = max(10, initial_volume)

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame()
        self.frame.setObjectName("FsOverlayFrame")
        self.frame.setMouseTracking(True)
        outer.addWidget(self.frame)

        root = QVBoxLayout(self.frame)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(6)

        # Fila 1: Badge en vivo + Título + tiempo/viewers
        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        self.live_badge = QLabel("● EN VIVO")
        self.live_badge.setObjectName("LiveBadge")
        self.live_badge.setVisible(False)
        top_row.addWidget(self.live_badge)

        self.title_lbl = QLabel("Sin video")
        self.title_lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        top_row.addWidget(self.title_lbl, 1)

        self.time_lbl = QLabel("0:00 / 0:00")
        self.time_lbl.setObjectName("MutedLabel")
        top_row.addWidget(self.time_lbl)
        root.addLayout(top_row)

        # Fila 2: Seek
        self.seek_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._on_seek_release)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        root.addWidget(self.seek_slider)

        # Fila 3: Controles
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.play_btn = QPushButton()
        self.play_btn.setObjectName("PlayAccentBtn")
        self.play_btn.setFixedSize(34, 34)
        self.play_btn.setIconSize(QSize(17, 17))
        self.play_btn.setToolTip("Reproducir / Pausar [Espacio]")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.clicked.connect(self.play_pause_requested.emit)
        btn_row.addWidget(self.play_btn)

        self.mute_btn = QPushButton()
        self.mute_btn.setObjectName("IconBtn")
        self.mute_btn.setFixedSize(30, 30)
        self.mute_btn.setToolTip("Silenciar / Activar sonido [M]")
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute_btn.clicked.connect(self._toggle_mute)
        btn_row.addWidget(self.mute_btn)

        self.vol_slider = ClickableSlider(Qt.Orientation.Horizontal, volume_wheel=True)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(initial_volume)
        self.vol_slider.setFixedWidth(88)
        self.vol_slider.setToolTip("Volumen (rueda: ±5%, Shift: ±1%)")
        self.vol_slider.valueChanged.connect(self._on_vol_changed)
        btn_row.addWidget(self.vol_slider)

        btn_row.addStretch()

        self.speed_combo = QComboBox()
        for sp in VALID_SPEEDS:
            lbl = f"{int(sp)}x" if sp.is_integer() else f"{sp}x"
            self.speed_combo.addItem(lbl, sp)
        self.speed_combo.setCurrentIndex(max(0, self.speed_combo.findData(initial_speed)))
        self.speed_combo.currentIndexChanged.connect(self._on_speed_combo)
        btn_row.addWidget(self.speed_combo)

        self.quality_combo = QComboBox()
        for q in VALID_QUALITIES:
            self.quality_combo.addItem(q, q)
        self.quality_combo.setCurrentIndex(max(0, self.quality_combo.findData(initial_quality)))
        self.quality_combo.currentIndexChanged.connect(self._on_quality_combo)
        btn_row.addWidget(self.quality_combo)

        self.audio_only_btn = QPushButton(" Solo Audio")
        self.audio_only_btn.setObjectName("PillBtn")
        self.audio_only_btn.setCheckable(True)
        self.audio_only_btn.setChecked(initial_audio_only)
        self.audio_only_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audio_only_btn.toggled.connect(self._on_audio_only_toggled)
        btn_row.addWidget(self.audio_only_btn)

        self.wl_btn = QPushButton()
        self.wl_btn.setObjectName("IconBtn")
        self.wl_btn.setFixedSize(30, 30)
        self.wl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wl_btn.clicked.connect(self.watch_later_toggled.emit)
        btn_row.addWidget(self.wl_btn)

        self.reload_live_btn = QPushButton()
        self.reload_live_btn.setObjectName("IconBtn")
        self.reload_live_btn.setFixedSize(30, 30)
        self.reload_live_btn.setToolTip("Sincronizar con el directo")
        self.reload_live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_live_btn.setVisible(False)
        self.reload_live_btn.clicked.connect(self.reload_live_requested.emit)
        btn_row.addWidget(self.reload_live_btn)

        self.dl_btn = QPushButton()
        self.dl_btn.setObjectName("IconBtn")
        self.dl_btn.setFixedSize(30, 30)
        self.dl_btn.setToolTip("Descargar video")
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.clicked.connect(self.download_requested.emit)
        btn_row.addWidget(self.dl_btn)

        self.exit_fs_btn = QPushButton()
        self.exit_fs_btn.setObjectName("IconBtn")
        self.exit_fs_btn.setFixedSize(30, 30)
        self.exit_fs_btn.setToolTip("Salir de pantalla completa [Esc / F]")
        self.exit_fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exit_fs_btn.clicked.connect(self.exit_fullscreen_requested.emit)
        btn_row.addWidget(self.exit_fs_btn)

        root.addLayout(btn_row)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(500)
        self._hide_timer.timeout.connect(self._on_hide_timeout)

        self._is_fading_out = False
        self._target_pos = QPoint(0, 0)
        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.finished.connect(self._on_anim_finished)
        self._pos_anim = QPropertyAnimation(self, b"pos", self)
        self.setWindowOpacity(0.0)

        self.set_audio_only_silent(initial_audio_only)
        self.update_theme(palette)

    def reveal(self, screen_rect: QRect) -> None:
        w = min(max(660, screen_rect.width() - 80), 980)
        h = 78 if self._is_live_mode else 98
        self.setFixedSize(w, h)
        x = screen_rect.center().x() - w // 2
        y = screen_rect.bottom() - self.height() - 22
        self._target_pos = QPoint(x, y)
        if not self.isVisible():
            self._opacity_anim.stop()
            self._pos_anim.stop()
            self._is_fading_out = False
            self.setWindowOpacity(0.0)
            self.move(x, y + 14)
            self.show()
            self.raise_()
            self._start_fade_in()
        elif self._is_fading_out or self.windowOpacity() < 0.99:
            self.raise_()
            self._start_fade_in()
        else:
            self.move(self._target_pos)
            self.raise_()
        self._hide_timer.start(500)

    def _start_fade_in(self) -> None:
        cur = float(self.windowOpacity())
        self._opacity_anim.stop()
        self._pos_anim.stop()
        self._is_fading_out = False
        dur = max(70, int(180 * (1.0 - cur)))
        self._opacity_anim.setDuration(dur)
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._opacity_anim.setStartValue(cur)
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.start()

        self._pos_anim.setDuration(dur)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(self._target_pos)
        self._pos_anim.start()

    def fade_out(self) -> None:
        if not self.isVisible() or self._is_fading_out:
            return
        cur = float(self.windowOpacity())
        self._opacity_anim.stop()
        self._pos_anim.stop()
        self._is_fading_out = True
        dur = max(80, int(200 * cur))
        self._opacity_anim.setDuration(dur)
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._opacity_anim.setStartValue(cur)
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.start()

        self._pos_anim.setDuration(dur)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(QPoint(self._target_pos.x(), self._target_pos.y() + 10))
        self._pos_anim.start()

    def _on_anim_finished(self) -> None:
        if self._is_fading_out:
            self._is_fading_out = False
            self.hide()

    def hide_immediately(self) -> None:
        self._hide_timer.stop()
        self._opacity_anim.stop()
        self._pos_anim.stop()
        self._is_fading_out = False
        self.setWindowOpacity(0.0)
        self.hide()

    def _on_hide_timeout(self) -> None:
        if (
            self.underMouse()
            or self._seeking
            or self.speed_combo.view().isVisible()
            or self.quality_combo.view().isVisible()
        ):
            self._hide_timer.start(500)
            return
        self.fade_out()

    def enterEvent(self, event: QEvent) -> None:
        if self._is_fading_out or self.windowOpacity() < 0.99:
            self._start_fade_in()
        self._hide_timer.start(500)
        super().enterEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_fading_out or self.windowOpacity() < 0.99:
            self._start_fade_in()
        self._hide_timer.start(500)
        super().mouseMoveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta != 0:
            step = 1 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 5
            self.wheel_scrolled.emit(step if delta > 0 else -step)
            self._hide_timer.start(500)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F):
            self.exit_fullscreen_requested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_T:
            self.theater_requested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            self.play_pause_requested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_M:
            self._toggle_mute()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_seek_moved(self, val: int) -> None:
        if self._duration > 0:
            pos = (val / 1000.0) * self._duration
            self.time_lbl.setText(
                f"{format_duration(pos) or '0:00'} / {format_duration(self._duration) or '0:00'}"
            )
            if not self._seeking:
                self.seek_requested.emit(pos)

    def _on_seek_release(self) -> None:
        self._seeking = False
        if self._duration > 0:
            self.seek_requested.emit((self.seek_slider.value() / 1000.0) * self._duration)

    def _toggle_mute(self) -> None:
        if self.vol_slider.value() > 0:
            self._prev_volume = self.vol_slider.value()
            self.vol_slider.setValue(0)
        else:
            self.vol_slider.setValue(max(10, self._prev_volume))

    def _on_vol_changed(self, val: int) -> None:
        if val > 0:
            self._prev_volume = val
        self._refresh_vol_icon()
        self.volume_changed.emit(val)

    def _on_speed_combo(self) -> None:
        val = self.speed_combo.currentData()
        if val is not None:
            self.speed_changed.emit(float(val))

    def _on_quality_combo(self) -> None:
        val = self.quality_combo.currentData()
        if val is not None:
            self.quality_changed.emit(str(val))

    def _on_audio_only_toggled(self, checked: bool) -> None:
        self.set_audio_only_silent(checked)
        self.audio_only_toggled.emit(checked)

    def set_item(self, item: MediaItem, in_watch_later: bool) -> None:
        txt = f"{item.title}  —  {item.author}" if item.author else item.title
        self.title_lbl.setText(txt)
        self.set_watch_later(in_watch_later)

    def set_duration(self, duration: float) -> None:
        self._duration = max(0.0, duration)
        self.time_lbl.setText(f"0:00 / {format_duration(self._duration) or '0:00'}")

    def set_position(self, pos: float) -> None:
        if self._seeking or self._duration <= 0:
            return
        ratio = max(0.0, min(1.0, pos / self._duration))
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(int(ratio * 1000))
        self.seek_slider.blockSignals(False)
        self.time_lbl.setText(
            f"{format_duration(pos) or '0:00'} / {format_duration(self._duration) or '0:00'}"
        )

    def set_paused(self, paused: bool) -> None:
        self._is_paused = paused
        self.play_btn.setIcon(
            render_icon("play" if paused else "pause", self._palette.accent_text, 17)
        )

    def set_volume_silent(self, vol: int) -> None:
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(vol)
        self.vol_slider.blockSignals(False)
        self._refresh_vol_icon()

    def set_speed_silent(self, speed: float) -> None:
        idx = self.speed_combo.findData(speed)
        if idx >= 0:
            self.speed_combo.blockSignals(True)
            self.speed_combo.setCurrentIndex(idx)
            self.speed_combo.blockSignals(False)

    def set_quality_silent(self, quality: str) -> None:
        idx = self.quality_combo.findData(quality)
        if idx >= 0:
            self.quality_combo.blockSignals(True)
            self.quality_combo.setCurrentIndex(idx)
            self.quality_combo.blockSignals(False)

    def set_audio_only_silent(self, audio_only: bool) -> None:
        self.audio_only_btn.blockSignals(True)
        self.audio_only_btn.setChecked(audio_only)
        self.audio_only_btn.blockSignals(False)
        self.audio_only_btn.setObjectName("PillBtn")
        self.audio_only_btn.setProperty("active", "true" if audio_only else "false")
        self.audio_only_btn.setIcon(
            render_icon(
                "audio_only",
                self._palette.text,
                14,
            )
        )
        self.audio_only_btn.style().unpolish(self.audio_only_btn)
        self.audio_only_btn.style().polish(self.audio_only_btn)

    def set_watch_later(self, active: bool) -> None:
        self._in_watch_later = active
        color = self._palette.accent if active else self._palette.text2
        if self._is_twitch:
            icon_name = "heart_filled" if active else "heart"
            tip = "Quitar de Favoritos" if active else "Guardar en Favoritos"
        else:
            icon_name = "bookmark_filled" if active else "bookmark"
            tip = "Quitar de Ver más tarde" if active else "Guardar en Ver más tarde"
        self.wl_btn.setIcon(render_icon(icon_name, color, 16))
        self.wl_btn.setToolTip(tip)

    def set_live_mode(
        self, is_live: bool, viewers_text: str = "", is_twitch: bool = False
    ) -> None:
        self._is_live_mode = bool(is_live)
        self._is_twitch = bool(is_twitch)
        self.live_badge.setVisible(self._is_live_mode)
        self.seek_slider.setVisible(not self._is_live_mode)
        self.speed_combo.setVisible(not self._is_live_mode)
        self.dl_btn.setVisible(not self._is_live_mode)
        self.reload_live_btn.setVisible(self._is_live_mode)
        if self._is_live_mode:
            self.time_lbl.setText(viewers_text)
            self.time_lbl.setVisible(bool(viewers_text))
        else:
            self._duration = 0.0
            self.time_lbl.setText("0:00 / 0:00")
            self.time_lbl.setVisible(True)
        self.set_watch_later(self._in_watch_later)

    def _refresh_vol_icon(self) -> None:
        name = "mute" if self.vol_slider.value() == 0 else "volume"
        self.mute_btn.setIcon(render_icon(name, self._palette.text, 16))

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.dl_btn.setIcon(render_icon("download", palette.text2, 16))
        self.reload_live_btn.setIcon(render_icon("refresh", palette.text, 16))
        self.exit_fs_btn.setIcon(render_icon("exit_fullscreen", palette.text, 16))
        self.set_paused(self._is_paused)
        self.set_watch_later(self._in_watch_later)
        self._refresh_vol_icon()
        self.set_audio_only_silent(self.audio_only_btn.isChecked())
