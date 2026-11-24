"""Panel nativo de Discord Voz y Chat (Bot puente aislado, 0 MB en reposo)."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import html
import math
import os
from pathlib import Path
import time
from typing import Any, Literal

import requests
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig, save_config
from swiftplay.discord_service import DiscordService
from swiftplay.paths import USER_DATA_DIR
from swiftplay.player import MediaPlayer
from swiftplay.screen_share_service import (
    SCREEN_SHARE_ENDED_MARKER,
    ScreenShareService,
    download_cloudflared,
    find_cloudflared,
    find_ffmpeg,
    format_screen_share_announcement,
    parse_screen_share_message,
)
from swiftplay.security import has_discord_token
from swiftplay.storage import LocalLibrary
from swiftplay.themes import ThemePalette, apply_windows_dark_titlebar
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.video_surface import VideoSurface
from swiftplay.ui.widgets import ClickableSlider

_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_MAX_CHAT_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_CHAT_DOWNLOAD_BYTES = 25 * 1024 * 1024

_PRESENCE_META: dict[str, tuple[str, str]] = {
    "online": ("#4ADE80", "En línea"),
    "idle": ("#FACC15", "Ausente"),
    "dnd": ("#F87171", "No molestar"),
    "offline": ("#6B7280", "Desconectado"),
}


def _normalize_presence(status: Any) -> str:
    s = str(status or "offline").strip().lower()
    return s if s in _PRESENCE_META else "offline"


def _presence_color(status: Any) -> str:
    return _PRESENCE_META[_normalize_presence(status)][0]


def _presence_label(status: Any) -> str:
    return _PRESENCE_META[_normalize_presence(status)][1]


def _build_status_dot_pixmap(status: Any, size: int = 10) -> QPixmap:
    sz = max(6, int(size))
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_presence_color(status)))
    pad = 1
    painter.drawEllipse(pad, pad, sz - pad * 2, sz - pad * 2)
    painter.end()
    return pm


def _format_file_size(size: int) -> str:
    val = max(0, int(size or 0))
    if val <= 0:
        return ""
    if val < 1024:
        return f"{val} B"
    if val < 1024 * 1024:
        return f"{val / 1024.0:.1f} KB"
    return f"{val / (1024.0 * 1024.0):.1f} MB"


def _is_image_attachment(att: dict[str, Any]) -> bool:
    ctype = str(att.get("content_type") or "").strip().lower()
    if ctype.startswith("image/"):
        return True
    name = str(att.get("name") or "").strip().lower()
    url = str(att.get("url") or "").split("?", 1)[0].strip().lower()
    return any(name.endswith(ext) or url.endswith(ext) for ext in _IMAGE_EXTS)


class _AttachmentSignals(QObject):
    loaded = Signal(bytes)
    saved = Signal(str)
    failed = Signal(str)


class _AttachmentFetchTask(QRunnable):
    def __init__(self, url: str, *, save_path: str = "") -> None:
        super().__init__()
        self.url = url
        self.save_path = save_path
        self.signals = _AttachmentSignals()

    def run(self) -> None:
        try:
            with requests.get(
                self.url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                total = 0
                if self.save_path:
                    tmp_out = f"{self.save_path}.part"
                    with open(tmp_out, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > _MAX_CHAT_DOWNLOAD_BYTES:
                                raise RuntimeError("Archivo demasiado grande (>25 MB).")
                            fh.write(chunk)
                    Path(tmp_out).replace(self.save_path)
                    self.signals.saved.emit(self.save_path)
                else:
                    chunks: list[bytes] = []
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_CHAT_DOWNLOAD_BYTES:
                            raise RuntimeError("Imagen demasiado grande (>25 MB).")
                        chunks.append(chunk)
                    self.signals.loaded.emit(b"".join(chunks))
        except Exception as exc:
            try:
                if self.save_path:
                    Path(f"{self.save_path}.part").unlink(missing_ok=True)
            except Exception:
                pass
            try:
                self.signals.failed.emit(str(exc))
            except RuntimeError:
                pass


class _ImageLightboxOverlay(QWidget):
    """Visor de imágenes superpuesto dentro de la ventana (estilo Discord, sin navegador externo)."""

    closed = Signal()

    def __init__(
        self,
        title: str,
        url: str,
        pool: QThreadPool,
        palette: ThemePalette,
        on_save_cb,
        on_copied_cb=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._pool = pool
        self._palette = palette
        self._on_copied_cb = on_copied_cb
        self._sig: _AttachmentSignals | None = None
        self._orig_pixmap: QPixmap | None = None
        self._closed = False
        self._fade_progress = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if parent is not None:
            self.setGeometry(parent.rect())
            parent.installEventFilter(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 24)
        root.setSpacing(12)

        self.top_bar = QFrame(self)
        self.top_bar.setObjectName("DiscordDeckCard")
        self.top_bar.setStyleSheet(
            f"QFrame#DiscordDeckCard {{ background-color: rgba(18, 20, 24, 238); "
            f"border: 1px solid {palette.border_bright}; border-radius: 10px; }}"
        )
        bar_lay = QHBoxLayout(self.top_bar)
        bar_lay.setContentsMargins(12, 7, 10, 7)
        bar_lay.setSpacing(8)

        self.title_lbl = QLabel(title or "Vista previa", self.top_bar)
        self.title_lbl.setStyleSheet(
            f"color: {palette.text}; font-size: 12.5px; font-weight: 600; background: transparent; border: none;"
        )
        bar_lay.addWidget(self.title_lbl, 1)

        self.copy_btn = QPushButton("Copiar imagen", self.top_bar)
        self.copy_btn.setObjectName("PillBtn")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_image)
        bar_lay.addWidget(self.copy_btn)

        self.save_btn = QPushButton("Guardar como...", self.top_bar)
        self.save_btn.setObjectName("PillBtn")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(on_save_cb)
        bar_lay.addWidget(self.save_btn)

        self.close_btn = QPushButton("Cerrar", self.top_bar)
        self.close_btn.setObjectName("AccentBtn")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close_overlay)
        bar_lay.addWidget(self.close_btn)
        root.addWidget(self.top_bar)

        root.addStretch(1)
        img_row = QHBoxLayout()
        img_row.addStretch(1)
        self.img_lbl = QLabel("Cargando imagen...", self)
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setStyleSheet(
            f"color: {palette.text2}; font-size: 13px; background: rgba(20, 22, 26, 220); "
            f"border-radius: 10px; padding: 14px 20px;"
        )
        img_row.addWidget(self.img_lbl)
        img_row.addStretch(1)
        root.addLayout(img_row)
        root.addStretch(1)

        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(150)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.valueChanged.connect(self._on_fade_tick)
        self._fade_anim.start()

        task = _AttachmentFetchTask(url)
        sig = task.signals
        self._sig = sig
        sig.loaded.connect(self._on_bytes_loaded)
        sig.failed.connect(self._on_bytes_failed)
        self._pool.start(task)

    def _release_signal(self) -> None:
        if self._sig is not None:
            try:
                self._sig.deleteLater()
            except RuntimeError:
                pass
            self._sig = None

    def _fit_pixmap(self) -> None:
        if self._orig_pixmap is None or self._orig_pixmap.isNull():
            return
        max_w = max(160, self.width() - 72)
        max_h = max(120, self.height() - 120)
        if self._orig_pixmap.width() > max_w or self._orig_pixmap.height() > max_h:
            pm = self._orig_pixmap.scaled(
                max_w,
                max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            pm = self._orig_pixmap
        self.img_lbl.setPixmap(pm)
        self.img_lbl.resize(pm.size())

    def _copy_image(self) -> None:
        if self._orig_pixmap is None or self._orig_pixmap.isNull():
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setPixmap(self._orig_pixmap)
            if callable(self._on_copied_cb):
                self._on_copied_cb()

    def _on_bytes_loaded(self, data: bytes) -> None:
        self._release_signal()
        img = QImage()
        if not img.loadFromData(data):
            self.img_lbl.setText("No se pudo decodificar la imagen.")
            return
        self._orig_pixmap = QPixmap.fromImage(img)
        self.img_lbl.setText("")
        self.img_lbl.setStyleSheet("background: transparent; border: none; border-radius: 6px;")
        self.copy_btn.setEnabled(True)
        self._fit_pixmap()

    def _on_bytes_failed(self, err: str) -> None:
        self._release_signal()
        self.img_lbl.setText(f"No se pudo descargar la imagen: {err}")

    def _on_fade_tick(self, val: Any) -> None:
        self._fade_progress = max(0.0, min(1.0, float(val)))
        self.update()

    def close_overlay(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._fade_anim.stop()
        except RuntimeError:
            pass
        self._release_signal()
        p = self.parentWidget()
        if p is not None:
            try:
                p.removeEventFilter(self)
            except RuntimeError:
                pass
        self.closed.emit()
        self.hide()
        self.setParent(None)
        self.deleteLater()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        alpha = max(30, min(214, int(round(214 * self._fade_progress))))
        painter.fillRect(self.rect(), QColor(0, 0, 0, alpha))
        painter.end()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_pixmap()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_overlay()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self.top_bar.geometry().contains(pos):
            super().mousePressEvent(event)
            return
        if self._orig_pixmap is not None and self.img_lbl.geometry().contains(pos):
            event.accept()
            return
        self.close_overlay()
        event.accept()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            p = self.parentWidget()
            if p is not None:
                self.setGeometry(p.rect())
                self._fit_pixmap()
        return super().eventFilter(obj, event)


def _should_group_message(
    prev_msg: dict[str, Any] | None, cur_msg: dict[str, Any]
) -> bool:
    """Agrupa mensajes consecutivos del mismo autor en <= 5 minutos sin reply."""
    if not isinstance(prev_msg, dict):
        return False
    if isinstance(cur_msg.get("reply_to"), dict):
        return False
    if str(prev_msg.get("date_label") or "") != str(cur_msg.get("date_label") or ""):
        return False
    p_uid = str(prev_msg.get("author_id") or "").strip()
    c_uid = str(cur_msg.get("author_id") or "").strip()
    if p_uid and c_uid:
        if p_uid != c_uid:
            return False
    elif str(prev_msg.get("author_name") or "") != str(cur_msg.get("author_name") or ""):
        return False
    p_ts = float(prev_msg.get("created_ts") or 0.0)
    c_ts = float(cur_msg.get("created_ts") or 0.0)
    if p_ts > 0.0 and c_ts > 0.0:
        return 0.0 <= (c_ts - p_ts) <= 300.0
    p_str = str(prev_msg.get("timestamp") or "").strip()
    c_str = str(cur_msg.get("timestamp") or "").strip()
    return bool(p_str and p_str == c_str)


class _VoiceChannelRowFrame(QFrame):
    """Fila de canal de voz clicable directamente para entrar sin botón redundante."""

    join_requested = Signal()

    def __init__(self, can_join: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._can_join = bool(can_join)
        if self._can_join:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if (
            self._can_join
            and hasattr(event, "button")
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.join_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _ClickableThumbLabel(QLabel):
    """Miniatura clicable dentro del chat para abrir el visor interno sin navegador."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if hasattr(event, "button") and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _ChatHoverToolbar(QFrame):
    """Barra flotante única sobre el mensaje bajo el cursor (Responder / Editar / Copiar / Pegar)."""

    reply_clicked = Signal(dict)
    edit_clicked = Signal(dict)
    copy_clicked = Signal(dict)
    paste_clicked = Signal(dict)
    hovered_changed = Signal(bool)

    def __init__(self, palette: ThemePalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._msg: dict[str, Any] = {}
        self.setObjectName("ChatHoverToolbar")
        self.setStyleSheet(
            f"QFrame#ChatHoverToolbar {{ background-color: #17191E; "
            f"border: 1px solid {palette.border_bright}; border-radius: 8px; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(2)

        self.reply_btn = QPushButton(self)
        self.reply_btn.setObjectName("IconBtn")
        self.reply_btn.setFixedSize(24, 24)
        self.reply_btn.setToolTip("Responder")
        self.reply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reply_btn.clicked.connect(lambda: self.reply_clicked.emit(dict(self._msg)))
        lay.addWidget(self.reply_btn)

        self.edit_btn = QPushButton(self)
        self.edit_btn.setObjectName("IconBtn")
        self.edit_btn.setFixedSize(24, 24)
        self.edit_btn.setToolTip("Editar mensaje (solo mensajes propios del Bot)")
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(dict(self._msg)))
        lay.addWidget(self.edit_btn)

        self.copy_btn = QPushButton(self)
        self.copy_btn.setObjectName("IconBtn")
        self.copy_btn.setFixedSize(24, 24)
        self.copy_btn.setToolTip("Copiar texto seleccionado o mensaje")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(lambda: self.copy_clicked.emit(dict(self._msg)))
        lay.addWidget(self.copy_btn)

        self.paste_btn = QPushButton(self)
        self.paste_btn.setObjectName("IconBtn")
        self.paste_btn.setFixedSize(24, 24)
        self.paste_btn.setToolTip("Pegar portapapeles o texto en el chat")
        self.paste_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.paste_btn.clicked.connect(lambda: self.paste_clicked.emit(dict(self._msg)))
        lay.addWidget(self.paste_btn)

        self.update_theme(palette)
        self.hide()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"QFrame#ChatHoverToolbar {{ background-color: #17191E; "
            f"border: 1px solid {palette.border_bright}; border-radius: 8px; }}"
        )
        self.reply_btn.setIcon(render_icon("reply", palette.text, 14))
        self.edit_btn.setIcon(render_icon("edit", palette.text, 14))
        self.copy_btn.setIcon(render_icon("copy", palette.text, 14))
        self.paste_btn.setIcon(render_icon("paste", palette.text, 14))

    def bind_message(self, msg: dict[str, Any], *, can_send: bool) -> None:
        self._msg = dict(msg)
        has_id = bool(str(msg.get("id") or "").strip())
        is_self = bool(msg.get("is_self", False))
        self.reply_btn.setVisible(bool(can_send and has_id))
        self.edit_btn.setVisible(bool(can_send and has_id and is_self))
        self.copy_btn.setVisible(True)
        self.paste_btn.setVisible(bool(can_send))
        self.adjustSize()

    def enterEvent(self, event) -> None:  # noqa: ANN001
        self.hovered_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self.hovered_changed.emit(False)
        super().leaveEvent(event)


class _ChatMessageRow(QFrame):
    """Fila nativa de mensaje estilo Discord (avatar izquierda, cabecera + texto, agrupación)."""

    hovered_entered = Signal(object)
    hovered_left = Signal(object)
    context_requested = Signal(object, QPoint, str, object)  # row, global_pos, sel_text, att_ref
    image_clicked = Signal(dict)
    save_attachment_requested = Signal(dict)
    screen_share_watch_requested = Signal(dict)

    _AVATAR_SIZE = QSize(36, 36)
    _THUMB_SIZE = QSize(280, 200)

    def __init__(
        self,
        msg: dict[str, Any],
        *,
        grouped: bool,
        first_in_list: bool,
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None,
        fallback_avatar_url: str = "",
        msg_by_id: dict[str, dict[str, Any]] | None = None,
        avatar_registry: dict[str, list[QLabel]] | None = None,
        thumb_registry: dict[str, list[QLabel]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.msg = dict(msg)
        self.grouped = bool(grouped)
        self._palette = palette
        self._hovered = False
        self.avatar_lbl: QLabel | None = None
        self.hover_time_lbl: QLabel | None = None
        self.body_lbl: QLabel | None = None

        self.setObjectName("ChatMessageRow")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(
                self, self.mapToGlobal(pos), self.selected_text(), None
            )
        )
        self._apply_row_style(False)

        outer = QVBoxLayout(self)
        top_pad = 1 if self.grouped else (2 if first_in_list else 8)
        outer.setContentsMargins(10, top_pad, 10, 2)
        outer.setSpacing(2)

        raw_author = str(msg.get("author_name") or "Usuario")
        ts_txt = str(msg.get("timestamp") or "")
        is_self = bool(msg.get("is_self", False))
        is_bot = bool(msg.get("is_bot", False))
        is_edited = bool(msg.get("edited", False))

        # Cita de respuesta si aplica (siempre inicia bloque propio)
        rep = msg.get("reply_to")
        if isinstance(rep, dict):
            r_id = str(rep.get("id") or "")
            r_author = str(rep.get("author_name") or "").strip()
            r_content = str(rep.get("content") or "").strip()
            if (not r_author or not r_content) and r_id and msg_by_id and r_id in msg_by_id:
                orig = msg_by_id[r_id]
                if not r_author:
                    r_author = str(orig.get("author_name") or "Usuario")
                if not r_content:
                    r_content = str(orig.get("content") or "").strip()[:90]
            rep_row = QHBoxLayout()
            rep_row.setContentsMargins(48, 0, 0, 1)
            rep_row.setSpacing(4)
            r_author_esc = html.escape(r_author or "mensaje original")
            r_snippet = html.escape(r_content[:90].replace("\n", " "))
            rep_txt = (
                f"<span style='color:#9299A8;'>╭ ↩ <b>@{r_author_esc}</b>: {r_snippet}</span>"
                if r_snippet
                else f"<span style='color:#9299A8;'>╭ ↩ @{r_author_esc}</span>"
            )
            rep_lbl = QLabel(rep_txt, self)
            rep_lbl.setTextFormat(Qt.TextFormat.RichText)
            rep_lbl.setStyleSheet("background: transparent; border: none; font-size: 11px;")
            rep_row.addWidget(rep_lbl, 1)
            outer.addLayout(rep_row)

        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(12)

        if self.grouped:
            self.hover_time_lbl = QLabel(ts_txt, self)
            self.hover_time_lbl.setFixedWidth(36)
            self.hover_time_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
            )
            self.hover_time_lbl.setStyleSheet(
                "background: transparent; border: none; color: transparent; "
                "font-size: 10px; padding-top: 2px;"
            )
            main_row.addWidget(self.hover_time_lbl, 0, Qt.AlignmentFlag.AlignTop)
        else:
            av = QLabel(self)
            av.setFixedSize(self._AVATAR_SIZE)
            av.setAlignment(Qt.AlignmentFlag.AlignCenter)
            av.setScaledContents(True)
            av_url = str(msg.get("author_avatar_url") or fallback_avatar_url or "").strip()
            av_pix = (
                thumb_cache.get_or_fetch(av_url, self._AVATAR_SIZE, circular=True)
                if (av_url and thumb_cache is not None)
                else None
            )
            if av_pix is not None and not av_pix.isNull():
                av.setPixmap(av_pix)
                av.setStyleSheet("background: transparent; border: none;")
            else:
                av_bg = _avatar_tint_for(raw_author)
                av.setText((raw_author.strip()[:1] or "?").upper())
                av.setStyleSheet(
                    f"background-color: {av_bg}; color: #F1F3F6; border: none; "
                    "border-radius: 18px; font-size: 14px; font-weight: 700;"
                )
                if av_url and avatar_registry is not None:
                    avatar_registry.setdefault(av_url, []).append(av)
            self.avatar_lbl = av
            main_row.addWidget(av, 0, Qt.AlignmentFlag.AlignTop)

        content_col = QVBoxLayout()
        content_col.setContentsMargins(0, 0, 0, 0)
        content_col.setSpacing(2)

        if not self.grouped:
            hdr_row = QHBoxLayout()
            hdr_row.setContentsMargins(0, 0, 0, 0)
            hdr_row.setSpacing(6)
            name_color = palette.accent if is_self else palette.text
            name_lbl = QLabel(raw_author, self)
            name_lbl.setStyleSheet(
                f"background: transparent; border: none; color: {name_color}; "
                "font-size: 13.5px; font-weight: 700;"
            )
            hdr_row.addWidget(name_lbl)

            if is_bot:
                app_tag = QLabel("APP", self)
                app_tag.setObjectName("DeckTag")
                hdr_row.addWidget(app_tag)

            if ts_txt:
                ts_lbl = QLabel(ts_txt, self)
                ts_lbl.setStyleSheet(
                    f"background: transparent; border: none; color: {palette.text2}; font-size: 11px;"
                )
                hdr_row.addWidget(ts_lbl)

            hdr_row.addStretch(1)
            content_col.addLayout(hdr_row)

        raw_content = str(msg.get("content") or "")
        atts = [a for a in (msg.get("attachments") or ()) if isinstance(a, dict)]
        if raw_content or not atts:
            if raw_content:
                body_html = html.escape(raw_content).replace("\n", "<br>")
                if is_edited:
                    body_html += " <span style='color:#7E8696; font-size:11px;'>(editado)</span>"
            else:
                body_html = "<span style='color:#9299A8; font-style:italic;'>(contenido multimedia o embed)</span>"
            body = QLabel(body_html, self)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setWordWrap(True)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            body.setStyleSheet(
                "QLabel { background: transparent; border: none; color: #D8DEE9; font-size: 13px; "
                "selection-background-color: #3A3F4B; selection-color: #FFFFFF; }"
            )
            body.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            body.customContextMenuRequested.connect(
                lambda p, b=body: self.context_requested.emit(
                    self,
                    b.mapToGlobal(p),
                    (b.selectedText() or "").strip(),
                    None,
                )
            )
            self.body_lbl = body
            content_col.addWidget(body)

        ss_info = parse_screen_share_message(raw_content)
        if ss_info is not None:
            ss_btn_row = QHBoxLayout()
            ss_btn_row.setContentsMargins(0, 3, 0, 3)
            ss_btn_row.setSpacing(6)
            ss_payload = {
                "title": f"{raw_author} · Pantalla compartida",
                "author_name": raw_author,
                "watch_url": ss_info["watch_url"],
                "stream_url": ss_info["stream_url"],
                "message_id": str(msg.get("id") or ""),
            }
            watch_btn = QPushButton(" Ver pantalla en SwiftPlay")
            watch_btn.setObjectName("PillBtn")
            watch_btn.setIcon(render_icon("screen_share", palette.text, 13))
            watch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            watch_btn.setToolTip("Abrir la transmisión de pantalla directamente dentro de SwiftPlay")
            watch_btn.clicked.connect(
                lambda _c=False, p=ss_payload: self.screen_share_watch_requested.emit(p)
            )
            ss_btn_row.addWidget(watch_btn)
            ss_btn_row.addStretch(1)
            content_col.addLayout(ss_btn_row)

        for att in atts:
            att_copy = dict(att)
            aname = str(att.get("name") or "archivo")
            raw_url = str(att.get("url") or att.get("proxy_url") or "")
            sz_str = _format_file_size(int(att.get("size") or 0))
            meta_txt = f"{aname} · {sz_str}" if sz_str else aname

            if _is_image_attachment(att) and raw_url:
                thumb_lbl = _ClickableThumbLabel(self)
                thumb_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                thumb_lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                thumb_lbl.customContextMenuRequested.connect(
                    lambda p, w=thumb_lbl, ref=att_copy: self.context_requested.emit(
                        self, w.mapToGlobal(p), self.selected_text(), ref
                    )
                )
                thumb_lbl.clicked.connect(lambda ref=att_copy: self.image_clicked.emit(ref))
                pix = (
                    thumb_cache.get_or_fetch(raw_url, self._THUMB_SIZE, crop=False, radius=8)
                    if thumb_cache is not None
                    else None
                )
                if pix is not None and not pix.isNull():
                    thumb_lbl.setPixmap(pix)
                    thumb_lbl.setFixedSize(pix.size())
                    thumb_lbl.setStyleSheet("background: transparent; border: none;")
                else:
                    thumb_lbl.setText(f"🖼️ {meta_txt}  (clic para ver)")
                    thumb_lbl.setStyleSheet(
                        "background-color: #14161A; color: #B8C0CE; "
                        "border: 1px solid #272B33; border-radius: 8px; padding: 6px 10px;"
                    )
                    if thumb_registry is not None:
                        thumb_registry.setdefault(raw_url, []).append(thumb_lbl)
                content_col.addWidget(thumb_lbl, 0, Qt.AlignmentFlag.AlignLeft)

                cap_row = QHBoxLayout()
                cap_row.setContentsMargins(0, 1, 0, 2)
                cap_row.setSpacing(6)
                cap_lbl = QLabel(meta_txt, self)
                cap_lbl.setStyleSheet(
                    f"background: transparent; border: none; color: {palette.text2}; font-size: 11px;"
                )
                cap_row.addWidget(cap_lbl)
                dl_btn = QPushButton("Guardar", self)
                dl_btn.setFlat(True)
                dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                dl_btn.setStyleSheet(
                    f"QPushButton {{ background: transparent; border: none; color: {palette.accent}; font-size: 11px; padding: 0 4px; }}"
                    "QPushButton:hover { text-decoration: underline; }"
                )
                dl_btn.clicked.connect(lambda _c=False, ref=att_copy: self.save_attachment_requested.emit(ref))
                cap_row.addWidget(dl_btn)
                cap_row.addStretch(1)
                content_col.addLayout(cap_row)
            else:
                file_card = QFrame(self)
                file_card.setStyleSheet(
                    f"QFrame {{ background-color: {palette.surface2}; border: 1px solid {palette.border}; border-radius: 8px; }}"
                )
                fl = QHBoxLayout(file_card)
                fl.setContentsMargins(10, 5, 10, 5)
                fl.setSpacing(8)
                fl_lbl = QLabel(f"📎 {meta_txt}", file_card)
                fl_lbl.setStyleSheet(
                    f"background: transparent; border: none; color: {palette.text}; font-size: 12px;"
                )
                fl.addWidget(fl_lbl)
                if raw_url:
                    dl_btn = QPushButton("Guardar", file_card)
                    dl_btn.setFlat(True)
                    dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    dl_btn.setStyleSheet(
                        f"QPushButton {{ background: transparent; border: none; color: {palette.accent}; font-size: 11.5px; padding: 0 4px; }}"
                        "QPushButton:hover { text-decoration: underline; }"
                    )
                    dl_btn.clicked.connect(
                        lambda _c=False, ref=att_copy: self.save_attachment_requested.emit(ref)
                    )
                    fl.addWidget(dl_btn)
                content_col.addWidget(file_card, 0, Qt.AlignmentFlag.AlignLeft)

        main_row.addLayout(content_col, 1)
        outer.addLayout(main_row)

    def _apply_row_style(self, hovered: bool) -> None:
        if hovered:
            self.setStyleSheet(
                "QFrame#ChatMessageRow { background-color: rgba(255, 255, 255, 12); border: none; border-radius: 6px; }"
            )
        else:
            self.setStyleSheet(
                "QFrame#ChatMessageRow { background-color: transparent; border: none; border-radius: 6px; }"
            )
        if self.hover_time_lbl is not None:
            col = "#7E8696" if hovered else "transparent"
            self.hover_time_lbl.setStyleSheet(
                f"background: transparent; border: none; color: {col}; font-size: 10px; padding-top: 2px;"
            )

    def set_hovered(self, hovered: bool) -> None:
        val = bool(hovered)
        if val == self._hovered:
            return
        self._hovered = val
        self._apply_row_style(val)

    def selected_text(self) -> str:
        if self.body_lbl is not None and self.body_lbl.hasSelectedText():
            return self.body_lbl.selectedText()
        return ""

    def plain_text_summary(self) -> str:
        parts: list[str] = []
        txt = str(self.msg.get("content") or "").strip()
        if txt:
            if self.msg.get("edited"):
                parts.append(f"{txt} (editado)")
            else:
                parts.append(txt)
        elif self.msg.get("edited"):
            parts.append("(editado)")
        for a in self.msg.get("attachments") or ():
            if isinstance(a, dict) and a.get("name"):
                parts.append(str(a["name"]))
        return "\n".join(parts)

    def enterEvent(self, event) -> None:  # noqa: ANN001
        self.hovered_entered.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self.hovered_left.emit(self)
        super().leaveEvent(event)


