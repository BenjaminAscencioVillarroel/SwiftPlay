"""Vista completa de reproducción de música con portada grande y cola integrada."""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPixmap, QRadialGradient, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from swiftplay.models import MediaItem, upgrade_thumb_url
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.widgets import ElidedLabel, MusicRowWidget, RoundedThumbWidget


class _MusicStageArea(QWidget):
    """Área izquierda (portada y alrededores): clic para pausar/reanudar, rueda para volumen y brillo ambiental."""

    clicked = Signal()
    wheel_scrolled = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._accent_color = QColor("#CFD5DF")
        self._active_bg = QColor("#2D3647")
        self._is_playing = False
        self._pulse_phase = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(40)
        self._pulse_timer.timeout.connect(self._on_pulse_tick)

    def set_theme_colors(self, accent: str, active_bg: str) -> None:
        self._accent_color = QColor(accent)
        self._active_bg = QColor(active_bg)
        self.update()

    def set_playing(self, playing: bool) -> None:
        want = bool(playing)
        if self._is_playing == want:
            return
        self._is_playing = want
        if want and self.isVisible():
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
        self.update()

    def _on_pulse_tick(self) -> None:
        self._pulse_phase = (self._pulse_phase + 0.08) % (2.0 * math.pi)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._is_playing and not self._pulse_timer.isActive():
            self._pulse_timer.start()

    def hideEvent(self, event) -> None:
        if self._pulse_timer.isActive():
            self._pulse_timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        w = float(self.width())
        h = float(self.height())
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pulse = (0.5 + 0.5 * math.sin(self._pulse_phase)) if self._is_playing else 0.0
        radius = min(w, h) * (0.48 + 0.05 * pulse)
        cx = w * 0.5
        cy = h * 0.44
        grad = QRadialGradient(QPointF(cx, cy), radius)
        inner = QColor(self._accent_color)
        inner.setAlpha(int(26 + 20 * pulse))
        mid = QColor(self._active_bg)
        mid.setAlpha(int(35 + 20 * pulse))
        outer = QColor(self._active_bg)
        outer.setAlpha(0)
        grad.setColorAt(0.0, inner)
        grad.setColorAt(0.5, mid)
        grad.setColorAt(1.0, outer)
        p.fillRect(self.rect(), grad)
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta != 0:
            step = 1 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 5
            self.wheel_scrolled.emit(step if delta > 0 else -step)
        event.accept()


