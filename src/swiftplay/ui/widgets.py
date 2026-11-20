"""Widgets reutilizables para tarjetas de video, filas de música y sliders."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from swiftplay.models import MediaItem, format_duration
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon


class ClickableSlider(QSlider):
    """QSlider que salta inmediatamente al punto donde el usuario hace clic."""

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
            "font-size: 15px; font-weight: 700; padding-top: 8px; padding-bottom: 2px;"
        )


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
        self._palette = palette
        self._thumb_url = item.normalized_thumb()
        self._avatar_url = item.channel_avatar_url or ""
        self._has_avatar_pix = False
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

        need_signal = False
        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_SIZE)
        if cached is not None:
            self.thumb_label.setPixmap(cached)
        else:
            need_signal = True

        if self._avatar_url:
            av_cached = thumb_cache.get_or_fetch(
                self._avatar_url, self.AVATAR_SIZE, circular=True
            )
            if av_cached is not None:
                self._set_avatar_pixmap(av_cached)
            else:
                need_signal = True

        if need_signal:
            thumb_cache.ready.connect(self._on_thumb_ready)

    def _set_avatar_pixmap(self, pix: QPixmap) -> None:
        self._has_avatar_pix = True
        self.avatar_label.setText("")
        self.avatar_label.setPixmap(pix)
        self.avatar_label.setStyleSheet("background-color: transparent; border: none;")

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url:
            self.thumb_label.setPixmap(pix)
        elif self._avatar_url and url == self._avatar_url:
            self._set_avatar_pixmap(pix)

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
    """Fila compacta para una canción de YouTube Music / Mi música / Cola."""

    play_requested = Signal(object)      # MediaItem
    my_music_toggled = Signal(object)    # MediaItem
    enqueue_requested = Signal(object)   # MediaItem
    download_requested = Signal(object)  # MediaItem
    remove_requested = Signal(object)    # MediaItem (opcional para cola)

    THUMB_SIZE = QSize(48, 48)

    def __init__(
        self,
        item: MediaItem,
        in_my_music: bool,
        thumb_cache: ThumbnailCache,
        palette: ThemePalette,
        show_enqueue: bool = True,
        show_remove: bool = False,
        is_current: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self._in_my_music = in_my_music
        self._palette = palette
        self._is_current = is_current
        self._thumb_url = item.normalized_thumb()
        self.setObjectName("MusicRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 7, 10, 7)
        root.setSpacing(10)

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(self.THUMB_SIZE)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.thumb_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_lbl = QLabel(item.title)
        self.title_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {palette.accent if is_current else palette.text};"
        )
        text_col.addWidget(self.title_lbl)

        self.artist_lbl = QLabel(item.author or "YouTube Music")
        self.artist_lbl.setObjectName("MutedLabel")
        text_col.addWidget(self.artist_lbl)
        root.addLayout(text_col, 1)

        self.dur_lbl = QLabel(item.duration)
        self.dur_lbl.setObjectName("MutedLabel")
        self.dur_lbl.setFixedWidth(48)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.dur_lbl)

        self.fav_btn = QPushButton()
        self.fav_btn.setObjectName("IconBtn")
        self.fav_btn.setFixedSize(32, 32)
        self.fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fav_btn.clicked.connect(lambda: self.my_music_toggled.emit(self.item))
        root.addWidget(self.fav_btn)

        self.queue_btn: QPushButton | None = None
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
            self.remove_btn.setFixedSize(32, 32)
            self.remove_btn.setToolTip("Quitar de la cola")
            self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.item))
            root.addWidget(self.remove_btn)

        self.update_theme(palette)
        self.set_my_music(in_my_music)

        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_SIZE)
        if cached is not None:
            self.thumb_label.setPixmap(cached)
        else:
            thumb_cache.ready.connect(self._on_thumb_ready)

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url:
            self.thumb_label.setPixmap(pix)

    def set_my_music(self, active: bool) -> None:
        self._in_my_music = active
        if active:
            self.fav_btn.setIcon(render_icon("heart_filled", self._palette.accent, 17))
            self.fav_btn.setToolTip("Quitar de Mi música")
        else:
            self.fav_btn.setIcon(render_icon("heart", self._palette.text2, 17))
            self.fav_btn.setToolTip("Guardar en Mi música")

    def set_current(self, is_current: bool) -> None:
        self._is_current = is_current
        color = self._palette.accent if is_current else self._palette.text
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {color};")

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_label.setStyleSheet(
            f"background-color: {palette.base}; border-radius: 6px; border: 1px solid {palette.border};"
        )
        color = palette.accent if self._is_current else palette.text
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {color};")
        self.dl_btn.setIcon(render_icon("download", palette.text2, 17))
        if self.queue_btn is not None:
            self.queue_btn.setIcon(render_icon("plus", palette.text2, 17))
        if self.remove_btn is not None:
            self.remove_btn.setIcon(render_icon("close", palette.text2, 16))
        self.set_my_music(self._in_my_music)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)