class _ChatMessagesView(QScrollArea):
    """Lista scrollable de mensajes nativos estilo Discord con barra flotante única por hover."""

    reply_requested = Signal(dict)
    edit_requested = Signal(dict)
    copy_requested = Signal(dict, str)  # msg, selected_text
    paste_requested = Signal(dict)
    image_clicked = Signal(dict)
    save_attachment_requested = Signal(dict)
    row_context_menu_requested = Signal(object, QPoint, str, object)
    screen_share_watch_requested = Signal(dict)

    def __init__(self, palette: ThemePalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._can_send = False
        self._rows: list[_ChatMessageRow] = []
        self._avatar_labels_by_url: dict[str, list[QLabel]] = {}
        self._thumb_labels_by_url: dict[str, list[QLabel]] = {}
        self._hovered_row: _ChatMessageRow | None = None
        self._toolbar_hovered = False
        self._stick_to_bottom = True
        self._stick_until = 0.0
        self._programmatic_scroll = False
        self._last_msg_dict: dict[str, Any] | None = None
        self._last_date_label: str = ""
        self._msg_by_id: dict[str, dict[str, Any]] = {}

        self.setObjectName("TransparentScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setObjectName("TransparentBox")
        self.viewport().setAutoFillBackground(False)
        self.viewport().setAcceptDrops(True)

        self.container = QWidget()
        self.container.setObjectName("TransparentBox")
        self.container.setAcceptDrops(True)
        self._lay = QVBoxLayout(self.container)
        self._lay.setContentsMargins(2, 6, 4, 8)
        self._lay.setSpacing(1)

        self.placeholder_lbl = QLabel("")
        self.placeholder_lbl.setObjectName("MutedLabel")
        self.placeholder_lbl.setWordWrap(True)
        self.placeholder_lbl.setContentsMargins(10, 6, 10, 6)
        self.placeholder_lbl.setVisible(False)
        self._lay.addWidget(self.placeholder_lbl)
        self._lay.addStretch(1)
        self.setWidget(self.container)

        self._hover_bar = _ChatHoverToolbar(palette, self.viewport())
        self._hover_bar.reply_clicked.connect(self.reply_requested.emit)
        self._hover_bar.edit_clicked.connect(self.edit_requested.emit)
        self._hover_bar.copy_clicked.connect(self._on_toolbar_copy)
        self._hover_bar.paste_clicked.connect(self.paste_requested.emit)
        self._hover_bar.hovered_changed.connect(self._on_toolbar_hovered)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(85)
        self._hide_timer.timeout.connect(self._check_clear_hover)

        sb = self.verticalScrollBar()
        if sb is not None:
            sb.valueChanged.connect(self._on_scroll_value_changed)
            sb.rangeChanged.connect(self._on_scroll_range_changed)

    def set_can_send(self, can_send: bool) -> None:
        self._can_send = bool(can_send)
        if self._hovered_row is not None and self._hover_bar.isVisible():
            self._hover_bar.bind_message(self._hovered_row.msg, can_send=self._can_send)
            self._reposition_hover_bar()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._hover_bar.update_theme(palette)

    def message_rows(self) -> list[_ChatMessageRow]:
        return list(self._rows)

    def toPlainText(self) -> str:
        if self.placeholder_lbl.isVisible():
            return self.placeholder_lbl.text()
        return "\n".join(r.plain_text_summary() for r in self._rows)

    def show_placeholder(self, text: str) -> None:
        self._stick_to_bottom = False
        self._stick_until = 0.0
        self._clear_rows()
        self.placeholder_lbl.setText(text)
        self.placeholder_lbl.setVisible(True)
        sb = self.verticalScrollBar()
        if sb is not None:
            self._programmatic_scroll = True
            try:
                sb.setValue(0)
            finally:
                self._programmatic_scroll = False

    def clear_hover(self) -> None:
        self._hide_timer.stop()
        self._toolbar_hovered = False
        if self._hovered_row is not None:
            try:
                self._hovered_row.set_hovered(False)
            except RuntimeError:
                pass
            self._hovered_row = None
        self._hover_bar.hide()

    def _on_scroll_value_changed(self, value: int) -> None:
        self.clear_hover()
        if self._programmatic_scroll:
            return
        sb = self.verticalScrollBar()
        if sb is None:
            return
        if time.monotonic() <= self._stick_until:
            return
        self._stick_to_bottom = value >= (sb.maximum() - 28)

    def _on_scroll_range_changed(self, _min_v: int, max_v: int) -> None:
        if self._stick_to_bottom or time.monotonic() <= self._stick_until:
            sb = self.verticalScrollBar()
            if sb is not None and max_v > 0:
                self._programmatic_scroll = True
                try:
                    sb.setValue(max_v)
                finally:
                    self._programmatic_scroll = False

    def _clear_rows(self) -> None:
        self.clear_hover()
        self._avatar_labels_by_url.clear()
        self._thumb_labels_by_url.clear()
        self._last_msg_dict = None
        self._last_date_label = ""
        self._msg_by_id.clear()
        for r in self._rows:
            r.hide()
            self._lay.removeWidget(r)
            r.setParent(None)
            r.deleteLater()
        self._rows.clear()
        for i in reversed(range(self._lay.count())):
            it = self._lay.itemAt(i)
            w = it.widget() if it is not None else None
            if w is not None and w is not self.placeholder_lbl:
                w.hide()
                self._lay.removeWidget(w)
                w.setParent(None)
                w.deleteLater()

    def _create_and_insert_row(
        self,
        msg: dict[str, Any],
        *,
        grouped: bool,
        first_in_list: bool,
        thumb_cache: ThumbnailCache | None,
        fallback_peer_avatar: str,
        fallback_bot_avatar: str,
    ) -> _ChatMessageRow:
        is_self = bool(msg.get("is_self", False))
        fb_av = fallback_bot_avatar if is_self else fallback_peer_avatar
        row = _ChatMessageRow(
            msg,
            grouped=grouped,
            first_in_list=first_in_list,
            palette=self._palette,
            thumb_cache=thumb_cache,
            fallback_avatar_url=fb_av,
            msg_by_id=self._msg_by_id,
            avatar_registry=self._avatar_labels_by_url,
            thumb_registry=self._thumb_labels_by_url,
            parent=self.container,
        )
        row.hovered_entered.connect(self.hover_row)
        row.hovered_left.connect(self._on_row_left)
        row.context_requested.connect(self.row_context_menu_requested.emit)
        row.image_clicked.connect(self.image_clicked.emit)
        row.save_attachment_requested.connect(self.save_attachment_requested.emit)
        row.screen_share_watch_requested.connect(self.screen_share_watch_requested.emit)
        self._rows.append(row)
        self._lay.insertWidget(self._lay.count() - 1, row)
        return row

    def set_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        thumb_cache: ThumbnailCache | None,
        fallback_peer_avatar: str = "",
        fallback_bot_avatar: str = "",
        scroll_to_bottom: bool = True,
    ) -> set[str]:
        sb = self.verticalScrollBar()
        prev_val = sb.value() if sb is not None else 0
        pending_urls: set[str] = set()
        if scroll_to_bottom:
            self._stick_to_bottom = True
            self._stick_until = time.monotonic() + 0.9

        self.setUpdatesEnabled(False)
        try:
            self._clear_rows()
            self.placeholder_lbl.setVisible(False)
            for m in messages:
                mid = str(m.get("id") or "")
                if mid:
                    self._msg_by_id[mid] = m
            last_date = ""
            prev_msg: dict[str, Any] | None = None
            for idx, msg in enumerate(messages):
                dlabel = str(msg.get("date_label") or "").strip()
                if dlabel and dlabel != last_date:
                    last_date = dlabel
                    div = QLabel(f"─── {dlabel} ───", self.container)
                    div.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    div.setStyleSheet(
                        "background: transparent; border: none; color: #9299A8; "
                        "font-size: 11px; padding: 6px 0 4px 0;"
                    )
                    self._lay.insertWidget(self._lay.count() - 1, div)

                grouped = _should_group_message(prev_msg, msg)
                self._create_and_insert_row(
                    msg,
                    grouped=grouped,
                    first_in_list=(idx == 0),
                    thumb_cache=thumb_cache,
                    fallback_peer_avatar=fallback_peer_avatar,
                    fallback_bot_avatar=fallback_bot_avatar,
                )
                prev_msg = msg
            self._last_msg_dict = dict(prev_msg) if prev_msg is not None else None
            self._last_date_label = last_date
        finally:
            self.setUpdatesEnabled(True)

        pending_urls.update(self._avatar_labels_by_url.keys())
        pending_urls.update(self._thumb_labels_by_url.keys())

        self._lay.activate()
        self.container.adjustSize()
        if sb is not None:
            if scroll_to_bottom:
                self.scroll_to_bottom()
                QTimer.singleShot(0, self.scroll_to_bottom)
                QTimer.singleShot(50, self.scroll_to_bottom)
            else:
                self._programmatic_scroll = True
                try:
                    sb.setValue(min(prev_val, sb.maximum()))
                finally:
                    self._programmatic_scroll = False
        return pending_urls

    def append_message(
        self,
        msg: dict[str, Any],
        *,
        thumb_cache: ThumbnailCache | None,
        fallback_peer_avatar: str = "",
        fallback_bot_avatar: str = "",
    ) -> set[str]:
        if not self._rows:
            return self.set_messages(
                [msg],
                thumb_cache=thumb_cache,
                fallback_peer_avatar=fallback_peer_avatar,
                fallback_bot_avatar=fallback_bot_avatar,
                scroll_to_bottom=True,
            )
        mid = str(msg.get("id") or "")
        if mid:
            self._msg_by_id[mid] = msg
        force_scroll = self._stick_to_bottom or bool(msg.get("is_self", False))
        if force_scroll:
            self._stick_to_bottom = True
            self._stick_until = time.monotonic() + 0.7

        dlabel = str(msg.get("date_label") or "").strip()
        if dlabel and dlabel != self._last_date_label:
            self._last_date_label = dlabel
            div = QLabel(f"─── {dlabel} ───", self.container)
            div.setAlignment(Qt.AlignmentFlag.AlignCenter)
            div.setStyleSheet(
                "background: transparent; border: none; color: #9299A8; "
                "font-size: 11px; padding: 6px 0 4px 0;"
            )
            self._lay.insertWidget(self._lay.count() - 1, div)

        grouped = _should_group_message(self._last_msg_dict, msg)
        self._create_and_insert_row(
            msg,
            grouped=grouped,
            first_in_list=False,
            thumb_cache=thumb_cache,
            fallback_peer_avatar=fallback_peer_avatar,
            fallback_bot_avatar=fallback_bot_avatar,
        )
        self._last_msg_dict = dict(msg)

        while len(self._rows) > 80:
            old = self._rows.pop(0)
            old_id = str(old.msg.get("id") or "")
            if old_id:
                self._msg_by_id.pop(old_id, None)
            old.hide()
            self._lay.removeWidget(old)
            old.setParent(None)
            old.deleteLater()

        self._lay.activate()
        self.container.adjustSize()
        if force_scroll:
            self.scroll_to_bottom()
            QTimer.singleShot(0, self.scroll_to_bottom)
            QTimer.singleShot(45, self.scroll_to_bottom)

        pending: set[str] = set()
        pending.update(self._avatar_labels_by_url.keys())
        pending.update(self._thumb_labels_by_url.keys())
        return pending

    def scroll_to_bottom(self) -> None:
        self._lay.activate()
        self.container.adjustSize()
        sb = self.verticalScrollBar()
        if sb is not None:
            self._programmatic_scroll = True
            try:
                sb.setValue(sb.maximum())
            finally:
                self._programmatic_scroll = False

    def on_image_ready(self, url: str, pix: QPixmap) -> None:
        if pix.isNull():
            return
        if pix.size() == _ChatMessageRow._AVATAR_SIZE and url in self._avatar_labels_by_url:
            lbls = self._avatar_labels_by_url.pop(url, [])
            for lbl in lbls:
                try:
                    lbl.setText("")
                    lbl.setPixmap(pix)
                    lbl.setStyleSheet("background: transparent; border: none;")
                except RuntimeError:
                    pass
        elif pix.size() != _ChatMessageRow._AVATAR_SIZE and url in self._thumb_labels_by_url:
            lbls = self._thumb_labels_by_url.pop(url, [])
            for lbl in lbls:
                try:
                    lbl.setText("")
                    lbl.setPixmap(pix)
                    lbl.setFixedSize(pix.size())
                    lbl.setStyleSheet("background: transparent; border: none;")
                except RuntimeError:
                    pass
            self._lay.activate()
            self.container.adjustSize()
            if self._stick_to_bottom:
                QTimer.singleShot(0, self.scroll_to_bottom)

    def hover_row(self, row: _ChatMessageRow) -> None:
        self._hide_timer.stop()
        if self._hovered_row is not None and self._hovered_row is not row:
            try:
                self._hovered_row.set_hovered(False)
            except RuntimeError:
                pass
        self._hovered_row = row
        row.set_hovered(True)
        self._hover_bar.bind_message(row.msg, can_send=self._can_send)
        self._reposition_hover_bar()
        self._hover_bar.show()
        self._hover_bar.raise_()

    def _on_row_left(self, row: _ChatMessageRow) -> None:
        if self._hovered_row is row:
            self._hide_timer.start()

    def _on_toolbar_hovered(self, inside: bool) -> None:
        self._toolbar_hovered = bool(inside)
        if inside:
            self._hide_timer.stop()
        else:
            self._hide_timer.start()

    def _check_clear_hover(self) -> None:
        if self._toolbar_hovered:
            return
        if self._hovered_row is not None:
            try:
                vp_pos = self.viewport().mapFromGlobal(QCursor.pos())
                top_left = self._hovered_row.mapTo(self.viewport(), QPoint(0, 0))
                row_rect = self._hovered_row.rect().translated(top_left)
                if row_rect.contains(vp_pos) or (
                    self._hover_bar.isVisible() and self._hover_bar.geometry().contains(vp_pos)
                ):
                    return
            except RuntimeError:
                pass
        self.clear_hover()

    def _reposition_hover_bar(self) -> None:
        row = self._hovered_row
        if row is None or not row.isVisible():
            return
        vp = self.viewport()
        top_left = row.mapTo(vp, QPoint(0, 0))
        bw = max(56, self._hover_bar.width())
        bh = max(28, self._hover_bar.height())
        x = max(6, min(vp.width() - bw - 8, top_left.x() + row.width() - bw - 10))
        y = max(4, min(vp.height() - bh - 4, top_left.y() - 6))
        self._hover_bar.move(x, y)

    def _on_toolbar_copy(self, msg: dict[str, Any]) -> None:
        sel = self._hovered_row.selected_text() if self._hovered_row is not None else ""
        self.copy_requested.emit(msg, sel)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._hovered_row is not None and self._hover_bar.isVisible():
            self._reposition_hover_bar()

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._hide_timer.start()
        super().leaveEvent(event)


_AVATAR_TINTS: tuple[str, ...] = (
    "#5A362D",
    "#334155",
    "#3B4454",
    "#2F4840",
    "#4B3B58",
    "#4F4430",
)


def _avatar_tint_for(name: str) -> str:
    acc = sum(ord(ch) for ch in (name or "U"))
    return _AVATAR_TINTS[acc % len(_AVATAR_TINTS)]


def _blend_hex(base_rgb: tuple[int, int, int], tint_rgb: tuple[int, int, int], weight: float) -> str:
    w = max(0.0, min(1.0, float(weight)))
    r = max(12, min(230, int(round(base_rgb[0] * (1.0 - w) + tint_rgb[0] * w))))
    g = max(14, min(230, int(round(base_rgb[1] * (1.0 - w) + tint_rgb[1] * w))))
    b = max(18, min(235, int(round(base_rgb[2] * (1.0 - w) + tint_rgb[2] * w))))
    return f"#{r:02X}{g:02X}{b:02X}"


def _extract_avatar_tint(pix: QPixmap) -> tuple[str, str]:
    """Calcula (bg_hex, border_hex) oscuros pero teñidos con el color dominante del avatar."""
    if pix.isNull():
        return "#1C212B", "#2E3646"
    img = pix.scaled(
        8,
        8,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    ).toImage().convertToFormat(QImage.Format.Format_ARGB32)

    r_sum = g_sum = b_sum = count = 0
    fb_r = fb_g = fb_b = fb_count = 0
    for y in range(img.height()):
        for x in range(img.width()):
            col = img.pixelColor(x, y)
            if col.alpha() < 120:
                continue
            r, g, b = col.red(), col.green(), col.blue()
            fb_r += r
            fb_g += g
            fb_b += b
            fb_count += 1
            if (r + g + b) >= 48 and (r + g + b) <= 710:
                r_sum += r
                g_sum += g
                b_sum += b
                count += 1

    if count == 0:
        if fb_count == 0:
            return "#1C212B", "#2E3646"
        ar, ag, ab = fb_r // fb_count, fb_g // fb_count, fb_b // fb_count
    else:
        ar, ag, ab = r_sum // count, g_sum // count, b_sum // count

    card_bg = _blend_hex((21, 25, 34), (ar, ag, ab), 0.25)
    card_border = _blend_hex((42, 49, 64), (ar, ag, ab), 0.45)
    return card_bg, card_border


def _default_card_tint(name: str) -> tuple[str, str]:
    c = QColor(_avatar_tint_for(name))
    rgb = (c.red(), c.green(), c.blue())
    return _blend_hex((21, 25, 34), rgb, 0.22), _blend_hex((42, 49, 64), rgb, 0.38)


def _clear_layout(layout: QLayout) -> None:
    while layout.count() > 0:
        item = layout.takeAt(0)
        if item is None:
            continue
        w = item.widget()
        if w is not None:
            rel = getattr(w, "release_resources", None)
            if callable(rel):
                try:
                    rel()
                except Exception:
                    pass
            w.hide()
            w.setParent(None)
            w.deleteLater()
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)


def _clear_grid_keep_widgets(grid: QGridLayout) -> None:
    while grid.count() > 0:
        grid.takeAt(0)


def _compute_voice_row_sizes(count: int, max_cols: int) -> list[int]:
    """Calcula la distribución balanceada por filas al estilo Discord (1, 2, 2+1, 2+2, 3+2, etc.)."""
    n = max(0, int(count))
    if n == 0:
        return []
    cap = max(1, int(max_cols))
    if cap == 1:
        return [1] * n
    if n == 1:
        return [1]
    if n == 2:
        return [2]
    if n == 3:
        return [2, 1]
    if n == 4:
        return [2, 2]
    if n == 5:
        return [3, 2] if cap >= 3 else [2, 2, 1]
    if n == 6:
        return [3, 3] if cap >= 3 else [2, 2, 2]
    if n == 7:
        if cap >= 4:
            return [4, 3]
        if cap == 3:
            return [3, 2, 2]
        return [2, 2, 2, 1]
    if n == 8:
        if cap >= 4:
            return [4, 4]
        if cap == 3:
            return [3, 3, 2]
        return [2, 2, 2, 2]
    if n == 9:
        if cap >= 3:
            return [3, 3, 3]
        return [2, 2, 2, 2, 1]
    rows = math.ceil(n / cap)
    base, rem = divmod(n, rows)
    return [base + (1 if i < rem else 0) for i in range(rows)]


def _compute_participant_card_size(
    count: int,
    row_sizes: list[int],
    avail_w: int,
    avail_h: int,
    spacing: int = 12,
    *,
    has_screen_share: bool = False,
) -> tuple[int, int]:
    """Dimensiona las tarjetas según la cantidad de participantes y el viewport disponible."""
    if count <= 1:
        pref_w, pref_h = 280, 196
    elif count == 2:
        pref_w, pref_h = 260, 186
    elif count <= 4:
        pref_w, pref_h = 240, 174
    elif count <= 6:
        pref_w, pref_h = 222, 162
    else:
        pref_w, pref_h = 205, 154

    if has_screen_share:
        pref_w = max(pref_w, 236)
        pref_h = max(pref_h, 210 if count <= 2 else 204)

    min_w = 204 if has_screen_share else 164
    min_h = 202 if has_screen_share else 142
    max_row = max(row_sizes) if row_sizes else 1
    num_rows = max(1, len(row_sizes))
    usable_w = max(190, int(avail_w) - 24)
    usable_h = max(160, int(avail_h) - 24)
    max_card_w = max(min_w, (usable_w - spacing * max(0, max_row - 1)) // max_row)
    max_card_h = max(min_h, (usable_h - spacing * max(0, num_rows - 1)) // num_rows)
    card_w = max(min_w, min(pref_w, max_card_w))
    card_h = max(min_h, min(pref_h, max_card_h))
    return card_w, card_h


def _make_section_toggle_btn(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("SectionHeader")
    btn.setFlat(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        "QPushButton { border: none; background: transparent; text-align: left; padding: 4px 2px; }"
    )
    return btn


def exec_user_dm_menu(
    parent: QWidget,
    uid: str,
    name: str,
    is_bot: bool,
    global_pos: Any,
) -> bool:
    """Muestra menú contextual para abrir DM con una persona (deshabilitado para otros bots)."""
    menu = QMenu(parent)
    try:
        if is_bot:
            act = menu.addAction("No se puede enviar DM a otro bot")
            act.setEnabled(False)
            menu.exec(global_pos)
            return False
        if not uid:
            return False
        act_dm = menu.addAction(f"Enviar DM a {name or uid}")
        chosen = menu.exec(global_pos)
        return chosen is act_dm
    finally:
        menu.deleteLater()


class _AvatarCircleWidget(QWidget):
    """Avatar circular con foto de perfil real (o inicial fallback) y anillo animado de habla."""

    def __init__(
        self,
        name: str,
        palette: ThemePalette,
        *,
        size_px: int = 64,
        speaking: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._name = name or "?"
        self._palette = palette
        self._speaking = bool(speaking)
        self._speak_t = 1.0 if self._speaking else 0.0
        self._speak_anim: QVariantAnimation | None = None
        self._pulse_phase = 0.0
        self._pulse_timer: QTimer | None = None
        self._bg_hex = _avatar_tint_for(self._name)
        self._pixmap: QPixmap | None = None
        self.setFixedSize(size_px, size_px)

    def _on_speak_val(self, val: object) -> None:
        self._speak_t = float(val)
        self.update()

    def _on_pulse_tick(self) -> None:
        self._pulse_phase = (self._pulse_phase + 0.16) % (2.0 * math.pi)
        self.update()

    def _sync_pulse_timer(self) -> None:
        if self._speaking and self.isVisible():
            if self._pulse_timer is None:
                timer = QTimer(self)
                timer.setInterval(50)
                timer.timeout.connect(self._on_pulse_tick)
                self._pulse_timer = timer
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        elif self._pulse_timer is not None and self._pulse_timer.isActive():
            self._pulse_timer.stop()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_pulse_timer()

    def hideEvent(self, event: Any) -> None:
        if self._pulse_timer is not None and self._pulse_timer.isActive():
            self._pulse_timer.stop()
        super().hideEvent(event)

    def set_speaking(self, speaking: bool) -> None:
        val = bool(speaking)
        if val != self._speaking:
            self._speaking = val
            if self._speak_anim is None:
                anim = QVariantAnimation(self)
                anim.setDuration(180)
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                anim.valueChanged.connect(self._on_speak_val)
                self._speak_anim = anim
            self._speak_anim.stop()
            self._speak_anim.setStartValue(self._speak_t)
            self._speak_anim.setEndValue(1.0 if val else 0.0)
            self._speak_anim.start()
            self._sync_pulse_timer()
            self.update()

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self._pixmap = pix if (pix is not None and not pix.isNull()) else None
        self.update()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        w = float(self.width())
        h = float(self.height())
        ring_rect = QRectF(3.0, 3.0, w - 6.0, h - 6.0)
        inner_rect = QRectF(7.0, 7.0, w - 14.0, h - 14.0)

        t = max(0.0, min(1.0, self._speak_t))
        if t > 0.01:
            pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
            halo = QColor(74, 222, 128, int((48 + 46 * pulse) * t))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(halo, 4.5 + 1.5 * pulse))
            painter.drawEllipse(QRectF(1.5, 1.5, w - 3.0, h - 3.0))

        base_c = QColor(self._palette.border_bright)
        speak_c = QColor("#4ADE80")
        ring_color = QColor(
            int(base_c.red() + (speak_c.red() - base_c.red()) * t),
            int(base_c.green() + (speak_c.green() - base_c.green()) * t),
            int(base_c.blue() + (speak_c.blue() - base_c.blue()) * t),
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ring_color, 3.0))
        painter.drawEllipse(ring_rect)

        if self._pixmap is not None and not self._pixmap.isNull():
            clip = QPainterPath()
            clip.addEllipse(inner_rect)
            painter.save()
            painter.setClipPath(clip)
            scaled = self._pixmap.scaled(
                int(inner_rect.width()),
                int(inner_rect.height()),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            sx = inner_rect.x() + (inner_rect.width() - scaled.width()) / 2.0
            sy = inner_rect.y() + (inner_rect.height() - scaled.height()) / 2.0
            painter.drawPixmap(int(round(sx)), int(round(sy)), scaled)
            painter.restore()
        else:
            painter.setBrush(QColor(self._bg_hex))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(inner_rect)

            initial = (self._name.strip()[:1] or "?").upper()
            font = QFont("Segoe UI", 21, QFont.Weight.Bold)
            painter.setFont(font)
            painter.setPen(QColor("#F1F3F6"))
            painter.drawText(inner_rect, Qt.AlignmentFlag.AlignCenter, initial)
        painter.end()


class _ParticipantCard(QFrame):
    """Tarjeta de participante en voz con avatar real y tinte extraído del perfil."""

    volume_changed = Signal(str, int)
    dm_requested = Signal(str, str, str)
    watch_share_requested = Signal(dict)
    _AVATAR_FETCH_SIZE = QSize(64, 64)

    def __init__(
        self,
        part: dict[str, Any],
        volume: int,
        speaking: bool,
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._uid = str(part.get("id") or "")
        self._is_self = bool(part.get("is_self", False))
        self._muted = bool(part.get("muted", False))
        self._deafened = bool(part.get("deafened", False))
        self._speaking = bool(speaking)
        self._palette = palette
        self._thumb_cache = thumb_cache
        self._avatar_url = str(part.get("avatar_url") or "")
        self._thumb_connected = False
        self._is_self_streaming = bool(self._is_self and part.get("is_streaming", False))
        raw_ss = part.get("screen_share")
        self._screen_share: dict[str, Any] | None = (
            dict(raw_ss)
            if isinstance(raw_ss, dict) and str(raw_ss.get("stream_url") or "").strip() and not self._is_self
            else None
        )

        name = str(part.get("name") or self._uid)
        is_bot = bool(part.get("is_bot", False))
        self._name = name
        self._is_bot = is_bot
        self._card_bg_hex, self._card_border_hex = _default_card_tint(name)

        self.setObjectName("DiscordParticipantCard")
        self.setMinimumWidth(204 if (self._screen_share is not None or self._is_self_streaming) else 164)
        self.setMinimumHeight(202 if (self._screen_share is not None or self._is_self_streaming) else 142)
        if not self._is_self:
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._on_context_menu)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        lay.addStretch(1)

        self.avatar = _AvatarCircleWidget(
            name=name,
            palette=palette,
            size_px=64,
            speaking=speaking,
        )
        lay.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)

        title_row = QHBoxLayout()
        title_row.setSpacing(5)
        title_row.addStretch(1)

        self._status_dot_lbl = QLabel()
        self._status_dot_lbl.setFixedSize(10, 10)
        st_init = _normalize_presence(part.get("status") or ("online" if self._is_self else "offline"))
        self._status_dot_lbl.setPixmap(_build_status_dot_pixmap(st_init, 9))
        self._status_dot_lbl.setToolTip(_presence_label(st_init))
        title_row.addWidget(self._status_dot_lbl)

        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            "font-size: 13px; font-weight: 700;"
            if self._is_self
            else "font-size: 13px; font-weight: 600;"
        )
        title_row.addWidget(name_lbl)

        if is_bot:
            tag = QLabel("APP")
            tag.setObjectName("DeckTag")
            title_row.addWidget(tag)

        self._mute_ico: QLabel | None = None
        if self._muted:
            self._mute_ico = QLabel()
            self._mute_ico.setToolTip("Micrófono silenciado")
            title_row.addWidget(self._mute_ico)

        self._deaf_ico: QLabel | None = None
        if self._deafened:
            self._deaf_ico = QLabel()
            self._deaf_ico.setToolTip("Audio ensordecido")
            title_row.addWidget(self._deaf_ico)

        title_row.addStretch(1)
        lay.addLayout(title_row)

        if self._screen_share is not None:
            ss_row = QHBoxLayout()
            ss_row.setSpacing(6)
            ss_row.addStretch(1)

            live_tag = QLabel("EN VIVO")
            live_tag.setObjectName("ScreenShareLiveBadge")
            live_tag.setFixedHeight(22)
            ss_row.addWidget(live_tag)

            q_txt = str(self._screen_share.get("quality") or "").strip()
            fps_txt = int(self._screen_share.get("fps") or 0)
            spec = f" ({q_txt} {fps_txt}fps)" if q_txt and fps_txt else ""
            watch_btn = QPushButton("Ver pantalla")
            watch_btn.setObjectName("ScreenShareCardWatchBtn")
            watch_btn.setFixedHeight(24)
            watch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            watch_btn.setToolTip(f"Ver pantalla compartida de {name}{spec} directo en SwiftPlay")
            watch_btn.clicked.connect(
                lambda _c=False, p=dict(self._screen_share): self.watch_share_requested.emit(p)
            )
            ss_row.addWidget(watch_btn)
            ss_row.addStretch(1)
            lay.addLayout(ss_row)
        elif self._is_self_streaming:
            ss_row = QHBoxLayout()
            ss_row.setSpacing(6)
            ss_row.addStretch(1)
            live_tag = QLabel("● EN VIVO")
            live_tag.setObjectName("ScreenShareLiveBadge")
            live_tag.setFixedHeight(22)
            live_tag.setToolTip("Estás transmitiendo pantalla en vivo")
            ss_row.addWidget(live_tag)
            ss_row.addStretch(1)
            lay.addLayout(ss_row)

        if not self._is_self:
            vol_row = QHBoxLayout()
            vol_row.setSpacing(6)
            self._vol_ico = QLabel()
            self._vol_ico.setFixedSize(16, 16)
            vol_row.addWidget(self._vol_ico)

            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(0, 200)
            self.slider.setValue(max(0, min(200, int(volume))))
            self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
            vol_row.addWidget(self.slider, 1)

            self.vol_lbl = QLabel(f"{self.slider.value()}%")
            self.vol_lbl.setObjectName("MutedLabel")
            self.vol_lbl.setFixedWidth(36)
            self.vol_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            vol_row.addWidget(self.vol_lbl)

            self.slider.valueChanged.connect(self._on_slider_changed)
            lay.addLayout(vol_row)
        else:
            self._vol_ico = None
            self.slider = None
            self.vol_lbl = None
            self_lbl = QLabel("Tú")
            self_lbl.setObjectName("MutedLabel")
            self_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self_lbl.setFixedHeight(20)
            lay.addWidget(self_lbl)

        lay.addStretch(1)
        self._load_avatar()
        self.update_theme(palette)

    def set_status(self, status: str) -> None:
        st = _normalize_presence(status)
        self._status_dot_lbl.setPixmap(_build_status_dot_pixmap(st, 9))
        self._status_dot_lbl.setToolTip(_presence_label(st))

    def _load_avatar(self) -> None:
        if self._thumb_cache is None or not self._avatar_url:
            return
        cached = self._thumb_cache.get_or_fetch(
            self._avatar_url, self._AVATAR_FETCH_SIZE, circular=True
        )
        if cached is not None:
            self._apply_avatar_pixmap(cached)
        else:
            self._thumb_cache.ready.connect(self._on_thumb_ready)
            self._thumb_connected = True

    def _disconnect_thumb(self) -> None:
        if self._thumb_connected and self._thumb_cache is not None:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_thumb_ready)
            except (RuntimeError, TypeError):
                pass

    def _on_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url == self._avatar_url and pix.size() == self._AVATAR_FETCH_SIZE:
            self._apply_avatar_pixmap(pix)
            self._disconnect_thumb()

    def _apply_avatar_pixmap(self, pix: QPixmap) -> None:
        self.avatar.set_pixmap(pix)
        self._card_bg_hex, self._card_border_hex = _extract_avatar_tint(pix)
        self._apply_card_style()

    def _apply_card_style(self) -> None:
        border_col = "#4ADE80" if self._speaking else self._card_border_hex
        self.setStyleSheet(
            f"QFrame#DiscordParticipantCard {{ background-color: {self._card_bg_hex}; "
            f"border: 1px solid {border_col}; border-radius: 12px; }}"
        )

    def release_resources(self) -> None:
        self._disconnect_thumb()
        self.avatar.set_pixmap(None)

    def _on_slider_changed(self, value: int) -> None:
        if self.vol_lbl is not None:
            self.vol_lbl.setText(f"{value}%")
        self.volume_changed.emit(self._uid, int(value))

    def _on_context_menu(self, pos: Any) -> None:
        if self._is_self:
            return
        if exec_user_dm_menu(
            self, self._uid, self._name, self._is_bot, self.mapToGlobal(pos)
        ):
            self.dm_requested.emit(self._uid, self._name, self._avatar_url)

    def set_status(self, status: str) -> None:
        st = _normalize_presence(status)
        self._status_dot_lbl.setPixmap(_build_status_dot_pixmap(st, 9))
        self._status_dot_lbl.setToolTip(_presence_label(st))

    def set_speaking(self, speaking: bool) -> None:
        val = bool(speaking)
        self.avatar.set_speaking(val)
        if val != self._speaking:
            self._speaking = val
            self._apply_card_style()

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.avatar.update_theme(palette)
        self._apply_card_style()
        if self._vol_ico is not None:
            self._vol_ico.setPixmap(
                render_icon("volume", palette.text2, 14).pixmap(QSize(14, 14))
            )
        if self._mute_ico is not None:
            self._mute_ico.setPixmap(
                render_icon("mic_off", "#F87171", 13).pixmap(QSize(13, 13))
            )
        if self._deaf_ico is not None:
            self._deaf_ico.setPixmap(
                render_icon("headphones_off", "#F87171", 13).pixmap(QSize(13, 13))
            )


class _DownloadBridge(QObject):
    progress = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str)