class MusicNowPlayingPanel(QWidget):
    """Vista dedicada para música: portada grande a la izquierda y cola 'A continuación' a la derecha."""

    back_requested = Signal()
    play_pause_requested = Signal()
    wheel_scrolled = Signal(int)
    queue_index_requested = Signal(int)
    remove_from_queue_requested = Signal(object)  # MediaItem
    my_music_toggled = Signal(object)             # MediaItem
    download_requested = Signal(object)           # MediaItem
    autoplay_toggled = Signal()
    clear_queue_requested = Signal()

    COVER_FETCH_SIZE = QSize(380, 380)

    def __init__(
        self,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        autoplay_enabled: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._current_item: MediaItem | None = None
        self._current_thumb_url = ""
        self._in_my_music = False
        self._autoplay_enabled = bool(autoplay_enabled)
        self._radio_loading = False
        self._is_playing = False
        self._queue_rows: list[MusicRowWidget] = []
        self._queue_scroll_anim: QPropertyAnimation | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Barra superior limpia
        self.header_frame = QFrame()
        self.header_frame.setObjectName("TopBar")
        hdr = QHBoxLayout(self.header_frame)
        hdr.setContentsMargins(16, 8, 16, 8)
        hdr.setSpacing(10)

        self.back_btn = QPushButton(" Volver")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.back_requested.emit)
        hdr.addWidget(self.back_btn)

        hdr.addStretch()

        self.autoplay_btn = QPushButton("Radio infinita")
        self.autoplay_btn.setObjectName("PillBtn")
        self.autoplay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.autoplay_btn.setToolTip("Genera automáticamente nuevas canciones relacionadas al llegar al final")
        self.autoplay_btn.clicked.connect(self.autoplay_toggled.emit)
        hdr.addWidget(self.autoplay_btn)

        self.clear_btn = QPushButton()
        self.clear_btn.setObjectName("IconBtn")
        self.clear_btn.setFixedSize(30, 30)
        self.clear_btn.setToolTip("Limpiar cola")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_queue_requested.emit)
        hdr.addWidget(self.clear_btn)

        root.addWidget(self.header_frame)

        # Cuerpo dividido: izquierda (portada + info), derecha (A continuación)
        body = QHBoxLayout()
        body.setContentsMargins(24, 14, 22, 14)
        body.setSpacing(28)

        # --- Columna izquierda: Portada y acciones (clic = pausar, rueda = volumen) ---
        self.stage_area = _MusicStageArea()
        self.stage_area.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stage_area.setToolTip("Clic para pausar/reanudar  •  Rueda: ±5% (Shift: ±1%)")
        self.stage_area.clicked.connect(self._on_stage_clicked)
        self.stage_area.wheel_scrolled.connect(self.wheel_scrolled.emit)

        left_col = QVBoxLayout(self.stage_area)
        left_col.setContentsMargins(8, 8, 8, 8)
        left_col.setSpacing(12)
        left_col.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_col.addStretch(1)

        self.cover_widget = RoundedThumbWidget(
            radius=14,
            bg_color=palette.surface1,
            enable_crossfade=True,
            ambient_shadow=True,
        )
        self.cover_widget.setFixedSize(320, 320)
        self.cover_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        left_col.addWidget(self.cover_widget, 0, Qt.AlignmentFlag.AlignCenter)

        self.title_lbl = QLabel("Sin canción seleccionada")
        self.title_lbl.setMinimumWidth(0)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumWidth(440)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("font-size: 21px; font-weight: 700;")
        left_col.addWidget(self.title_lbl, 0, Qt.AlignmentFlag.AlignCenter)

        self.artist_lbl = ElidedLabel("", max_lines=1)
        self.artist_lbl.setObjectName("MutedLabel")
        self.artist_lbl.setMaximumWidth(440)
        self.artist_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.artist_lbl.setStyleSheet("font-size: 14px;")
        left_col.addWidget(self.artist_lbl, 0, Qt.AlignmentFlag.AlignCenter)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(10)
        actions_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.fav_btn = QPushButton(" Mi música")
        self.fav_btn.setObjectName("PillBtn")
        self.fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fav_btn.clicked.connect(self._on_fav_clicked)
        actions_row.addWidget(self.fav_btn)

        self.dl_btn = QPushButton(" Descargar")
        self.dl_btn.setObjectName("PillBtn")
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.clicked.connect(self._on_dl_clicked)
        actions_row.addWidget(self.dl_btn)

        left_col.addLayout(actions_row)
        left_col.addStretch(1)
        body.addWidget(self.stage_area, 3)

        # --- Columna derecha: Cola "A CONTINUACIÓN" ---
        right_panel = QWidget()
        right_panel.setMinimumWidth(300)
        right_panel.setMaximumWidth(480)
        right_lay = QVBoxLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(8)

        self.tab_lbl = QLabel("A CONTINUACIÓN")
        self.tab_lbl.setObjectName("QueueTabActive")
        right_lay.addWidget(self.tab_lbl)

        sub_row = QHBoxLayout()
        sub_row.setContentsMargins(2, 0, 2, 0)
        sub_row.setSpacing(8)
        self.queue_info_lbl = QLabel("Reproduciendo desde Radio")
        self.queue_info_lbl.setObjectName("MutedLabel")
        sub_row.addWidget(self.queue_info_lbl, 1)

        self.radio_status_lbl = QLabel("")
        self.radio_status_lbl.setObjectName("MutedLabel")
        sub_row.addWidget(self.radio_status_lbl)
        right_lay.addLayout(sub_row)

        self.queue_scroll = QScrollArea()
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        queue_container = QWidget()
        queue_container.setMinimumWidth(0)
        self.queue_lay = QVBoxLayout(queue_container)
        self.queue_lay.setContentsMargins(0, 2, 4, 8)
        self.queue_lay.setSpacing(2)

        self.empty_lbl = QLabel(
            "La cola está vacía. Reproduce cualquier canción para iniciar la radio."
        )
        self.empty_lbl.setObjectName("MutedLabel")
        self.empty_lbl.setWordWrap(True)
        self.queue_lay.addWidget(self.empty_lbl)
        self.queue_lay.addStretch()

        self.queue_scroll.setWidget(queue_container)
        right_lay.addWidget(self.queue_scroll, 1)
        body.addWidget(right_panel, 2)

        root.addLayout(body, 1)

        self._thumb_cache.ready.connect(self._on_thumb_ready)
        self.set_autoplay_state(autoplay_enabled)
        self.update_theme(palette)

    def _on_stage_clicked(self) -> None:
        if self._current_item is not None:
            self.cover_widget.trigger_press_feedback()
            self.play_pause_requested.emit()

    def _on_fav_clicked(self) -> None:
        if self._current_item is not None:
            self.my_music_toggled.emit(self._current_item)

    def _on_dl_clicked(self) -> None:
        if self._current_item is not None:
            self.download_requested.emit(self._current_item)

    def set_current_item(self, item: MediaItem | None, in_my_music: bool) -> None:
        self._current_item = item
        if item is None:
            self._current_thumb_url = ""
            self.cover_widget.clear()
            self.title_lbl.setText("Sin canción seleccionada")
            self.artist_lbl.setText("")
            self.fav_btn.setEnabled(False)
            self.dl_btn.setEnabled(False)
            self.set_my_music_state(False)
            return

        self.fav_btn.setEnabled(True)
        self.dl_btn.setEnabled(True)
        self.title_lbl.setText(item.title)
        self.artist_lbl.setText(item.author or "YouTube Music")
        self.set_my_music_state(in_my_music)

        self._current_thumb_url = upgrade_thumb_url(item.normalized_thumb(), 544)
        pix = self._thumb_cache.get_or_fetch(self._current_thumb_url, self.COVER_FETCH_SIZE)
        if pix is not None:
            self.cover_widget.set_pixmap(pix)
        else:
            self.cover_widget.clear()

    def refresh_saved_state(self, video_id: str, saved: bool) -> None:
        if self._current_item is not None and self._current_item.video_id == video_id:
            self.set_my_music_state(saved)
        for row in self._queue_rows:
            if row.item.video_id == video_id:
                row.set_my_music(saved)

    def set_my_music_state(self, active: bool) -> None:
        self._in_my_music = bool(active)
        self.fav_btn.setProperty("active", "true" if self._in_my_music else "false")
        self.fav_btn.style().unpolish(self.fav_btn)
        self.fav_btn.style().polish(self.fav_btn)
        if self._in_my_music:
            self.fav_btn.setText(" En Mi música")
            self.fav_btn.setIcon(render_icon("heart_filled", self._palette.text, 15))
        else:
            self.fav_btn.setText(" Mi música")
            self.fav_btn.setIcon(render_icon("heart", self._palette.text, 15))

    def set_autoplay_state(self, enabled: bool) -> None:
        self._autoplay_enabled = bool(enabled)
        self.autoplay_btn.setText("Radio infinita")
        self.autoplay_btn.setToolTip(
            "Radio infinita activada (clic para desactivar)"
            if self._autoplay_enabled
            else "Radio infinita desactivada (clic para activar)"
        )
        self.autoplay_btn.setProperty("active", "true" if self._autoplay_enabled else "false")
        self.autoplay_btn.style().unpolish(self.autoplay_btn)
        self.autoplay_btn.style().polish(self.autoplay_btn)

    def set_radio_loading(self, loading: bool) -> None:
        self._radio_loading = bool(loading)
        self.radio_status_lbl.setText("Cargando más canciones..." if loading else "")

    def play_entrance_animation(self) -> None:
        self.cover_widget.play_entrance()

    def set_playing_state(self, playing: bool) -> None:
        self._is_playing = bool(playing)
        self.stage_area.set_playing(self._is_playing)
        for row in self._queue_rows:
            row.set_playing_indicator(self._is_playing)

    def clear_queue_widgets(self) -> None:
        for i in reversed(range(self.queue_lay.count())):
            item = self.queue_lay.itemAt(i)
            w = item.widget() if item else None
            if w is not None and w is not self.empty_lbl:
                rel = getattr(w, "release_resources", None)
                if callable(rel):
                    rel()
                w.hide()
                self.queue_lay.removeWidget(w)
                w.deleteLater()
        self._queue_rows.clear()

    def render_queue(
        self,
        queue: list[MediaItem],
        current_index: int,
        is_saved_fn: Callable[[str], bool],
    ) -> None:
        self.clear_queue_widgets()

        n = len(queue)
        self.empty_lbl.setVisible(n == 0)
        if n == 0:
            self.queue_info_lbl.setText("Cola vacía")
            return

        mode_txt = "Radio infinita" if self._autoplay_enabled else "Cola manual"
        self.queue_info_lbl.setText(f"Reproduciendo desde {mode_txt} • {n} canciones")

        current_row: MusicRowWidget | None = None
        for idx, it in enumerate(queue):
            is_cur = idx == current_index
            row = MusicRowWidget(
                item=it,
                in_my_music=is_saved_fn(it.video_id),
                thumb_cache=self._thumb_cache,
                palette=self._palette,
                show_enqueue=False,
                show_remove=True,
                is_current=is_cur,
                compact=True,
            )
            row.play_requested.connect(lambda _it, i=idx: self.queue_index_requested.emit(i))
            row.my_music_toggled.connect(self.my_music_toggled.emit)
            row.download_requested.connect(self.download_requested.emit)
            row.remove_requested.connect(self.remove_from_queue_requested.emit)
            self._queue_rows.append(row)
            self.queue_lay.insertWidget(self.queue_lay.count() - 1, row)
            if is_cur:
                row.set_playing_indicator(self._is_playing)
                current_row = row

        if current_row is not None:
            target = current_row
            QTimer.singleShot(
                40,
                lambda: self._scroll_to_row(target),
            )

    def _scroll_to_row(self, row: MusicRowWidget) -> None:
        try:
            if row not in self._queue_rows:
                return
            vbar = self.queue_scroll.verticalScrollBar()
            vh = self.queue_scroll.viewport().height()
            target = max(
                vbar.minimum(),
                min(vbar.maximum(), row.y() - max(0, (vh - row.height()) // 2)),
            )
            if abs(vbar.value() - target) < 4:
                return
            if self._queue_scroll_anim is None:
                anim = QPropertyAnimation(vbar, b"value", self)
                anim.setDuration(220)
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                self._queue_scroll_anim = anim
            self._queue_scroll_anim.stop()
            self._queue_scroll_anim.setStartValue(vbar.value())
            self._queue_scroll_anim.setEndValue(target)
            self._queue_scroll_anim.start()
        except RuntimeError:
            pass

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if (
            self._current_thumb_url
            and url == self._current_thumb_url
            and pix.size() == self.COVER_FETCH_SIZE
        ):
            self.cover_widget.set_pixmap(pix)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        side = max(220, min(380, int(min(self.width() * 0.34, self.height() * 0.54))))
        if self.cover_widget.width() != side:
            self.cover_widget.setFixedSize(side, side)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.stage_area.set_theme_colors(palette.accent, palette.active_bg)
        self.back_btn.setIcon(render_icon("back", palette.text, 15))
        self.clear_btn.setIcon(render_icon("trash", palette.text2, 15))
        self.dl_btn.setIcon(render_icon("download", palette.text, 15))
        self.cover_widget.set_bg_color(palette.surface1)
        self.cover_widget.set_accent_color(palette.accent)
        self.set_my_music_state(self._in_my_music)
        for row in self._queue_rows:
            row.update_theme(palette)
