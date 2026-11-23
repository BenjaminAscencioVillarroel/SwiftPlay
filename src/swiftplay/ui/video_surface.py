"""Panel de reproducción de video embebido con libmpv y controles dedicados."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import VALID_QUALITIES, VALID_SPEEDS
from swiftplay.models import MediaItem, format_duration
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.widgets import (
    ClickableSlider,
    ElidedLabel,
    LoadingShimmerBar as _LoadingShimmerBar,
    RoundedThumbWidget,
)


class VideoSurface(QWidget):
    """Superficie nativa HWND para renderizado directo por hardware con libmpv."""

    clicked = Signal()
    double_clicked = Signal()
    wheel_scrolled = Signal(int)
    mouse_moved = Signal()
    surface_shown = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEngine(self):
        return None

    def paintEvent(self, _event) -> None:
        # Qt no debe pintar fondo raster sobre el HWND ocupado por D3D11/libmpv.
        return

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.surface_shown.emit()

    def force_native_redraw(self) -> None:
        if not self.isVisible() or self.width() <= 4 or self.height() <= 4:
            return
        w, h = self.width(), self.height()
        if sys.platform == "win32":
            try:
                user32 = ctypes.windll.user32
                hwnd = int(self.winId())
                lparam = ((h & 0xFFFF) << 16) | (w & 0xFFFF)
                # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
                user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)
                user32.PostMessageW(hwnd, 0x0005, 0, lparam)  # WM_SIZE
                child = user32.FindWindowExW(hwnd, 0, None, None)
                if child:
                    user32.SetWindowPos(child, 0, 0, 0, w, h, 0x0014)
                    user32.PostMessageW(child, 0x0005, 0, lparam)
                # RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN
                user32.RedrawWindow(hwnd, None, None, 0x0181)
            except Exception:
                pass
        self.resize(max(2, w - 1), h)
        self.resize(w, h)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta != 0:
            step = 1 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 5
            self.wheel_scrolled.emit(step if delta > 0 else -step)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)


class VideoPlayerPanel(QWidget):
    """Reproductor de video completo sin subtítulos y con velocidades 1x–2.5x."""

    back_requested = Signal()
    play_pause_requested = Signal()
    seek_requested = Signal(float)
    volume_changed = Signal(int)
    speed_changed = Signal(float)
    quality_changed = Signal(str)
    audio_only_toggled = Signal(bool)
    watch_later_toggled = Signal()
    download_requested = Signal()
    fullscreen_toggled = Signal()
    video_clicked = Signal()
    video_double_clicked = Signal()
    theater_toggled = Signal()
    wheel_scrolled = Signal(int)
    reload_live_requested = Signal()
    channel_clicked = Signal()

    def __init__(
        self,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        initial_quality: str = "720p",
        initial_speed: float = 1.0,
        initial_volume: int = 80,
        initial_audio_only: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._duration = 0.0
        self._live_display_offset = 0.0
        self._seeking = False
        self._is_paused = True
        self._is_fullscreen = False
        self._is_theater = False
        self._in_watch_later = False
        self._is_live_mode = False
        self._is_twitch = False
        self._current_thumb_url = ""
        self._current_avatar_url = ""
        self._has_avatar_pix = False
        self._is_resume_preview = False
        self._prev_volume = max(10, initial_volume)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Barra superior compacta dentro del reproductor
        self.header_frame = QFrame()
        self.header_frame.setObjectName("TopBar")
        header_lay = QHBoxLayout(self.header_frame)
        header_lay.setContentsMargins(12, 8, 12, 8)
        header_lay.setSpacing(10)

        self.back_btn = QPushButton(" Volver")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.back_requested.emit)
        header_lay.addWidget(self.back_btn)

        self.channel_avatar_lbl = QLabel()
        self.channel_avatar_lbl.setFixedSize(24, 24)
        self.channel_avatar_lbl.setScaledContents(True)
        self.channel_avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.channel_avatar_lbl.installEventFilter(self)
        header_lay.addWidget(self.channel_avatar_lbl)

        self.channel_btn = QPushButton("")
        self.channel_btn.setObjectName("PillBtn")
        self.channel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.channel_btn.setVisible(False)
        self.channel_btn.setToolTip("Abrir canal de YouTube")
        self.channel_btn.clicked.connect(self.channel_clicked.emit)
        header_lay.addWidget(self.channel_btn)

        self.video_title_lbl = ElidedLabel("Selecciona un video", max_lines=1)
        self.video_title_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        header_lay.addWidget(self.video_title_lbl, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("MutedLabel")
        header_lay.addWidget(self.status_lbl)

        root.addWidget(self.header_frame)

        # Área central: VideoSurface (pág 0) vs Banner Solo Audio (pág 1)
        self.media_stack = QStackedWidget()
        self.video_surface = VideoSurface()
        self.video_surface.clicked.connect(self.video_clicked.emit)
        self.video_surface.double_clicked.connect(self.video_double_clicked.emit)
        self.video_surface.wheel_scrolled.connect(self.wheel_scrolled.emit)
        self.media_stack.addWidget(self.video_surface)

        audio_page = QWidget()
        audio_page_lay = QVBoxLayout(audio_page)
        audio_page_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.audio_banner = QFrame()
        self.audio_banner.setObjectName("AudioOnlyBanner")
        self.audio_banner.setFixedWidth(440)
        self.audio_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audio_banner.installEventFilter(self)
        banner_lay = QVBoxLayout(self.audio_banner)
        banner_lay.setContentsMargins(20, 20, 20, 20)
        banner_lay.setSpacing(10)
        banner_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.audio_thumb = QLabel()
        self.audio_thumb.setFixedSize(256, 144)
        self.audio_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_lay.addWidget(self.audio_thumb, 0, Qt.AlignmentFlag.AlignCenter)

        self.audio_title = QLabel("")
        self.audio_title.setWordWrap(True)
        self.audio_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.audio_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        banner_lay.addWidget(self.audio_title)

        self.audio_hint = QLabel("Modo Solo Audio activo — ahorrando GPU y RAM")
        self.audio_hint.setObjectName("MutedLabel")
        self.audio_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_lay.addWidget(self.audio_hint)

        audio_page_lay.addWidget(self.audio_banner)
        self.media_stack.addWidget(audio_page)

        # Página 2: Vista previa instantánea al cambiar de video o retomar sesión
        loading_page = QWidget()
        loading_page_lay = QVBoxLayout(loading_page)
        loading_page_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.loading_card = QWidget()
        self.loading_card.setFixedWidth(440)
        load_lay = QVBoxLayout(self.loading_card)
        load_lay.setContentsMargins(16, 16, 16, 16)
        load_lay.setSpacing(12)
        load_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.loading_thumb = RoundedThumbWidget(
            radius=12,
            show_play_on_hover=True,
            bg_color=palette.surface1,
            ambient_shadow=True,
        )
        self.loading_thumb.setFixedSize(384, 216)
        self.loading_thumb.installEventFilter(self)
        load_lay.addWidget(self.loading_thumb, 0, Qt.AlignmentFlag.AlignCenter)

        self.loading_title = QLabel("")
        self.loading_title.setWordWrap(True)
        self.loading_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_title.setStyleSheet("font-size: 14px; font-weight: 600;")
        load_lay.addWidget(self.loading_title)

        self.loading_sub = QLabel("Cargando video...")
        self.loading_sub.setObjectName("MutedLabel")
        self.loading_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        load_lay.addWidget(self.loading_sub)

        self.loading_bar = _LoadingShimmerBar(palette.accent)
        self.loading_bar.setFixedWidth(300)
        load_lay.addWidget(self.loading_bar, 0, Qt.AlignmentFlag.AlignCenter)

        loading_page_lay.addWidget(self.loading_card)
        self.media_stack.addWidget(loading_page)
        root.addWidget(self.media_stack, 1)

        # Barra inferior de controles del reproductor de video
        self.controls_frame = QFrame()
        self.controls_frame.setObjectName("VideoControlsBar")
        ctrl_root = QVBoxLayout(self.controls_frame)
        ctrl_root.setContentsMargins(12, 8, 12, 10)
        ctrl_root.setSpacing(6)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self.pos_lbl = QLabel("0:00")
        self.pos_lbl.setObjectName("MutedLabel")
        self.pos_lbl.setFixedWidth(52)
        seek_row.addWidget(self.pos_lbl)

        self.seek_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(self._on_seek_start)
        self.seek_slider.sliderReleased.connect(self._on_seek_release)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        seek_row.addWidget(self.seek_slider, 1)

        self.dur_lbl = QLabel("0:00")
        self.dur_lbl.setObjectName("MutedLabel")
        self.dur_lbl.setFixedWidth(52)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self.dur_lbl)
        ctrl_root.addLayout(seek_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.play_btn = QPushButton()
        self.play_btn.setObjectName("PlayAccentBtn")
        self.play_btn.setFixedSize(36, 36)
        self.play_btn.setIconSize(QSize(18, 18))
        self.play_btn.setToolTip("Reproducir / Pausar [Espacio]")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.clicked.connect(self.play_pause_requested.emit)
        btn_row.addWidget(self.play_btn)

        self.mute_btn = QPushButton()
        self.mute_btn.setObjectName("IconBtn")
        self.mute_btn.setFixedSize(32, 32)
        self.mute_btn.setToolTip("Silenciar / Activar sonido [M]  •  Rueda: ±5% (Shift: ±1%)")
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute_btn.clicked.connect(self._toggle_mute)
        btn_row.addWidget(self.mute_btn)

        self.vol_slider = ClickableSlider(Qt.Orientation.Horizontal, volume_wheel=True)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(initial_volume)
        self.vol_slider.setFixedWidth(92)
        self.vol_slider.setToolTip("Volumen (rueda: ±5%, Shift: ±1%)")
        self.vol_slider.valueChanged.connect(self._on_vol_changed)
        btn_row.addWidget(self.vol_slider)

        btn_row.addStretch()

        self.live_badge = QPushButton("● EN VIVO")
        self.live_badge.setObjectName("LiveBadge")
        self.live_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.live_badge.setToolTip("Ir al momento real del directo (En vivo)")
        self.live_badge.clicked.connect(self.reload_live_requested.emit)
        self.live_badge.setVisible(False)
        btn_row.addWidget(self.live_badge)

        self.viewers_lbl = QLabel("")
        self.viewers_lbl.setObjectName("MutedLabel")
        self.viewers_lbl.setVisible(False)
        btn_row.addWidget(self.viewers_lbl)

        # Velocidades estrictamente: 1x, 1.5x, 2x, 2.5x
        self.speed_combo = QComboBox()
        self.speed_combo.setToolTip("Velocidad de reproducción")
        for sp in VALID_SPEEDS:
            label = f"{int(sp)}x" if sp.is_integer() else f"{sp}x"
            self.speed_combo.addItem(label, sp)
        idx = self.speed_combo.findData(initial_speed)
        self.speed_combo.setCurrentIndex(max(0, idx))
        self.speed_combo.currentIndexChanged.connect(self._on_speed_combo)
        btn_row.addWidget(self.speed_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.setToolTip("Calidad de video")
        for q in VALID_QUALITIES:
            self.quality_combo.addItem(q, q)
        qidx = self.quality_combo.findData(initial_quality)
        self.quality_combo.setCurrentIndex(max(0, qidx))
        self.quality_combo.currentIndexChanged.connect(self._on_quality_combo)
        btn_row.addWidget(self.quality_combo)

        self.audio_only_btn = QPushButton(" Solo Audio")
        self.audio_only_btn.setObjectName("PillBtn")
        self.audio_only_btn.setCheckable(True)
        self.audio_only_btn.setChecked(initial_audio_only)
        self.audio_only_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audio_only_btn.setToolTip("Reproduce solo el audio para ahorrar GPU y RAM")
        self.audio_only_btn.toggled.connect(self._on_audio_only_toggled)
        btn_row.addWidget(self.audio_only_btn)

        self.wl_btn = QPushButton()
        self.wl_btn.setObjectName("IconBtn")
        self.wl_btn.setFixedSize(32, 32)
        self.wl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wl_btn.clicked.connect(self.watch_later_toggled.emit)
        btn_row.addWidget(self.wl_btn)

        self.reload_live_btn = QPushButton(" Ir al vivo")
        self.reload_live_btn.setObjectName("PillBtn")
        self.reload_live_btn.setToolTip("Ir al momento real del directo (En vivo)")
        self.reload_live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_live_btn.setVisible(False)
        self.reload_live_btn.clicked.connect(self.reload_live_requested.emit)
        btn_row.addWidget(self.reload_live_btn)

        self.dl_btn = QPushButton()
        self.dl_btn.setObjectName("IconBtn")
        self.dl_btn.setFixedSize(32, 32)
        self.dl_btn.setToolTip("Descargar")
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.clicked.connect(self.download_requested.emit)
        btn_row.addWidget(self.dl_btn)

        self.theater_btn = QPushButton()
        self.theater_btn.setObjectName("IconBtn")
        self.theater_btn.setFixedSize(32, 32)
        self.theater_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theater_btn.clicked.connect(self.theater_toggled.emit)
        btn_row.addWidget(self.theater_btn)

        self.fs_btn = QPushButton()
        self.fs_btn.setObjectName("IconBtn")
        self.fs_btn.setFixedSize(32, 32)
        self.fs_btn.setToolTip("Pantalla completa [F]")
        self.fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fs_btn.clicked.connect(self.fullscreen_toggled.emit)
        btn_row.addWidget(self.fs_btn)

        ctrl_root.addLayout(btn_row)
        root.addWidget(self.controls_frame)

        self._thumb_cache.ready.connect(self._on_thumb_ready)
        self.set_audio_only_visual(initial_audio_only)
        self.update_theme(palette)

    def _on_seek_start(self) -> None:
        self._seeking = True

    def _on_seek_moved(self, val: int) -> None:
        if self._duration > 0:
            pos = (val / 1000.0) * self._duration
            off = self._live_display_offset if (self._is_live_mode and not self._is_twitch) else 0.0
            self.pos_lbl.setText(format_duration(pos + off) or "0:00")
            if not self._seeking:
                self.seek_requested.emit(pos)

    def _on_seek_release(self) -> None:
        self._seeking = False
        if self._duration > 0:
            pos = (self.seek_slider.value() / 1000.0) * self._duration
            self.seek_requested.emit(pos)

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
        self.set_audio_only_visual(checked)
        self.audio_only_toggled.emit(checked)

    def set_audio_only_visual(self, audio_only: bool) -> None:
        self.audio_only_btn.blockSignals(True)
        self.audio_only_btn.setChecked(audio_only)
        self.audio_only_btn.blockSignals(False)
        self.audio_only_btn.setObjectName("PillBtn")
        self.audio_only_btn.setProperty("active", "true" if audio_only else "false")
        self.audio_only_btn.setIcon(render_icon("audio_only", self._palette.text, 15))
        self.audio_only_btn.style().unpolish(self.audio_only_btn)
        self.audio_only_btn.style().polish(self.audio_only_btn)
        if self.media_stack.currentIndex() != 2 or audio_only:
            if audio_only:
                self.loading_bar.stop()
            self.media_stack.setCurrentIndex(1 if audio_only else 0)

    def set_loading_state(self, loading: bool) -> None:
        if loading and not self.audio_only_btn.isChecked():
            self._is_resume_preview = False
            self.loading_thumb.set_force_play_overlay(False)
            self.loading_thumb.setCursor(Qt.CursorShape.ArrowCursor)
            self.loading_sub.setText("Cargando video...")
            self.loading_bar.setVisible(True)
            self.media_stack.setCurrentIndex(2)
            self.loading_bar.start()
        else:
            self._is_resume_preview = False
            self.loading_thumb.set_force_play_overlay(False)
            self.loading_thumb.setCursor(Qt.CursorShape.ArrowCursor)
            self.loading_bar.stop()
            self.loading_bar.setVisible(False)
            self.media_stack.setCurrentIndex(1 if self.audio_only_btn.isChecked() else 0)

    def set_resume_preview(self, item: MediaItem, position: float) -> None:
        """Muestra vista previa limpia para retomar un video sin cargar libmpv aún."""
        self.set_item(item, self._in_watch_later)
        if self.audio_only_btn.isChecked():
            return
        self._is_resume_preview = True
        self.loading_thumb.set_force_play_overlay(True)
        self.loading_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.loading_bar.stop()
        self.loading_bar.setVisible(False)
        pos_str = format_duration(position) if position >= 3.0 else ""
        self.loading_sub.setText(
            f"Continuar desde {pos_str}" if pos_str else "Clic para reproducir"
        )
        self.media_stack.setCurrentIndex(2)

    def set_item(self, item: MediaItem, in_watch_later: bool) -> None:
        show_chan = bool(item.kind == "video" and (item.author or item.channel_id))
        if show_chan:
            self.channel_btn.setText(item.author or "Canal")
            self.channel_btn.setVisible(True)
            self.channel_avatar_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            self.channel_avatar_lbl.setToolTip(f"Ver canal: {item.author or 'YouTube'}")
            self.video_title_lbl.setText(item.title)
        else:
            self.channel_btn.setVisible(False)
            self.channel_avatar_lbl.setCursor(Qt.CursorShape.ArrowCursor)
            self.channel_avatar_lbl.setToolTip("")
            self.video_title_lbl.setText(f"{item.title}  —  {item.author}" if item.author else item.title)
        self.audio_title.setText(f"{item.title}\n{item.author}" if item.author else item.title)
        self.loading_title.setText(f"{item.title}\n{item.author}" if item.author else item.title)
        self._current_thumb_url = item.normalized_thumb()
        self._current_avatar_url = item.channel_avatar_url or ""
        self._has_avatar_pix = False

        pix = self._thumb_cache.get_or_fetch(self._current_thumb_url, QSize(256, 144))
        if pix is not None:
            self.audio_thumb.setPixmap(pix)
        else:
            self.audio_thumb.clear()

        lpix = self._thumb_cache.get_or_fetch(self._current_thumb_url, QSize(384, 216))
        if lpix is not None:
            self.loading_thumb.set_pixmap(lpix)
        else:
            self.loading_thumb.clear()

        if self._current_avatar_url:
            av_pix = self._thumb_cache.get_or_fetch(
                self._current_avatar_url, QSize(24, 24), circular=True
            )
            if av_pix is not None:
                self._set_channel_avatar(av_pix)
            else:
                self._set_channel_initial(item.author)
        else:
            self._set_channel_initial(item.author)
        self.set_watch_later(in_watch_later)

    def _set_channel_avatar(self, pix: QPixmap) -> None:
        self._has_avatar_pix = True
        self.channel_avatar_lbl.setText("")
        self.channel_avatar_lbl.setPixmap(pix)
        self.channel_avatar_lbl.setStyleSheet("background-color: transparent; border: none;")

    def _set_channel_initial(self, author: str) -> None:
        self._has_avatar_pix = False
        self.channel_avatar_lbl.clear()
        self.channel_avatar_lbl.setText((author or "Y").strip()[:1].upper())
        self.channel_avatar_lbl.setStyleSheet(
            f"background-color: {self._palette.surface2}; color: {self._palette.text2}; "
            f"border-radius: 12px; border: 1px solid {self._palette.border}; "
            "font-size: 11px; font-weight: 700;"
        )

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._current_thumb_url:
            if pix.size() == QSize(256, 144):
                self.audio_thumb.setPixmap(pix)
            elif pix.size() == QSize(384, 216):
                self.loading_thumb.set_pixmap(pix)
        elif (
            self._current_avatar_url
            and url == self._current_avatar_url
            and pix.size() == QSize(24, 24)
        ):
            self._set_channel_avatar(pix)

    def set_status(self, text: str) -> None:
        self.status_lbl.setText(text)

    def set_live_display_offset(self, offset: float) -> None:
        self._live_display_offset = max(0.0, float(offset or 0.0))

    def set_duration(self, duration: float) -> None:
        self._duration = max(0.0, duration)
        off = self._live_display_offset if (self._is_live_mode and not self._is_twitch) else 0.0
        self.dur_lbl.setText(format_duration(self._duration + off) or "0:00")

    def set_position(self, pos: float) -> None:
        if self._seeking:
            return
        off = self._live_display_offset if (self._is_live_mode and not self._is_twitch) else 0.0
        if self._duration <= 0:
            if self._is_live_mode and not self._is_twitch and pos >= 0:
                self.pos_lbl.setText(format_duration(pos + off) or "0:00")
            return
        ratio = max(0.0, min(1.0, pos / self._duration))
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(int(ratio * 1000))
        self.seek_slider.blockSignals(False)
        self.pos_lbl.setText(format_duration(pos + off) or "0:00")

    def set_paused(self, paused: bool) -> None:
        self._is_paused = paused
        self.play_btn.setIcon(
            render_icon("play" if paused else "pause", self._palette.accent_text, 18)
        )

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

    def set_volume_silent(self, vol: int) -> None:
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(vol)
        self.vol_slider.blockSignals(False)
        self._refresh_vol_icon()

    def set_watch_later(self, active: bool) -> None:
        self._in_watch_later = active
        if self._is_twitch:
            icon_name = "heart_filled" if active else "heart"
            tip = "Quitar de Favoritos" if active else "Guardar en Favoritos"
        else:
            icon_name = "bookmark_filled" if active else "bookmark"
            tip = "Quitar de Ver más tarde" if active else "Guardar en Ver más tarde"
        color = self._palette.accent if active else self._palette.text2
        self.wl_btn.setIcon(render_icon(icon_name, color, 17))
        self.wl_btn.setToolTip(tip)

    def set_live_mode(
        self, is_live: bool, viewers_text: str = "", is_twitch: bool = False
    ) -> None:
        self._is_live_mode = bool(is_live)
        self._is_twitch = bool(is_twitch)
        if not self._is_live_mode or self._is_twitch:
            self._live_display_offset = 0.0
        hide_timeline = self._is_live_mode and self._is_twitch
        self.pos_lbl.setVisible(not hide_timeline)
        self.seek_slider.setVisible(not hide_timeline)
        self.dur_lbl.setVisible(not hide_timeline)
        self.speed_combo.setVisible(not hide_timeline)
        self.dl_btn.setVisible(not self._is_live_mode)

        self.live_badge.setVisible(self._is_live_mode)
        self.viewers_lbl.setText(viewers_text)
        self.viewers_lbl.setVisible(self._is_live_mode and bool(viewers_text))
        self.reload_live_btn.setVisible(self._is_live_mode)
        if not self._is_live_mode or hide_timeline:
            self._duration = 0.0
        self.set_watch_later(self._in_watch_later)

    def set_fullscreen_state(self, is_fs: bool) -> None:
        self._is_fullscreen = is_fs
        self.header_frame.setVisible(not is_fs)
        self.controls_frame.setVisible(not is_fs)
        self._refresh_fs_icon()

    def set_theater_state(self, active: bool) -> None:
        self._is_theater = bool(active)
        self.theater_btn.setProperty("active", "true" if self._is_theater else "false")
        self.theater_btn.style().unpolish(self.theater_btn)
        self.theater_btn.style().polish(self.theater_btn)
        self._refresh_theater_icon()

    def _refresh_vol_icon(self) -> None:
        name = "mute" if self.vol_slider.value() == 0 else "volume"
        self.mute_btn.setIcon(render_icon(name, self._palette.text, 17))

    def _refresh_theater_icon(self) -> None:
        color = self._palette.accent if self._is_theater else self._palette.text
        self.theater_btn.setIcon(render_icon("theater", color, 17))
        self.theater_btn.setToolTip(
            "Salir de modo teatro (mostrar barra lateral) [T]"
            if self._is_theater
            else "Modo teatro (ocultar barra lateral) [T]"
        )

    def _refresh_fs_icon(self) -> None:
        name = "exit_fullscreen" if self._is_fullscreen else "fullscreen"
        self.fs_btn.setIcon(render_icon(name, self._palette.text, 17))
        self.fs_btn.setToolTip(
            "Salir de pantalla completa [Esc / F]"
            if self._is_fullscreen
            else "Pantalla completa [F]"
        )

    def eventFilter(self, obj, event) -> bool:
        try:
            etype = event.type()
        except RuntimeError:
            return False
        if etype == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                if obj is self.channel_avatar_lbl and not self.channel_btn.isHidden():
                    self.channel_clicked.emit()
                    return True
                if obj is self.audio_banner or (
                    obj is self.loading_thumb and self._is_resume_preview
                ):
                    self.video_clicked.emit()
                    return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta != 0:
            step = 1 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 5
            self.wheel_scrolled.emit(step if delta > 0 else -step)
        event.accept()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.back_btn.setIcon(render_icon("back", palette.text, 15))
        self.audio_only_btn.setIcon(render_icon("audio_only", palette.text, 15))
        self.dl_btn.setIcon(render_icon("download", palette.text2, 17))
        self.reload_live_btn.setIcon(render_icon("refresh", palette.text, 16))
        self.audio_thumb.setStyleSheet(
            f"background-color: {palette.base}; border-radius: 8px; border: 1px solid {palette.border};"
        )
        self.loading_thumb.set_bg_color(palette.surface1)
        self.loading_bar.set_accent(palette.accent)
        self.set_paused(self._is_paused)
        self.set_watch_later(self._in_watch_later)
        self._refresh_vol_icon()
        self._refresh_theater_icon()
        self._refresh_fs_icon()