class _ScreenShareSetupDialog(QDialog):
    """Diálogo compacto para elegir fuente, calidad, FPS, audio y destinos Discord."""

    def __init__(
        self,
        service: ScreenShareService,
        palette: ThemePalette,
        text_channels: list[tuple[str, str]],
        dm_targets: list[tuple[str, str]] | None = None,
        default_target: tuple[str, str] | None = None,
        *,
        is_live_edit: bool = False,
        current_source_id: str = "",
        current_quality: str = "720p",
        current_fps: int = 30,
        current_include_audio: bool = True,
        auto_targets_count: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._palette = palette
        self._is_live_edit = bool(is_live_edit)
        self._downloading = False
        self._dl_bridge = _DownloadBridge(self)
        self._dl_bridge.progress.connect(self._on_dl_progress)
        self._dl_bridge.finished.connect(self._on_dl_finished)
        self._dl_bridge.failed.connect(self._on_dl_failed)

        self.setWindowTitle(
            "Cambiar transmisión en vivo" if self._is_live_edit else "Compartir pantalla"
        )
        self.setMinimumWidth(470)
        self.setModal(True)
        try:
            apply_windows_dark_titlebar(int(self.winId()), palette.base)
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hdr_lbl = QLabel(
            "Ajustes en vivo · Pantalla compartida"
            if self._is_live_edit
            else "Configurar transmisión de pantalla"
        )
        hdr_lbl.setStyleSheet("font-size: 14px; font-weight: 700;")
        root.addWidget(hdr_lbl)

        sub_lbl = QLabel(
            "Cambia la ventana/monitor, calidad, FPS o audio sin cortar tu transmisión actual."
            if self._is_live_edit
            else "Quienes usen SwiftPlay verán tu transmisión EN VIVO directo en la app (sin enviar links). También puedes mandar el enlace para Discord oficial."
        )
        sub_lbl.setObjectName("MutedLabel")
        sub_lbl.setWordWrap(True)
        root.addWidget(sub_lbl)

        # Fuente (Monitores / Ventanas)
        src_row = QHBoxLayout()
        src_row.setSpacing(6)
        src_lbl = QLabel("Fuente:")
        src_lbl.setFixedWidth(74)
        src_row.addWidget(src_lbl)
        self.source_combo = QComboBox()
        src_row.addWidget(self.source_combo, 1)
        self.refresh_btn = QPushButton()
        self.refresh_btn.setObjectName("IconBtn")
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.setIcon(render_icon("refresh", palette.text, 14))
        self.refresh_btn.setToolTip("Actualizar monitores y ventanas abiertas")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._reload_sources)
        src_row.addWidget(self.refresh_btn)
        root.addLayout(src_row)

        # Calidad y FPS
        qf_row = QHBoxLayout()
        qf_row.setSpacing(8)
        q_lbl = QLabel("Calidad:")
        q_lbl.setFixedWidth(74)
        qf_row.addWidget(q_lbl)
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("720p (Equilibrado)", "720p")
        self.quality_combo.addItem("1080p (Alta definición)", "1080p")
        self.quality_combo.addItem("480p (Menor consumo)", "480p")
        qf_row.addWidget(self.quality_combo, 1)

        f_lbl = QLabel("FPS:")
        qf_row.addWidget(f_lbl)
        self.fps_combo = QComboBox()
        self.fps_combo.addItem("30 fps", 30)
        self.fps_combo.addItem("60 fps", 60)
        qf_row.addWidget(self.fps_combo)
        root.addLayout(qf_row)

        # Audio del sistema
        self.audio_check = QCheckBox("Incluir audio del sistema (WASAPI)")
        self.audio_check.setChecked(True)
        root.addWidget(self.audio_check)

        # Envío opcional por Discord (desactivado por defecto; canales y/o DMs)
        has_targets = bool(text_channels or dm_targets)
        self.announce_check = QCheckBox("Enviar enlace por chat (para Discord oficial / navegador)")
        self.announce_check.setChecked(False)
        self.announce_check.setEnabled(has_targets)
        root.addWidget(self.announce_check)

        if int(auto_targets_count or 0) > 0 and not self._is_live_edit:
            n_auto = int(auto_targets_count)
            auto_lbl = QLabel(
                f"Destinos automáticos activos en Ajustes → Discord: {n_auto} "
                "(se enviará el enlace siempre al iniciar)."
            )
            auto_lbl.setObjectName("MutedLabel")
            auto_lbl.setWordWrap(True)
            auto_lbl.setStyleSheet("font-size: 11px; background: transparent;")
            root.addWidget(auto_lbl)

        self.targets_box = QFrame()
        self.targets_box.setObjectName("ScreenShareBanner")
        tb_lay = QVBoxLayout(self.targets_box)
        tb_lay.setContentsMargins(10, 8, 10, 8)
        tb_lay.setSpacing(6)

        self.target_filter_edit = QLineEdit()
        self.target_filter_edit.setPlaceholderText("Filtrar canal #texto o @persona...")
        self.target_filter_edit.textChanged.connect(self._filter_targets)
        tb_lay.addWidget(self.target_filter_edit)

        self.targets_list = QListWidget()
        self.targets_list.setMaximumHeight(175)
        self.targets_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { padding: 4px 6px; border-radius: 6px; }"
            "QListWidget::item:hover { background-color: #24272F; }"
        )
        for cid, cname in text_channels:
            it = QListWidgetItem(f"# {cname.lstrip('# ')}  ·  Canal")
            it.setData(Qt.ItemDataRole.UserRole, ("channel", str(cid)))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.targets_list.addItem(it)

        for uid, ulabel in (dm_targets or []):
            it = QListWidgetItem(f"@ {ulabel.lstrip('@ ')}  ·  DM")
            it.setData(Qt.ItemDataRole.UserRole, ("dm", str(uid)))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.targets_list.addItem(it)
        tb_lay.addWidget(self.targets_list)

        target_hint = QLabel(
            "Los DMs llegan a usuarios reales de Discord; si tu compa usa Bot en SwiftPlay, marca también un #canal."
        )
        target_hint.setObjectName("MutedLabel")
        target_hint.setWordWrap(True)
        target_hint.setStyleSheet("font-size: 11px; background: transparent;")
        tb_lay.addWidget(target_hint)

        self.targets_box.setVisible(False)
        self.announce_check.toggled.connect(self._on_announce_toggled)
        root.addWidget(self.targets_box)

        # Estado de binarios (ffmpeg / cloudflared)
        self.deps_frame = QFrame()
        self.deps_frame.setObjectName("ScreenShareBanner")
        deps_lay = QHBoxLayout(self.deps_frame)
        deps_lay.setContentsMargins(10, 8, 10, 8)
        deps_lay.setSpacing(8)
        self.deps_lbl = QLabel("")
        self.deps_lbl.setWordWrap(True)
        self.deps_lbl.setStyleSheet("color: #FBBF24; font-size: 11.5px; background: transparent;")
        deps_lay.addWidget(self.deps_lbl, 1)
        self.dl_cloudflared_btn = QPushButton("Descargar cloudflared (~35 MB)")
        self.dl_cloudflared_btn.setObjectName("PillBtn")
        self.dl_cloudflared_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_cloudflared_btn.clicked.connect(self._start_download_cloudflared)
        deps_lay.addWidget(self.dl_cloudflared_btn)
        root.addWidget(self.deps_frame)

        # Botones finales
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        cancel_btn = QPushButton("Cancelar")
        cancel_btn.setObjectName("PillBtn")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)

        self.start_btn = QPushButton(
            "Aplicar cambios" if self._is_live_edit else "Iniciar transmisión"
        )
        self.start_btn.setObjectName("AccentBtn")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.accept)
        btns.addWidget(self.start_btn)
        root.addLayout(btns)

        self._reload_sources()
        if current_source_id:
            s_idx = self.source_combo.findData(str(current_source_id))
            if s_idx >= 0:
                self.source_combo.setCurrentIndex(s_idx)
        q_idx = self.quality_combo.findData(str(current_quality))
        if q_idx >= 0:
            self.quality_combo.setCurrentIndex(q_idx)
        f_idx = self.fps_combo.findData(60 if int(current_fps or 30) >= 60 else 30)
        if f_idx >= 0:
            self.fps_combo.setCurrentIndex(f_idx)
        self.audio_check.setChecked(bool(current_include_audio))
        if self._is_live_edit:
            self.announce_check.setText("Enviar enlace a más canales o DMs")
        self._refresh_deps_state()

    def _on_announce_toggled(self, checked: bool) -> None:
        self.targets_box.setVisible(bool(checked))
        self.adjustSize()

    def _filter_targets(self, query: str) -> None:
        q = str(query or "").strip().lower()
        for idx in range(self.targets_list.count()):
            it = self.targets_list.item(idx)
            if it is None:
                continue
            it.setHidden(bool(q) and q not in it.text().lower())

    def _reload_sources(self) -> None:
        self.source_combo.clear()
        try:
            sources = self._service.list_sources_sync(timeout=8.0)
        except Exception:
            sources = []
        if not sources:
            sources = [{"id": "monitor:0", "label": "Monitor 1 (Principal)"}]
        for src in sources:
            self.source_combo.addItem(
                str(src.get("label") or src.get("id") or "Monitor"),
                str(src.get("id") or "monitor:0"),
            )

    def _refresh_deps_state(self) -> None:
        ff_ok = find_ffmpeg() is not None
        cf_ok = find_cloudflared() is not None
        if not ff_ok:
            self.deps_frame.setVisible(True)
            self.deps_lbl.setText(
                "No se encontró bin/ffmpeg.exe. Es necesario para capturar pantalla."
            )
            self.dl_cloudflared_btn.setVisible(False)
            self.start_btn.setEnabled(False)
            return
        if not cf_ok:
            self.deps_frame.setVisible(True)
            self.deps_lbl.setText(
                "Falta cloudflared para crear el enlace seguro sin abrir puertos."
            )
            self.dl_cloudflared_btn.setVisible(True)
            self.dl_cloudflared_btn.setEnabled(not self._downloading)
            self.start_btn.setEnabled(False)
            return
        self.deps_frame.setVisible(False)
        self.start_btn.setEnabled(True)

    def _start_download_cloudflared(self) -> None:
        if self._downloading:
            return
        self._downloading = True
        self.dl_cloudflared_btn.setEnabled(False)
        self.dl_cloudflared_btn.setText("Descargando...")

        def _worker() -> None:
            try:
                path = download_cloudflared(
                    progress_cb=lambda d, t: self._dl_bridge.progress.emit(d, t)
                )
                self._dl_bridge.finished.emit(str(path))
            except Exception as exc:
                self._dl_bridge.failed.emit(str(exc))

        import threading

        threading.Thread(target=_worker, daemon=True, name="dl-cloudflared").start()

    def _on_dl_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = min(100, int((done * 100) / total))
            self.dl_cloudflared_btn.setText(f"Descargando {pct}%...")
        else:
            mb = done / (1024 * 1024)
            self.dl_cloudflared_btn.setText(f"{mb:.1f} MB...")

    def _on_dl_finished(self, _path: str) -> None:
        self._downloading = False
        self.dl_cloudflared_btn.setText("Listo")
        self._refresh_deps_state()

    def _on_dl_failed(self, err: str) -> None:
        self._downloading = False
        self.dl_cloudflared_btn.setText("Reintentar descarga")
        self.dl_cloudflared_btn.setEnabled(True)
        self.deps_lbl.setText(f"Error descargando cloudflared: {err}")

    def selected_options(self) -> dict[str, Any]:
        targets: list[tuple[str, str]] = []
        if self.announce_check.isChecked():
            for idx in range(self.targets_list.count()):
                it = self.targets_list.item(idx)
                if it is None or it.checkState() != Qt.CheckState.Checked:
                    continue
                meta = it.data(Qt.ItemDataRole.UserRole)
                if isinstance(meta, tuple) and len(meta) == 2:
                    targets.append((str(meta[0]), str(meta[1])))
        return {
            "source_id": str(self.source_combo.currentData() or "monitor:0"),
            "title": str(self.source_combo.currentText() or "Pantalla compartida"),
            "quality": str(self.quality_combo.currentData() or "720p"),
            "fps": int(self.fps_combo.currentData() or 30),
            "include_audio": bool(self.audio_check.isChecked()),
            "announce": bool(self.announce_check.isChecked() and targets),
            "targets": targets,
        }


