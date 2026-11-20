"""Mini-barra flotante ultracompacta para controlar la reproducción."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from swiftplay.models import MediaItem
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.widgets import ClickableSlider


class MiniBarWindow(QWidget):
    """Ventana flotante ultracompacta (~430x74 px), arrastrable y tematizada."""

    expand_requested = Signal()
    play_pause_requested = Signal()
    prev_requested = Signal()
    next_requested = Signal()
    seek_requested = Signal(float)
    volume_changed = Signal(int)
    pin_toggled = Signal(bool)

    THUMB_SIZE = QSize(44, 44)

    def __init__(
        self,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        always_on_top: bool = True,
        initial_volume: int = 80,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._thumb_cache = thumb_cache
        self._palette = palette
        self._always_on_top = always_on_top
        self._is_paused = True
        self._duration = 0.0
        self._seeking = False
        self._current_thumb_url = ""
        self._drag_pos: QPoint | None = None

        self._apply_window_flags()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(440, 78)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame()
        self.frame.setObjectName("MiniBarFrame")
        outer.addWidget(self.frame)

        root = QVBoxLayout(self.frame)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.thumb_lbl = QLabel()
        self.thumb_lbl.setFixedSize(self.THUMB_SIZE)
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(self.thumb_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.title_lbl = QLabel("Sin reproducción")
        self.title_lbl.setStyleSheet("font-size: 12px; font-weight: 600;")
        self.title_lbl.setMaximumWidth(160)
        text_col.addWidget(self.title_lbl)

        self.sub_lbl = QLabel("SwiftPlay")
        self.sub_lbl.setObjectName("MutedLabel")
        self.sub_lbl.setMaximumWidth(160)
        text_col.addWidget(self.sub_lbl)
        top_row.addLayout(text_col, 1)

        self.prev_btn = QPushButton()
        self.prev_btn.setObjectName("IconBtn")
        self.prev_btn.setFixedSize(28, 28)
        self.prev_btn.setToolTip("Anterior")
        self.prev_btn.clicked.connect(self.prev_requested.emit)
        top_row.addWidget(self.prev_btn)

        self.play_btn = QPushButton()
        self.play_btn.setObjectName("PlayAccentBtn")
        self.play_btn.setFixedSize(32, 32)
        self.play_btn.setIconSize(QSize(16, 16))
        self.play_btn.setToolTip("Reproducir / Pausar [Espacio]")
        self.play_btn.clicked.connect(self.play_pause_requested.emit)
        top_row.addWidget(self.play_btn)

        self.next_btn = QPushButton()
        self.next_btn.setObjectName("IconBtn")
        self.next_btn.setFixedSize(28, 28)
        self.next_btn.setToolTip("Siguiente")
        self.next_btn.clicked.connect(self.next_requested.emit)
        top_row.addWidget(self.next_btn)

        self.vol_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(initial_volume)
        self.vol_slider.setFixedWidth(56)
        self.vol_slider.setToolTip("Volumen")
        self.vol_slider.valueChanged.connect(self.volume_changed.emit)
        top_row.addWidget(self.vol_slider)

        self.pin_btn = QPushButton()
        self.pin_btn.setObjectName("IconBtn")
        self.pin_btn.setFixedSize(26, 26)
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(always_on_top)
        self.pin_btn.setToolTip("Mantener siempre visible")
        self.pin_btn.toggled.connect(self._on_pin_toggled)
        top_row.addWidget(self.pin_btn)

        self.expand_btn = QPushButton()
        self.expand_btn.setObjectName("IconBtn")
        self.expand_btn.setFixedSize(26, 26)
        self.expand_btn.setToolTip("Volver a la ventana principal")
        self.expand_btn.clicked.connect(self.expand_requested.emit)
        top_row.addWidget(self.expand_btn)

        root.addLayout(top_row)

        self.seek_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setFixedHeight(12)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._on_seek_release)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        root.addWidget(self.seek_slider)

        self._thumb_cache.ready.connect(self._on_thumb_ready)
        self.update_theme(palette)

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def _on_pin_toggled(self, checked: bool) -> None:
        self._always_on_top = checked
        self._apply_window_flags()
        self._refresh_pin_icon()
        self.pin_toggled.emit(checked)

    def _on_seek_moved(self, val: int) -> None:
        if not self._seeking and self._duration > 0:
            self.seek_requested.emit((val / 1000.0) * self._duration)

    def _on_seek_release(self) -> None:
        self._seeking = False
        if self._duration > 0:
            self.seek_requested.emit((self.seek_slider.value() / 1000.0) * self._duration)

    def set_item(self, item: MediaItem | None) -> None:
        if item is None:
            self.title_lbl.setText("Sin reproducción")
            self.sub_lbl.setText("SwiftPlay")
            self.thumb_lbl.clear()
            self._current_thumb_url = ""
            return
        fm_t = self.title_lbl.fontMetrics()
        self.title_lbl.setText(fm_t.elidedText(item.title, Qt.TextElideMode.ElideRight, 155))
        fm_s = self.sub_lbl.fontMetrics()
        self.sub_lbl.setText(
            fm_s.elidedText(item.author or "SwiftPlay", Qt.TextElideMode.ElideRight, 155)
        )
        self._current_thumb_url = item.normalized_thumb()
        pix = self._thumb_cache.get_or_fetch(self._current_thumb_url, self.THUMB_SIZE)
        if pix is not None:
            self.thumb_lbl.setPixmap(pix)

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._current_thumb_url:
            self.thumb_lbl.setPixmap(pix)

    def set_duration(self, duration: float) -> None:
        self._duration = max(0.0, duration)

    def set_position(self, pos: float) -> None:
        if self._seeking or self._duration <= 0:
            return
        ratio = max(0.0, min(1.0, pos / self._duration))
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(int(ratio * 1000))
        self.seek_slider.blockSignals(False)

    def set_paused(self, paused: bool) -> None:
        self._is_paused = paused
        self.play_btn.setIcon(
            render_icon("play" if paused else "pause", self._palette.accent_text, 16)
        )

    def set_volume_silent(self, vol: int) -> None:
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(vol)
        self.vol_slider.blockSignals(False)

    def _refresh_pin_icon(self) -> None:
        color = self._palette.accent if self._always_on_top else self._palette.text2
        self.pin_btn.setIcon(render_icon("pin", color, 14))

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_lbl.setStyleSheet(
            f"background-color: {palette.base}; border-radius: 6px; border: 1px solid {palette.border};"
        )
        self.prev_btn.setIcon(render_icon("prev", palette.text, 15))
        self.next_btn.setIcon(render_icon("next", palette.text, 15))
        self.expand_btn.setIcon(render_icon("fullscreen", palette.text, 14))
        self.set_paused(self._is_paused)
        self._refresh_pin_icon()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)
