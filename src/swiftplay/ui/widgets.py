"""Widgets reutilizables estilo YouTube / YT Music sin cards, grillas y sliders."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
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

from swiftplay.models import MediaItem, format_duration, upgrade_thumb_url
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
            "font-size: 16px; font-weight: 700; padding-top: 8px; padding-bottom: 4px;"
        )


class RoundedThumbWidget(QWidget):
    """Miniatura con esquinas redondeadas, badge de duración, progreso y overlay Play."""

    def __init__(
        self,
        radius: int = 8,
        duration_text: str = "",
        progress_ratio: float = 0.0,
        show_play_on_hover: bool = False,
        force_play_overlay: bool = False,
        bg_color: str = "#121418",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radius = max(0, int(radius))
        self._duration_text = duration_text.strip()
        self._progress_ratio = max(0.0, min(1.0, float(progress_ratio)))
        self._show_play_on_hover = show_play_on_hover
        self._force_play_overlay = force_play_overlay
        self._bg_color = bg_color
        self._hovered = False
        self._pixmap: QPixmap | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self._pixmap = pix
        self.update()

    def clear(self) -> None:
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

    def enterEvent(self, event) -> None:
        if self._show_play_on_hover:
            self._hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._show_play_on_hover:
            self._hovered = False
            self.update()
        super().leaveEvent(event)

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

        if self._pixmap is not None and not self._pixmap.isNull():
            pw = self._pixmap.width()
            ph = self._pixmap.height()
            if pw > 0 and ph > 0:
                if pw == w and ph == h:
                    painter.drawPixmap(0, 0, self._pixmap)
                else:
                    scale = max(w / pw, h / ph)
                    dw = pw * scale
                    dh = ph * scale
                    dx = (w - dw) / 2.0
                    dy = (h - dh) / 2.0
                    painter.drawPixmap(
                        QRectF(dx, dy, dw, dh),
                        self._pixmap,
                        QRectF(0.0, 0.0, float(pw), float(ph)),
                    )

        if self._force_play_overlay or (self._show_play_on_hover and self._hovered):
            painter.fillRect(rect, QColor(11, 12, 14, 120))
            tri = max(12.0, min(36.0, min(w, h) * 0.24))
            cx = w / 2.0
            cy = h / 2.0
            play_path = QPainterPath()
            play_path.moveTo(cx - tri * 0.36, cy - tri * 0.50)
            play_path.lineTo(cx + tri * 0.52, cy)
            play_path.lineTo(cx - tri * 0.36, cy + tri * 0.50)
            play_path.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawPath(play_path)

        if self._duration_text:
            font = QFont(self.font())
            font.setPixelSize(11)
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(self._duration_text)
            th = fm.height()
            pad_x = 6
            pad_y = 2
            bw = tw + pad_x * 2
            bh = th + pad_y * 2
            margin = 7
            y_extra = 4 if self._progress_ratio > 0.0 else 0
            bx = max(4.0, float(w - bw - margin))
            by = max(4.0, float(h - bh - margin - y_extra))
            badge_rect = QRectF(bx, by, float(bw), float(bh))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 12, 14, 218))
            painter.drawRoundedRect(badge_rect, 4.0, 4.0)
            painter.setPen(QColor("#F1F3F6"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self._duration_text)

        if self._progress_ratio > 0.0:
            bar_h = 3.0
            bar_y = float(h) - bar_h
            painter.fillRect(QRectF(0.0, bar_y, float(w), bar_h), QColor(255, 255, 255, 58))
            painter.fillRect(
                QRectF(0.0, bar_y, max(4.0, float(w) * self._progress_ratio), bar_h),
                QColor("#E4E8EE"),
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

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 4, 4, 16)
        self._grid.setHorizontalSpacing(self._spacing)
        self._grid.setVerticalSpacing(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def clear_widgets(self) -> None:
        for w in self._widgets:
            self._grid.removeWidget(w)
            w.deleteLater()
        self._widgets.clear()
        self._last_cols = 0
        self.updateGeometry()

    def set_items_widgets(self, widgets: list[QWidget]) -> None:
        self.clear_widgets()
        self._widgets = list(widgets)
        self.relayout(force_regrid=True)

    def relayout(self, force_regrid: bool = False) -> None:
        if not self._widgets:
            return
        m = self._grid.contentsMargins()
        avail_w = max(self.width(), self._min_col_width + m.left() + m.right())
        inner_w = max(self._min_col_width, avail_w - m.left() - m.right())
        cols = max(
            1,
            min(
                self._max_cols,
                (inner_w + self._spacing) // (self._min_col_width + self._spacing),
            ),
        )
        col_w = max(200, (inner_w - self._spacing * (cols - 1)) // cols)

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
        self.relayout(force_regrid=False)


class VideoGridCard(QFrame):
    """Elemento de video en grilla estilo YouTube (sin borde ni fondo de card)."""

    play_requested = Signal(object)       # MediaItem
    watch_later_toggled = Signal(object)  # MediaItem
    download_requested = Signal(object)   # MediaItem
    remove_requested = Signal(object)     # MediaItem

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
        self._palette = palette
        self._show_remove = show_remove
        self._thumb_url = item.normalized_thumb()
        self._avatar_url = item.channel_avatar_url or ""
        self._has_avatar_pix = False
        self.setObjectName("YtVideoTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        ratio = 0.0
        if item.resume_pos >= 3.0 and item.duration_sec > 0:
            ratio = max(0.02, min(1.0, item.resume_pos / item.duration_sec))

        self.thumb_widget = RoundedThumbWidget(
            radius=10,
            duration_text=item.duration,
            progress_ratio=ratio,
            bg_color=palette.surface1,
        )
        self.thumb_widget.setFixedSize(272, 153)
        root.addWidget(self.thumb_widget)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
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
        text_col.setSpacing(3)

        self.title_lbl = QLabel(item.title)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumHeight(40)
        self.title_lbl.setStyleSheet("font-size: 13.5px; font-weight: 600;")
        text_col.addWidget(self.title_lbl)

        meta_parts = [p for p in (item.author or "YouTube",) if p]
        if item.resume_pos >= 3.0:
            res_str = format_duration(item.resume_pos)
            if res_str:
                meta_parts.append(f"Continuar en {res_str}")
        self.meta_lbl = QLabel("  •  ".join(meta_parts))
        self.meta_lbl.setObjectName("MutedLabel")
        self.meta_lbl.setWordWrap(False)
        text_col.addWidget(self.meta_lbl)

        meta_row.addLayout(text_col, 1)

        self.more_btn = QPushButton()
        self.more_btn.setObjectName("TileMoreBtn")
        self.more_btn.setFixedSize(28, 28)
        self.more_btn.setToolTip("Opciones")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.clicked.connect(self._open_context_menu)
        meta_row.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignTop)

        root.addLayout(meta_row)
        self.set_tile_width(272)
        self.update_theme(palette)
        self.set_watch_later(in_watch_later)

        need_signal = False
        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_FETCH_SIZE)
        if cached is not None:
            self.thumb_widget.set_pixmap(cached)
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

    def set_tile_width(self, width: int) -> None:
        w = max(200, int(width))
        h = max(112, int(round(w * 9 / 16)))
        self.setFixedWidth(w)
        self.thumb_widget.setFixedSize(w, h)

    def _open_context_menu(self) -> None:
        menu = QMenu(self)
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
        elif (
            self._avatar_url
            and url == self._avatar_url
            and pix.size() == self.AVATAR_SIZE
        ):
            self._set_avatar_pixmap(pix)

    def set_watch_later(self, active: bool) -> None:
        self._in_watch_later = active

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_widget.set_bg_color(palette.surface1)
        if not self._has_avatar_pix:
            self.avatar_label.setStyleSheet(
                f"background-color: {palette.surface2}; color: {palette.text2}; "
                f"border-radius: 17px; border: 1px solid {palette.border}; "
                "font-size: 13px; font-weight: 700;"
            )
        self.more_btn.setIcon(render_icon("more", palette.text2, 16))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)


class MusicTileWidget(QFrame):
    """Tile cuadrado estilo YouTube Music para estanterías horizontales (sin card)."""

    play_requested = Signal(object)      # MediaItem
    my_music_toggled = Signal(object)    # MediaItem
    enqueue_requested = Signal(object)   # MediaItem
    download_requested = Signal(object)  # MediaItem

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
        self._palette = palette
        self._thumb_url = upgrade_thumb_url(item.normalized_thumb(), 360)
        self.setObjectName("YtMusicTile")
        self.setFixedWidth(self.TILE_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)

        self.thumb_widget = RoundedThumbWidget(
            radius=8,
            show_play_on_hover=True,
            bg_color=palette.surface1,
        )
        self.thumb_widget.setFixedSize(self.TILE_SIZE, self.TILE_SIZE)
        root.addWidget(self.thumb_widget)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(4)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_lbl = QLabel(item.title)
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumHeight(36)
        self.title_lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        text_col.addWidget(self.title_lbl)

        self.artist_lbl = QLabel()
        self.artist_lbl.setObjectName("MutedLabel")
        fm = self.artist_lbl.fontMetrics()
        sub_text = item.author or "YouTube Music"
        if item.duration:
            sub_text = f"{sub_text}  •  {item.duration}"
        self.artist_lbl.setText(fm.elidedText(sub_text, Qt.TextElideMode.ElideRight, 138))
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
        else:
            thumb_cache.ready.connect(self._on_thumb_ready)

    def _open_context_menu(self) -> None:
        menu = QMenu(self)
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

    def contextMenuEvent(self, _event) -> None:
        self._open_context_menu()

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == self.THUMB_FETCH_SIZE:
            self.thumb_widget.set_pixmap(pix)

    def set_my_music(self, active: bool) -> None:
        self._in_my_music = active

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.thumb_widget.set_bg_color(palette.surface1)
        self.more_btn.setIcon(render_icon("more", palette.text2, 15))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
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
        bar.setValue(bar.value() + delta)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.prev_btn.setIcon(render_icon("chevron_left", palette.text, 15))
        self.next_btn.setIcon(render_icon("chevron_right", palette.text, 15))
        for tile in self._tiles:
            tile.update_theme(palette)


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
        if url == self._thumb_url and pix.size() == self.THUMB_SIZE:
            self.thumb_label.setPixmap(pix)
        elif (
            self._avatar_url
            and url == self._avatar_url
            and pix.size() == self.AVATAR_SIZE
        ):
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
    """Fila limpia estilo YouTube Music (sin borde de card) para listas, búsqueda y cola."""

    play_requested = Signal(object)      # MediaItem
    my_music_toggled = Signal(object)    # MediaItem
    enqueue_requested = Signal(object)   # MediaItem
    download_requested = Signal(object)  # MediaItem
    remove_requested = Signal(object)    # MediaItem

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
        self._palette = palette
        self._is_current = is_current
        self._compact = compact
        self._thumb_url = item.normalized_thumb()
        self.setObjectName("MusicRow")
        self.setProperty("current", "true" if is_current else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        root = QHBoxLayout(self)
        root.setContentsMargins(10 if not compact else 8, 6, 10 if not compact else 8, 6)
        root.setSpacing(10)

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
        self.dur_lbl.setFixedWidth(44)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.dur_lbl)

        self.fav_btn: QPushButton | None = None
        self.queue_btn: QPushButton | None = None
        self.dl_btn: QPushButton | None = None
        self.more_btn: QPushButton | None = None

        if compact:
            self.more_btn = QPushButton()
            self.more_btn.setObjectName("TileMoreBtn")
            self.more_btn.setFixedSize(28, 28)
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
            self.remove_btn.setFixedSize(28 if compact else 32, 28 if compact else 32)
            self.remove_btn.setToolTip("Quitar")
            self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.item))
            root.addWidget(self.remove_btn)

        self.update_theme(palette)
        self.set_my_music(in_my_music)

        cached = thumb_cache.get_or_fetch(self._thumb_url, self.THUMB_SIZE)
        if cached is not None:
            self.thumb_label.set_pixmap(cached)
        else:
            thumb_cache.ready.connect(self._on_thumb_ready)

    def _open_compact_menu(self) -> None:
        menu = QMenu(self)
        mm_label = (
            "Quitar de Mi música"
            if self._in_my_music
            else "Guardar en Mi música"
        )
        act_mm = menu.addAction(mm_label)
        act_mm.triggered.connect(lambda: self.my_music_toggled.emit(self.item))

        act_dl = menu.addAction("Descargar canción")
        act_dl.triggered.connect(lambda: self.download_requested.emit(self.item))

        if self.more_btn is not None:
            pos = self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft())
            menu.exec(pos)

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._thumb_url and pix.size() == self.THUMB_SIZE:
            self.thumb_label.set_pixmap(pix)

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
        color = self._palette.accent if is_current else self._palette.text
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {color};")

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

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self.item)
        super().mousePressEvent(event)