class DiscordVoicePanel(QWidget):
    """Vista minimalista de Discord Voz + Estados + Chat (Bot puente aislado, 0 MB en reposo)."""

    open_token_settings_requested = Signal()
    open_shortcuts_settings_requested = Signal()
    voice_status_changed = Signal(bool, bool, bool)  # active, muted, deafened
    channel_alert_requested = Signal(dict)
    dm_alert_requested = Signal(dict)
    unread_count_changed = Signal(int)
    sound_event_requested = Signal(str)
    toast_requested = Signal(str)
    screen_share_announced = Signal(str, str, str)  # kind, target_id, message_id
    screen_share_active_changed = Signal(bool)
    screen_share_ram_updated = Signal(float, float)  # ws_mb, priv_mb
    screen_viewer_fullscreen_changed = Signal(bool)
    screen_viewer_volume_hud_requested = Signal(int)
    screen_viewer_mouse_moved = Signal()
    screen_viewer_state_updated = Signal(str, int, bool)  # title, effective_vol, muted

    _LIST_AVATAR_SIZE = QSize(28, 28)

    def __init__(
        self,
        config: AppConfig,
        service: DiscordService,
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None = None,
        db: LocalLibrary | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = config
        self.svc = service
        self.palette = palette
        self._thumb_cache = thumb_cache
        self._db = db

        self._guilds: list[dict[str, Any]] = []
        self._voice_state: dict[str, Any] = {
            "in_voice": False,
            "channel_id": "",
            "channel_name": "",
            "guild_name": "",
            "muted": False,
            "deafened": False,
            "dave_active": False,
            "dave_code": "",
            "participants": [],
        }
        self._speaking: dict[str, bool] = {}
        self._muted = False
        self._deafened = False
        self._mic_test_active = False
        self._mic_test_prev_flags: tuple[bool, bool] | None = None
        self._bot_state = "disconnected"
        self._joining_channel_id = ""
        self._user_volumes: dict[str, int] = {}
        self._participant_cards: dict[str, _ParticipantCard] = {}
        self._participant_cards_order: list[_ParticipantCard] = []
        self._last_grid_cols: int = 0
        self._last_voice_row_sizes: list[int] = []
        self._last_voice_layout_key: tuple[int, int, int] = (0, 0, 0)
        self._populating_devices = False
        self._selected_guild_id = ""
        self._active_chat_kind = ""
        self._active_chat_id = ""
        self._active_chat_can_send = False
        self._active_chat_can_attach = True
        self._active_dm_peer_name = ""
        self._active_dm_peer_avatar = ""
        self._chat_messages: deque[dict[str, Any]] = deque(maxlen=80)
        self._pending_attachments: list[dict[str, Any]] = []
        self._chat_attachment_refs: list[dict[str, Any]] = []
        self._chat_pending_img_urls: set[str] = set()
        self._chat_last_can_read: bool = True
        self._reply_to_message: dict[str, Any] | None = None
        self._editing_message_id: str = ""
        self._image_lightbox: _ImageLightboxOverlay | None = None
        self._last_mic_rms: int = 0
        self._att_pool = QThreadPool(self)
        self._att_pool.setMaxThreadCount(2)
        self._att_pending_signals: set[_AttachmentSignals] = set()
        self._chat_img_rerender_timer = QTimer(self)
        self._chat_img_rerender_timer.setSingleShot(True)
        self._chat_img_rerender_timer.setInterval(50)
        self._chat_img_rerender_timer.timeout.connect(self._rerender_chat_after_images)
        self._dm_threads_mem: list[dict[str, str]] = []
        self._dm_msgs_mem: dict[str, list[dict[str, Any]]] = {}
        self._members: list[dict[str, Any]] = []
        self._online_count: int = 0
        self._channel_members: list[dict[str, Any]] = []
        self._channel_online_count: int = 0
        self._channel_members_loaded: bool = False
        self._members_scope: Literal["channel", "server"] = "channel"
        self._member_row_by_url: dict[str, list[QListWidgetItem]] = {}
        self._user_statuses: dict[str, str] = {}
        self._unread_channels: dict[str, int] = {}
        self._unread_dms: dict[str, int] = {}
        self._tc_buttons: dict[str, QPushButton] = {}
        self._tc_badges: dict[str, QLabel] = {}
        self._dm_buttons: dict[str, QPushButton] = {}
        self._dm_badges: dict[str, QLabel] = {}
        self._chat_history_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_channels_sig: Any = None
        self._last_participants_sig: Any = None
        self._worker_ws_mb = 0.0
        self._worker_priv_mb = 0.0

        # Compartida de pantalla aislada + visor nativo libmpv lazy
        self.screen_share_svc = ScreenShareService(self)
        self._screen_share_state = "idle"
        self._screen_share_info: dict[str, Any] = {}
        self._screen_share_stats: dict[str, Any] = {}
        self._screen_share_ws_mb = 0.0
        self._screen_share_priv_mb = 0.0
        self._screen_share_pending_opts: dict[str, Any] | None = None
        self._screen_share_announces: list[tuple[str, str, str]] = []  # [(kind, target_id, message_id)]
        self._screen_share_rev = 0
        self._remote_screen_shares: dict[str, dict[str, Any]] = {}
        self._dismissed_share_urls: set[str] = set()
        self._remote_screen_share: dict[str, Any] | None = None
        self._screen_viewer_active = False
        self._screen_viewer_is_local_preview = False
        self._screen_viewer_theater = False
        self._screen_viewer_fullscreen = False
        self._screen_viewer_pre_fs_theater = False
        self._pre_theater_chrome: dict[str, bool] = {}
        self._screen_viewer_muted = False
        self._screen_viewer_volume = 80
        self._screen_viewer_prev_vol = 80
        self._screen_viewer_stream_url = ""
        self._screen_viewer_watch_url = ""
        self._screen_viewer_author_id = ""
        self._screen_viewer_rev = 0
        self._screen_player: MediaPlayer | None = None

        # Estado de acordeones y vista exclusiva central (Voz o Chat)
        self._channels_visible = True
        self._members_visible = False
        self._audio_drawer_open = False
        self._vc_section_open = True
        self._tc_section_open = True
        self._dm_section_open = True
        self._locked_vc_expanded = False
        self._voice_pane_visible = True
        self._chat_pane_visible = False
        self._fade_target_widget: QWidget | None = None
        self._center_fade_anim = QVariantAnimation(self)
        self._center_fade_anim.setDuration(130)
        self._center_fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._center_fade_anim.setStartValue(0.84)
        self._center_fade_anim.setEndValue(1.0)
        self._center_fade_anim.valueChanged.connect(self._on_center_fade_value)
        self._center_fade_anim.finished.connect(self._on_center_fade_finished)

        self._build_ui()
        self._render_dm_threads()
        self._connect_service_signals()
        self.refresh_token_state()
        self.update_theme(self.palette)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(10)
        self._root_lay = root

        # --- Cabecera minimalista ---
        self.top_hdr_widget = QWidget()
        self.top_hdr_widget.setObjectName("TransparentBox")
        hdr = QHBoxLayout(self.top_hdr_widget)
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(8)
        self.title_lbl = QLabel("Discord")
        self.title_lbl.setObjectName("ViewTitle")
        hdr.addWidget(self.title_lbl)

        self.status_pill = QLabel("Desconectado")
        self.status_pill.setObjectName("DeckTag")
        hdr.addWidget(self.status_pill)

        self.info_pill = QPushButton(" 0.0 MB")
        self.info_pill.setObjectName("PillBtn")
        self.info_pill.setEnabled(False)
        self.info_pill.setVisible(False)
        self.info_pill.setToolTip("Uso de memoria del worker aislado y cifrado DAVE E2EE")
        hdr.addWidget(self.info_pill)

        hdr.addStretch()

        self.toggle_channels_btn = QPushButton("Canales")
        self.toggle_channels_btn.setObjectName("PillBtn")
        self.toggle_channels_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_channels_btn.setToolTip("Mostrar u ocultar la lista de canales")
        self.toggle_channels_btn.clicked.connect(self._toggle_channels_panel)
        hdr.addWidget(self.toggle_channels_btn)

        self.toggle_members_btn = QPushButton("Miembros")
        self.toggle_members_btn.setObjectName("PillBtn")
        self.toggle_members_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_members_btn.setToolTip("Mostrar u ocultar miembros del canal o servidor")
        self.toggle_members_btn.clicked.connect(self._toggle_members_panel)
        hdr.addWidget(self.toggle_members_btn)

        self.connect_btn = QPushButton("Conectar Bot")
        self.connect_btn.setObjectName("AccentBtn")
        self.connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_btn.clicked.connect(self._on_connect_toggle_clicked)
        hdr.addWidget(self.connect_btn)
        root.addWidget(self.top_hdr_widget)

        # --- Tarjeta de configuración inicial (cuando aún no hay Bot Token) ---
        self.setup_card = QFrame()
        self.setup_card.setObjectName("DiscordSidebarCard")
        setup_lay = QVBoxLayout(self.setup_card)
        setup_lay.setContentsMargins(18, 16, 18, 16)
        setup_lay.setSpacing(10)

        setup_title = QLabel("Puente ultraliviano con Bot oficial (0 MB en reposo)")
        setup_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        setup_lay.addWidget(setup_title)

        setup_desc = QLabel(
            "Conecta voz con DAVE (E2EE), mira los miembros del canal y chatea sin la pesada "
            "aplicación oficial (0 MB cuando desconectas el bot).\n\n"
            "Pasos rápidos (una sola vez):\n"
            "1. Abre https://discord.com/developers/applications → New Application → Bot → Token.\n"
            "2. Opcional para estados y chat: en Bot activa 'Presence Intent', 'Server Members Intent' y 'Message Content Intent'.\n"
            "3. En OAuth2 → URL Generator marca 'bot' y permisos: View Channels, Connect, Speak, Read Message History, Send Messages.\n"
            "4. Invita el bot a tu servidor con esa URL y guarda el Token cifrado con Windows DPAPI en SwiftPlay."
        )
        setup_desc.setObjectName("MutedLabel")
        setup_desc.setWordWrap(True)
        setup_lay.addWidget(setup_desc)

        setup_btn_row = QHBoxLayout()
        self.setup_go_btn = QPushButton("Configurar Bot Token (DPAPI)")
        self.setup_go_btn.setObjectName("AccentBtn")
        self.setup_go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setup_go_btn.clicked.connect(self.open_token_settings_requested.emit)
        setup_btn_row.addWidget(self.setup_go_btn)
        setup_btn_row.addStretch()
        setup_lay.addLayout(setup_btn_row)
        root.addWidget(self.setup_card)

        # --- Workspace principal ---
        self.workspace_widget = QWidget()
        ws_lay = QHBoxLayout(self.workspace_widget)
        ws_lay.setContentsMargins(0, 0, 0, 0)
        ws_lay.setSpacing(10)

        # Columna izquierda unificada: Servidor + Canales de Voz + Canales de Texto sin espacio muerto
        self.left_card = QFrame()
        self.left_card.setObjectName("DiscordSidebarCard")
        self.left_card.setMinimumWidth(232)
        self.left_card.setMaximumWidth(272)
        left_lay = QVBoxLayout(self.left_card)
        left_lay.setContentsMargins(10, 10, 10, 10)
        left_lay.setSpacing(6)

        srv_row = QHBoxLayout()
        srv_row.setSpacing(6)
        self.guild_combo = QComboBox()
        self.guild_combo.currentIndexChanged.connect(self._on_guild_combo_changed)
        srv_row.addWidget(self.guild_combo, 1)

        self.refresh_guilds_btn = QPushButton()
        self.refresh_guilds_btn.setObjectName("IconBtn")
        self.refresh_guilds_btn.setFixedSize(28, 28)
        self.refresh_guilds_btn.setToolTip("Actualizar servidores y canales")
        self.refresh_guilds_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_guilds_btn.clicked.connect(self.svc.refresh_guilds)
        srv_row.addWidget(self.refresh_guilds_btn)

        self.close_channels_btn = QPushButton()
        self.close_channels_btn.setObjectName("IconBtn")
        self.close_channels_btn.setFixedSize(26, 26)
        self.close_channels_btn.setToolTip("Ocultar panel de canales (reabrir con 'Canales')")
        self.close_channels_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_channels_btn.clicked.connect(self._toggle_channels_panel)
        srv_row.addWidget(self.close_channels_btn)
        left_lay.addLayout(srv_row)

        self.channels_scroll = QScrollArea()
        self.channels_scroll.setObjectName("TransparentScroll")
        self.channels_scroll.setWidgetResizable(True)
        self.channels_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.channels_scroll.viewport().setObjectName("TransparentBox")
        self.channels_scroll.viewport().setAutoFillBackground(False)
        ch_container = QWidget()
        ch_container.setObjectName("TransparentBox")
        unified_lay = QVBoxLayout(ch_container)
        unified_lay.setContentsMargins(0, 2, 2, 4)
        unified_lay.setSpacing(6)

        self.vc_section_btn = _make_section_toggle_btn("▾ CANALES DE VOZ")
        self.vc_section_btn.clicked.connect(self._toggle_voice_channels_section)
        unified_lay.addWidget(self.vc_section_btn)

        self.vc_list_widget = QWidget()
        self.vc_list_widget.setObjectName("TransparentBox")
        self.channels_lay = QVBoxLayout(self.vc_list_widget)
        self.channels_lay.setContentsMargins(0, 0, 0, 4)
        self.channels_lay.setSpacing(5)
        self.channels_empty_lbl = QLabel("Pulsa 'Conectar Bot' para ver tus canales.")
        self.channels_empty_lbl.setObjectName("MutedLabel")
        self.channels_empty_lbl.setWordWrap(True)
        self.channels_lay.addWidget(self.channels_empty_lbl)
        unified_lay.addWidget(self.vc_list_widget)

        self.tc_section_btn = _make_section_toggle_btn("▾ CANALES DE TEXTO")
        self.tc_section_btn.clicked.connect(self._toggle_text_channels_section)
        unified_lay.addWidget(self.tc_section_btn)

        self.text_channels_scroll = QWidget()
        self.text_channels_scroll.setObjectName("TransparentBox")
        self.text_channels_lay = QVBoxLayout(self.text_channels_scroll)
        self.text_channels_lay.setContentsMargins(0, 0, 0, 4)
        self.text_channels_lay.setSpacing(3)
        tc_empty = QLabel("Conecta el bot para ver canales de texto.")
        tc_empty.setObjectName("MutedLabel")
        tc_empty.setWordWrap(True)
        self.text_channels_lay.addWidget(tc_empty)
        unified_lay.addWidget(self.text_channels_scroll)

        self.dm_section_btn = _make_section_toggle_btn("▾ MENSAJES DIRECTOS")
        self.dm_section_btn.clicked.connect(self._toggle_dm_channels_section)
        unified_lay.addWidget(self.dm_section_btn)

        self.dm_channels_scroll = QWidget()
        self.dm_channels_scroll.setObjectName("TransparentBox")
        self.dm_channels_lay = QVBoxLayout(self.dm_channels_scroll)
        self.dm_channels_lay.setContentsMargins(0, 0, 0, 4)
        self.dm_channels_lay.setSpacing(3)
        unified_lay.addWidget(self.dm_channels_scroll)

        unified_lay.addStretch(1)
        self.channels_scroll.setWidget(ch_container)
        left_lay.addWidget(self.channels_scroll, 1)
        ws_lay.addWidget(self.left_card)

        # Columna central: Banner intents + Split (Voz arriba + Chat abajo) + Deck centrado
        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        self.intents_banner = QFrame()
        self.intents_banner.setObjectName("DiscordHeaderBar")
        ib_lay = QHBoxLayout(self.intents_banner)
        ib_lay.setContentsMargins(10, 6, 10, 6)
        ib_lay.setSpacing(8)
        ib_lbl = QLabel(
            "Para ver estados en línea y leer todo el chat, activa Presence, Server Members y Message Content Intents en Discord Developer Portal → Bot."
        )
        ib_lbl.setObjectName("MutedLabel")
        ib_lbl.setWordWrap(True)
        ib_lbl.setStyleSheet("color: #FBBF24; font-size: 11px;")
        ib_lay.addWidget(ib_lbl, 1)
        self.intents_close_btn = QPushButton()
        self.intents_close_btn.setObjectName("IconBtn")
        self.intents_close_btn.setFixedSize(24, 24)
        self.intents_close_btn.setToolTip("Ocultar aviso")
        self.intents_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.intents_close_btn.clicked.connect(lambda: self.intents_banner.setVisible(False))
        ib_lay.addWidget(self.intents_close_btn)
        self.intents_banner.setVisible(False)
        right_col.addWidget(self.intents_banner)

        # Cabecera del área central (Voz / Chat toggles + canal activo)
        center_hdr_card = QFrame()
        center_hdr_card.setObjectName("DiscordHeaderBar")
        self.center_hdr_card = center_hdr_card
        chdr = QHBoxLayout(center_hdr_card)
        chdr.setContentsMargins(12, 7, 12, 7)
        chdr.setSpacing(6)

        self.tab_voice_btn = QPushButton("Voz")
        self.tab_voice_btn.setObjectName("PillBtn")
        self.tab_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tab_voice_btn.setToolTip("Mostrar u ocultar tarjetas de voz")
        self.tab_voice_btn.clicked.connect(lambda: self._on_center_tab_toggled("voice"))
        chdr.addWidget(self.tab_voice_btn)

        self.tab_chat_btn = QPushButton("Chat")
        self.tab_chat_btn.setObjectName("PillBtn")
        self.tab_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tab_chat_btn.setToolTip("Mostrar u ocultar panel de chat")
        self.tab_chat_btn.clicked.connect(lambda: self._on_center_tab_toggled("chat"))
        chdr.addWidget(self.tab_chat_btn)

        self.active_channel_lbl = QLabel("Sin canal activo")
        self.active_channel_lbl.setStyleSheet(
            "font-size: 13px; font-weight: 700; margin-left: 6px;"
        )
        chdr.addWidget(self.active_channel_lbl, 1)
        right_col.addWidget(center_hdr_card)

        # Área central exclusiva: Voz o Chat según selección (sin dividir la pantalla)
        self.center_stack = QStackedWidget()

        # Panel superior: Fondo propio de Voz + tarjetas de participantes
        self.voice_pane = QFrame()
        self.voice_pane.setObjectName("DiscordVoiceArea")
        vp_lay = QVBoxLayout(self.voice_pane)
        vp_lay.setContentsMargins(10, 8, 10, 10)
        vp_lay.setSpacing(8)
        self._vp_lay = vp_lay

        voice_hdr_frame = QFrame()
        voice_hdr_frame.setObjectName("DiscordVoiceHeader")
        self.voice_hdr_frame = voice_hdr_frame
        vh_lay = QHBoxLayout(voice_hdr_frame)
        vh_lay.setContentsMargins(10, 5, 8, 5)
        vh_lay.setSpacing(6)
        v_title = QLabel("LLAMADA DE VOZ")
        v_title.setObjectName("SectionHeader")
        v_title.setStyleSheet(
            "padding: 2px 0px; font-size: 10px; font-weight: 700; letter-spacing: 1.2px;"
        )
        vh_lay.addWidget(v_title)
        vh_lay.addStretch()

        self.screen_share_btn = QPushButton(" Compartir pantalla")
        self.screen_share_btn.setObjectName("PillBtn")
        self.screen_share_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen_share_btn.setToolTip(
            "Comparte pantalla + audio del sistema (nativo en SwiftPlay y 1 clic web para Discord)"
        )
        self.screen_share_btn.clicked.connect(self._on_screen_share_btn_clicked)
        self.screen_share_btn.setVisible(False)
        vh_lay.addWidget(self.screen_share_btn)

        self.close_voice_btn = QPushButton()
        self.close_voice_btn.setObjectName("IconBtn")
        self.close_voice_btn.setFixedSize(24, 24)
        self.close_voice_btn.setToolTip("Ir al panel de Chat")
        self.close_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_voice_btn.clicked.connect(lambda: self._set_pane_visible("chat", True))
        self.close_voice_btn.setVisible(False)
        vh_lay.addWidget(self.close_voice_btn)
        vp_lay.addWidget(voice_hdr_frame)

        # Franja activa cuando el usuario local está compartiendo pantalla
        self.share_live_strip = QFrame()
        self.share_live_strip.setObjectName("ScreenShareLiveStrip")
        sls_lay = QHBoxLayout(self.share_live_strip)
        sls_lay.setContentsMargins(10, 6, 10, 6)
        sls_lay.setSpacing(6)
        self.share_live_lbl = QLabel("🔴 Compartiendo pantalla...")
        self.share_live_lbl.setStyleSheet(
            "color: #F87171; font-size: 12px; font-weight: 700; background: transparent;"
        )
        sls_lay.addWidget(self.share_live_lbl, 1)

        self.share_copy_url_btn = QPushButton("Copiar link")
        self.share_copy_url_btn.setObjectName("PillBtn")
        self.share_copy_url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.share_copy_url_btn.clicked.connect(self._copy_active_share_url)
        sls_lay.addWidget(self.share_copy_url_btn)

        self.share_settings_btn = QPushButton("Cambiar")
        self.share_settings_btn.setObjectName("PillBtn")
        self.share_settings_btn.setToolTip(
            "Cambiar ventana/monitor, calidad, FPS o audio sin cortar el enlace actual"
        )
        self.share_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.share_settings_btn.clicked.connect(self._on_screen_share_reconfigure_clicked)
        sls_lay.addWidget(self.share_settings_btn)

        self.share_preview_btn = QPushButton("Vista previa")
        self.share_preview_btn.setObjectName("PillBtn")
        self.share_preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.share_preview_btn.clicked.connect(self._open_local_share_preview)
        sls_lay.addWidget(self.share_preview_btn)

        self.share_stop_btn = QPushButton("Detener")
        self.share_stop_btn.setObjectName("DangerBtn")
        self.share_stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.share_stop_btn.clicked.connect(self._stop_screen_share)
        sls_lay.addWidget(self.share_stop_btn)
        self.share_live_strip.setVisible(False)
        vp_lay.addWidget(self.share_live_strip)

        # Banner de transmisión remota detectada en Discord
        self.remote_share_banner = QFrame()
        self.remote_share_banner.setObjectName("ScreenShareBanner")
        rsb_lay = QHBoxLayout(self.remote_share_banner)
        rsb_lay.setContentsMargins(10, 6, 8, 6)
        rsb_lay.setSpacing(6)
        self.remote_share_lbl = QLabel("🖥️ Pantalla compartida disponible")
        self.remote_share_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 600; background: transparent;"
        )
        rsb_lay.addWidget(self.remote_share_lbl, 1)

        self.remote_watch_btn = QPushButton("Ver aquí")
        self.remote_watch_btn.setObjectName("AccentBtn")
        self.remote_watch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remote_watch_btn.clicked.connect(self._watch_detected_remote_share)
        rsb_lay.addWidget(self.remote_watch_btn)

        self.remote_browser_btn = QPushButton("Navegador")
        self.remote_browser_btn.setObjectName("PillBtn")
        self.remote_browser_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remote_browser_btn.clicked.connect(self._open_detected_remote_in_browser)
        rsb_lay.addWidget(self.remote_browser_btn)

        self.remote_dismiss_btn = QPushButton()
        self.remote_dismiss_btn.setObjectName("IconBtn")
        self.remote_dismiss_btn.setFixedSize(24, 24)
        self.remote_dismiss_btn.setToolTip("Ocultar aviso")
        self.remote_dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remote_dismiss_btn.clicked.connect(self._dismiss_remote_share_banner)
        rsb_lay.addWidget(self.remote_dismiss_btn)
        self.remote_share_banner.setVisible(False)
        vp_lay.addWidget(self.remote_share_banner)

        # Visor nativo embebido libmpv para ver pantalla compartida sin navegador
        self.screen_viewer_card = QFrame()
        self.screen_viewer_card.setObjectName("ScreenShareViewerCard")
        svc_lay = QVBoxLayout(self.screen_viewer_card)
        svc_lay.setContentsMargins(8, 8, 8, 6)
        svc_lay.setSpacing(6)
        self._svc_lay = svc_lay

        self.screen_video_surface = VideoSurface(self.screen_viewer_card)
        self.screen_video_surface.setMinimumHeight(240)
        self.screen_video_surface.double_clicked.connect(self.toggle_screen_viewer_fullscreen)
        self.screen_video_surface.wheel_scrolled.connect(self._on_screen_viewer_wheel)
        self.screen_video_surface.mouse_moved.connect(self.screen_viewer_mouse_moved.emit)
        svc_lay.addWidget(self.screen_video_surface, 1)

        self.screen_viewer_hdr_widget = QWidget()
        self.screen_viewer_hdr_widget.setObjectName("TransparentBox")
        sv_hdr = QHBoxLayout(self.screen_viewer_hdr_widget)
        sv_hdr.setContentsMargins(0, 0, 0, 0)
        sv_hdr.setSpacing(6)
        self._sv_hdr_lay = sv_hdr
        self.screen_viewer_title_lbl = QLabel("Pantalla compartida")
        self.screen_viewer_title_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 700; background: transparent;"
        )
        sv_hdr.addWidget(self.screen_viewer_title_lbl, 1)

        self.screen_viewer_mute_btn = QPushButton()
        self.screen_viewer_mute_btn.setObjectName("IconBtn")
        self.screen_viewer_mute_btn.setFixedSize(26, 26)
        self.screen_viewer_mute_btn.setToolTip("Silenciar o activar audio del stream [M]")
        self.screen_viewer_mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen_viewer_mute_btn.clicked.connect(
            lambda _c=False: self.toggle_screen_viewer_mute_from_hotkey(show_hud=False)
        )
        sv_hdr.addWidget(self.screen_viewer_mute_btn)

        self.screen_viewer_vol_slider = ClickableSlider(
            Qt.Orientation.Horizontal, volume_wheel=True
        )
        self.screen_viewer_vol_slider.setRange(0, 100)
        self.screen_viewer_vol_slider.setValue(80)
        self.screen_viewer_vol_slider.setFixedWidth(90)
        self.screen_viewer_vol_slider.setToolTip(
            "Volumen del stream compartido (rueda: ±5%, Shift: ±1%)"
        )
        self.screen_viewer_vol_slider.valueChanged.connect(self._on_screen_viewer_vol_changed)
        sv_hdr.addWidget(self.screen_viewer_vol_slider)

        self.screen_viewer_theater_btn = QPushButton("Teatro")
        self.screen_viewer_theater_btn.setObjectName("PillBtn")
        self.screen_viewer_theater_btn.setToolTip(
            "Modo teatro (ocupa toda el área de Discord manteniendo la barra lateral) [T]"
        )
        self.screen_viewer_theater_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen_viewer_theater_btn.clicked.connect(self.toggle_screen_viewer_theater)
        sv_hdr.addWidget(self.screen_viewer_theater_btn)

        self.screen_viewer_fs_btn = QPushButton()
        self.screen_viewer_fs_btn.setObjectName("IconBtn")
        self.screen_viewer_fs_btn.setFixedSize(28, 28)
        self.screen_viewer_fs_btn.setToolTip("Pantalla completa [F]")
        self.screen_viewer_fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen_viewer_fs_btn.clicked.connect(self.toggle_screen_viewer_fullscreen)
        sv_hdr.addWidget(self.screen_viewer_fs_btn)

        self.screen_viewer_browser_btn = QPushButton("Navegador")
        self.screen_viewer_browser_btn.setObjectName("PillBtn")
        self.screen_viewer_browser_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen_viewer_browser_btn.clicked.connect(self._open_current_viewer_in_browser)
        sv_hdr.addWidget(self.screen_viewer_browser_btn)

        self.screen_viewer_close_btn = QPushButton("Cerrar")
        self.screen_viewer_close_btn.setObjectName("DangerBtn")
        self.screen_viewer_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen_viewer_close_btn.clicked.connect(self._close_screen_share_viewer)
        sv_hdr.addWidget(self.screen_viewer_close_btn)
        svc_lay.addWidget(self.screen_viewer_hdr_widget)

        self.screen_viewer_card.setVisible(False)
        vp_lay.addWidget(self.screen_viewer_card, 3)

        self.participants_scroll = QScrollArea()
        self.participants_scroll.setObjectName("TransparentScroll")
        self.participants_scroll.setWidgetResizable(True)
        self.participants_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.participants_scroll.viewport().setObjectName("TransparentBox")
        self.participants_scroll.viewport().setAutoFillBackground(False)
        part_container = QWidget()
        part_container.setObjectName("TransparentBox")
        self.participants_container = part_container
        part_outer = QVBoxLayout(part_container)
        part_outer.setContentsMargins(16, 16, 16, 16)
        part_outer.setSpacing(12)
        part_outer.addStretch(1)

        self.participants_rows_layout = QVBoxLayout()
        self.participants_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.participants_rows_layout.setSpacing(12)
        self.participants_grid = self.participants_rows_layout
        part_outer.addLayout(self.participants_rows_layout)

        self.participants_empty_lbl = QLabel(
            "Elige un canal de voz a la izquierda para conectarte y hablar en tiempo real."
        )
        self.participants_empty_lbl.setObjectName("MutedLabel")
        self.participants_empty_lbl.setWordWrap(True)
        self.participants_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.participants_empty_lbl.setContentsMargins(12, 24, 12, 24)
        part_outer.addWidget(self.participants_empty_lbl)
        part_outer.addStretch(1)

        self.participants_scroll.setWidget(part_container)
        vp_lay.addWidget(self.participants_scroll, 2)
        self.center_stack.addWidget(self.voice_pane)

        # Panel inferior: Chat con cabecera propia y estado de usuario en DM
        self.chat_pane = QFrame()
        self.chat_pane.setObjectName("DiscordChatCard")
        chat_lay = QVBoxLayout(self.chat_pane)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        chat_hdr_frame = QFrame()
        chat_hdr_frame.setObjectName("DiscordChatHeader")
        chat_hdr = QHBoxLayout(chat_hdr_frame)
        chat_hdr.setContentsMargins(14, 8, 14, 8)
        chat_hdr.setSpacing(8)
        self.chat_title_lbl = QLabel("Sin chat seleccionado")
        self.chat_title_lbl.setStyleSheet("font-size: 14px; font-weight: 700;")
        chat_hdr.addWidget(self.chat_title_lbl)

        self.chat_status_pill = QLabel("")
        self.chat_status_pill.setObjectName("DeckTag")
        self.chat_status_pill.setVisible(False)
        chat_hdr.addWidget(self.chat_status_pill)

        chat_hdr.addStretch()

        self.bot_chat_badge = QLabel("Envía como Bot")
        self.bot_chat_badge.setObjectName("DeckTag")
        self.bot_chat_badge.setToolTip(
            "Los mensajes escritos aquí se envían usando la cuenta del Bot oficial."
        )
        chat_hdr.addWidget(self.bot_chat_badge)

        self.close_chat_btn = QPushButton()
        self.close_chat_btn.setObjectName("IconBtn")
        self.close_chat_btn.setFixedSize(24, 24)
        self.close_chat_btn.setToolTip("Ir al panel de Voz")
        self.close_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_chat_btn.clicked.connect(lambda: self._set_pane_visible("voice", True))
        self.close_chat_btn.setVisible(False)
        chat_hdr.addWidget(self.close_chat_btn)
        chat_lay.addWidget(chat_hdr_frame)

        chat_body = QWidget()
        chat_body.setObjectName("TransparentBox")
        chat_body_lay = QVBoxLayout(chat_body)
        chat_body_lay.setContentsMargins(14, 8, 14, 10)
        chat_body_lay.setSpacing(8)

        self.chat_browser = _ChatMessagesView(self.palette, self.chat_pane)
        self.chat_browser.setAcceptDrops(True)
        self.chat_browser.installEventFilter(self)
        if self.chat_browser.viewport() is not None:
            self.chat_browser.viewport().setAcceptDrops(True)
            self.chat_browser.viewport().installEventFilter(self)
        self.chat_browser.container.setAcceptDrops(True)
        self.chat_browser.container.installEventFilter(self)
        self.chat_browser.reply_requested.connect(self._start_reply_to)
        self.chat_browser.edit_requested.connect(self._start_edit_message)
        self.chat_browser.copy_requested.connect(self._on_chat_row_copy_requested)
        self.chat_browser.paste_requested.connect(self._paste_into_chat_from_action)
        self.chat_browser.image_clicked.connect(self._open_image_lightbox)
        self.chat_browser.save_attachment_requested.connect(self._save_attachment_to_disk)
        self.chat_browser.row_context_menu_requested.connect(self._show_chat_row_context_menu)
        self.chat_browser.screen_share_watch_requested.connect(self._open_screen_share_viewer)
        self.chat_browser.show_placeholder(
            "Selecciona un canal de texto a la izquierda para ver y enviar mensajes aquí."
        )
        chat_body_lay.addWidget(self.chat_browser, 1)

        self.chat_drop_hint = QLabel("Suelta archivos o imágenes aquí para adjuntar")
        self.chat_drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_drop_hint.setStyleSheet(
            f"background-color: rgba(139, 92, 246, 36); color: {self.palette.text}; "
            "border: 1px dashed #8B5CF6; border-radius: 8px; padding: 8px; font-weight: 600;"
        )
        self.chat_drop_hint.setVisible(False)
        chat_body_lay.addWidget(self.chat_drop_hint)

        self.chat_context_bar = QFrame()
        self.chat_context_bar.setObjectName("PillBtn")
        self.chat_context_bar.setStyleSheet(
            f"QFrame {{ background-color: {self.palette.surface2}; "
            f"border: 1px solid {self.palette.border_bright}; border-radius: 8px; }}"
        )
        ctx_lay = QHBoxLayout(self.chat_context_bar)
        ctx_lay.setContentsMargins(10, 4, 6, 4)
        ctx_lay.setSpacing(6)
        self.chat_context_lbl = QLabel("")
        self.chat_context_lbl.setStyleSheet("background: transparent; border: none; font-size: 11.5px;")
        ctx_lay.addWidget(self.chat_context_lbl, 1)
        self.chat_context_cancel_btn = QPushButton("×")
        self.chat_context_cancel_btn.setObjectName("IconBtn")
        self.chat_context_cancel_btn.setFixedSize(20, 20)
        self.chat_context_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_context_cancel_btn.setToolTip("Cancelar respuesta o edición [Esc]")
        self.chat_context_cancel_btn.clicked.connect(self._cancel_chat_compose_mode)
        ctx_lay.addWidget(self.chat_context_cancel_btn)
        self.chat_context_bar.setVisible(False)
        chat_body_lay.addWidget(self.chat_context_bar)

        self.chat_pending_bar = QWidget()
        self.chat_pending_bar.setObjectName("TransparentBox")
        self.chat_pending_lay = QHBoxLayout(self.chat_pending_bar)
        self.chat_pending_lay.setContentsMargins(0, 2, 0, 2)
        self.chat_pending_lay.setSpacing(6)
        self.chat_pending_lay.addStretch(1)
        self.chat_pending_bar.setVisible(False)
        chat_body_lay.addWidget(self.chat_pending_bar)

        chat_input_row = QHBoxLayout()
        chat_input_row.setSpacing(8)
        self.chat_attach_btn = QPushButton()
        self.chat_attach_btn.setObjectName("IconBtn")
        self.chat_attach_btn.setFixedSize(32, 32)
        self.chat_attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_attach_btn.setEnabled(False)
        self.chat_attach_btn.setToolTip(
            "Adjuntar archivo o imagen (arrastra al chat o pega con Ctrl+V)"
        )
        self.chat_attach_btn.clicked.connect(self._on_attach_files_clicked)
        chat_input_row.addWidget(self.chat_attach_btn)

        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("DiscordChatInput")
        self.chat_input.setPlaceholderText("Selecciona un canal #texto para escribir... [Enter]")
        self.chat_input.setEnabled(False)
        self.chat_input.setAcceptDrops(True)
        self.chat_input.installEventFilter(self)
        self.chat_input.returnPressed.connect(self._on_send_chat_clicked)
        chat_input_row.addWidget(self.chat_input, 1)

        self.chat_send_btn = QPushButton("Enviar")
        self.chat_send_btn.setObjectName("AccentBtn")
        self.chat_send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_send_btn.setEnabled(False)
        self.chat_send_btn.clicked.connect(self._on_send_chat_clicked)
        chat_input_row.addWidget(self.chat_send_btn)
        chat_body_lay.addLayout(chat_input_row)
        chat_lay.addWidget(chat_body, 1)

        self.chat_pane.setAcceptDrops(True)
        self.chat_pane.installEventFilter(self)

        self.center_stack.addWidget(self.chat_pane)
        right_col.addWidget(self.center_stack, 1)

        self.center_collapsed_card = QFrame()
        self.center_collapsed_card.setObjectName("DiscordVoiceArea")
        cc_lay = QVBoxLayout(self.center_collapsed_card)
        cc_lay.setContentsMargins(20, 24, 20, 24)
        cc_lay.setSpacing(10)
        cc_lay.addStretch(1)
        cc_lbl = QLabel("Los paneles de Voz y Chat están colapsados.")
        cc_lbl.setObjectName("MutedLabel")
        cc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cc_lay.addWidget(cc_lbl)
        cc_btns = QHBoxLayout()
        cc_btns.setSpacing(10)
        cc_btns.addStretch()
        self.reopen_voice_btn = QPushButton("Abrir Voz")
        self.reopen_voice_btn.setObjectName("AccentBtn")
        self.reopen_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reopen_voice_btn.clicked.connect(lambda: self._set_pane_visible("voice", True))
        cc_btns.addWidget(self.reopen_voice_btn)
        self.reopen_chat_btn = QPushButton("Abrir Chat")
        self.reopen_chat_btn.setObjectName("AccentBtn")
        self.reopen_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reopen_chat_btn.clicked.connect(lambda: self._set_pane_visible("chat", True))
        cc_btns.addWidget(self.reopen_chat_btn)
        cc_btns.addStretch()
        cc_lay.addLayout(cc_btns)
        cc_lay.addStretch(1)
        self.center_collapsed_card.setVisible(False)
        right_col.addWidget(self.center_collapsed_card, 1)

        self._sync_center_panes()

        # Popover flotante de configuración de audio (abierto desde el engranaje lateral)
        self.audio_popup = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.audio_popup.setObjectName("DiscordDeckCard")
        self.audio_popup.setFixedWidth(404)
        pop_lay = QVBoxLayout(self.audio_popup)
        pop_lay.setContentsMargins(12, 10, 12, 10)
        pop_lay.setSpacing(8)

        pop_hdr = QHBoxLayout()
        pop_hdr.setSpacing(8)
        pop_title = QLabel("Audio de Discord")
        pop_title.setStyleSheet("font-size: 12.5px; font-weight: 700;")
        pop_hdr.addWidget(pop_title)
        pop_hdr.addStretch(1)
        pop_lay.addLayout(pop_hdr)

        self.audio_advanced_box = QWidget()
        self.audio_advanced_box.setObjectName("TransparentBox")
        adv_lay = QVBoxLayout(self.audio_advanced_box)
        adv_lay.setContentsMargins(0, 2, 0, 0)
        adv_lay.setSpacing(8)

        out_col = QVBoxLayout()
        out_col.setSpacing(3)
        out_hdr = QHBoxLayout()
        out_hdr.addWidget(QLabel("Salida"))
        self.out_vol_val = QLabel(f"{self.cfg.discord_output_volume}%")
        self.out_vol_val.setObjectName("MutedLabel")
        out_hdr.addStretch()
        out_hdr.addWidget(self.out_vol_val)
        out_col.addLayout(out_hdr)
        self.out_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.out_vol_slider.setRange(0, 200)
        self.out_vol_slider.setValue(self.cfg.discord_output_volume)
        self.out_vol_slider.valueChanged.connect(self._on_out_vol_changed)
        out_col.addWidget(self.out_vol_slider)
        adv_lay.addLayout(out_col)

        mic_col = QVBoxLayout()
        mic_col.setSpacing(3)
        mic_hdr = QHBoxLayout()
        mic_hdr.addWidget(QLabel("Ganancia Mic"))
        self.mic_gain_val = QLabel(f"{self.cfg.discord_mic_gain}%")
        self.mic_gain_val.setObjectName("MutedLabel")
        mic_hdr.addStretch()
        mic_hdr.addWidget(self.mic_gain_val)
        mic_col.addLayout(mic_hdr)
        self.mic_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_gain_slider.setRange(0, 400)
        self.mic_gain_slider.setValue(self.cfg.discord_mic_gain)
        self.mic_gain_slider.valueChanged.connect(self._on_mic_gain_changed)
        mic_col.addWidget(self.mic_gain_slider)
        adv_lay.addLayout(mic_col)

        vad_col = QVBoxLayout()
        vad_col.setSpacing(3)
        vad_hdr = QHBoxLayout()
        vad_hdr.addWidget(QLabel("Corte de ruido (VAD)"))
        self.vad_val = QLabel()
        self.vad_val.setObjectName("MutedLabel")
        vad_hdr.addStretch()
        vad_hdr.addWidget(self.vad_val)
        vad_col.addLayout(vad_hdr)
        self.vad_slider = QSlider(Qt.Orientation.Horizontal)
        self.vad_slider.setRange(0, 80)
        self.vad_slider.setValue(min(80, self.cfg.discord_vad_threshold))
        self.vad_slider.setToolTip(
            "Más a la izquierda = más sensible (0% = abierto). Recomendado: 15–25%. Valores >45% pueden cortar palabras."
        )
        self.vad_slider.valueChanged.connect(self._on_vad_changed)
        vad_col.addWidget(self.vad_slider)
        self._update_vad_label(self.vad_slider.value())
        adv_lay.addLayout(vad_col)

        dev_in_row = QHBoxLayout()
        dev_in_row.setSpacing(6)
        in_lbl = QLabel("Micrófono:")
        in_lbl.setFixedWidth(70)
        dev_in_row.addWidget(in_lbl)
        self.input_combo = QComboBox()
        self.input_combo.addItem("Predeterminado del sistema", "")
        self.input_combo.currentIndexChanged.connect(self._on_devices_combo_changed)
        dev_in_row.addWidget(self.input_combo, 1)

        self.refresh_dev_btn = QPushButton()
        self.refresh_dev_btn.setObjectName("IconBtn")
        self.refresh_dev_btn.setFixedSize(28, 28)
        self.refresh_dev_btn.setToolTip("Detectar micrófonos y salidas de audio")
        self.refresh_dev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_dev_btn.clicked.connect(self._on_refresh_devices_clicked)
        dev_in_row.addWidget(self.refresh_dev_btn)
        adv_lay.addLayout(dev_in_row)

        dev_out_row = QHBoxLayout()
        dev_out_row.setSpacing(6)
        out_lbl = QLabel("Salida:")
        out_lbl.setFixedWidth(70)
        dev_out_row.addWidget(out_lbl)
        self.output_combo = QComboBox()
        self.output_combo.addItem("Predeterminado del sistema", "")
        self.output_combo.currentIndexChanged.connect(self._on_devices_combo_changed)
        dev_out_row.addWidget(self.output_combo, 1)
        adv_lay.addLayout(dev_out_row)

        ns_row = QHBoxLayout()
        ns_row.setSpacing(6)
        ns_lbl = QLabel("Supresión:")
        ns_lbl.setFixedWidth(70)
        ns_row.addWidget(ns_lbl)
        self.ns_combo = QComboBox()
        for ns_label, ns_key in (
            ("IA Máxima (estilo Krisp)", "ai_max"),
            ("IA (DeepFilterNet3)", "ai"),
            ("Estándar (WebRTC)", "webrtc"),
            ("Desactivada", "off"),
        ):
            self.ns_combo.addItem(ns_label, ns_key)
        ns_idx = self.ns_combo.findData(self.cfg.discord_noise_suppression)
        if ns_idx >= 0:
            self.ns_combo.setCurrentIndex(ns_idx)
        self.ns_combo.setToolTip(
            "Los modos IA usan DeepFilterNet3 + Silero VAD v6 para eliminar teclado, mouse, ventiladores y ruido ambiente como Krisp en Discord."
        )
        self.ns_combo.currentIndexChanged.connect(self._on_ns_changed)
        ns_row.addWidget(self.ns_combo, 1)
        adv_lay.addLayout(ns_row)

        bottom_audio_row = QHBoxLayout()
        bottom_audio_row.setSpacing(8)
        self.agc_check = QCheckBox("Auto-ganancia")
        self.agc_check.setChecked(self.cfg.discord_mic_agc)
        self.agc_check.setToolTip(
            "Ajusta automáticamente el volumen de tu micrófono (AGC con limitador anti-saturación)"
        )
        self.agc_check.toggled.connect(self._on_agc_toggled)
        bottom_audio_row.addWidget(self.agc_check)
        bottom_audio_row.addStretch(1)

        self.mic_test_btn = QPushButton("Probar micrófono")
        self.mic_test_btn.setObjectName("PillBtn")
        self.mic_test_btn.setProperty("active", "false")
        self.mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_test_btn.setToolTip(
            "Te silencia y ensordece en el canal para escucharte en local con tus ajustes de voz."
        )
        self.mic_test_btn.clicked.connect(self._toggle_mic_test)
        bottom_audio_row.addWidget(self.mic_test_btn)
        adv_lay.addLayout(bottom_audio_row)

        self.mic_test_hint_lbl = QLabel(
            "Escuchándote en local (silenciado y ensordecido en el canal)"
        )
        self.mic_test_hint_lbl.setObjectName("MutedLabel")
        self.mic_test_hint_lbl.setStyleSheet("color: #4ADE80; font-size: 11px;")
        self.mic_test_hint_lbl.setWordWrap(True)
        self.mic_test_hint_lbl.setVisible(False)
        adv_lay.addWidget(self.mic_test_hint_lbl)

        pop_lay.addWidget(self.audio_advanced_box)
        self.audio_popup.hide()

        ws_lay.addLayout(right_col, 1)

        # Columna derecha desplegable: Miembros por Canal o Servidor (OCULTA por defecto)
        self.members_card = QFrame()
        self.members_card.setObjectName("DiscordSidebarCard")
        self.members_card.setMinimumWidth(215)
        self.members_card.setMaximumWidth(250)
        m_lay = QVBoxLayout(self.members_card)
        m_lay.setContentsMargins(10, 10, 10, 10)
        m_lay.setSpacing(6)

        m_hdr = QHBoxLayout()
        m_title = QLabel("MIEMBROS")
        m_title.setObjectName("SectionHeader")
        m_title.setStyleSheet(
            "padding: 2px 0px; font-size: 10px; font-weight: 700; letter-spacing: 1.2px;"
        )
        m_hdr.addWidget(m_title)
        m_hdr.addStretch()

        self.close_members_btn = QPushButton()
        self.close_members_btn.setObjectName("IconBtn")
        self.close_members_btn.setFixedSize(24, 24)
        self.close_members_btn.setToolTip("Ocultar panel de miembros")
        self.close_members_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_members_btn.clicked.connect(self._toggle_members_panel)
        m_hdr.addWidget(self.close_members_btn)
        m_lay.addLayout(m_hdr)

        scope_row = QHBoxLayout()
        scope_row.setSpacing(6)
        self.members_scope_channel_btn = QPushButton("Canal")
        self.members_scope_channel_btn.setObjectName("PillBtn")
        self.members_scope_channel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.members_scope_channel_btn.setToolTip("Mostrar solo miembros con acceso al canal activo")
        self.members_scope_channel_btn.clicked.connect(lambda: self._set_members_scope("channel"))
        scope_row.addWidget(self.members_scope_channel_btn, 1)

        self.members_scope_server_btn = QPushButton("Servidor")
        self.members_scope_server_btn.setObjectName("PillBtn")
        self.members_scope_server_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.members_scope_server_btn.setToolTip("Mostrar todos los miembros del servidor")
        self.members_scope_server_btn.clicked.connect(lambda: self._set_members_scope("server"))
        scope_row.addWidget(self.members_scope_server_btn, 1)
        m_lay.addLayout(scope_row)

        self.members_online_lbl = QLabel("0 en línea")
        self.members_online_lbl.setObjectName("MutedLabel")
        m_lay.addWidget(self.members_online_lbl)

        self.members_list = QListWidget()
        self.members_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.members_list.setWordWrap(True)
        self.members_list.setIconSize(self._LIST_AVATAR_SIZE)
        self.members_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { padding: 5px 6px; border-radius: 7px; border: 1px solid transparent; }"
            "QListWidget::item:selected { background-color: #262A32; color: #F1F3F6; "
            "border: 1px solid #CFD5DF; font-weight: 700; }"
        )
        self.members_list.setToolTip("Miembros del canal o servidor (clic derecho o doble clic para DM)")
        self.members_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.members_list.customContextMenuRequested.connect(self._on_members_context_menu)
        self.members_list.itemDoubleClicked.connect(lambda _it: self._on_open_selected_dm())
        m_lay.addWidget(self.members_list, 1)

        self.dm_hint_lbl = QLabel("Entre bots usen un canal privado")
        self.dm_hint_lbl.setObjectName("MutedLabel")
        self.dm_hint_lbl.setWordWrap(True)
        self.dm_hint_lbl.setVisible(False)
        m_lay.addWidget(self.dm_hint_lbl)

        self.dm_btn = QPushButton("Enviar DM")
        self.dm_btn.setToolTip("Abrir conversación directa con la persona seleccionada")
        self.dm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dm_btn.setEnabled(False)
        self.dm_btn.setVisible(False)
        self.dm_btn.clicked.connect(self._on_open_selected_dm)
        self.members_list.itemSelectionChanged.connect(self._on_member_selection_changed)
        m_lay.addWidget(self.dm_btn)

        self.members_card.setVisible(False)
        ws_lay.addWidget(self.members_card)

        root.addWidget(self.workspace_widget, 1)
        self._refresh_header_toggles()
        self._refresh_scope_buttons()

    def _connect_service_signals(self) -> None:
        self.svc.state_changed.connect(self._on_bot_state_changed)
        self.svc.guilds_updated.connect(self._on_guilds_updated)
        self.svc.guild_members_updated.connect(self._on_guild_members_updated)
        self.svc.presence_updated.connect(self._on_presence_updated)
        self.svc.chat_history_loaded.connect(self._on_chat_history_loaded)
        self.svc.chat_message_received.connect(self._on_chat_message_received)
        self.svc.chat_message_edited.connect(self._on_chat_message_edited)
        self.svc.channel_message_incoming.connect(self._on_channel_message_incoming)
        self.svc.dm_incoming.connect(self._on_dm_incoming)
        self.svc.voice_state_updated.connect(self._on_voice_state_updated)
        self.svc.speaking_updated.connect(self._on_speaking_updated)
        self.svc.mic_level_updated.connect(self._on_mic_level_updated)
        self.svc.devices_updated.connect(self._on_devices_updated)
        self.svc.worker_ram_updated.connect(self._on_worker_ram_updated)
        self.svc.error_occurred.connect(self._on_service_error)
        self.svc.warning_occurred.connect(lambda msg: self.toast_requested.emit(msg))
        self.svc.screen_share_detected.connect(self._on_remote_screen_share_detected)
        self.svc.screen_share_ended.connect(self._on_remote_screen_share_ended)
        self.screen_share_svc.state_changed.connect(self._on_screen_share_state_changed)
        self.screen_share_svc.stats_updated.connect(self._on_screen_share_stats_updated)
        self.screen_share_svc.worker_ram_updated.connect(self._on_screen_share_worker_ram)
        self.screen_share_svc.error_occurred.connect(self._on_screen_share_error)
        self.screen_share_svc.warning_occurred.connect(lambda msg: self.toast_requested.emit(msg))
        self.screen_share_announced.connect(self._on_screen_share_announced)
        if self._thumb_cache is not None:
            self._thumb_cache.ready.connect(self._on_member_avatar_ready)
            self._thumb_cache.ready.connect(self._on_chat_image_ready)

    def toggle_audio_popover(self, anchor: QWidget | None = None) -> None:
        if not hasattr(self, "audio_popup"):
            return
        if self.audio_popup.isVisible():
            self.audio_popup.hide()
            return
        self.audio_popup.adjustSize()
        pop_w = self.audio_popup.width()
        pop_h = self.audio_popup.sizeHint().height()
        if anchor is not None and anchor.isVisible():
            top_left = anchor.mapToGlobal(QPoint(anchor.width() + 8, anchor.height() - pop_h))
        else:
            top_left = self.mapToGlobal(QPoint(20, max(20, self.height() - pop_h - 16)))
        scr = QApplication.screenAt(top_left) or QApplication.primaryScreen()
        if scr is not None:
            geo = scr.availableGeometry()
            tx = max(geo.left() + 8, min(geo.right() - pop_w - 8, top_left.x()))
            ty = max(geo.top() + 8, min(geo.bottom() - pop_h - 8, top_left.y()))
            top_left = QPoint(tx, ty)
        self.audio_popup.move(top_left)
        self.audio_popup.show()
        self.audio_popup.raise_()

    def close_audio_popover(self) -> None:
        if hasattr(self, "audio_popup") and self.audio_popup.isVisible():
            self.audio_popup.hide()

    def _emit_voice_status(self) -> None:
        active = bool(
            self._voice_state.get("in_voice", False)
            or self._bot_state == "connected"
            or self.svc.is_connected
        )
        self.voice_status_changed.emit(active, self._muted, self._deafened)

    def sync_notify_channels(self) -> None:
        self.svc.set_notify_channels(self.cfg.discord_notify_channels)

    def open_notification_target(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("kind") or "").strip().lower()
        tid = str(payload.get("target_id") or "").strip()
        if not kind or not tid:
            return
        if kind == "dm":
            title = str(payload.get("title") or tid).strip() or tid
            av = str(payload.get("avatar_url") or "").strip()
            self._open_dm_with_user(tid, title, av)
        elif kind == "channel":
            gid = str(payload.get("guild_id") or "").strip()
            if gid and hasattr(self, "guild_combo"):
                idx = self.guild_combo.findData(gid)
                if idx >= 0 and idx != self.guild_combo.currentIndex():
                    self.guild_combo.setCurrentIndex(idx)
            gid = gid or str(self.guild_combo.currentData() or "")
            cname = str(payload.get("title") or "canal").strip() or "canal"
            self._open_text_channel_clicked(gid, tid, cname)

    def _is_window_actively_focused(self) -> bool:
        win = self.window()
        return bool(
            win is not None
            and win.isVisible()
            and not win.isMinimized()
            and win.isActiveWindow()
        )

    def is_viewing_channel(self, channel_id: str) -> bool:
        return (
            self._is_window_actively_focused()
            and self.isVisible()
            and self._active_chat_kind == "channel"
            and self._active_chat_id == str(channel_id)
            and self._chat_pane_visible
        )

    def is_viewing_dm(self, user_id: str) -> bool:
        return (
            self._is_window_actively_focused()
            and self.isVisible()
            and self._active_chat_kind == "dm"
            and self._active_chat_id == str(user_id)
            and self._chat_pane_visible
        )

    def on_window_activated(self) -> None:
        self._clear_active_unread_if_viewing()

    def total_unread_count(self) -> int:
        return sum(self._unread_channels.values()) + sum(self._unread_dms.values())

    def _emit_unread_count(self) -> None:
        self.unread_count_changed.emit(self.total_unread_count())

    def _clear_active_unread_if_viewing(self) -> None:
        if not (
            self._is_window_actively_focused()
            and self.isVisible()
            and self._chat_pane_visible
            and self._active_chat_id
        ):
            return
        if self._active_chat_kind == "channel":
            if self._unread_channels.pop(self._active_chat_id, 0) > 0:
                if not self._sync_chat_selection_in_place():
                    self._render_channels(force=True)
                self._emit_unread_count()
        elif self._active_chat_kind == "dm":
            if self._unread_dms.pop(self._active_chat_id, 0) > 0:
                if not self._sync_chat_selection_in_place():
                    self._render_dm_threads()
                self._emit_unread_count()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._clear_active_unread_if_viewing()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self.close_image_lightbox()
        super().hideEvent(event)

    def refresh_ui_preferences(self) -> None:
        """Aplica cambios de preferencias visuales (canales sin permiso, avisos, etc.)."""
        self._render_channels(force=True)
        self._emit_voice_status()

    def _refresh_header_toggles(self) -> None:
        self.toggle_channels_btn.setProperty("active", "true" if self._channels_visible else "false")
        self.toggle_members_btn.setProperty("active", "true" if self._members_visible else "false")
        for b in (self.toggle_channels_btn, self.toggle_members_btn):
            b.style().unpolish(b)
            b.style().polish(b)

    def _refresh_scope_buttons(self) -> None:
        self.members_scope_channel_btn.setProperty(
            "active", "true" if self._members_scope == "channel" else "false"
        )
        self.members_scope_server_btn.setProperty(
            "active", "true" if self._members_scope == "server" else "false"
        )
        for b in (self.members_scope_channel_btn, self.members_scope_server_btn):
            b.style().unpolish(b)
            b.style().polish(b)

    def _set_members_scope(self, scope: Literal["channel", "server"]) -> None:
        self._members_scope = "server" if scope == "server" else "channel"
        self._refresh_scope_buttons()
        self._refresh_members_list()

    def _toggle_channels_panel(self) -> None:
        self._channels_visible = not self._channels_visible
        self.left_card.setVisible(self._channels_visible)
        self._refresh_header_toggles()

    def _toggle_members_panel(self) -> None:
        self._members_visible = not self._members_visible
        self.members_card.setVisible(self._members_visible)
        self._refresh_header_toggles()

    def _toggle_audio_drawer(self) -> None:
        self._audio_drawer_open = not self._audio_drawer_open
        self.audio_advanced_box.setVisible(self._audio_drawer_open)
        self._refresh_header_toggles()

    def _toggle_voice_channels_section(self) -> None:
        self._vc_section_open = not self._vc_section_open
        self.vc_list_widget.setVisible(self._vc_section_open)
        self.vc_section_btn.setText(
            "▾ CANALES DE VOZ" if self._vc_section_open else "▸ CANALES DE VOZ"
        )

    def _toggle_text_channels_section(self) -> None:
        self._tc_section_open = not self._tc_section_open
        self.text_channels_scroll.setVisible(self._tc_section_open)
        self._update_tc_section_label()

    def _format_chat_placeholder(self, kind: str, label: str, can_send: bool) -> str:
        if not can_send:
            if kind == "channel":
                return "Sin permiso para enviar mensajes en este canal."
            return "Conecta el Bot para enviar nuevos mensajes (historial guardado visible)."
        clean = str(label or "").strip()
        if kind == "dm":
            if clean.startswith("DM · "):
                clean = clean[5:].strip()
            clean = clean.lstrip("@ ").strip() or "usuario"
            return f"Enviar mensaje directo a {clean}... [Enter]"
        if kind == "channel":
            clean = clean.lstrip("# ").strip() or "canal"
            return f"Enviar mensaje a #{clean}... [Enter]"
        return "Selecciona un canal #texto para escribir... [Enter]"

    def _update_tc_section_label(self) -> None:
        gid = str(self.guild_combo.currentData() or "")
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
        hidden = set(self.cfg.discord_hidden_channels)
        tc_cnt = sum(
            1
            for tc in ((guild or {}).get("text_channels") or ())
            if str(tc.get("id") or "") not in hidden
        )
        arrow = "▾" if self._tc_section_open else "▸"
        suffix = f" ({tc_cnt})" if tc_cnt > 0 else ""
        unread_tc = sum(self._unread_channels.values())
        un_txt = f" · ● {unread_tc}" if unread_tc > 0 else ""
        self.tc_section_btn.setText(f"{arrow} CANALES DE TEXTO{suffix}{un_txt}")

    def _update_dm_section_label(self, thread_count: int | None = None) -> None:
        if not hasattr(self, "dm_section_btn"):
            return
        if thread_count is None:
            thread_count = len(self._dm_buttons) if self._dm_buttons else len(self._load_dm_threads())
        arrow = "▾" if self._dm_section_open else "▸"
        suffix = f" ({thread_count})" if thread_count > 0 else ""
        unread_dm = sum(self._unread_dms.values())
        un_txt = f" · ● {unread_dm}" if unread_dm > 0 else ""
        self.dm_section_btn.setText(f"{arrow} MENSAJES DIRECTOS{suffix}{un_txt}")

    def _sync_chat_selection_in_place(self) -> bool:
        self._update_tc_section_label()
        self._update_dm_section_label()
        found_active = False

        for cid, btn in list(self._tc_buttons.items()):
            is_act = self._active_chat_kind == "channel" and self._active_chat_id == cid
            unread_n = self._unread_channels.get(cid, 0)
            has_unread = unread_n > 0 and not self.is_viewing_channel(cid)
            if is_act:
                found_active = True
            btn.setProperty("active", "true" if is_act else "false")
            btn.setProperty("unread", "true" if has_unread else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            badge = self._tc_badges.get(cid)
            if badge is not None:
                badge.setText("9+" if unread_n > 9 else str(max(1, unread_n)))
                badge.setVisible(has_unread)

        for uid, btn in list(self._dm_buttons.items()):
            is_act = self._active_chat_kind == "dm" and self._active_chat_id == uid
            unread_n = self._unread_dms.get(uid, 0)
            has_unread = unread_n > 0 and not self.is_viewing_dm(uid)
            if is_act:
                found_active = True
            btn.setProperty("active", "true" if is_act else "false")
            btn.setProperty("unread", "true" if has_unread else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            badge = self._dm_badges.get(uid)
            if badge is not None:
                badge.setText("9+" if unread_n > 9 else str(max(1, unread_n)))
                badge.setVisible(has_unread)

        gid = str(self.guild_combo.currentData() or "")
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
        self._last_channels_sig = self._channels_signature(gid, guild)
        return found_active or not self._active_chat_id

    def _on_center_fade_value(self, val: Any) -> None:
        w = self._fade_target_widget
        if w is None:
            return
        eff = w.graphicsEffect()
        if isinstance(eff, QGraphicsOpacityEffect):
            eff.setOpacity(max(0.0, min(1.0, float(val))))

    def _on_center_fade_finished(self) -> None:
        w = self._fade_target_widget
        self._fade_target_widget = None
        if w is not None:
            try:
                w.setGraphicsEffect(None)
            except RuntimeError:
                pass

    def _animate_center_widget(self, widget: QWidget | None) -> None:
        if widget is None or not self.isVisible():
            return
        try:
            self._center_fade_anim.stop()
        except RuntimeError:
            pass
        if self._fade_target_widget is not None:
            try:
                self._fade_target_widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
            self._fade_target_widget = None
        eff = QGraphicsOpacityEffect(widget)
        eff.setOpacity(0.84)
        widget.setGraphicsEffect(eff)
        self._fade_target_widget = widget
        self._center_fade_anim.start()

    def _sync_center_panes(self, *, animate: bool = False) -> None:
        if self._chat_pane_visible:
            self._voice_pane_visible = False
        else:
            self._voice_pane_visible = True
            self._chat_pane_visible = False
        target = self.voice_pane if self._voice_pane_visible else self.chat_pane
        changed = self.center_stack.currentWidget() is not target
        self.center_stack.setVisible(True)
        self.center_collapsed_card.setVisible(False)
        self.center_stack.setCurrentWidget(target)
        if target is self.voice_pane and self._participant_cards_order:
            QTimer.singleShot(0, self._relayout_participant_cards_if_needed)
        if animate and changed:
            self._animate_center_widget(target)
        self.tab_voice_btn.setProperty("active", "true" if self._voice_pane_visible else "false")
        self.tab_chat_btn.setProperty("active", "true" if self._chat_pane_visible else "false")
        for b in (self.tab_voice_btn, self.tab_chat_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        self._clear_active_unread_if_viewing()

    def _set_pane_visible(self, which: Literal["voice", "chat"], visible: bool) -> None:
        if which == "voice":
            show_voice = bool(visible)
        else:
            show_voice = not bool(visible)
        self._voice_pane_visible = show_voice
        self._chat_pane_visible = not show_voice
        self._sync_center_panes(animate=True)

    def _on_center_tab_toggled(self, which: Literal["voice", "chat"]) -> None:
        self._voice_pane_visible = which == "voice"
        self._chat_pane_visible = which == "chat"
        self._sync_center_panes(animate=True)

    def get_available_share_targets_for_settings(self) -> list[tuple[str, str]]:
        """Retorna [('channel:<id>', '# canal (Servidor)'), ('dm:<id>', '@ Usuario (DM)')] para Ajustes."""
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for g in self._guilds:
            gname = str(g.get("name") or "").strip() or "Servidor"
            for tc in g.get("text_channels") or ():
                cid = str(tc.get("id") or "").strip()
                cname = str(tc.get("name") or "general").strip().lstrip("# ")
                key = f"channel:{cid}"
                if cid and key not in seen:
                    seen.add(key)
                    items.append((key, f"# {cname} ({gname})  ·  Canal"))
        for uid, ulabel in self._current_dm_targets():
            key = f"dm:{uid}"
            if uid and key not in seen:
                seen.add(key)
                items.append((key, f"@ {ulabel.lstrip('@ ')}  ·  DM"))
        for raw_key in self.cfg.discord_share_auto_targets:
            key = str(raw_key or "").strip()
            if key and key not in seen:
                seen.add(key)
                lbl = self.cfg.discord_share_auto_target_names.get(key) or key
                items.append((key, lbl))
        return items

    def get_available_channels_for_settings(self) -> list[tuple[str, str]]:
        """Retorna [(channel_id, etiqueta)] de los canales conocidos para la gestión en Ajustes."""
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for g in self._guilds:
            gname = str(g.get("name") or "").strip() or "Servidor"
            for ch in g.get("voice_channels") or ():
                cid = str(ch.get("id") or "").strip()
                cname = str(ch.get("name") or "voz").strip()
                if cid and cid not in seen:
                    seen.add(cid)
                    items.append((cid, f"🔊 {cname} ({gname})"))
            for tc in g.get("text_channels") or ():
                cid = str(tc.get("id") or "").strip()
                cname = str(tc.get("name") or "texto").strip()
                if cid and cid not in seen:
                    seen.add(cid)
                    items.append((cid, f"# {cname} ({gname})"))
        for cid in self.cfg.discord_hidden_channels:
            if cid not in seen:
                seen.add(cid)
                lbl = self.cfg.discord_hidden_channel_names.get(cid) or f"Canal {cid}"
                items.append((cid, lbl))
        return items

    def hide_channel_permanently(self, channel_id: str, label: str = "") -> None:
        cid = str(channel_id or "").strip()
        if not cid:
            return
        if cid not in self.cfg.discord_hidden_channels:
            self.cfg.discord_hidden_channels = list(self.cfg.discord_hidden_channels) + [cid]
        if label:
            names = dict(self.cfg.discord_hidden_channel_names)
            names[cid] = str(label).strip()
            self.cfg.discord_hidden_channel_names = names
        save_config(self.cfg)
        self._render_channels(force=True)
        disp = label or cid
        self.toast_requested.emit(f"Canal oculto siempre: {disp} (Ajustes → Discord)")

    def _show_channel_context_menu(self, global_pos: Any, channel_id: str, label: str) -> None:
        menu = QMenu(self)
        act_hide = menu.addAction("Ocultar este canal siempre")
        chosen = menu.exec(global_pos)
        if chosen is act_hide:
            self.hide_channel_permanently(channel_id, label)

    def _ensure_chat_pane_visible(self) -> None:
        if not self._chat_pane_visible:
            self._voice_pane_visible = False
            self._chat_pane_visible = True
            self._sync_center_panes(animate=True)

    def _set_center_tab(self, idx: int) -> None:
        """Compatibilidad: 0 = solo voz, 1 = solo chat."""
        val = int(idx)
        if val == 1:
            self._voice_pane_visible = False
            self._chat_pane_visible = True
        else:
            self._voice_pane_visible = True
            self._chat_pane_visible = False
        self._sync_center_panes(animate=True)

    def _update_info_pill(self) -> None:
        if self._bot_state != "connected" and not self.svc.is_connected:
            self.info_pill.setVisible(False)
            return

        self.info_pill.setVisible(True)
        in_voice = bool(self._voice_state.get("in_voice", False))
        dave_active = bool(self._voice_state.get("dave_active", False))
        ram_txt = f"{self._worker_ws_mb:.1f} MB" if self._worker_ws_mb > 0 else "Activo"

        if in_voice:
            dave_code = str(self._voice_state.get("dave_code") or "")
            tx_pkt = int(self._voice_state.get("tx_packets") or 0)
            rx_pkt = int(self._voice_state.get("rx_packets") or 0)
            tx_err = int(self._voice_state.get("tx_errors") or 0)
            rx_err = int(self._voice_state.get("rx_decrypt_errors") or 0)
            self.info_pill.setIcon(
                render_icon("shield", "#4ADE80" if dave_active else "#FBBF24", 13)
            )
            self.info_pill.setText(f" {ram_txt}")
            dave_line = (
                f"Cifrado E2EE DAVE activo (código: {dave_code})"
                if dave_active and dave_code
                else ("Cifrado E2EE DAVE activo" if dave_active else "Negociando cifrado DAVE E2EE...")
            )
            diag_line = str(self._voice_state.get("connect_diag") or "").strip()
            extra_diag = f"\n{diag_line}" if diag_line else ""
            self.info_pill.setToolTip(
                f"{dave_line}{extra_diag}\n"
                f"Worker: {self._worker_ws_mb:.1f} MB (Priv: {self._worker_priv_mb:.1f} MB)\n"
                f"Paquetes: TX {tx_pkt} · RX {rx_pkt} (Err TX:{tx_err} RX:{rx_err})"
            )
        else:
            self.info_pill.setIcon(QIcon())
            self.info_pill.setText(ram_txt)
            self.info_pill.setToolTip(
                f"Worker aislado: {self._worker_ws_mb:.1f} MB (Priv: {self._worker_priv_mb:.1f} MB)"
            )

    def _on_service_error(self, msg: str) -> None:
        if self._joining_channel_id:
            self._joining_channel_id = ""
            if self.svc.is_connected and not self.svc.is_in_voice:
                self.status_pill.setText(f"Conectado · {self.svc.bot_name}")
            self._render_channels(force=True)
        self.toast_requested.emit(f"Discord: {msg}")

    def refresh_token_state(self) -> None:
        configured = has_discord_token()
        self.setup_card.setVisible(not configured)
        self.workspace_widget.setVisible(configured)
        self.connect_btn.setEnabled(configured)

    def update_theme(self, palette: ThemePalette) -> None:
        self.palette = palette
        p = palette
        self.refresh_guilds_btn.setIcon(render_icon("refresh", p.text, 14))
        self.refresh_dev_btn.setIcon(render_icon("refresh", p.text, 14))
        if hasattr(self, "chat_attach_btn"):
            self.chat_attach_btn.setIcon(render_icon("paperclip", p.text, 15))
        if hasattr(self, "chat_browser"):
            self.chat_browser.update_theme(p)
        close_ico = render_icon("close", p.text2, 13)
        for btn in (
            self.close_channels_btn,
            self.intents_close_btn,
            self.close_voice_btn,
            self.close_chat_btn,
            self.close_members_btn,
            self.remote_dismiss_btn,
        ):
            btn.setIcon(close_ico)
        self.members_list.setStyleSheet(
            f"QListWidget {{ background: transparent; border: none; outline: none; }}"
            f"QListWidget::item {{ padding: 5px 6px; border-radius: 7px; border: 1px solid transparent; }}"
            f"QListWidget::item:selected {{ background-color: {p.active_bg}; color: {p.text}; "
            f"border: 1px solid {p.accent}; font-weight: 700; }}"
        )
        self._sync_screen_share_icons()
        self._update_info_pill()
        self._refresh_mute_deaf_buttons()
        for card in self._participant_cards_order:
            card.update_theme(p)

    def _refresh_mute_deaf_buttons(self) -> None:
        return

    def _on_connect_toggle_clicked(self) -> None:
        if self._bot_state in ("connecting", "connected"):
            self.svc.disconnect_and_kill()
        else:
            if not has_discord_token():
                self.open_token_settings_requested.emit()
                return
            self.svc.connect_bot(self.cfg)

    def _on_bot_state_changed(self, state: str, data: dict[str, Any]) -> None:
        self._bot_state = state
        if state == "connecting":
            self.status_pill.setText(
                "Reconectando..." if data.get("reconnecting") else "Conectando..."
            )
            self.connect_btn.setText("Cancelar")
            self.connect_btn.setObjectName("DangerBtn")
            self.intents_banner.setVisible(False)
            self.info_pill.setVisible(False)
        elif state == "connected":
            bot_name = str(data.get("bot_name") or "Bot")
            self.status_pill.setText(f"Conectado · {bot_name}")
            self.connect_btn.setText("Desconectar")
            self.connect_btn.setObjectName("DangerBtn")
            priv_ok = bool(data.get("privileged_intents_ok", False))
            self.intents_banner.setVisible(not priv_ok)
            pres = data.get("presences")
            if isinstance(pres, dict):
                self._user_statuses.update({str(k): _normalize_presence(v) for k, v in pres.items()})
                self._render_dm_threads()
                self._sync_dm_header_status()
            self._update_info_pill()
            self.sync_notify_channels()
            self.screen_share_btn.setVisible(True)
            if self._screen_share_state == "sharing" and self._screen_share_info.get("watch_url"):
                self.svc.set_screen_share_presence(
                    {
                        "watch_url": str(self._screen_share_info.get("watch_url") or ""),
                        "quality": str(self._screen_share_info.get("quality") or "720p"),
                        "fps": int(self._screen_share_info.get("fps") or 30),
                        "has_audio": bool(self._screen_share_info.get("has_audio", False)),
                        "rev": max(1, self._screen_share_rev),
                    }
                )
        elif state == "error":
            self._joining_channel_id = ""
            self._mic_test_active = False
            self._mic_test_prev_flags = None
            self._sync_mic_test_ui()
            self.status_pill.setText("Error")
            self.connect_btn.setText(
                "Desconectar" if self.svc.is_worker_running else "Conectar Bot"
            )
            self.connect_btn.setObjectName(
                "DangerBtn" if self.svc.is_worker_running else "AccentBtn"
            )
            self.intents_banner.setVisible(False)
            self.info_pill.setVisible(False)
            self.screen_share_btn.setVisible(self.svc.is_connected)
        else:
            self._remote_screen_shares.clear()
            self._dismissed_share_urls.clear()
            self.stop_screen_share_session()
            self.screen_share_btn.setVisible(False)
            self._joining_channel_id = ""
            self._mic_test_active = False
            self._mic_test_prev_flags = None
            self._sync_mic_test_ui()
            self._selected_guild_id = ""
            self._active_chat_kind = ""
            self._active_chat_id = ""
            self._active_chat_can_send = False
            self._active_dm_peer_name = ""
            self._active_dm_peer_avatar = ""
            self._unread_channels.clear()
            self._unread_dms.clear()
            self._last_channels_sig = None
            self._last_participants_sig = None
            self._worker_ws_mb = 0.0
            self._worker_priv_mb = 0.0
            self._chat_messages.clear()
            self._members = []
            self._online_count = 0
            self._channel_members = []
            self._channel_online_count = 0
            self._channel_members_loaded = False
            self._member_row_by_url.clear()
            self._user_statuses.clear()
            self.chat_status_pill.setVisible(False)
            self.members_list.clear()
            self.members_online_lbl.setText("0 en línea")
            self.toggle_members_btn.setText("Miembros")
            self.dm_btn.setEnabled(False)
            self.dm_btn.setVisible(False)
            self.dm_hint_lbl.setVisible(False)
            self._active_chat_can_send = False
            self._active_chat_can_attach = False
            self._chat_pending_img_urls.clear()
            self._chat_attachment_refs.clear()
            self._chat_img_rerender_timer.stop()
            self._cancel_chat_compose_mode()
            self._clear_pending_attachments(delete_temp=True)
            self.close_image_lightbox()
            self.chat_title_lbl.setText("Sin chat seleccionado")
            self.chat_input.clear()
            self.chat_input.setPlaceholderText("Selecciona un canal #texto para escribir... [Enter]")
            self.chat_input.setEnabled(False)
            self.chat_send_btn.setEnabled(False)
            self.chat_attach_btn.setEnabled(False)
            self.close_audio_popover()
            self.chat_browser.set_can_send(False)
            self.chat_browser.show_placeholder(
                "Selecciona un canal de texto a la izquierda para ver y enviar mensajes aquí."
            )
            self.intents_banner.setVisible(False)
            self.info_pill.setVisible(False)
            self.status_pill.setText("Desconectado")
            self.connect_btn.setText("Conectar Bot")
            self.connect_btn.setObjectName("AccentBtn")
            self._guilds = []
            self._render_channels(force=True)
            self._render_dm_threads()
            self._voice_state = {"in_voice": False, "participants": []}
            self._render_participants(force=True)

        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)
        self._emit_voice_status()
        self._emit_unread_count()

    def _on_worker_ram_updated(self, ws_mb: float, priv_mb: float) -> None:
        self._worker_ws_mb = float(ws_mb)
        self._worker_priv_mb = float(priv_mb)
        self._update_info_pill()

    def _auto_open_first_text_channel(self, gid: str) -> None:
        if not gid or not self.svc.is_connected or self._active_chat_id:
            return
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
        if guild is None:
            return
        hidden = set(self.cfg.discord_hidden_channels)
        tcs = [
            tc
            for tc in (guild.get("text_channels") or ())
            if str(tc.get("id") or "") not in hidden
        ]
        if not tcs:
            return
        chosen = next((tc for tc in tcs if tc.get("can_read", True)), tcs[0])
        cid = str(chosen.get("id") or "")
        cname = str(chosen.get("name") or "texto")
        if cid:
            self._active_chat_kind = "channel"
            self._active_chat_id = cid
            self._unread_channels.pop(cid, None)
            self.chat_title_lbl.setText(f"# {cname}")
            self._sync_dm_header_status()
            self.chat_input.setPlaceholderText(
                self._format_chat_placeholder("channel", cname, self.svc.is_connected)
            )
            if not self._sync_chat_selection_in_place():
                self._render_channels(force=True)
                self._render_dm_threads()
            self._emit_unread_count()
            self.svc.open_text_channel(gid, cid)

    def _on_guilds_updated(self, guilds: list[dict[str, Any]]) -> None:
        self._guilds = guilds
        status_changed = False
        voice_people: list[dict[str, Any]] = []
        for g in guilds:
            for vc in g.get("voice_channels") or ():
                for vm in vc.get("members") or ():
                    if isinstance(vm, dict):
                        voice_people.append(vm)
                    uid = str(vm.get("id") or "")
                    st_raw = vm.get("status")
                    if uid and st_raw:
                        norm_st = _normalize_presence(st_raw)
                        if self._user_statuses.get(uid) != norm_st:
                            self._user_statuses[uid] = norm_st
                            status_changed = True
        healed = self._heal_dm_thread_names(voice_people)
        prev_gid = self.guild_combo.currentData()
        self.guild_combo.blockSignals(True)
        self.guild_combo.clear()
        for g in guilds:
            self.guild_combo.addItem(str(g.get("name") or "Servidor"), str(g.get("id") or ""))
        if prev_gid:
            idx = self.guild_combo.findData(prev_gid)
            if idx >= 0:
                self.guild_combo.setCurrentIndex(idx)
        self.guild_combo.blockSignals(False)
        self._render_channels()
        if status_changed or healed:
            self._render_dm_threads()
            self._sync_dm_header_status()
        gid = str(self.guild_combo.currentData() or "")
        if gid and self.svc.is_connected and gid != self._selected_guild_id:
            self._selected_guild_id = gid
            self.svc.select_guild(gid)
        if gid:
            self._auto_open_first_text_channel(gid)

    def _on_guild_combo_changed(self, _idx: int) -> None:
        self._cancel_chat_compose_mode()
        self._active_chat_kind = ""
        self._active_chat_id = ""
        self._active_chat_can_send = False
        self._channel_members = []
        self._channel_online_count = 0
        self._channel_members_loaded = False
        self._chat_messages.clear()
        self._render_channels(force=True)
        self._render_dm_threads()
        gid = str(self.guild_combo.currentData() or "")
        if gid and self.svc.is_connected and gid != self._selected_guild_id:
            self._selected_guild_id = gid
            self._member_row_by_url.clear()
            self._user_statuses.clear()
            self.chat_status_pill.setVisible(False)
            self.members_list.clear()
            self.members_online_lbl.setText("Cargando...")
            self.dm_btn.setEnabled(False)
            self.dm_btn.setVisible(False)
            self.dm_hint_lbl.setVisible(False)
            self.svc.select_guild(gid)
            self._auto_open_first_text_channel(gid)

    def _build_voice_channel_row(
        self, gid: str, ch: dict[str, Any], active_cid: str
    ) -> QFrame:
        cid = str(ch.get("id") or "")
        cname = str(ch.get("name") or "Voz")
        members = ch.get("members") or []
        can_connect = bool(ch.get("can_connect", True))
        is_cur = bool(self._voice_state.get("in_voice")) and cid == active_cid
        is_joining = bool(self._joining_channel_id and cid == self._joining_channel_id)
        can_join = bool(can_connect and not is_cur and not is_joining)
        is_clickable = bool(can_join or is_cur or is_joining)

        row_frame = _VoiceChannelRowFrame(is_clickable)
        row_frame.setObjectName("DiscordChannelRow")
        row_frame.setProperty("active", "true" if is_cur else "false")
        row_frame.setProperty("clickable", "true" if is_clickable else "false")
        if can_join:
            row_frame.join_requested.connect(
                lambda g=gid, c=cid, n=cname: self._on_join_channel_clicked(g, c, n)
            )
            row_frame.setToolTip(f"Clic para entrar a 🔊 {cname}")
        elif is_cur or is_joining:
            row_frame.join_requested.connect(lambda: self._set_pane_visible("voice", True))
            row_frame.setToolTip(f"Clic para ver la llamada en 🔊 {cname}")
        gname = next(
            (str(g.get("name") or "") for g in self._guilds if str(g.get("id") or "") == gid),
            "",
        ).strip() or "Servidor"
        vc_label = f"🔊 {cname} ({gname})"
        row_frame.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        row_frame.customContextMenuRequested.connect(
            lambda pos, c=cid, lbl=vc_label, w=row_frame: self._show_channel_context_menu(
                w.mapToGlobal(pos), c, lbl
            )
        )
        row_lay = QVBoxLayout(row_frame)
        row_lay.setContentsMargins(9, 7, 9, 7)
        row_lay.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(5)
        name_lbl = QLabel(f"🔊 {cname}")
        name_lbl.setStyleSheet("font-weight: 700;" if is_cur else "font-weight: 500;")
        top.addWidget(name_lbl, 1)

        if is_cur:
            live_lbl = QLabel("En Voz")
            live_lbl.setStyleSheet("color: #4ADE80; font-size: 11px; font-weight: 700;")
            top.addWidget(live_lbl)
        elif is_joining:
            j_lbl = QLabel("Conectando...")
            j_lbl.setObjectName("MutedLabel")
            j_lbl.setStyleSheet("font-size: 11px;")
            top.addWidget(j_lbl)
        elif not can_connect:
            bl_lbl = QLabel("Bloq.")
            bl_lbl.setObjectName("DeckTag")
            bl_lbl.setToolTip("El bot no tiene permiso 'Conectar' en este canal.")
            top.addWidget(bl_lbl)
        elif members:
            cnt_lbl = QLabel(f"{len(members)}")
            cnt_lbl.setObjectName("DeckTag")
            top.addWidget(cnt_lbl)
        row_lay.addLayout(top)

        if members:
            members_box = QVBoxLayout()
            members_box.setContentsMargins(10, 2, 2, 0)
            members_box.setSpacing(2)
            for m in members[:12]:
                m_id = str(m.get("id") or "")
                m_name = str(m.get("name") or "")
                m_bot = bool(m.get("is_bot", False))
                m_av = str(m.get("avatar_url") or "")
                m_muted = bool(m.get("muted", False))
                m_deaf = bool(m.get("deafened", False))

                mrow_w = QWidget()
                mrow_w.setObjectName("TransparentBox")
                mrow_w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                mrow_w.customContextMenuRequested.connect(
                    lambda pos, w=mrow_w, u=m_id, n=m_name, b=m_bot, a=m_av: (
                        self._open_dm_with_user(u, n, a)
                        if exec_user_dm_menu(w, u, n, b, w.mapToGlobal(pos))
                        else None
                    )
                )
                m_row = QHBoxLayout(mrow_w)
                m_row.setContentsMargins(0, 1, 0, 1)
                m_row.setSpacing(5)

                dot = QLabel("●")
                m_st = _normalize_presence(m.get("status") or self._user_statuses.get(m_id) or ("online" if is_cur else "offline"))
                dot.setStyleSheet(f"color: {_presence_color(m_st)}; font-size: 9px;")
                mrow_w.setToolTip(f"{m_name} · {_presence_label(m_st)}")
                m_row.addWidget(dot)

                mlbl = QLabel(m_name)
                mlbl.setObjectName("MutedLabel")
                mlbl.setStyleSheet("font-size: 11px;")
                m_row.addWidget(mlbl, 1)

                if m_bot:
                    btag = QLabel("APP")
                    btag.setObjectName("DeckTag")
                    m_row.addWidget(btag)
                own_bot_id = str(self.svc.bot_id or "")
                m_share = m.get("screen_share") or (
                    self._remote_screen_shares.get(m_id)
                    if (m_id and m_id != own_bot_id)
                    else None
                )
                if isinstance(m_share, dict) and str(m_share.get("stream_url") or "").strip():
                    live_btn = QPushButton("EN VIVO")
                    live_btn.setObjectName("ScreenShareLiveBadge")
                    live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    live_btn.setToolTip(f"Ver pantalla de {m_name} directo en SwiftPlay")
                    live_btn.clicked.connect(
                        lambda _c=False, p=dict(m_share): self._open_screen_share_viewer(p)
                    )
                    m_row.addWidget(live_btn)
                if m_deaf:
                    dl = QLabel()
                    dl.setPixmap(render_icon("headphones_off", "#F87171", 11).pixmap(QSize(11, 11)))
                    m_row.addWidget(dl)
                elif m_muted:
                    ml = QLabel()
                    ml.setPixmap(render_icon("mic_off", "#F87171", 11).pixmap(QSize(11, 11)))
                    m_row.addWidget(ml)

                members_box.addWidget(mrow_w)
            if len(members) > 12:
                extra = QLabel(f"+{len(members) - 12} más")
                extra.setObjectName("MutedLabel")
                extra.setStyleSheet("font-size: 11px;")
                members_box.addWidget(extra)
            row_lay.addLayout(members_box)

        return row_frame

    def _toggle_locked_vc_group(self) -> None:
        self._locked_vc_expanded = not self._locked_vc_expanded
        self._render_channels(force=True)

    def _toggle_channel_notify(self, channel_id: str, name: str) -> None:
        cid = str(channel_id or "").strip()
        if not cid:
            return
        if cid in self.cfg.discord_notify_channels:
            self.cfg.discord_notify_channels = [
                x for x in self.cfg.discord_notify_channels if x != cid
            ]
            self._unread_channels.pop(cid, None)
            self._emit_unread_count()
            msg = f"Avisos silenciados para #{name}"
        else:
            self.cfg.discord_notify_channels = list(self.cfg.discord_notify_channels) + [cid]
            msg = f"Avisos activos para #{name}"
        save_config(self.cfg)
        self.sync_notify_channels()
        self._render_channels(force=True)
        self.toast_requested.emit(msg)

    def _channels_signature(self, gid: str, guild: dict[str, Any] | None) -> Any:
        if guild is None:
            return (gid, None, len(self._guilds))
        vcs = tuple(
            (
                str(ch.get("id") or ""),
                str(ch.get("name") or ""),
                bool(ch.get("can_connect", True)),
                tuple(
                    (
                        str(m.get("id") or ""),
                        str(m.get("name") or ""),
                        bool(m.get("muted", False)),
                        bool(m.get("deafened", False)),
                        str(((m.get("screen_share") or {}).get("stream_url")) or ((self._remote_screen_shares.get(str(m.get("id") or "")) or {}).get("stream_url")) or ""),
                        int(((m.get("screen_share") or {}).get("rev")) or ((self._remote_screen_shares.get(str(m.get("id") or "")) or {}).get("rev")) or 0),
                    )
                    for m in (ch.get("members") or ())
                ),
            )
            for ch in (guild.get("voice_channels") or ())
        )
        tcs = tuple(
            (str(tc.get("id") or ""), str(tc.get("name") or ""))
            for tc in (guild.get("text_channels") or ())
        )
        return (
            gid,
            vcs,
            tcs,
            bool(self._voice_state.get("in_voice")),
            str(self._voice_state.get("channel_id") or ""),
            self._joining_channel_id,
            self._active_chat_kind,
            self._active_chat_id,
            self.cfg.discord_show_locked_channels,
            self._locked_vc_expanded,
            tuple(self.cfg.discord_notify_channels),
            tuple(self.cfg.discord_hidden_channels),
            tuple(sorted(self._unread_channels.items())),
        )

    def _render_channels(self, force: bool = False) -> None:
        gid = str(self.guild_combo.currentData() or "")
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
        sig = self._channels_signature(gid, guild)
        if not force and sig == self._last_channels_sig:
            return
        self._last_channels_sig = sig

        _clear_layout(self.channels_lay)
        self._tc_buttons.clear()
        self._tc_badges.clear()
        _clear_layout(self.text_channels_lay)
        self._update_tc_section_label()

        if guild is None:
            msg = (
                "No se encontraron servidores."
                if self._guilds
                else "Pulsa 'Conectar Bot' para ver tus canales."
            )
            lbl = QLabel(msg)
            lbl.setObjectName("MutedLabel")
            lbl.setWordWrap(True)
            self.channels_lay.addWidget(lbl)

            tlbl = QLabel("Conecta el bot para ver canales de texto.")
            tlbl.setObjectName("MutedLabel")
            tlbl.setWordWrap(True)
            self.text_channels_lay.addWidget(tlbl)
            return

        hidden = set(self.cfg.discord_hidden_channels)
        active_cid = str(self._voice_state.get("channel_id") or "")
        gname = str(guild.get("name") or "").strip() or "Servidor"
        voice_channels = [
            ch
            for ch in (guild.get("voice_channels") or [])
            if str(ch.get("id") or "") not in hidden
        ]
        if not voice_channels:
            lbl = QLabel("No hay canales de voz visibles en este servidor.")
            lbl.setObjectName("MutedLabel")
            lbl.setWordWrap(True)
            self.channels_lay.addWidget(lbl)
        else:
            allowed = []
            locked = []
            for ch in voice_channels:
                cid = str(ch.get("id") or "")
                is_cur = bool(self._voice_state.get("in_voice")) and cid == active_cid
                if bool(ch.get("can_connect", True)) or is_cur:
                    allowed.append(ch)
                else:
                    locked.append(ch)

            for ch in allowed:
                self.channels_lay.addWidget(self._build_voice_channel_row(gid, ch, active_cid))

            if locked:
                if not self.cfg.discord_show_locked_channels:
                    if not allowed:
                        hint = QLabel(
                            "No tienes canales de voz accesibles aquí.\n"
                            "Activa 'Mostrar canales sin permiso' en Ajustes → Discord para verlos."
                        )
                        hint.setObjectName("MutedLabel")
                        hint.setWordWrap(True)
                        self.channels_lay.addWidget(hint)
                else:
                    if not allowed:
                        for ch in locked:
                            self.channels_lay.addWidget(
                                self._build_voice_channel_row(gid, ch, active_cid)
                            )
                    else:
                        arrow = "▾" if self._locked_vc_expanded else "▸"
                        lbtn = _make_section_toggle_btn(f"{arrow} {len(locked)} sin permiso")
                        lbtn.setObjectName("MutedLabel")
                        lbtn.clicked.connect(self._toggle_locked_vc_group)
                        self.channels_lay.addWidget(lbtn)
                        if self._locked_vc_expanded:
                            for ch in locked:
                                self.channels_lay.addWidget(
                                    self._build_voice_channel_row(gid, ch, active_cid)
                                )

        text_channels = [
            tc
            for tc in (guild.get("text_channels") or [])
            if str(tc.get("id") or "") not in hidden
        ]
        if not text_channels:
            tlbl = QLabel("Sin canales de texto visibles.")
            tlbl.setObjectName("MutedLabel")
            tlbl.setWordWrap(True)
            self.text_channels_lay.addWidget(tlbl)
        else:
            for tc in text_channels:
                cid = str(tc.get("id") or "")
                cname = str(tc.get("name") or "texto")
                is_active_tc = (
                    self._active_chat_kind == "channel" and self._active_chat_id == cid
                )
                unread_n = self._unread_channels.get(cid, 0)
                has_unread = unread_n > 0 and not self.is_viewing_channel(cid)

                row_w = QWidget()
                row_w.setObjectName("TransparentBox")
                tc_row = QHBoxLayout(row_w)
                tc_row.setContentsMargins(0, 0, 0, 0)
                tc_row.setSpacing(4)

                tbtn = QPushButton(f"  # {cname}")
                tbtn.setObjectName("NavBtn")
                tbtn.setProperty("unread", "true" if has_unread else "false")
                tbtn.setProperty("active", "true" if is_active_tc else "false")
                tbtn.setCursor(Qt.CursorShape.PointingHandCursor)
                tbtn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                tc_label = f"# {cname} ({gname})"
                tbtn.customContextMenuRequested.connect(
                    lambda pos, c=cid, lbl=tc_label, w=tbtn: self._show_channel_context_menu(
                        w.mapToGlobal(pos), c, lbl
                    )
                )
                tbtn.clicked.connect(
                    lambda _c=False, g=gid, c=cid, n=cname: self._open_text_channel_clicked(g, c, n)
                )
                tc_row.addWidget(tbtn, 1)
                self._tc_buttons[cid] = tbtn

                cnt_badge = QLabel("9+" if unread_n > 9 else str(max(1, unread_n)))
                cnt_badge.setObjectName("UnreadBadge")
                cnt_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cnt_badge.setVisible(has_unread)
                tc_row.addWidget(cnt_badge)
                self._tc_badges[cid] = cnt_badge

                notified = cid in self.cfg.discord_notify_channels
                bell_btn = QPushButton()
                bell_btn.setObjectName("IconBtn")
                bell_btn.setFixedSize(26, 26)
                bell_btn.setIcon(
                    render_icon(
                        "bell" if notified else "bell_off",
                        self.palette.accent if notified else self.palette.text2,
                        13,
                    )
                )
                bell_btn.setToolTip(
                    "Avisos activos para este canal (clic para silenciar)"
                    if notified
                    else "Activar avisos en barra de tareas para este canal"
                )
                bell_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                bell_btn.clicked.connect(
                    lambda _c=False, c=cid, n=cname: self._toggle_channel_notify(c, n)
                )
                tc_row.addWidget(bell_btn)
                self.text_channels_lay.addWidget(row_w)

    def _sync_chat_composer_state(
        self, kind: str, label: str, can_send: bool, can_attach: bool
    ) -> None:
        self._active_chat_can_send = bool(can_send)
        self._active_chat_can_attach = bool(can_send and can_attach)
        if hasattr(self, "chat_browser"):
            self.chat_browser.set_can_send(self._active_chat_can_send)
        self.chat_input.setEnabled(self._active_chat_can_send)
        self.chat_send_btn.setEnabled(
            self._active_chat_can_send or bool(self._pending_attachments and can_send and can_attach)
        )
        self.chat_attach_btn.setEnabled(
            self._active_chat_can_attach and not self._editing_message_id
        )
        self.chat_input.setPlaceholderText(
            self._format_chat_placeholder(kind, label, self._active_chat_can_send)
        )

    def _open_text_channel_clicked(self, guild_id: str, channel_id: str, name: str) -> None:
        if (self._active_chat_kind, self._active_chat_id) != ("channel", channel_id):
            self._cancel_chat_compose_mode()
        self._active_chat_kind = "channel"
        self._active_chat_id = channel_id
        self._unread_channels.pop(channel_id, None)
        self._chat_pending_img_urls.clear()
        self.chat_title_lbl.setText(f"# {name}")
        self._sync_dm_header_status()
        cached = self._chat_history_cache.get(("channel", channel_id))
        if cached is not None:
            can_send = bool(cached.get("can_send", False))
            can_attach = bool(cached.get("can_attach", can_send))
            can_read = bool(cached.get("can_read", True))
            self._sync_chat_composer_state("channel", name, can_send, can_attach)
            self._chat_messages.clear()
            for m in cached.get("messages") or ():
                self._chat_messages.append(m)
            self._render_chat_messages(can_read=can_read)
        else:
            can_send = self.svc.is_connected
            self._sync_chat_composer_state("channel", name, can_send, can_send)
        self._ensure_chat_pane_visible()
        if not self._sync_chat_selection_in_place():
            self._render_channels(force=True)
            self._render_dm_threads()
        self._emit_unread_count()
        self.svc.open_text_channel(guild_id, channel_id)

    def _on_channel_message_incoming(self, data: dict[str, Any]) -> None:
        if not self.cfg.discord_notify_chat_enabled:
            return
        cid = str(data.get("channel_id") or "")
        if not cid or cid not in self.cfg.discord_notify_channels:
            return
        if self.is_viewing_channel(cid):
            return
        self._unread_channels[cid] = self._unread_channels.get(cid, 0) + 1
        if not self._sync_chat_selection_in_place():
            self._render_channels(force=True)
        self._emit_unread_count()
        self.channel_alert_requested.emit(data)

    def _sync_dm_header_status(self) -> None:
        if not hasattr(self, "chat_status_pill"):
            return
        if self._active_chat_kind != "dm" or not self._active_chat_id:
            self.chat_status_pill.setVisible(False)
            return
        st = _normalize_presence(self._user_statuses.get(self._active_chat_id, "offline"))
        col = _presence_color(st)
        lbl = _presence_label(st)
        self.chat_status_pill.setText(f"● {lbl}")
        self.chat_status_pill.setStyleSheet(
            f"background-color: {self.palette.surface1}; color: {col}; "
            f"border: 1px solid {self.palette.border}; border-radius: 6px; "
            "padding: 2px 7px; font-size: 11px; font-weight: 600;"
        )
        self.chat_status_pill.setVisible(True)

    def _on_presence_updated(self, data: dict[str, Any]) -> None:
        uid = str(data.get("user_id") or "").strip()
        if not uid:
            return
        st = _normalize_presence(data.get("status"))
        prev = self._user_statuses.get(uid)
        self._user_statuses[uid] = st
        if prev != st:
            self._render_dm_threads()
            if self._active_chat_kind == "dm" and self._active_chat_id == uid:
                self._sync_dm_header_status()
            card = self._participant_cards.get(uid)
            if card is not None:
                card.set_status(st)

    def _on_guild_members_updated(self, data: dict[str, Any]) -> None:
        cur_gid = str(self.guild_combo.currentData() or "")
        members_list = list(data.get("members") or [])
        for m in members_list:
            uid = str(m.get("id") or "")
            if uid and m.get("status") is not None:
                self._user_statuses[uid] = _normalize_presence(m.get("status"))
        self._heal_dm_thread_names(members_list)
        self._render_dm_threads()
        self._sync_dm_header_status()
        for uid, card in self._participant_cards.items():
            if uid != "local" and uid in self._user_statuses:
                card.set_status(self._user_statuses[uid])
        if cur_gid and str(data.get("guild_id") or "") != cur_gid:
            return
        self._members = list(data.get("members") or [])
        self._online_count = int(data.get("online_count") or 0)
        self._refresh_members_list()

    def _on_member_avatar_ready(self, url: str, pix: QPixmap) -> None:
        if pix.size() != self._LIST_AVATAR_SIZE:
            return
        rows = self._member_row_by_url.get(url)
        if not rows:
            return
        ico = QIcon(pix)
        for item in rows:
            try:
                item.setIcon(ico)
            except RuntimeError:
                pass

    def _refresh_members_list(self) -> None:
        in_voice = bool(self._voice_state.get("in_voice", False))
        voice_parts = list(self._voice_state.get("participants") or [])

        source_members: list[dict[str, Any]]
        header_label: str

        if self._members_scope == "channel":
            if self._active_chat_kind == "channel" and self._channel_members_loaded:
                source_members = self._channel_members
                ch_title = self.chat_title_lbl.text().strip() or "#canal"
                header_label = f"En {ch_title} · {len(source_members)} ({self._channel_online_count} en línea)"
            elif in_voice and voice_parts:
                source_members = [
                    {
                        "id": str(pt.get("id") or ""),
                        "name": str(pt.get("name") or ""),
                        "is_bot": bool(pt.get("is_bot", False)),
                        "avatar_url": str(pt.get("avatar_url") or ""),
                        "status": "online",
                        "activity": "En canal de voz",
                    }
                    for pt in voice_parts
                ]
                ch_name = str(self._voice_state.get("channel_name") or "Voz")
                header_label = f"En 🔊 {ch_name} · {len(source_members)}"
            else:
                source_members = self._members
                header_label = f"{self._online_count} en línea"
        else:
            source_members = self._members
            header_label = f"Servidor · {self._online_count} en línea"

        self.members_online_lbl.setText(header_label)
        badge_count = (
            len(source_members)
            if (self._members_scope == "channel" and (self._channel_members_loaded or in_voice))
            else self._online_count
        )
        self.toggle_members_btn.setText(
            f"Miembros · {badge_count}" if badge_count > 0 else "Miembros"
        )

        self._member_row_by_url.clear()
        self.members_list.clear()
        status_dots = {
            "online": "🟢",
            "dnd": "🔴",
            "idle": "🟡",
            "offline": "⚪",
        }
        status_names = {
            "online": "En línea",
            "dnd": "No molestar",
            "idle": "Ausente",
            "offline": "Desconectado",
        }
        for m in source_members:
            uid = str(m.get("id") or "")
            name = str(m.get("name") or uid)
            is_bot = bool(m.get("is_bot", False))
            av_url = str(m.get("avatar_url") or "")
            st = _normalize_presence(m.get("status") or self._user_statuses.get(uid) or "offline")
            full_act = str(m.get("activity") or "").strip()
            short_act = full_act[:33] + "..." if len(full_act) > 36 else full_act
            dot = status_dots.get(st, "⚪")
            st_lbl = status_names.get(st, _presence_label(st))
            tag = " [APP]" if is_bot else ""
            text = f"{dot} {name}{tag}"
            if short_act:
                text += f"\n    {st_lbl} · {short_act}"
            elif st != "offline":
                text += f"\n    {st_lbl}"
            item = QListWidgetItem(text)
            item.setData(
                Qt.ItemDataRole.UserRole,
                {"id": uid, "name": name, "is_bot": is_bot, "avatar_url": av_url},
            )
            item.setToolTip(f"{name} ({st_lbl})" + (f" · {full_act}" if full_act else ""))
            if av_url and self._thumb_cache is not None:
                self._member_row_by_url.setdefault(av_url, []).append(item)
                cached = self._thumb_cache.get_or_fetch(
                    av_url, self._LIST_AVATAR_SIZE, circular=True
                )
                if cached is not None:
                    item.setIcon(QIcon(cached))
            self.members_list.addItem(item)
        self._on_member_selection_changed()

    def _on_member_selection_changed(self) -> None:
        items = self.members_list.selectedItems()
        if not items:
            self.dm_btn.setEnabled(False)
            self.dm_btn.setVisible(False)
            self.dm_hint_lbl.setVisible(False)
            return
        meta = items[0].data(Qt.ItemDataRole.UserRole) or {}
        has_id = bool(meta.get("id"))
        is_bot = bool(meta.get("is_bot", False))
        can_dm = has_id and not is_bot
        self.dm_btn.setVisible(can_dm)
        self.dm_btn.setEnabled(can_dm)
        self.dm_hint_lbl.setVisible(has_id and is_bot)

    def _on_members_context_menu(self, pos: Any) -> None:
        item = self.members_list.itemAt(pos)
        if item is None:
            return
        meta = item.data(Qt.ItemDataRole.UserRole) or {}
        uid = str(meta.get("id") or "")
        name = str(meta.get("name") or uid)
        is_bot = bool(meta.get("is_bot", False))
        av = str(meta.get("avatar_url") or "")
        vp = self.members_list.viewport()
        gpos = vp.mapToGlobal(pos) if vp is not None else self.members_list.mapToGlobal(pos)
        if exec_user_dm_menu(self.members_list, uid, name, is_bot, gpos):
            self._open_dm_with_user(uid, name, av)

    def _on_open_selected_dm(self) -> None:
        items = self.members_list.selectedItems()
        if not items:
            return
        meta = items[0].data(Qt.ItemDataRole.UserRole) or {}
        uid = str(meta.get("id") or "")
        name = str(meta.get("name") or uid)
        av = str(meta.get("avatar_url") or "")
        if not uid or bool(meta.get("is_bot", False)):
            return
        self._open_dm_with_user(uid, name, av)

    def _toggle_dm_channels_section(self) -> None:
        self._dm_section_open = not self._dm_section_open
        self.dm_channels_scroll.setVisible(self._dm_section_open)
        self._render_dm_threads()

    def _load_dm_threads(self) -> list[dict[str, str]]:
        if self._db is not None:
            return self._db.list_discord_dm_threads(limit=25)
        return list(self._dm_threads_mem)

    def _resolve_known_dm_peer(self, uid: str) -> tuple[str, str]:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return "", ""
        best_av = ""
        for bucket in (self._members, self._channel_members):
            for m in bucket or ():
                if str(m.get("id") or "").strip() != clean_id:
                    continue
                cand = str(m.get("name") or "").strip()
                av = str(m.get("avatar_url") or "").strip()
                if av and not best_av:
                    best_av = av
                if cand and cand != clean_id:
                    return cand, best_av or av
        for g in self._guilds or ():
            for vc in g.get("voice_channels") or ():
                for vm in vc.get("members") or ():
                    if str(vm.get("id") or "").strip() != clean_id:
                        continue
                    cand = str(vm.get("name") or "").strip()
                    av = str(vm.get("avatar_url") or "").strip()
                    if av and not best_av:
                        best_av = av
                    if cand and cand != clean_id:
                        return cand, best_av or av
        for th in self._load_dm_threads():
            if str(th.get("user_id") or "").strip() != clean_id:
                continue
            cand = str(th.get("user_name") or "").strip()
            av = str(th.get("avatar_url") or "").strip()
            if av and not best_av:
                best_av = av
            if cand and cand != clean_id:
                return cand, best_av or av
        return "", best_av

    def _heal_dm_thread_names(self, people: list[dict[str, Any]]) -> bool:
        if not people:
            return False
        lookup: dict[str, tuple[str, str]] = {}
        for p in people:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            pname = str(p.get("name") or "").strip()
            pav = str(p.get("avatar_url") or "").strip()
            if pid and pname and pname != pid:
                lookup[pid] = (pname, pav)
        if not lookup:
            return False
        healed = False
        for th in self._load_dm_threads():
            uid = str(th.get("user_id") or "").strip()
            cur_name = str(th.get("user_name") or "").strip()
            if not uid or (cur_name and cur_name != uid):
                continue
            resolved = lookup.get(uid)
            if resolved is None:
                continue
            real_name, av_url = resolved
            self._upsert_dm_thread_local(uid, real_name, av_url, touch_timestamp=False)
            healed = True
            if (
                self._active_chat_kind == "dm"
                and self._active_chat_id == uid
                and self._active_dm_peer_name in ("", uid)
            ):
                self._active_dm_peer_name = real_name
                if av_url and not self._active_dm_peer_avatar:
                    self._active_dm_peer_avatar = av_url
                self.chat_title_lbl.setText(f"DM · {real_name}")
        return healed

    def _upsert_dm_thread_local(
        self,
        uid: str,
        name: str,
        avatar_url: str = "",
        preview: str = "",
        *,
        touch_timestamp: bool = True,
    ) -> None:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return
        raw_name = str(name or "").strip()
        clean_av = str(avatar_url or "").strip()
        clean_prev = str(preview or "").strip()[:120]
        if self._db is not None:
            self._db.upsert_discord_dm_thread(
                clean_id,
                raw_name,
                avatar_url=clean_av,
                last_preview=clean_prev,
                touch_timestamp=touch_timestamp,
            )
            return
        idx = next(
            (i for i, t in enumerate(self._dm_threads_mem) if t["user_id"] == clean_id),
            None,
        )
        prev_entry = self._dm_threads_mem[idx] if idx is not None else None
        prev_name = str(prev_entry["user_name"] or "").strip() if prev_entry else ""
        if raw_name and raw_name != clean_id:
            chosen_name = raw_name
        elif prev_name and prev_name != clean_id:
            chosen_name = prev_name
        else:
            chosen_name = raw_name or prev_name or clean_id
        entry = {
            "user_id": clean_id,
            "user_name": chosen_name,
            "avatar_url": clean_av or (prev_entry["avatar_url"] if prev_entry else ""),
            "last_preview": clean_prev or (prev_entry["last_preview"] if prev_entry else ""),
        }
        if idx is not None and not touch_timestamp:
            self._dm_threads_mem[idx] = entry
            return
        if idx is not None:
            del self._dm_threads_mem[idx]
        self._dm_threads_mem.insert(0, entry)

    def _save_dm_message_local(
        self,
        uid: str,
        msg: dict[str, Any],
        peer_name: str = "",
        peer_avatar_url: str = "",
        *,
        touch_timestamp: bool = True,
    ) -> None:
        clean_id = str(uid or "").strip()
        if not clean_id or not isinstance(msg, dict):
            return
        if self._db is not None:
            self._db.save_discord_dm_message(
                clean_id,
                msg,
                peer_name=peer_name,
                peer_avatar_url=peer_avatar_url,
                touch_timestamp=touch_timestamp,
            )
            return
        bucket = self._dm_msgs_mem.setdefault(clean_id, [])
        mid = str(msg.get("id") or "")
        if not mid or not any(str(x.get("id") or "") == mid for x in bucket):
            bucket.append(dict(msg))
            if len(bucket) > 80:
                del bucket[:-80]
        prev = str(msg.get("content") or "").strip()
        self._upsert_dm_thread_local(
            clean_id, peer_name, peer_avatar_url, prev, touch_timestamp=touch_timestamp
        )

    def _load_dm_messages_local(self, uid: str) -> list[dict[str, Any]]:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return []
        if self._db is not None:
            return self._db.list_discord_dm_messages(clean_id, limit=80)
        return list(self._dm_msgs_mem.get(clean_id, []))

    def _remove_dm_thread_local(self, uid: str) -> None:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return
        if self._db is not None:
            self._db.remove_discord_dm_thread(clean_id)
        else:
            self._dm_threads_mem = [t for t in self._dm_threads_mem if t["user_id"] != clean_id]
            self._dm_msgs_mem.pop(clean_id, None)
        self._unread_dms.pop(clean_id, None)
        if self._active_chat_kind == "dm" and self._active_chat_id == clean_id:
            self._active_chat_kind = ""
            self._active_chat_id = ""
            self._active_dm_peer_name = ""
            self._active_dm_peer_avatar = ""
            self.chat_title_lbl.setText("Sin chat seleccionado")
        self._render_dm_threads()
        self._emit_unread_count()

    def _show_dm_thread_context_menu(self, global_pos: Any, uid: str, name: str) -> None:
        menu = QMenu(self)
        try:
            act_rm = menu.addAction(f"Quitar DM con {name}")
            chosen = menu.exec(global_pos)
            if chosen is act_rm:
                self._remove_dm_thread_local(uid)
        finally:
            menu.deleteLater()

    def _render_dm_threads(self) -> None:
        if not hasattr(self, "dm_channels_lay"):
            return
        threads = self._load_dm_threads()
        self._update_dm_section_label(len(threads))
        self._dm_buttons.clear()
        self._dm_badges.clear()
        _clear_layout(self.dm_channels_lay)
        if not threads:
            empty_lbl = QLabel("Clic derecho en una persona → DM.")
            empty_lbl.setObjectName("MutedLabel")
            empty_lbl.setWordWrap(True)
            self.dm_channels_lay.addWidget(empty_lbl)
            return

        for th in threads:
            uid = str(th.get("user_id") or "")
            uname = str(th.get("user_name") or uid)
            av = str(th.get("avatar_url") or "")
            prev = str(th.get("last_preview") or "").strip()
            is_act = self._active_chat_kind == "dm" and self._active_chat_id == uid
            unread_n = self._unread_dms.get(uid, 0)
            has_unread = unread_n > 0 and not self.is_viewing_dm(uid)

            row_w = QWidget()
            row_w.setObjectName("TransparentBox")
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(4)

            st_dm = _normalize_presence(self._user_statuses.get(uid, "offline"))
            btn = QPushButton(f"  @ {uname}")
            btn.setObjectName("NavBtn")
            btn.setIcon(QIcon(_build_status_dot_pixmap(st_dm, 9)))
            btn.setIconSize(QSize(10, 10))
            btn.setProperty("unread", "true" if has_unread else "false")
            btn.setProperty("active", "true" if is_act else "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            tip = f"DM con {uname} · {_presence_label(st_dm)}" + (f"\nÚltimo: {prev}" if prev else "")
            btn.setToolTip(tip)
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, w=btn, u=uid, n=uname: self._show_dm_thread_context_menu(
                    w.mapToGlobal(pos), u, n
                )
            )
            btn.clicked.connect(
                lambda _c=False, u=uid, n=uname, a=av: self._open_dm_with_user(u, n, a)
            )
            row_lay.addWidget(btn, 1)
            self._dm_buttons[uid] = btn

            cnt_badge = QLabel("9+" if unread_n > 9 else str(max(1, unread_n)))
            cnt_badge.setObjectName("UnreadBadge")
            cnt_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cnt_badge.setVisible(has_unread)
            row_lay.addWidget(cnt_badge)
            self._dm_badges[uid] = cnt_badge

            self.dm_channels_lay.addWidget(row_w)

    def _open_dm_with_user(self, uid: str, name: str, avatar_url: str = "") -> None:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return
        if (self._active_chat_kind, self._active_chat_id) != ("dm", clean_id):
            self._cancel_chat_compose_mode()
        peer_name = str(name or clean_id).strip() or clean_id
        self._active_chat_kind = "dm"
        self._active_chat_id = clean_id
        self._unread_dms.pop(clean_id, None)
        self._active_dm_peer_name = peer_name
        self._active_dm_peer_avatar = str(avatar_url or "").strip()
        self.chat_title_lbl.setText(f"DM · {peer_name}")
        self._sync_dm_header_status()

        self._upsert_dm_thread_local(
            clean_id, peer_name, self._active_dm_peer_avatar, touch_timestamp=False
        )
        saved_msgs = self._load_dm_messages_local(clean_id)
        if not saved_msgs:
            cached = self._chat_history_cache.get(("dm", clean_id))
            if cached is not None:
                saved_msgs = list(cached.get("messages") or [])
        self._chat_messages.clear()
        for sm in saved_msgs[-80:]:
            self._chat_messages.append(sm)

        can_send = self.svc.is_connected
        self._chat_pending_img_urls.clear()
        self._sync_chat_composer_state("dm", peer_name, can_send, can_send)
        self._ensure_chat_pane_visible()
        if not self._sync_chat_selection_in_place():
            self._render_channels(force=True)
            self._render_dm_threads()
        self._emit_unread_count()
        self._render_chat_messages(can_read=True)
        if can_send:
            self.svc.open_dm(clean_id)

    def _on_chat_history_loaded(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        if not kind or not tid:
            return
        raw_title = str(data.get("title") or "Chat")
        clean_title = (
            f"# {raw_title[1:].strip()}"
            if raw_title.startswith("#") and not raw_title.startswith("# ")
            else raw_title
        )
        live_msgs = list(data.get("messages") or [])[-80:]
        if kind == "dm":
            raw_pn = str(
                data.get("peer_name") or clean_title.replace("DM · ", "").strip() or ""
            ).strip()
            known_name, known_av = self._resolve_known_dm_peer(tid)
            peer_name = raw_pn if (raw_pn and raw_pn != tid) else known_name
            peer_av = str(data.get("peer_avatar_url") or known_av or "")
            self._upsert_dm_thread_local(
                tid, peer_name, peer_av, touch_timestamp=False
            )
            for m in live_msgs:
                if isinstance(m, dict):
                    self._save_dm_message_local(
                        tid,
                        m,
                        peer_name=peer_name,
                        peer_avatar_url=peer_av,
                        touch_timestamp=False,
                    )
            if not live_msgs:
                live_msgs = self._load_dm_messages_local(tid)

        can_send = bool(data.get("can_send", False))
        can_attach = bool(data.get("can_attach", can_send))
        can_read = bool(data.get("can_read", True))
        if (kind, tid) in self._chat_history_cache:
            del self._chat_history_cache[(kind, tid)]
        elif len(self._chat_history_cache) >= 16:
            self._chat_history_cache.pop(next(iter(self._chat_history_cache)))
        self._chat_history_cache[(kind, tid)] = {
            "messages": list(live_msgs),
            "can_send": can_send,
            "can_attach": can_attach,
            "can_read": can_read,
            "title": clean_title,
        }

        # Descartar respuesta rezagada si el usuario ya seleccionó otro chat o DM.
        if self._active_chat_kind and (kind, tid) != (self._active_chat_kind, self._active_chat_id):
            return

        self._active_chat_kind = kind
        self._active_chat_id = tid
        self._chat_pending_img_urls.clear()
        if kind == "channel":
            self._unread_channels.pop(tid, None)
        elif kind == "dm":
            self._unread_dms.pop(tid, None)
            raw_pn = str(
                data.get("peer_name") or clean_title.replace("DM · ", "").strip() or ""
            ).strip()
            known_name, known_av = self._resolve_known_dm_peer(tid)
            if raw_pn and raw_pn != tid:
                resolved_pn = raw_pn
            elif known_name:
                resolved_pn = known_name
            elif self._active_dm_peer_name and self._active_dm_peer_name != tid:
                resolved_pn = self._active_dm_peer_name
            else:
                resolved_pn = tid
            self._active_dm_peer_name = resolved_pn
            self._active_dm_peer_avatar = str(
                data.get("peer_avatar_url") or known_av or self._active_dm_peer_avatar or ""
            )
        self._emit_unread_count()
        self.chat_title_lbl.setText(clean_title)

        self._chat_messages.clear()
        for m in live_msgs[-80:]:
            self._chat_messages.append(m)

        if kind == "channel" and "channel_members" in data:
            self._channel_members = list(data.get("channel_members") or [])
            self._channel_online_count = int(data.get("channel_online_count") or 0)
            self._channel_members_loaded = True
            self._refresh_members_list()

        self._sync_dm_header_status()
        self._sync_chat_composer_state(
            kind,
            self._active_dm_peer_name if kind == "dm" else clean_title,
            can_send,
            can_attach,
        )
        stale_dm_btn = False
        if kind == "dm" and tid in self._dm_buttons:
            want_txt = f"  @ {self._active_dm_peer_name}"
            if self._dm_buttons[tid].text() != want_txt:
                stale_dm_btn = True
        if stale_dm_btn or not self._sync_chat_selection_in_place():
            self._render_channels(force=True)
            self._render_dm_threads()
        self._render_chat_messages(can_read=can_read)

    def _append_live_chat_message(self, msg: dict[str, Any]) -> None:
        if not self.chat_browser.message_rows():
            self._render_chat_messages(can_read=True)
            return
        self._chat_last_can_read = True
        for a in msg.get("attachments") or ():
            if isinstance(a, dict):
                self._chat_attachment_refs.append(dict(a))
        pending = self.chat_browser.append_message(
            msg,
            thumb_cache=self._thumb_cache,
            fallback_peer_avatar=self._active_dm_peer_avatar if self._active_chat_kind == "dm" else "",
            fallback_bot_avatar=self.svc.bot_avatar_url,
        )
        if pending:
            self._chat_pending_img_urls.update(pending)

    def _on_chat_message_received(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        msg = data.get("message")
        if not isinstance(msg, dict):
            return
        mid = str(msg.get("id") or "")
        cached = self._chat_history_cache.get((kind, tid))
        if cached is not None:
            c_msgs = list(cached.get("messages") or [])
            if not mid or not any(str(x.get("id") or "") == mid for x in c_msgs):
                c_msgs.append(dict(msg))
                if len(c_msgs) > 80:
                    c_msgs = c_msgs[-80:]
                cached["messages"] = c_msgs
        if kind == "dm" and tid:
            raw_pn = str(data.get("peer_name") or "").strip()
            known_name, known_av = self._resolve_known_dm_peer(tid)
            if raw_pn and raw_pn != tid:
                peer_name = raw_pn
            elif known_name:
                peer_name = known_name
            elif (
                self._active_chat_kind == "dm"
                and self._active_chat_id == tid
                and self._active_dm_peer_name
                and self._active_dm_peer_name != tid
            ):
                peer_name = self._active_dm_peer_name
            else:
                peer_name = ""
            peer_av = str(
                data.get("peer_avatar_url")
                or known_av
                or (self._active_dm_peer_avatar if self._active_chat_kind == "dm" and self._active_chat_id == tid else "")
            )
            self._save_dm_message_local(tid, msg, peer_name=peer_name, peer_avatar_url=peer_av)
            self._render_dm_threads()
        if kind != self._active_chat_kind or tid != self._active_chat_id:
            return
        if mid and any(str(x.get("id") or "") == mid for x in self._chat_messages):
            return
        self._chat_messages.append(msg)
        self._append_live_chat_message(msg)

    def _on_chat_message_edited(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        mid = str(data.get("message_id") or "")
        if not kind or not tid or not mid:
            return
        new_content = str(data.get("content") or "")
        new_msg = data.get("message")

        def _patch_entry(entry: dict[str, Any]) -> dict[str, Any]:
            if isinstance(new_msg, dict):
                merged = dict(entry)
                merged.update(new_msg)
                merged["edited"] = True
                return merged
            updated = dict(entry)
            updated["content"] = new_content
            updated["edited"] = True
            return updated

        cached = self._chat_history_cache.get((kind, tid))
        if cached is not None:
            c_msgs = list(cached.get("messages") or [])
            for idx, m in enumerate(c_msgs):
                if str(m.get("id") or "") == mid:
                    c_msgs[idx] = _patch_entry(m)
            cached["messages"] = c_msgs

        dm_patched: dict[str, Any] | None = None
        if kind == "dm":
            known_name, known_av = self._resolve_known_dm_peer(tid)
            dm_msgs = self._load_dm_messages_local(tid)
            for m in dm_msgs:
                if str(m.get("id") or "") == mid:
                    dm_patched = _patch_entry(m)
                    self._save_dm_message_local(
                        tid,
                        dm_patched,
                        peer_name=known_name,
                        peer_avatar_url=known_av,
                        touch_timestamp=False,
                    )
                    break

        if kind == self._active_chat_kind and tid == self._active_chat_id:
            changed = False
            cur_list = list(self._chat_messages)
            for idx, m in enumerate(cur_list):
                if str(m.get("id") or "") == mid:
                    cur_list[idx] = _patch_entry(m)
                    changed = True
            if changed:
                self._chat_messages.clear()
                for m in cur_list:
                    self._chat_messages.append(m)
                self._render_chat_messages(
                    can_read=self._chat_last_can_read, scroll_to_bottom=False
                )
            elif dm_patched is not None:
                self._render_chat_messages(
                    can_read=self._chat_last_can_read, scroll_to_bottom=False
                )

    def _on_dm_incoming(self, data: dict[str, Any]) -> None:
        uid = str(data.get("user_id") or "")
        author = str(data.get("author_name") or "Usuario")
        peer_name = str(data.get("peer_name") or author)
        peer_av = str(data.get("peer_avatar_url") or "")
        preview = str(data.get("preview") or "").strip()
        raw_msg = data.get("message")
        if uid and isinstance(raw_msg, dict):
            self._save_dm_message_local(uid, raw_msg, peer_name=peer_name, peer_avatar_url=peer_av)
            cached = self._chat_history_cache.get(("dm", uid))
            if cached is not None:
                c_msgs = list(cached.get("messages") or [])
                mid = str(raw_msg.get("id") or "")
                if not mid or not any(str(x.get("id") or "") == mid for x in c_msgs):
                    c_msgs.append(dict(raw_msg))
                    cached["messages"] = c_msgs[-80:]
            if self._active_chat_kind == "dm" and self._active_chat_id == uid:
                mid = str(raw_msg.get("id") or "")
                if not mid or not any(str(x.get("id") or "") == mid for x in self._chat_messages):
                    self._chat_messages.append(raw_msg)
                    self._append_live_chat_message(raw_msg)
        elif uid:
            self._upsert_dm_thread_local(uid, peer_name, peer_av, preview)

        viewing_now = bool(uid and self.is_viewing_dm(uid))
        if uid and not viewing_now:
            self._unread_dms[uid] = self._unread_dms.get(uid, 0) + 1
            self._emit_unread_count()
        self._render_dm_threads()
        if not viewing_now:
            msg = f"DM de {author}: {preview}" if preview else f"Nuevo mensaje directo de {author}"
            self.toast_requested.emit(msg)
            self.sound_event_requested.emit("message")
            self.dm_alert_requested.emit(data)

    @staticmethod
    def _extract_local_files_from_mime(mime: Any) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        out: list[str] = []
        for u in mime.urls():
            if u.isLocalFile():
                lp = u.toLocalFile()
                if lp and os.path.isfile(lp):
                    out.append(lp)
        return out

    def _handle_dropped_file_paths(self, paths: list[str]) -> int:
        valid = [str(p) for p in paths if str(p or "").strip() and os.path.isfile(str(p))]
        if not valid:
            return 0
        if not (self._active_chat_can_send and self._active_chat_can_attach):
            self.toast_requested.emit("El bot no puede adjuntar archivos en este chat.")
            return 0
        if self._editing_message_id:
            self.toast_requested.emit("Termina o cancela la edición antes de adjuntar archivos.")
            return 0
        before = len(self._pending_attachments)
        self._add_pending_attachments(valid, is_temp=False)
        self.chat_input.setFocus()
        return max(0, len(self._pending_attachments) - before)

    def _handle_dropped_mime(self, mime: Any) -> bool:
        files = self._extract_local_files_from_mime(mime)
        if files:
            self._handle_dropped_file_paths(files)
            return True
        if mime is not None and mime.hasImage():
            if not (self._active_chat_can_send and self._active_chat_can_attach):
                self.toast_requested.emit("El bot no puede adjuntar imágenes en este chat.")
                return True
            if self._editing_message_id:
                self.toast_requested.emit("Termina o cancela la edición antes de adjuntar imágenes.")
                return True
            img_obj = mime.imageData()
            img = img_obj if isinstance(img_obj, QImage) else QImage()
            if img.isNull() and isinstance(img_obj, QPixmap):
                img = img_obj.toImage()
            if not img.isNull():
                tmp_dir = USER_DATA_DIR / "tmp_uploads"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                out_path = tmp_dir / f"drop_{int(time.time() * 1000)}.png"
                if img.save(str(out_path), "PNG"):
                    self._add_pending_attachments([str(out_path)], is_temp=True)
                    self.chat_input.setFocus()
                    return True
        return False

    def _is_chat_drop_target(self, obj: object) -> bool:
        try:
            pane = getattr(self, "chat_pane", None)
            browser = getattr(self, "chat_browser", None)
            inp = getattr(self, "chat_input", None)
            vp = browser.viewport() if browser is not None else None
            cont = getattr(browser, "container", None) if browser is not None else None
            return obj is not None and (
                obj is pane or obj is browser or obj is vp or obj is cont or obj is inp
            )
        except RuntimeError:
            return False

    def eventFilter(self, obj, event) -> bool:
        try:
            etype = event.type()
        except RuntimeError:
            return False
        if (
            etype
            in (
                QEvent.Type.DragEnter,
                QEvent.Type.DragMove,
                QEvent.Type.DragLeave,
                QEvent.Type.Drop,
            )
            and self._is_chat_drop_target(obj)
        ):
            if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                mime = event.mimeData() if hasattr(event, "mimeData") else None
                if self._extract_local_files_from_mime(mime) or (
                    mime is not None and mime.hasImage()
                ):
                    event.acceptProposedAction()
                    if hasattr(self, "chat_drop_hint"):
                        self.chat_drop_hint.setVisible(True)
                    return True
            elif etype == QEvent.Type.DragLeave:
                if hasattr(self, "chat_drop_hint"):
                    inside = self.chat_pane.rect().contains(
                        self.chat_pane.mapFromGlobal(QCursor.pos())
                    )
                    if not inside:
                        self.chat_drop_hint.setVisible(False)
                return True
            elif etype == QEvent.Type.Drop:
                if hasattr(self, "chat_drop_hint"):
                    self.chat_drop_hint.setVisible(False)
                mime = event.mimeData() if hasattr(event, "mimeData") else None
                if self._handle_dropped_mime(mime):
                    event.acceptProposedAction()
                    return True

        if hasattr(self, "chat_input") and obj is self.chat_input and etype == QEvent.Type.KeyPress:
            if isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
                if self._reply_to_message is not None or self._editing_message_id:
                    self._cancel_chat_compose_mode()
                    return True
            if event.matches(QKeySequence.StandardKey.Paste):
                cb = QApplication.clipboard()
                mime = cb.mimeData() if cb is not None else None
                if mime is not None and mime.hasImage():
                    if self._editing_message_id:
                        self.toast_requested.emit("Termina o cancela la edición antes de adjuntar imágenes.")
                        return True
                    if self._active_chat_can_send and self._active_chat_can_attach:
                        img = cb.image()
                        if not img.isNull():
                            tmp_dir = USER_DATA_DIR / "tmp_uploads"
                            tmp_dir.mkdir(parents=True, exist_ok=True)
                            out_path = tmp_dir / f"paste_{int(time.time() * 1000)}.png"
                            if img.save(str(out_path), "PNG"):
                                self._add_pending_attachments([str(out_path)], is_temp=True)
                                return True
                    else:
                        self.toast_requested.emit("El bot no puede adjuntar imágenes en este chat.")
                        return True
                elif mime is not None and mime.hasUrls():
                    files = self._extract_local_files_from_mime(mime)
                    if files:
                        if self._editing_message_id:
                            self.toast_requested.emit("Termina o cancela la edición antes de adjuntar archivos.")
                            return True
                        if self._active_chat_can_send and self._active_chat_can_attach:
                            self._add_pending_attachments(files, is_temp=False)
                        else:
                            self.toast_requested.emit("El bot no puede adjuntar archivos en este chat.")
                        return True
        return super().eventFilter(obj, event)

    def _on_attach_files_clicked(self) -> None:
        if not (self._active_chat_can_send and self._active_chat_can_attach):
            self.toast_requested.emit("El bot no tiene permiso para adjuntar archivos aquí.")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Adjuntar archivos o imágenes",
            "",
            "Todos los archivos (*.*)",
        )
        if files:
            self._add_pending_attachments(files, is_temp=False)

    def _add_pending_attachments(self, paths: list[str], *, is_temp: bool = False) -> None:
        existing = {entry["path"] for entry in self._pending_attachments}
        for raw_p in paths:
            p = str(raw_p or "").strip()
            if not p or not os.path.isfile(p) or p in existing:
                continue
            if len(self._pending_attachments) >= 10:
                self.toast_requested.emit("Máximo 10 archivos por mensaje.")
                break
            sz = os.path.getsize(p)
            if sz > _MAX_CHAT_UPLOAD_BYTES:
                self.toast_requested.emit(
                    f"'{os.path.basename(p)}' supera el límite de 25 MB."
                )
                continue
            self._pending_attachments.append(
                {
                    "path": p,
                    "name": os.path.basename(p),
                    "size": sz,
                    "is_temp": bool(is_temp),
                }
            )
            existing.add(p)
        self._render_pending_attachments()

    def _remove_pending_attachment(self, idx: int) -> None:
        if 0 <= idx < len(self._pending_attachments):
            removed = self._pending_attachments.pop(idx)
            if removed.get("is_temp"):
                self._cleanup_temp_upload(str(removed.get("path") or ""))
            self._render_pending_attachments()

    def _cleanup_temp_upload(self, path_str: str) -> None:
        if not path_str:
            return
        try:
            tmp_dir = (USER_DATA_DIR / "tmp_uploads").resolve()
            resolved = Path(path_str).resolve()
            if resolved.is_relative_to(tmp_dir) and resolved.is_file():
                resolved.unlink(missing_ok=True)
        except Exception:
            pass

    def _clear_pending_attachments(self, *, delete_temp: bool = True) -> None:
        if delete_temp:
            for item in self._pending_attachments:
                if item.get("is_temp"):
                    self._cleanup_temp_upload(str(item.get("path") or ""))
        self._pending_attachments.clear()
        self._render_pending_attachments()

    def _render_pending_attachments(self) -> None:
        if not hasattr(self, "chat_pending_bar"):
            return
        while self.chat_pending_lay.count() > 0:
            it = self.chat_pending_lay.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        if not self._pending_attachments:
            self.chat_pending_bar.setVisible(False)
            self.chat_pending_lay.addStretch(1)
            return
        for idx, att in enumerate(self._pending_attachments):
            chip = QFrame()
            chip.setObjectName("PillBtn")
            chip.setStyleSheet(
                f"QFrame {{ background-color: {self.palette.surface2}; border: 1px solid {self.palette.border_bright}; border-radius: 8px; }}"
            )
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(8, 3, 6, 3)
            cl.setSpacing(5)
            sz_txt = _format_file_size(int(att.get("size") or 0))
            name_txt = str(att.get("name") or "archivo")
            if len(name_txt) > 22:
                name_txt = name_txt[:19] + "…"
            lbl = QLabel(f"📎 {name_txt}" + (f" ({sz_txt})" if sz_txt else ""))
            lbl.setStyleSheet("background: transparent; border: none; font-size: 11px;")
            cl.addWidget(lbl)
            rm = QPushButton("×")
            rm.setObjectName("IconBtn")
            rm.setFixedSize(18, 18)
            rm.setCursor(Qt.CursorShape.PointingHandCursor)
            rm.setToolTip("Quitar archivo")
            rm.clicked.connect(lambda _c=False, i=idx: self._remove_pending_attachment(i))
            cl.addWidget(rm)
            self.chat_pending_lay.addWidget(chip)
        self.chat_pending_lay.addStretch(1)
        self.chat_pending_bar.setVisible(True)

    def _cancel_chat_compose_mode(self) -> None:
        was_editing = bool(self._editing_message_id)
        self._reply_to_message = None
        self._editing_message_id = ""
        if hasattr(self, "chat_context_bar"):
            self.chat_context_bar.setVisible(False)
            self.chat_context_lbl.setText("")
        if hasattr(self, "chat_send_btn"):
            self.chat_send_btn.setText("Enviar")
        if hasattr(self, "chat_attach_btn"):
            self.chat_attach_btn.setEnabled(
                bool(self._active_chat_can_send and self._active_chat_can_attach)
            )
        if was_editing and hasattr(self, "chat_input"):
            self.chat_input.clear()

    def _start_reply_to(self, msg: dict[str, Any]) -> None:
        if not self._active_chat_can_send:
            self.toast_requested.emit("El bot no tiene permiso para escribir en este chat.")
            return
        mid = str(msg.get("id") or "").strip()
        if not mid:
            return
        if self._editing_message_id:
            self.chat_input.clear()
            self._editing_message_id = ""
        self._reply_to_message = dict(msg)
        author = str(msg.get("author_name") or "Usuario")
        snippet = str(msg.get("content") or "").strip().replace("\n", " ")
        if not snippet and msg.get("attachments"):
            att0 = msg["attachments"][0]
            snippet = f"📎 {att0.get('name') or 'archivo'}"
        if len(snippet) > 64:
            snippet = snippet[:61] + "…"
        self.chat_context_lbl.setText(
            f"↩ Respondiendo a {author}" + (f": {snippet}" if snippet else "")
        )
        self.chat_context_bar.setVisible(True)
        self.chat_send_btn.setText("Enviar")
        self.chat_attach_btn.setEnabled(
            bool(self._active_chat_can_send and self._active_chat_can_attach)
        )
        self.chat_input.setFocus()

    def _start_edit_message(self, msg: dict[str, Any]) -> None:
        if not self._active_chat_can_send or not msg.get("is_self"):
            self.toast_requested.emit("Solo puedes editar mensajes propios del bot.")
            return
        mid = str(msg.get("id") or "").strip()
        if not mid:
            return
        self._reply_to_message = None
        self._clear_pending_attachments(delete_temp=True)
        self._editing_message_id = mid
        self.chat_context_lbl.setText("✎ Editando mensaje del bot (Enter para guardar · Esc cancelar)")
        self.chat_context_bar.setVisible(True)
        self.chat_send_btn.setText("Guardar")
        self.chat_attach_btn.setEnabled(False)
        self.chat_input.setText(str(msg.get("content") or ""))
        self.chat_input.selectAll()
        self.chat_input.setFocus()

    def _copy_message_text(self, msg: dict[str, Any]) -> None:
        parts: list[str] = []
        txt = str(msg.get("content") or "").strip()
        if txt:
            parts.append(txt)
        for a in msg.get("attachments") or ():
            if isinstance(a, dict) and a.get("url"):
                parts.append(str(a["url"]))
        final_txt = "\n".join(parts).strip()
        if not final_txt:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(final_txt)
            self.toast_requested.emit("Mensaje copiado al portapapeles")

    def _on_chat_row_copy_requested(self, msg: dict[str, Any], selected_text: str) -> None:
        sel = str(selected_text or "").strip()
        if sel:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(sel)
                self.toast_requested.emit("Texto seleccionado copiado")
            return
        self._copy_message_text(msg)

    def _paste_into_chat_from_action(self, msg: dict[str, Any] | None = None) -> None:
        if not self._active_chat_can_send:
            return
        cb = QApplication.clipboard()
        mime = cb.mimeData() if cb is not None else None
        if mime is not None:
            if mime.hasImage() and self._active_chat_can_attach and not self._editing_message_id:
                self._handle_dropped_mime(mime)
                return
            files = self._extract_local_files_from_mime(mime)
            if files and self._active_chat_can_attach and not self._editing_message_id:
                self._handle_dropped_file_paths(files)
                return
            clip_txt = str(cb.text() or "").strip()
            if clip_txt:
                self.chat_input.insert(clip_txt)
                self.chat_input.setFocus()
                return
        if isinstance(msg, dict):
            msg_txt = str(msg.get("content") or "").strip()
            if msg_txt:
                self.chat_input.insert(msg_txt)
        self.chat_input.setFocus()

    def _copy_attachment_image(self, img_url: str) -> None:
        if not img_url:
            return
        task = _AttachmentFetchTask(img_url)
        sig = task.signals
        self._att_pending_signals.add(sig)

        def _loaded(data: bytes, s: _AttachmentSignals = sig) -> None:
            self._att_pending_signals.discard(s)
            try:
                s.deleteLater()
            except RuntimeError:
                pass
            img = QImage()
            if img.loadFromData(data):
                cb = QApplication.clipboard()
                if cb is not None:
                    cb.setImage(img)
                    self.toast_requested.emit("Imagen copiada al portapapeles")

        def _err(err: str, s: _AttachmentSignals = sig) -> None:
            self._att_pending_signals.discard(s)
            try:
                s.deleteLater()
            except RuntimeError:
                pass
            self.toast_requested.emit(f"No se pudo copiar la imagen: {err}")

        sig.loaded.connect(_loaded)
        sig.failed.connect(_err)
        self._att_pool.start(task)

    def _open_image_lightbox(self, ref: dict[str, Any]) -> None:
        img_url = str(ref.get("url") or ref.get("proxy_url") or "").strip()
        if not img_url:
            return
        self.close_image_lightbox()
        host = self.window() or self
        ov = _ImageLightboxOverlay(
            str(ref.get("name") or "Imagen"),
            img_url,
            self._att_pool,
            self.palette,
            lambda r=dict(ref): self._save_attachment_to_disk(r),
            on_copied_cb=lambda: self.toast_requested.emit("Imagen copiada al portapapeles"),
            parent=host,
        )
        ov.closed.connect(lambda: setattr(self, "_image_lightbox", None) if self._image_lightbox is ov else None)
        self._image_lightbox = ov
        ov.show()
        ov.raise_()
        ov.setFocus()

    def close_image_lightbox(self) -> bool:
        ov = getattr(self, "_image_lightbox", None)
        if ov is None:
            return False
        self._image_lightbox = None
        try:
            ov.close_overlay()
        except RuntimeError:
            pass
        return True

    def _show_chat_row_context_menu(
        self,
        row: _ChatMessageRow,
        global_pos: QPoint,
        selected_txt: str = "",
        att_ref: dict[str, Any] | None = None,
    ) -> None:
        menu = QMenu(self.chat_browser)
        try:
            msg = row.msg
            sel = str(selected_txt or row.selected_text() or "").strip()
            act_copy_sel = menu.addAction("Copiar texto seleccionado") if sel else None

            act_img_view = act_img_copy = act_img_save = None
            if isinstance(att_ref, dict):
                act_img_view = menu.addAction("Ver imagen")
                act_img_copy = menu.addAction("Copiar imagen")
                act_img_save = menu.addAction("Guardar imagen como...")
                menu.addSeparator()

            act_reply = None
            if msg.get("id"):
                act_reply = menu.addAction("Responder")
                act_reply.setEnabled(bool(self._active_chat_can_send))

            act_edit = None
            if msg.get("is_self") and msg.get("id"):
                act_edit = menu.addAction("Editar mensaje")
                act_edit.setEnabled(bool(self._active_chat_can_send))

            act_copy_msg = menu.addAction("Copiar mensaje")
            act_paste = menu.addAction("Pegar en el chat")
            act_paste.setEnabled(bool(self._active_chat_can_send))

            chosen = menu.exec(global_pos)
            if chosen is None:
                return
            if chosen is act_copy_sel:
                cb = QApplication.clipboard()
                if cb is not None and sel:
                    cb.setText(sel)
                    self.toast_requested.emit("Texto seleccionado copiado")
            elif chosen is act_img_view and att_ref is not None:
                self._open_image_lightbox(att_ref)
            elif chosen is act_img_copy and att_ref is not None:
                self._copy_attachment_image(
                    str(att_ref.get("url") or att_ref.get("proxy_url") or "")
                )
            elif chosen is act_img_save and att_ref is not None:
                self._save_attachment_to_disk(att_ref)
            elif chosen is act_reply:
                self._start_reply_to(msg)
            elif chosen is act_edit:
                self._start_edit_message(msg)
            elif chosen is act_copy_msg:
                self._copy_message_text(msg)
            elif chosen is act_paste:
                self._paste_into_chat_from_action(msg)
        finally:
            menu.deleteLater()

    def _on_send_chat_clicked(self) -> None:
        if not self._active_chat_kind or not self._active_chat_id or not self._active_chat_can_send:
            return
        text = self.chat_input.text().strip()
        if self._editing_message_id:
            if not text:
                self.toast_requested.emit("El mensaje no puede quedar vacío al editar.")
                return
            mid = self._editing_message_id
            kind = self._active_chat_kind
            tid = self._active_chat_id
            self._cancel_chat_compose_mode()
            self._on_chat_message_edited(
                {
                    "kind": kind,
                    "target_id": tid,
                    "message_id": mid,
                    "content": text,
                    "edited": True,
                }
            )
            self.svc.edit_chat_message(kind, tid, mid, text)
            return

        atts = list(self._pending_attachments)
        if not text and not atts:
            return
        reply_id = str(self._reply_to_message.get("id") or "") if self._reply_to_message else ""
        file_paths = [str(a["path"]) for a in atts if a.get("path")]
        cleanup_paths = [str(a["path"]) for a in atts if a.get("is_temp") and a.get("path")]
        self.chat_input.clear()
        self._cancel_chat_compose_mode()
        self._clear_pending_attachments(delete_temp=False)
        if self._active_chat_kind == "dm":
            dm_preview = text[:120] if text else f"📎 {atts[0]['name']}"
            self._upsert_dm_thread_local(
                self._active_chat_id,
                self._active_dm_peer_name or self._active_chat_id,
                self._active_dm_peer_avatar,
                preview=dm_preview,
            )
            self._render_dm_threads()
        self.svc.send_chat_message(
            self._active_chat_kind,
            self._active_chat_id,
            text,
            file_paths=file_paths,
            cleanup_paths=cleanup_paths,
            reply_to_id=reply_id or None,
        )

    def _on_chat_image_ready(self, url: str, pix: QPixmap) -> None:
        self._chat_pending_img_urls.discard(url)
        if hasattr(self, "chat_browser"):
            self.chat_browser.on_image_ready(url, pix)

    def _rerender_chat_after_images(self) -> None:
        return

    def _save_attachment_to_disk(self, ref: dict[str, Any]) -> None:
        dl_url = str(ref.get("url") or ref.get("proxy_url") or "").strip()
        if not dl_url:
            return
        def_name = str(ref.get("name") or "archivo").strip() or "archivo"
        dest_path, _ = QFileDialog.getSaveFileName(self, "Guardar archivo", def_name)
        if not dest_path:
            return
        self.toast_requested.emit(f"Descargando {def_name}...")
        task = _AttachmentFetchTask(dl_url, save_path=dest_path)
        sig = task.signals
        self._att_pending_signals.add(sig)

        def _done(saved_p: str, s: _AttachmentSignals = sig) -> None:
            self._att_pending_signals.discard(s)
            try:
                s.deleteLater()
            except RuntimeError:
                pass
            self.toast_requested.emit(f"Archivo guardado: {os.path.basename(saved_p)}")

        def _err(err: str, s: _AttachmentSignals = sig) -> None:
            self._att_pending_signals.discard(s)
            try:
                s.deleteLater()
            except RuntimeError:
                pass
            self.toast_requested.emit(f"Error al descargar archivo: {err}")

        sig.saved.connect(_done)
        sig.failed.connect(_err)
        self._att_pool.start(task)

    def _render_chat_messages(
        self, *, can_read: bool = True, scroll_to_bottom: bool = True
    ) -> None:
        self._chat_last_can_read = bool(can_read)
        self._chat_attachment_refs.clear()
        if not can_read and not self._chat_messages:
            self._chat_pending_img_urls.clear()
            self.chat_browser.show_placeholder(
                "El bot no tiene permiso Read Message History en este canal. Los mensajes nuevos en vivo sí aparecerán aquí."
            )
            return
        if not self._chat_messages:
            self._chat_pending_img_urls.clear()
            self.chat_browser.show_placeholder(
                "Aún no hay mensajes recientes en este chat."
            )
            return

        msgs = list(self._chat_messages)
        for m in msgs:
            for a in m.get("attachments") or ():
                if isinstance(a, dict):
                    self._chat_attachment_refs.append(dict(a))

        self.chat_browser.set_can_send(self._active_chat_can_send)
        self._chat_pending_img_urls = self.chat_browser.set_messages(
            msgs,
            thumb_cache=self._thumb_cache,
            fallback_peer_avatar=self._active_dm_peer_avatar if self._active_chat_kind == "dm" else "",
            fallback_bot_avatar=self.svc.bot_avatar_url,
            scroll_to_bottom=scroll_to_bottom,
        )

    def toggle_mute_from_hotkey(self) -> bool:
        self._toggle_mute()
        return self._muted

    def toggle_deafen_from_hotkey(self) -> bool:
        self._toggle_deaf()
        return self._deafened

    def hangup_from_hotkey(self) -> bool:
        if self.svc.is_in_voice:
            self.svc.leave_channel()
            return True
        return False

    @property
    def is_screen_viewer_active(self) -> bool:
        return bool(self._screen_viewer_active)

    @property
    def is_screen_viewer_theater(self) -> bool:
        return bool(self._screen_viewer_theater)

    @property
    def is_screen_viewer_fullscreen(self) -> bool:
        return bool(self._screen_viewer_fullscreen)

    def start_screen_share_from_hotkey(self) -> str:
        if self._screen_share_state in ("starting", "sharing") or self.screen_share_svc.is_sharing:
            return "already_sharing"
        if not self.svc.is_connected and self._bot_state != "connected":
            return "not_connected"
        if find_ffmpeg() is None:
            return "no_ffmpeg"
        if find_cloudflared() is None:
            return "no_cloudflared"
        src_id = str(self.cfg.discord_share_last_source_id or "monitor:0").strip() or "monitor:0"
        quality = str(self.cfg.discord_share_last_quality or "720p").strip() or "720p"
        fps = 60 if int(self.cfg.discord_share_last_fps or 30) >= 60 else 30
        include_audio = bool(self.cfg.discord_share_last_audio)
        title = "Monitor 1" if src_id == "monitor:0" else ""
        self._screen_share_pending_opts = {
            "source_id": src_id,
            "title": title,
            "quality": quality,
            "fps": fps,
            "include_audio": include_audio,
            "announce": False,
            "targets": [],
        }
        self.screen_share_btn.setEnabled(False)
        self.screen_share_btn.setText(" Iniciando...")
        self.screen_share_svc.start_share(
            source_id=src_id,
            quality=quality,
            fps=fps,
            include_audio=include_audio,
            title=title,
            use_tunnel=True,
            exclude_pid=int(self.svc.worker_pid or 0),
        )
        return "started"

    def stop_screen_share_from_hotkey(self) -> bool:
        if self._screen_share_state in ("starting", "sharing") or self.screen_share_svc.is_sharing:
            self._stop_screen_share()
            return True
        return False

    def _on_join_channel_clicked(self, guild_id: str, channel_id: str, channel_name: str) -> None:
        if self._mic_test_active:
            self._mic_test_active = False
            prev_m, prev_d = self._mic_test_prev_flags or (False, False)
            self._mic_test_prev_flags = None
            self._muted = bool(prev_m)
            self._deafened = bool(prev_d)
            self._sync_mic_test_ui()
            self._emit_voice_status()
        self._joining_channel_id = channel_id
        self.status_pill.setText(f"Conectando a {channel_name}...")
        self._set_pane_visible("voice", True)
        self._render_channels(force=True)
        self.svc.sync_audio_config(self.cfg, muted=self._muted, deafened=self._deafened)
        self.svc.join_channel(guild_id, channel_id)

    def _on_voice_state_updated(self, state: dict[str, Any]) -> None:
        prev_in_voice = bool(self._voice_state.get("in_voice", False))
        prev_ch_id = str(self._voice_state.get("channel_id") or "")
        prev_parts = self._voice_state.get("participants") or []
        prev_other_ids = {
            str(p.get("id") or "")
            for p in prev_parts
            if str(p.get("id") or "") and not p.get("is_self")
        }
        self._voice_state = state
        self._joining_channel_id = str(state.get("joining_channel_id") or "")
        in_voice = bool(state.get("in_voice", False))
        new_ch_id = str(state.get("channel_id") or "")
        if "mic_test" in state:
            srv_test = bool(state.get("mic_test", False))
            if not srv_test and self._mic_test_active and not prev_in_voice and not in_voice:
                pass
            elif srv_test != self._mic_test_active:
                self._mic_test_active = srv_test
                if not srv_test and prev_in_voice and not in_voice and self._mic_test_prev_flags is not None:
                    self._muted, self._deafened = self._mic_test_prev_flags
                    self._mic_test_prev_flags = None
                self._sync_mic_test_ui()

        if in_voice:
            ch_name = str(state.get("channel_name") or "Voz")
            g_name = str(state.get("guild_name") or "")
            self.active_channel_lbl.setText(f"{ch_name} · {g_name}" if g_name else ch_name)
            if state.get("reconnecting"):
                self.status_pill.setText(f"Reconectando voz · {ch_name}")
            else:
                self.status_pill.setText(f"En voz · {ch_name}")
        else:
            self.active_channel_lbl.setText("Sin canal activo")
            if self._joining_channel_id:
                self.status_pill.setText("Conectando a voz...")
            elif self.svc.is_connected:
                self.status_pill.setText(f"Conectado · {self.svc.bot_name}")

        self._update_info_pill()
        self._render_channels()
        self._render_participants()
        new_parts = state.get("participants") or []
        new_other_ids = {
            str(p.get("id") or "")
            for p in new_parts
            if str(p.get("id") or "") and not p.get("is_self")
        }
        if not prev_in_voice and in_voice:
            self.sound_event_requested.emit("self_join")
        elif prev_in_voice and not in_voice:
            self.sound_event_requested.emit("self_leave")
        elif prev_in_voice and in_voice:
            if new_ch_id and prev_ch_id and new_ch_id != prev_ch_id:
                self.sound_event_requested.emit("self_join")
            elif new_other_ids - prev_other_ids:
                self.sound_event_requested.emit("user_join")
            elif prev_other_ids - new_other_ids:
                self.sound_event_requested.emit("user_leave")

        if in_voice != prev_in_voice or [p.get("id") for p in new_parts] != [p.get("id") for p in prev_parts]:
            self._refresh_members_list()
        self._emit_voice_status()

    def _compute_grid_cols(self) -> int:
        vp = self.participants_scroll.viewport()
        w = vp.width() if vp is not None else self.width()
        if w <= 0:
            w = 680
        return max(1, min(4, (max(190, w - 24) + 12) // 176))

    def _voice_viewport_size(self) -> tuple[int, int]:
        vp = self.participants_scroll.viewport()
        w = vp.width() if vp is not None else self.width()
        h = vp.height() if vp is not None else self.height()
        return (w if w > 0 else 680), (h if h > 0 else 420)

    def _detach_participant_rows(self) -> None:
        while self.participants_rows_layout.count() > 0:
            item = self.participants_rows_layout.takeAt(0)
            if item is None:
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count() > 0:
                    sub.takeAt(0)
                sub.deleteLater()

    def _relayout_participant_cards_if_needed(self) -> None:
        if not self._participant_cards_order:
            return
        avail_w, avail_h = self._voice_viewport_size()
        has_share = any(c._screen_share is not None for c in self._participant_cards_order)
        key = (len(self._participant_cards_order), has_share, avail_w // 20, avail_h // 20)
        if key != self._last_voice_layout_key:
            self._relayout_participant_cards()

    def _relayout_participant_cards(self) -> None:
        if not self._participant_cards_order:
            return
        avail_w, avail_h = self._voice_viewport_size()
        max_cols = self._compute_grid_cols()
        self._last_grid_cols = max_cols
        count = len(self._participant_cards_order)
        has_share = any(c._screen_share is not None for c in self._participant_cards_order)
        row_sizes = _compute_voice_row_sizes(count, max_cols)
        card_w, card_h = _compute_participant_card_size(
            count,
            row_sizes,
            avail_w,
            avail_h,
            spacing=12,
            has_screen_share=has_share,
        )
        req_w = max((c.minimumWidth() for c in self._participant_cards_order), default=card_w)
        req_h = max(
            (max(c.minimumHeight(), c.sizeHint().height()) for c in self._participant_cards_order),
            default=card_h,
        )
        card_w = max(card_w, req_w)
        card_h = max(card_h, req_h)
        self._last_voice_row_sizes = row_sizes
        self._last_voice_layout_key = (count, has_share, avail_w // 20, avail_h // 20)

        self._detach_participant_rows()
        idx = 0
        for rc in row_sizes:
            row_lay = QHBoxLayout()
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(12)
            row_lay.addStretch(1)
            for _ in range(rc):
                if idx >= count:
                    break
                card = self._participant_cards_order[idx]
                idx += 1
                card.setFixedSize(card_w, card_h)
                card.show()
                row_lay.addWidget(card)
            row_lay.addStretch(1)
            self.participants_rows_layout.addLayout(row_lay)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._relayout_participant_cards_if_needed()

    @property
    def is_screen_sharing(self) -> bool:
        return self._screen_share_state == "sharing"

    def _participants_signature(self) -> Any:
        in_voice = bool(self._voice_state.get("in_voice", False))
        parts = self._voice_state.get("participants") or []
        return (
            in_voice,
            self._screen_share_state == "sharing",
            tuple(
                (
                    str(p.get("id") or ""),
                    str(p.get("name") or ""),
                    bool(p.get("is_bot", False)),
                    bool(p.get("is_self", False)),
                    bool(p.get("muted", False)),
                    bool(p.get("deafened", False)),
                    str(p.get("avatar_url") or ""),
                    str(
                        ((p.get("screen_share") or {}).get("stream_url"))
                        or ((self._remote_screen_shares.get(str(p.get("id") or "")) or {}).get("stream_url"))
                        or ""
                    )
                    if not p.get("is_self")
                    else "",
                    int(
                        ((p.get("screen_share") or {}).get("rev"))
                        or ((self._remote_screen_shares.get(str(p.get("id") or "")) or {}).get("rev"))
                        or 0
                    )
                    if not p.get("is_self")
                    else 0,
                )
                for p in parts
            ),
        )

    def _render_participants(self, force: bool = False) -> None:
        sig = self._participants_signature()
        if not force and sig == self._last_participants_sig:
            return
        self._last_participants_sig = sig

        for old_card in self._participant_cards_order:
            rel = getattr(old_card, "release_resources", None)
            if callable(rel):
                try:
                    rel()
                except Exception:
                    pass
            old_card.hide()
            old_card.setParent(None)
            old_card.deleteLater()
        self._participant_cards.clear()
        self._participant_cards_order.clear()
        self._last_voice_row_sizes = []
        self._last_voice_layout_key = (0, 0, 0)
        _clear_layout(self.participants_rows_layout)

        parts = self._voice_state.get("participants") or []
        if not self._voice_state.get("in_voice") or not parts:
            self.participants_empty_lbl.setVisible(True)
            return

        self.participants_empty_lbl.setVisible(False)
        for part in parts:
            uid = str(part.get("id") or "")
            is_self = bool(part.get("is_self", False))
            key = "local" if is_self else uid
            speaking = bool(self._speaking.get(key, False))
            vol = self._user_volumes.get(uid, 100)
            part_with_status = dict(part)
            if is_self and self._screen_share_state == "sharing":
                part_with_status["is_streaming"] = True
            if uid in self._user_statuses:
                part_with_status["status"] = self._user_statuses[uid]
            if not is_self and not part_with_status.get("screen_share") and uid in self._remote_screen_shares:
                part_with_status["screen_share"] = dict(self._remote_screen_shares[uid])
            card = _ParticipantCard(
                part_with_status,
                vol,
                speaking,
                self.palette,
                thumb_cache=self._thumb_cache,
                parent=self.participants_container,
            )
            card.volume_changed.connect(self._on_user_vol_value_changed)
            card.dm_requested.connect(self._open_dm_with_user)
            card.watch_share_requested.connect(self._open_screen_share_viewer)
            self._participant_cards[key] = card
            self._participant_cards_order.append(card)
        self._relayout_participant_cards()

    def _on_speaking_updated(self, speaking_map: dict[str, bool]) -> None:
        self._speaking = dict(speaking_map)
        for key, card in self._participant_cards.items():
            card.set_speaking(bool(self._speaking.get(key, False)))

    def _on_mic_level_updated(self, rms: int, transmitting: bool) -> None:
        self._last_mic_rms = max(0, min(100, int(rms)))

    def _on_devices_updated(self, data: dict[str, Any]) -> None:
        self._populating_devices = True
        try:
            self.input_combo.clear()
            for dev in data.get("inputs") or []:
                self.input_combo.addItem(
                    str(dev.get("label") or dev.get("name") or ""),
                    str(dev.get("name") or ""),
                )
            idx_in = self.input_combo.findData(self.cfg.discord_input_device)
            if idx_in >= 0:
                self.input_combo.setCurrentIndex(idx_in)

            self.output_combo.clear()
            for dev in data.get("outputs") or []:
                self.output_combo.addItem(
                    str(dev.get("label") or dev.get("name") or ""),
                    str(dev.get("name") or ""),
                )
            idx_out = self.output_combo.findData(self.cfg.discord_output_device)
            if idx_out >= 0:
                self.output_combo.setCurrentIndex(idx_out)
        finally:
            self._populating_devices = False

    def _on_refresh_devices_clicked(self) -> None:
        self.svc.refresh_devices(spawn=True)
        if self.svc.is_in_voice:
            in_name = str(self.input_combo.currentData() or self.cfg.discord_input_device)
            out_name = str(self.output_combo.currentData() or self.cfg.discord_output_device)
            self.svc.set_devices(in_name, out_name)

    def _on_devices_combo_changed(self, _idx: int) -> None:
        if self._populating_devices:
            return
        in_name = str(self.input_combo.currentData() or "")
        out_name = str(self.output_combo.currentData() or "")
        self.cfg.discord_input_device = in_name
        self.cfg.discord_output_device = out_name
        save_config(self.cfg)
        self.svc.set_devices(in_name, out_name)

    def _sync_mic_test_ui(self) -> None:
        if not hasattr(self, "mic_test_btn"):
            return
        active = bool(self._mic_test_active)
        self.mic_test_btn.setText("Dejar de probar" if active else "Probar micrófono")
        self.mic_test_btn.setProperty("active", "true" if active else "false")
        self.mic_test_btn.style().unpolish(self.mic_test_btn)
        self.mic_test_btn.style().polish(self.mic_test_btn)
        if hasattr(self, "mic_test_hint_lbl"):
            self.mic_test_hint_lbl.setVisible(active)

    def _toggle_mic_test(self) -> None:
        if not self._mic_test_active:
            if not self.svc.is_connected and self._bot_state != "connected":
                self.toast_requested.emit(
                    "Conecta el Bot de Discord primero para probar tu micrófono."
                )
                return
            self._mic_test_prev_flags = (self._muted, self._deafened)
            self._mic_test_active = True
            self._muted = True
            self._deafened = True
            self._sync_mic_test_ui()
            self._refresh_mute_deaf_buttons()
            self._emit_voice_status()
            self.svc.set_mic_test(True)
            return

        self._mic_test_active = False
        prev_m, prev_d = self._mic_test_prev_flags or (False, False)
        self._mic_test_prev_flags = None
        self._muted = bool(prev_m)
        self._deafened = bool(prev_d)
        self._sync_mic_test_ui()
        self._refresh_mute_deaf_buttons()
        self._emit_voice_status()
        self.svc.set_mic_test(False)

    def _toggle_mute(self) -> None:
        if self._mic_test_active:
            self._mic_test_active = False
            self._mic_test_prev_flags = None
            self._sync_mic_test_ui()
        self._muted = not self._muted
        self._refresh_mute_deaf_buttons()
        self.svc.set_mute(self._muted)
        self._emit_voice_status()
        self.sound_event_requested.emit("mute_on" if self._muted else "mute_off")

    def _toggle_deaf(self) -> None:
        if self._mic_test_active:
            self._mic_test_active = False
            self._mic_test_prev_flags = None
            self._sync_mic_test_ui()
        self._deafened = not self._deafened
        self._refresh_mute_deaf_buttons()
        self.svc.set_deaf(self._deafened)
        self._emit_voice_status()
        self.sound_event_requested.emit("deafen_on" if self._deafened else "deafen_off")

    def _on_out_vol_changed(self, value: int) -> None:
        self.out_vol_val.setText(f"{value}%")
        self.cfg.discord_output_volume = value
        save_config(self.cfg)
        self.svc.set_output_volume(value)

    def _on_mic_gain_changed(self, value: int) -> None:
        self.mic_gain_val.setText(f"{value}%")
        self.cfg.discord_mic_gain = value
        save_config(self.cfg)
        self.svc.set_mic_gain(value)

    def _update_vad_label(self, value: int) -> None:
        if value == 0:
            self.vad_val.setText("Abierto")
            self.vad_val.setStyleSheet("")
            self.vad_val.setToolTip("Micrófono siempre abierto.")
        elif value > 45:
            self.vad_val.setText(f"{value}% (alto)")
            self.vad_val.setStyleSheet("color: #FBBF24; font-weight: 600;")
            self.vad_val.setToolTip(
                "Valor alto: puede cortar el final de tus palabras. Recomendado: 15–25% o 0% (Abierto)."
            )
        else:
            self.vad_val.setText(f"{value}%")
            self.vad_val.setStyleSheet("")
            self.vad_val.setToolTip("Corte de ruido activo (15–25% recomendado).")

    def _on_agc_toggled(self, checked: bool) -> None:
        self.cfg.discord_mic_agc = bool(checked)
        save_config(self.cfg)
        self.svc.set_mic_agc(self.cfg.discord_mic_agc)

    def _on_ns_changed(self, _idx: int) -> None:
        mode = str(self.ns_combo.currentData() or "ai")
        self.cfg.discord_noise_suppression = mode
        save_config(self.cfg)
        self.svc.set_noise_suppression(mode)

    def _on_vad_changed(self, value: int) -> None:
        self._update_vad_label(value)
        self.cfg.discord_vad_threshold = value
        save_config(self.cfg)
        self.svc.set_vad_threshold(value)

    def _on_user_vol_value_changed(self, uid: str, value: int) -> None:
        self._user_volumes[uid] = value
        self.svc.set_user_volume(uid, value)

    # ------------------------------------------------------------------ #
    # Compartida de pantalla aislada + visor nativo libmpv lazy          #
    # ------------------------------------------------------------------ #

    def _sync_screen_share_icons(self) -> None:
        p = self.palette
        if hasattr(self, "screen_share_btn"):
            sharing = self._screen_share_state in ("starting", "sharing")
            self.screen_share_btn.setIcon(
                render_icon(
                    "screen_share_off" if sharing else "screen_share",
                    "#F87171" if sharing else p.text,
                    14,
                )
            )
        if hasattr(self, "screen_viewer_mute_btn"):
            is_mute = self._screen_viewer_muted or self._screen_viewer_volume <= 0
            self.screen_viewer_mute_btn.setIcon(
                render_icon("mute" if is_mute else "volume", p.text, 14)
            )
        if hasattr(self, "screen_viewer_fs_btn"):
            fs_icon = "exit_fullscreen" if self._screen_viewer_fullscreen else "fullscreen"
            self.screen_viewer_fs_btn.setIcon(render_icon(fs_icon, p.text, 15))

    def _save_last_share_config(
        self,
        *,
        source_id: str,
        quality: str,
        fps: int,
        include_audio: bool,
    ) -> None:
        self.cfg.discord_share_last_source_id = str(source_id or "monitor:0").strip() or "monitor:0"
        sq = str(quality or "720p").strip().lower()
        self.cfg.discord_share_last_quality = sq if sq in ("480p", "720p", "1080p") else "720p"
        self.cfg.discord_share_last_fps = 60 if int(fps or 30) >= 60 else 30
        self.cfg.discord_share_last_audio = bool(include_audio)
        save_config(self.cfg)

    def _get_auto_share_targets(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in (self.cfg.discord_share_auto_targets or []):
            txt = str(raw or "").strip()
            if ":" not in txt:
                continue
            kind, tid = txt.split(":", 1)
            k = kind.strip()
            t = tid.strip()
            if k in ("channel", "dm") and t and (k, t) not in seen:
                seen.add((k, t))
                out.append((k, t))
        return out

    def _current_guild_text_channels(self) -> list[tuple[str, str]]:
        gid = str(self.guild_combo.currentData() or self._selected_guild_id or "")
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
        if not guild:
            return []
        hidden = set(self.cfg.discord_hidden_channels)
        out: list[tuple[str, str]] = []
        for tc in guild.get("text_channels") or ():
            cid = str(tc.get("id") or "")
            if not cid or cid in hidden:
                continue
            cname = str(tc.get("name") or "general")
            out.append((cid, cname))
        return out

    def _current_dm_targets(self) -> list[tuple[str, str]]:
        targets: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(uid: str, name: str, suffix: str = "") -> None:
            clean_uid = str(uid or "").strip()
            clean_name = str(name or "").strip()
            if not clean_uid or not clean_name or clean_uid in seen:
                return
            seen.add(clean_uid)
            targets.append((clean_uid, f"{clean_name}{suffix}"))

        for part in self._voice_state.get("participants") or ():
            if part.get("is_self") or part.get("is_bot"):
                continue
            _add(str(part.get("id") or ""), str(part.get("name") or ""), " (en voz)")

        for dm in self._load_dm_threads():
            _add(str(dm.get("user_id") or ""), str(dm.get("user_name") or ""))

        for pool in (self._channel_members, self._members):
            for mem in pool:
                if mem.get("is_bot"):
                    continue
                _add(str(mem.get("id") or ""), str(mem.get("name") or ""))
        return targets

    def _on_screen_share_btn_clicked(self) -> None:
        if self._screen_share_state in ("starting", "sharing"):
            self._stop_screen_share()
            return
        channels = self._current_guild_text_channels()
        dm_targets = self._current_dm_targets()
        default_target: tuple[str, str] | None = None
        if self._active_chat_kind in ("channel", "dm") and self._active_chat_id:
            default_target = (self._active_chat_kind, str(self._active_chat_id))
        dlg = _ScreenShareSetupDialog(
            self.screen_share_svc,
            self.palette,
            channels,
            dm_targets=dm_targets,
            default_target=default_target,
            current_source_id=self.cfg.discord_share_last_source_id,
            current_quality=self.cfg.discord_share_last_quality,
            current_fps=self.cfg.discord_share_last_fps,
            current_include_audio=self.cfg.discord_share_last_audio,
            auto_targets_count=len(self.cfg.discord_share_auto_targets),
            parent=self.window() or self,
        )
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            opts = dlg.selected_options()
        finally:
            dlg.deleteLater()

        self._save_last_share_config(
            source_id=str(opts["source_id"]),
            quality=str(opts["quality"]),
            fps=int(opts["fps"]),
            include_audio=bool(opts["include_audio"]),
        )
        self._screen_share_pending_opts = opts
        self.screen_share_btn.setEnabled(False)
        self.screen_share_btn.setText(" Iniciando...")
        self.screen_share_svc.start_share(
            source_id=str(opts["source_id"]),
            quality=str(opts["quality"]),
            fps=int(opts["fps"]),
            include_audio=bool(opts["include_audio"]),
            title=str(opts["title"]),
            use_tunnel=True,
            exclude_pid=int(self.svc.worker_pid or 0),
        )

    def _on_screen_share_reconfigure_clicked(self) -> None:
        if self._screen_share_state != "sharing":
            return
        info = dict(self._screen_share_info)
        channels = self._current_guild_text_channels()
        dm_targets = self._current_dm_targets()
        dlg = _ScreenShareSetupDialog(
            self.screen_share_svc,
            self.palette,
            channels,
            dm_targets=dm_targets,
            is_live_edit=True,
            current_source_id=str(info.get("source_id") or self.cfg.discord_share_last_source_id),
            current_quality=str(info.get("quality") or self.cfg.discord_share_last_quality),
            current_fps=int(info.get("fps") or self.cfg.discord_share_last_fps),
            current_include_audio=bool(info.get("include_audio", info.get("has_audio", True))),
            parent=self.window() or self,
        )
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            opts = dlg.selected_options()
        finally:
            dlg.deleteLater()

        self._save_last_share_config(
            source_id=str(opts["source_id"]),
            quality=str(opts["quality"]),
            fps=int(opts["fps"]),
            include_audio=bool(opts["include_audio"]),
        )
        self._screen_share_pending_opts = opts
        self.share_settings_btn.setEnabled(False)
        self.share_settings_btn.setText("Cambiando...")
        self.screen_share_svc.reconfigure_share(
            source_id=str(opts["source_id"]),
            quality=str(opts["quality"]),
            fps=int(opts["fps"]),
            include_audio=bool(opts["include_audio"]),
            title=str(opts["title"]),
            exclude_pid=int(self.svc.worker_pid or 0),
        )

    def _on_screen_share_worker_ram(self, ws_mb: float, priv_mb: float) -> None:
        self._screen_share_ws_mb = max(0.0, float(ws_mb))
        self._screen_share_priv_mb = max(0.0, float(priv_mb))
        if self._screen_share_state == "sharing":
            self._refresh_share_live_strip()
        self.screen_share_ram_updated.emit(self._screen_share_ws_mb, self._screen_share_priv_mb)

    def _on_screen_share_state_changed(self, state: str, data: dict[str, Any]) -> None:
        prev_state = self._screen_share_state
        self._screen_share_state = state
        self.screen_share_btn.setEnabled(True)
        self.share_settings_btn.setEnabled(True)
        self.share_settings_btn.setText("Cambiar")
        if state == "starting":
            self.screen_share_btn.setText(" Iniciando...")
            self._sync_screen_share_icons()
            return

        if state == "sharing":
            is_reconf = bool(data.get("reconfigured", False))
            self._screen_share_info = dict(data)
            self._screen_share_rev = (self._screen_share_rev + 1) if is_reconf else 1
            if not is_reconf:
                self._screen_share_stats = {}
            self.screen_share_btn.setText(" Dejar de compartir")
            self.screen_share_btn.setObjectName("DangerBtn")
            self.screen_share_btn.style().unpolish(self.screen_share_btn)
            self.screen_share_btn.style().polish(self.screen_share_btn)
            self._sync_screen_share_icons()
            self._refresh_share_live_strip()
            self.share_live_strip.setVisible(True)
            self._set_pane_visible("voice", True)
            if not is_reconf and prev_state != "sharing":
                self.sound_event_requested.emit("stream_start")
            self.screen_share_active_changed.emit(True)
            self._render_participants()
            if self.svc.is_connected and data.get("watch_url"):
                self.svc.set_screen_share_presence(
                    {
                        "watch_url": str(data.get("watch_url") or ""),
                        "quality": str(data.get("quality") or "720p"),
                        "fps": int(data.get("fps") or 30),
                        "has_audio": bool(data.get("has_audio", False)),
                        "rev": self._screen_share_rev,
                    }
                )

            opts = self._screen_share_pending_opts
            self._screen_share_pending_opts = None
            manual_targets = (
                list(opts.get("targets") or [])
                if (opts and opts.get("announce"))
                else []
            )
            auto_targets = [] if is_reconf else self._get_auto_share_targets()
            merged_targets: list[tuple[str, str]] = []
            seen_t: set[tuple[str, str]] = set()
            for tgt_kind, tgt_id in [*auto_targets, *manual_targets]:
                pair = (str(tgt_kind).strip(), str(tgt_id).strip())
                if pair[0] in ("channel", "dm") and pair[1] and pair not in seen_t:
                    seen_t.add(pair)
                    merged_targets.append(pair)

            if merged_targets and self.svc.is_connected:
                txt = format_screen_share_announcement(
                    watch_url=str(data.get("watch_url") or ""),
                    quality=str(data.get("quality") or "720p"),
                    fps=int(data.get("fps") or 30),
                    has_audio=bool(data.get("has_audio", False)),
                    title=str(data.get("title") or "Pantalla compartida"),
                )
                for tgt_kind, tgt_id in merged_targets:
                    fut = self.svc.send_chat_message(tgt_kind, tgt_id, txt, await_result=True)
                    if fut is not None:
                        def _on_sent(
                            f: Any, k: str = str(tgt_kind), tid: str = str(tgt_id)
                        ) -> None:
                            try:
                                res = f.result()
                                if isinstance(res, dict) and res.get("message_id"):
                                    self.screen_share_announced.emit(
                                        k, tid, str(res["message_id"])
                                    )
                            except Exception:
                                pass

                        fut.add_done_callback(_on_sent)

            if is_reconf and self._screen_viewer_active and self._screen_viewer_is_local_preview:
                local_url = str(
                    data.get("local_stream_url") or data.get("stream_url") or ""
                ).strip()
                if local_url and self._screen_player is not None:
                    self.screen_viewer_title_lbl.setText(
                        f"Vista previa · {data.get('title') or 'Tu pantalla'}"
                    )
                    self._screen_player.play_stream(local_url, is_live=True, low_latency=True)
                    QTimer.singleShot(80, self.screen_video_surface.force_native_redraw)

            self.toast_requested.emit(
                "Transmisión actualizada en vivo."
                if is_reconf
                else "Compartida de pantalla iniciada."
            )
            return

        # Idle
        self._screen_share_info = {}
        self._screen_share_stats = {}
        self._screen_share_ws_mb = 0.0
        self._screen_share_priv_mb = 0.0
        self._screen_share_rev = 0
        self._screen_share_pending_opts = None
        if self.svc.is_connected:
            self.svc.set_screen_share_presence(None)
        self.screen_share_btn.setText(" Compartir pantalla")
        self.screen_share_btn.setObjectName("PillBtn")
        self.screen_share_btn.style().unpolish(self.screen_share_btn)
        self.screen_share_btn.style().polish(self.screen_share_btn)
        self._sync_screen_share_icons()
        self.share_live_strip.setVisible(False)
        if self._screen_viewer_active and self._screen_viewer_is_local_preview:
            self._close_screen_share_viewer()
        self._mark_announced_share_ended()
        if prev_state == "sharing":
            self.sound_event_requested.emit("stream_stop")
        self.screen_share_active_changed.emit(False)
        self._render_participants()
        self.screen_share_ram_updated.emit(0.0, 0.0)

    def _on_screen_share_announced(
        self, kind: str, target_id: str, message_id: str
    ) -> None:
        if self._screen_share_state == "sharing":
            self._screen_share_announces.append(
                (str(kind), str(target_id), str(message_id))
            )

    def _mark_announced_share_ended(self) -> None:
        anns = list(self._screen_share_announces)
        self._screen_share_announces.clear()
        if anns and self.svc.is_connected:
            for kind, tid, mid in anns:
                try:
                    self.svc.edit_chat_message(
                        kind, tid, mid, f"⏹️ {SCREEN_SHARE_ENDED_MARKER}."
                    )
                except Exception:
                    pass

    def _on_screen_share_stats_updated(self, stats: dict[str, Any]) -> None:
        self._screen_share_stats = dict(stats)
        if self._screen_share_state == "sharing":
            self._refresh_share_live_strip()

    def _refresh_share_live_strip(self) -> None:
        info = self._screen_share_info
        st = self._screen_share_stats
        q = str(st.get("quality") or info.get("quality") or "720p")
        fps = int(st.get("fps") or info.get("fps") or 30)
        enc = str(st.get("encoder") or info.get("encoder") or "h264")
        enc_lbl = enc.replace("h264_", "").upper()
        viewers = int(st.get("viewers") or 0)
        kbps = int(st.get("bitrate_kbps") or 0)
        br_txt = f" · {kbps / 1000.0:.1f} Mbps" if kbps > 0 else ""
        v_txt = f"{viewers} espectador" if viewers == 1 else f"{viewers} espectadores"
        self.share_live_lbl.setText(
            f"🔴 Compartiendo · {q} {fps}fps · {enc_lbl}{br_txt} · {v_txt}"
        )
        ram_tip = (
            f"Consumo real de compartida (worker + FFmpeg + túnel): "
            f"{self._screen_share_ws_mb:.1f} MB (Priv: {self._screen_share_priv_mb:.1f} MB)"
            if self._screen_share_ws_mb > 0.0
            else "Midiendo consumo del worker de pantalla..."
        )
        self.share_live_lbl.setToolTip(ram_tip)

    def _on_screen_share_error(self, msg: str) -> None:
        self._screen_share_pending_opts = None
        self.screen_share_btn.setEnabled(True)
        self._on_screen_share_state_changed("idle", {})
        self.toast_requested.emit(f"Pantalla compartida: {msg}")

    def _stop_screen_share(self) -> None:
        if self._screen_viewer_active and self._screen_viewer_is_local_preview:
            self._close_screen_share_viewer()
        self._mark_announced_share_ended()
        self._screen_share_rev = 0
        if self.svc.is_connected:
            self.svc.set_screen_share_presence(None)
        self.screen_share_svc.stop_share()

    def _copy_active_share_url(self) -> None:
        url = str(self._screen_share_info.get("watch_url") or "").strip()
        if not url:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(url)
            self.toast_requested.emit("Enlace de pantalla compartida copiado")

    def _open_local_share_preview(self) -> None:
        url = str(
            self._screen_share_info.get("local_stream_url")
            or self._screen_share_info.get("stream_url")
            or ""
        ).strip()
        watch = str(
            self._screen_share_info.get("watch_url")
            or self._screen_share_info.get("local_watch_url")
            or ""
        ).strip()
        if not url:
            return
        self._open_screen_share_viewer(
            {
                "title": f"Vista previa · {self._screen_share_info.get('title') or 'Tu pantalla'}",
                "stream_url": url,
                "watch_url": watch,
                "is_local_preview": True,
                "start_muted": True,
            }
        )

    def _refresh_remote_share_banner(self) -> None:
        if not hasattr(self, "remote_share_banner"):
            return
        cur_cid = str(self._voice_state.get("channel_id") or "")
        candidates = [
            v
            for v in self._remote_screen_shares.values()
            if str(v.get("stream_url") or "") not in self._dismissed_share_urls
        ]
        if not candidates:
            self._remote_screen_share = None
            self.remote_share_banner.setVisible(False)
            return

        # Priorizar transmisiones en el canal de voz actual o de presencia nativa
        candidates.sort(
            key=lambda v: (
                0 if (cur_cid and str(v.get("channel_id") or "") == cur_cid) else 1,
                0 if v.get("source") == "presence" else 1,
            )
        )
        chosen = dict(candidates[0])
        self._remote_screen_share = chosen
        author = str(chosen.get("author_name") or "Usuario")
        ch_name = str(chosen.get("channel_name") or "")
        q_txt = str(chosen.get("quality") or "").strip()
        fps_n = int(chosen.get("fps") or 0)
        if chosen.get("source") == "presence":
            spec = f" · {q_txt} {fps_n}fps" if q_txt and fps_n else ""
            ch_ctx = f" en 🔊 {ch_name}" if ch_name else ""
            self.remote_share_lbl.setText(f"🔴 {author} está compartiendo pantalla{ch_ctx}{spec}")
            self.remote_browser_btn.setVisible(False)
        else:
            if chosen.get("is_dm") or ch_name.startswith("DM"):
                loc = f" ({ch_name})" if ch_name else " (DM)"
            else:
                loc = f" en #{ch_name}" if ch_name else ""
            self.remote_share_lbl.setText(f"🖥️ {author} está compartiendo pantalla{loc}")
            self.remote_browser_btn.setVisible(bool(chosen.get("watch_url")))
        self.remote_watch_btn.setText("Ver pantalla")
        self.remote_share_banner.setVisible(True)

    def _on_remote_screen_share_detected(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        aid = str(data.get("author_id") or "").strip()
        if aid and aid == str(self.svc.bot_id or ""):
            return
        if data.get("is_self") and self._screen_share_state == "sharing":
            return
        stream_u = str(data.get("stream_url") or "").strip()
        if not stream_u:
            return
        mid = str(data.get("message_id") or "").strip()
        key = aid or (f"msg:{mid}" if mid else stream_u)
        self._dismissed_share_urls.discard(stream_u)
        self._remote_screen_shares[key] = dict(data)
        self._refresh_remote_share_banner()
        self._render_participants()
        self._render_channels()

        new_rev = int(data.get("rev") or 1)
        if (
            self._screen_viewer_active
            and not self._screen_viewer_is_local_preview
            and aid
            and aid == self._screen_viewer_author_id
            and (new_rev > self._screen_viewer_rev or stream_u != self._screen_viewer_stream_url)
        ):
            self._open_screen_share_viewer(data)

    def _on_remote_screen_share_ended(self, data: dict[str, Any]) -> None:
        d = data or {}
        aid = str(d.get("author_id") or "").strip()
        mid = str(d.get("message_id") or "").strip()
        removed_urls: set[str] = set()
        for k in list(self._remote_screen_shares.keys()):
            v = self._remote_screen_shares[k]
            match = (
                (aid and (k == aid or str(v.get("author_id") or "") == aid))
                or (mid and str(v.get("message_id") or "") == mid)
                or (not aid and not mid)
            )
            if match:
                u = str(v.get("stream_url") or "")
                if u:
                    removed_urls.add(u)
                del self._remote_screen_shares[k]

        self._refresh_remote_share_banner()
        self._render_participants()
        self._render_channels()

        if self._screen_viewer_active and not self._screen_viewer_is_local_preview:
            viewer_ended = (
                (aid and aid == self._screen_viewer_author_id)
                or (self._screen_viewer_stream_url and self._screen_viewer_stream_url in removed_urls)
                or (not aid and not mid)
            )
            if viewer_ended:
                self._close_screen_share_viewer()
                self.toast_requested.emit("La transmisión de pantalla ha finalizado.")

    def _dismiss_remote_share_banner(self) -> None:
        if self._remote_screen_share is not None:
            u = str(self._remote_screen_share.get("stream_url") or "")
            if u:
                self._dismissed_share_urls.add(u)
        self._refresh_remote_share_banner()

    def _watch_detected_remote_share(self) -> None:
        if self._remote_screen_share:
            self._open_screen_share_viewer(self._remote_screen_share)

    def _open_detected_remote_in_browser(self) -> None:
        if not self._remote_screen_share:
            return
        url = str(self._remote_screen_share.get("watch_url") or "").strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_screen_player_vo_error(self, _detail: str) -> None:
        if self._screen_viewer_active and self._screen_player is not None:
            if self._screen_player.refresh_video_output():
                QTimer.singleShot(120, self.screen_video_surface.force_native_redraw)
            else:
                self.screen_video_surface.force_native_redraw()

    def _ensure_screen_player(self) -> MediaPlayer:
        if self._screen_player is None:
            self._screen_player = MediaPlayer(
                wid=int(self.screen_video_surface.winId()),
                initial_volume=self._screen_viewer_volume,
                normalization=False,
                lazy=True,
                parent=self,
            )
            self._screen_player.vo_error_detected.connect(self._on_screen_player_vo_error)
            self._screen_player.wheel_scrolled.connect(self._on_screen_viewer_wheel)
            self._screen_player.mouse_moved.connect(self.screen_viewer_mouse_moved.emit)
            self._screen_player.double_clicked.connect(self.toggle_screen_viewer_fullscreen)
        return self._screen_player

    def _emit_screen_viewer_state(self) -> None:
        title = (
            self.screen_viewer_title_lbl.text().strip()
            if hasattr(self, "screen_viewer_title_lbl")
            else "Pantalla compartida"
        )
        eff_vol = 0 if self._screen_viewer_muted else self._screen_viewer_volume
        self.screen_viewer_state_updated.emit(title, eff_vol, self._screen_viewer_muted)

    def _apply_screen_viewer_layout_mode(
        self, *, theater: bool, fullscreen: bool
    ) -> None:
        in_expanded = bool(theater or fullscreen)
        if in_expanded and not self._pre_theater_chrome:
            self._pre_theater_chrome = {
                "channels": self._channels_visible,
                "members": self._members_visible,
                "intents": self.intents_banner.isVisible(),
            }

        if in_expanded:
            self._voice_pane_visible = True
            self._chat_pane_visible = False
            self.center_stack.setVisible(True)
            self.center_collapsed_card.setVisible(False)
            self.center_stack.setCurrentWidget(self.voice_pane)
            self.top_hdr_widget.setVisible(False)
            self.left_card.setVisible(False)
            self.members_card.setVisible(False)
            self.intents_banner.setVisible(False)
            self.center_hdr_card.setVisible(False)
            self.voice_hdr_frame.setVisible(False)
            self.share_live_strip.setVisible(False)
            self.remote_share_banner.setVisible(False)
            self.participants_scroll.setVisible(False)
            self._root_lay.setContentsMargins(0, 0, 0, 0)
            self._vp_lay.setContentsMargins(0, 0, 0, 0)
            if fullscreen:
                self.screen_viewer_hdr_widget.setVisible(False)
                self._svc_lay.setContentsMargins(0, 0, 0, 0)
                self._svc_lay.setSpacing(0)
                self._sv_hdr_lay.setContentsMargins(0, 0, 0, 0)
            else:
                self.screen_viewer_hdr_widget.setVisible(True)
                self._svc_lay.setContentsMargins(0, 0, 0, 0)
                self._svc_lay.setSpacing(0)
                self._sv_hdr_lay.setContentsMargins(12, 8, 12, 8)
        else:
            saved = dict(self._pre_theater_chrome)
            self._pre_theater_chrome = {}
            self._root_lay.setContentsMargins(18, 12, 18, 10)
            self._vp_lay.setContentsMargins(10, 8, 10, 10)
            self._svc_lay.setContentsMargins(8, 8, 8, 6)
            self._svc_lay.setSpacing(6)
            self._sv_hdr_lay.setContentsMargins(0, 0, 0, 0)
            self.top_hdr_widget.setVisible(True)
            self.center_hdr_card.setVisible(True)
            self.voice_hdr_frame.setVisible(True)
            self.screen_viewer_hdr_widget.setVisible(True)
            self.participants_scroll.setVisible(True)
            self.left_card.setVisible(saved.get("channels", self._channels_visible))
            self.members_card.setVisible(saved.get("members", self._members_visible))
            self.intents_banner.setVisible(saved.get("intents", False))
            self.share_live_strip.setVisible(self._screen_share_state == "sharing")
            self._refresh_remote_share_banner()

        if hasattr(self, "screen_viewer_theater_btn"):
            theater_only = bool(theater and not fullscreen)
            self.screen_viewer_theater_btn.setText(
                "Salir teatro" if theater_only else "Teatro"
            )
            self.screen_viewer_theater_btn.setProperty(
                "active", "true" if theater_only else "false"
            )
            self.screen_viewer_theater_btn.style().unpolish(self.screen_viewer_theater_btn)
            self.screen_viewer_theater_btn.style().polish(self.screen_viewer_theater_btn)
        self._sync_screen_share_icons()
        if self._screen_viewer_active:
            QTimer.singleShot(45, self.screen_video_surface.force_native_redraw)

    def _open_screen_share_viewer(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        stream_url = str(payload.get("stream_url") or "").strip()
        watch_url = str(payload.get("watch_url") or "").strip()
        if not stream_url:
            return
        author = str(payload.get("author_name") or "").strip()
        title = str(payload.get("title") or "").strip()
        header = title or (f"Pantalla de {author}" if author else "Pantalla compartida")

        self._screen_viewer_stream_url = stream_url
        self._screen_viewer_watch_url = watch_url
        self._screen_viewer_author_id = str(payload.get("author_id") or "").strip()
        self._screen_viewer_rev = int(payload.get("rev") or 1)
        self._screen_viewer_active = True
        self._screen_viewer_is_local_preview = bool(payload.get("is_local_preview", False))
        if "start_muted" in payload:
            self._screen_viewer_muted = bool(payload.get("start_muted"))
        elif not self._screen_viewer_is_local_preview:
            self._screen_viewer_muted = False
        self.screen_viewer_title_lbl.setText(header)
        if hasattr(self, "screen_viewer_browser_btn"):
            self.screen_viewer_browser_btn.setVisible(
                bool(watch_url)
                and not self._screen_viewer_is_local_preview
                and payload.get("source") != "presence"
            )
        self.screen_viewer_card.setVisible(True)
        self._set_pane_visible("voice", True)

        player = self._ensure_screen_player()
        eff_vol = 0 if self._screen_viewer_muted else self._screen_viewer_volume
        player.set_volume(eff_vol)
        self.screen_viewer_vol_slider.blockSignals(True)
        self.screen_viewer_vol_slider.setValue(eff_vol)
        self.screen_viewer_vol_slider.blockSignals(False)
        player.play_stream(stream_url, is_live=True, low_latency=True)
        self._sync_screen_share_icons()
        self._emit_screen_viewer_state()
        QTimer.singleShot(45, self.screen_video_surface.force_native_redraw)

    def _close_screen_share_viewer(self) -> None:
        was_fs = self._screen_viewer_fullscreen
        self._screen_viewer_active = False
        self._screen_viewer_is_local_preview = False
        self._screen_viewer_theater = False
        self._screen_viewer_fullscreen = False
        self._screen_viewer_pre_fs_theater = False
        self._screen_viewer_stream_url = ""
        self._screen_viewer_watch_url = ""
        self._screen_viewer_author_id = ""
        self._screen_viewer_rev = 0
        if self._screen_player is not None:
            self._screen_player.set_mouse_tracking(False)
            self._screen_player.set_cursor_autohide("no")
            try:
                self._screen_player.unload()
            except Exception:
                pass
        if self._pre_theater_chrome or was_fs:
            self._apply_screen_viewer_layout_mode(theater=False, fullscreen=False)
        if was_fs:
            self.screen_viewer_fullscreen_changed.emit(False)
        self.screen_viewer_card.setVisible(False)

    def toggle_screen_viewer_theater(self) -> bool:
        if not self._screen_viewer_active:
            return False
        if self._screen_viewer_fullscreen:
            self._screen_viewer_fullscreen = False
            self._screen_viewer_theater = True
            if self._screen_player is not None:
                self._screen_player.set_mouse_tracking(False)
                self._screen_player.set_cursor_autohide("no")
            self._apply_screen_viewer_layout_mode(theater=True, fullscreen=False)
            self.screen_viewer_fullscreen_changed.emit(False)
            return True
        self._screen_viewer_theater = not self._screen_viewer_theater
        self._apply_screen_viewer_layout_mode(
            theater=self._screen_viewer_theater, fullscreen=False
        )
        return True

    def toggle_screen_viewer_fullscreen(self) -> bool:
        if not self._screen_viewer_active:
            return False
        if not self._screen_viewer_fullscreen:
            self._screen_viewer_pre_fs_theater = self._screen_viewer_theater
            self._screen_viewer_fullscreen = True
            self._apply_screen_viewer_layout_mode(theater=True, fullscreen=True)
            if self._screen_player is not None:
                self._screen_player.set_mouse_tracking(True)
                self._screen_player.set_cursor_autohide(500)
            self._emit_screen_viewer_state()
            self.screen_viewer_fullscreen_changed.emit(True)
        else:
            self._screen_viewer_fullscreen = False
            self._screen_viewer_theater = bool(self._screen_viewer_pre_fs_theater)
            if self._screen_player is not None:
                self._screen_player.set_mouse_tracking(False)
                self._screen_player.set_cursor_autohide("no")
            self._apply_screen_viewer_layout_mode(
                theater=self._screen_viewer_theater, fullscreen=False
            )
            self.screen_viewer_fullscreen_changed.emit(False)
        return True

    def exit_screen_viewer_fullscreen(self) -> bool:
        if self._screen_viewer_fullscreen:
            return self.toggle_screen_viewer_fullscreen()
        return False

    def set_screen_viewer_volume(self, value: int, *, show_hud: bool = False) -> int:
        vol = max(0, min(100, int(value)))
        if vol > 0:
            self._screen_viewer_volume = vol
            self._screen_viewer_prev_vol = vol
            self._screen_viewer_muted = False
        else:
            self._screen_viewer_muted = True
        self.screen_viewer_vol_slider.blockSignals(True)
        self.screen_viewer_vol_slider.setValue(vol)
        self.screen_viewer_vol_slider.blockSignals(False)
        if self._screen_player is not None:
            self._screen_player.set_volume(vol)
        self._sync_screen_share_icons()
        self._emit_screen_viewer_state()
        if show_hud and self._screen_viewer_active:
            self.screen_viewer_volume_hud_requested.emit(vol)
        return vol

    def toggle_screen_viewer_mute_from_hotkey(self, *, show_hud: bool = True) -> bool:
        if self._screen_viewer_muted or self.screen_viewer_vol_slider.value() <= 0:
            restore = max(10, int(self._screen_viewer_prev_vol or self._screen_viewer_volume or 80))
            self._screen_viewer_muted = False
            self._screen_viewer_volume = restore
            eff = restore
        else:
            self._screen_viewer_prev_vol = max(10, int(self._screen_viewer_volume or 80))
            self._screen_viewer_muted = True
            eff = 0
        self.screen_viewer_vol_slider.blockSignals(True)
        self.screen_viewer_vol_slider.setValue(eff)
        self.screen_viewer_vol_slider.blockSignals(False)
        if self._screen_player is not None:
            self._screen_player.set_volume(eff)
        self._sync_screen_share_icons()
        self._emit_screen_viewer_state()
        if show_hud and self._screen_viewer_active:
            self.screen_viewer_volume_hud_requested.emit(eff)
        return self._screen_viewer_muted

    def _on_screen_viewer_wheel(self, delta: int) -> None:
        if not self._screen_viewer_active:
            return
        cur = 0 if self._screen_viewer_muted else self._screen_viewer_volume
        self.set_screen_viewer_volume(cur + int(delta), show_hud=True)

    def _toggle_screen_viewer_mute(self) -> None:
        self.toggle_screen_viewer_mute_from_hotkey(show_hud=False)

    def _on_screen_viewer_vol_changed(self, value: int) -> None:
        self.set_screen_viewer_volume(value, show_hud=False)

    def _open_current_viewer_in_browser(self) -> None:
        if self._screen_viewer_watch_url:
            QDesktopServices.openUrl(QUrl(self._screen_viewer_watch_url))

    def stop_screen_share_session(self) -> None:
        """Detiene compartida y visor al desconectar Discord, manteniendo el servicio reutilizable."""
        self._remote_screen_shares.clear()
        self._dismissed_share_urls.clear()
        self._remote_screen_share = None
        if hasattr(self, "remote_share_banner"):
            self.remote_share_banner.setVisible(False)
        self._close_screen_share_viewer()
        self._mark_announced_share_ended()
        self._screen_share_rev = 0
        if self.svc.is_connected:
            self.svc.set_screen_share_presence(None)
        if hasattr(self, "screen_share_svc"):
            try:
                self.screen_share_svc.stop_share()
            except Exception:
                pass

    def shutdown_screen_share(self) -> None:
        self.stop_screen_share_session()
        if self._screen_player is not None:
            try:
                self._screen_player.shutdown()
            except Exception:
                pass
            self._screen_player = None
        if hasattr(self, "screen_share_svc"):
            try:
                self.screen_share_svc.close()
            except Exception:
                pass
