"""Panel dedicado de Chats privados con amigos agregados en WatchParty."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.watchparty_panel import _clear_layout_safe, _rounded_pixmap
from swiftplay.watchparty_service import MAX_WP_FILE_BYTES

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _format_file_size(num_bytes: int) -> str:
    n = max(0, int(num_bytes or 0))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024.0:.1f} KB"
    return f"{n / (1024.0 * 1024.0):.2f} MB"

_STATUS_SORT_ORDER = {"active": 0, "idle": 1, "offline": 2}
_STATUS_DOT_COLOR = {
    "active": "#4ADE80",
    "idle": "#FACC15",
    "offline": "#6B7280",
}


class _ChatFriendRow(QFrame):
    """Fila de amigo en la lista lateral de Chats."""

    clicked = Signal(str)

    def __init__(
        self,
        friend: dict[str, Any],
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None,
        *,
        preview: dict[str, Any] | None = None,
        unread_count: int = 0,
        active: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.friend_code = str(friend.get("code") or "").strip().upper()
        self._palette = palette
        self._thumb_cache = thumb_cache
        self._avatar_url = ""
        self._active = bool(active)
        self._unread_count = max(0, int(unread_count or 0))

        self.setObjectName("DiscordChannelRow")
        self.setProperty("clickable", "true")
        self.setProperty("active", "true" if self._active else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        self.avatar_lbl = QLabel()
        self.avatar_lbl.setFixedSize(36, 36)
        self.avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.avatar_lbl)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.dot_lbl = QLabel()
        self.dot_lbl.setFixedSize(8, 8)
        top.addWidget(self.dot_lbl)

        self.name_lbl = QLabel("")
        self.name_lbl.setStyleSheet("font-size: 13px; font-weight: 700; background: transparent;")
        top.addWidget(self.name_lbl, 1)

        self.time_lbl = QLabel("")
        self.time_lbl.setObjectName("MutedLabel")
        self.time_lbl.setStyleSheet("font-size: 10px; background: transparent;")
        top.addWidget(self.time_lbl)
        col.addLayout(top)

        bot = QHBoxLayout()
        bot.setContentsMargins(0, 0, 0, 0)
        bot.setSpacing(6)
        self.preview_lbl = QLabel("")
        self.preview_lbl.setObjectName("MutedLabel")
        self.preview_lbl.setStyleSheet("font-size: 11.5px; background: transparent;")
        bot.addWidget(self.preview_lbl, 1)

        self.unread_lbl = QLabel("0")
        self.unread_lbl.setObjectName("WatchPartyUnreadBadge")
        self.unread_lbl.setFixedHeight(18)
        self.unread_lbl.setMinimumWidth(18)
        self.unread_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unread_lbl.setVisible(False)
        bot.addWidget(self.unread_lbl)
        col.addLayout(bot)

        lay.addLayout(col, 1)
        self.update_data(
            friend,
            preview=preview,
            unread_count=self._unread_count,
            active=self._active,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.friend_code)
            event.accept()
            return
        super().mousePressEvent(event)

    def update_data(
        self,
        friend: dict[str, Any],
        *,
        preview: dict[str, Any] | None = None,
        unread_count: int = 0,
        active: bool = False,
    ) -> None:
        name = str(friend.get("name") or self.friend_code).strip() or self.friend_code
        status = str(friend.get("status") or "offline").lower()
        if status not in _STATUS_DOT_COLOR:
            status = "offline"
        dot_hex = _STATUS_DOT_COLOR[status]

        self.name_lbl.setText(name)
        pm = QPixmap(8, 8)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(dot_hex))
        p.drawEllipse(0, 0, 8, 8)
        p.end()
        self.dot_lbl.setPixmap(pm)

        av_url = str(friend.get("avatar_url") or "").strip()
        self._avatar_url = av_url
        av_sz = QSize(36, 36)
        pix = (
            self._thumb_cache.get_or_fetch(av_url, av_sz, circular=True)
            if (av_url and self._thumb_cache)
            else None
        )
        if pix is not None and not pix.isNull():
            self.avatar_lbl.setPixmap(_rounded_pixmap(pix, 36, 36, 18))
            self.avatar_lbl.setStyleSheet("background: transparent;")
        else:
            self.avatar_lbl.clear()
            self.avatar_lbl.setText((name[:1] or "?").upper())
            self.avatar_lbl.setStyleSheet(
                f"background-color: {self._palette.surface2}; color: {self._palette.text}; "
                f"border: 1px solid {dot_hex if status != 'offline' else self._palette.border}; "
                "border-radius: 18px; font-size: 13px; font-weight: 700;"
            )

        prev_txt = ""
        ts_txt = ""
        if isinstance(preview, dict):
            raw_c = str(preview.get("content") or "").strip().replace("\n", " ")
            att_name = str(preview.get("attachment_name") or "").strip()
            body_desc = raw_c or (f"📎 {att_name}" if att_name else "")
            if body_desc:
                prefix = "Tú: " if preview.get("is_self") else ""
                full = f"{prefix}{body_desc}"
                prev_txt = (full[:34] + "…") if len(full) > 35 else full
                ts_txt = str(preview.get("timestamp") or "").strip()
        if not prev_txt:
            prev_txt = "En línea" if status != "offline" else "Desconectado"
        self.preview_lbl.setText(prev_txt)
        self.time_lbl.setText(ts_txt)

        self.set_unread_count(unread_count)
        self.set_active(active)

    def handle_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url and url == self._avatar_url and not pix.isNull():
            self.avatar_lbl.setPixmap(_rounded_pixmap(pix, 36, 36, 18))
            self.avatar_lbl.setStyleSheet("background: transparent;")

    def set_unread_count(self, count: int) -> None:
        self._unread_count = max(0, int(count or 0))
        self.unread_lbl.setText("99+" if self._unread_count > 99 else str(self._unread_count))
        self.unread_lbl.setVisible(self._unread_count > 0)

    def set_active(self, active: bool) -> None:
        val = bool(active)
        if self._active == val and self.property("active") == ("true" if val else "false"):
            return
        self._active = val
        self.setProperty("active", "true" if val else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette


class WatchPartyChatPanel(QWidget):
    """Pestaña separada exclusiva para mensajes directos con amigos de WatchParty."""

    chat_opened = Signal(str)  # friend_code
    send_message_requested = Signal(str, str, str)  # friend_code, text, attachment_path
    open_watchparty_requested = Signal()
    reconnect_requested = Signal()
    copy_relay_code_requested = Signal()
    toast_requested = Signal(str)

    def __init__(
        self,
        cfg: AppConfig,
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.palette = palette
        self.thumb_cache = thumb_cache

        self._live_by_code: dict[str, dict[str, Any]] = {}
        self._friends: list[dict[str, Any]] = []
        self._previews: dict[str, dict[str, Any]] = {}
        self._unread_counts: dict[str, int] = {}
        self._rows: dict[str, _ChatFriendRow] = {}
        self._active_chat_code: str = ""
        self._pending_file_path: str = ""
        self._chat_rendered_ids: set[str] = set()
        self._msg_status_labels: dict[str, QLabel] = {}
        self._conn_state: str = "disconnected"
        self._conn_detail: str = ""
        self._server_supports_dm: bool | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        # Cabecera
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        title = QLabel("Chats")
        title.setObjectName("PageTitle")
        hdr.addWidget(title)

        self.status_pill = QLabel("Sin servidor")
        self.status_pill.setObjectName("DeckTag")
        hdr.addWidget(self.status_pill)

        sub = QLabel("Mensajes privados con amigos agregados en WatchParty")
        sub.setObjectName("MutedLabel")
        hdr.addWidget(sub)
        hdr.addStretch(1)

        self.open_wp_btn = QPushButton("Abrir WatchParty")
        self.open_wp_btn.setObjectName("PillBtn")
        self.open_wp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_wp_btn.clicked.connect(self.open_watchparty_requested.emit)
        hdr.addWidget(self.open_wp_btn)

        self.refresh_btn = QPushButton()
        self.refresh_btn.setObjectName("IconBtn")
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.setToolTip("Reconectar / actualizar estado")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.reconnect_requested.emit)
        hdr.addWidget(self.refresh_btn)
        root.addLayout(hdr)

        # Aviso si el Worker de Cloudflare aún corre una versión previa sin mensajería
        self.relay_warn_card = QFrame()
        self.relay_warn_card.setObjectName("WatchPartySubCard")
        self.relay_warn_card.setVisible(False)
        warn_lay = QHBoxLayout(self.relay_warn_card)
        warn_lay.setContentsMargins(12, 9, 12, 9)
        warn_lay.setSpacing(10)

        self.relay_warn_lbl = QLabel(
            "Tu servidor WatchParty en Cloudflare aún tiene la versión anterior y no soporta chats. "
            "Actualiza el Worker con `watchparty_relay/src/index.js` (`npx wrangler deploy` o pegando el código en Cloudflare)."
        )
        self.relay_warn_lbl.setWordWrap(True)
        self.relay_warn_lbl.setStyleSheet("font-size: 12px; background: transparent;")
        warn_lay.addWidget(self.relay_warn_lbl, 1)

        self.copy_worker_btn = QPushButton("Copiar código Worker")
        self.copy_worker_btn.setObjectName("PillBtn")
        self.copy_worker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_worker_btn.clicked.connect(self.copy_relay_code_requested.emit)
        warn_lay.addWidget(self.copy_worker_btn)

        self.warn_reconnect_btn = QPushButton("Reconectar")
        self.warn_reconnect_btn.setObjectName("PillBtn")
        self.warn_reconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.warn_reconnect_btn.clicked.connect(self.reconnect_requested.emit)
        warn_lay.addWidget(self.warn_reconnect_btn)
        root.addWidget(self.relay_warn_card)

        # Cuerpo principal de 2 columnas: lista de amigos | conversación
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)

        # Columna izquierda: amigos de WatchParty
        left_card = QFrame()
        left_card.setObjectName("DiscordSidebarCard")
        left_card.setFixedWidth(270)
        left_lay = QVBoxLayout(left_card)
        left_lay.setContentsMargins(10, 10, 10, 10)
        left_lay.setSpacing(8)

        self.friends_count_lbl = QLabel("Amigos (0)")
        self.friends_count_lbl.setObjectName("SectionHeader")
        left_lay.addWidget(self.friends_count_lbl)

        self.friend_filter_input = QLineEdit()
        self.friend_filter_input.setObjectName("DiscordChatInput")
        self.friend_filter_input.setPlaceholderText("Buscar amigo...")
        self.friend_filter_input.textChanged.connect(lambda _t: self._render_friends_list())
        left_lay.addWidget(self.friend_filter_input)

        self.friends_scroll = QScrollArea()
        self.friends_scroll.setObjectName("TransparentScroll")
        self.friends_scroll.setWidgetResizable(True)
        self.friends_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.friends_scroll.viewport().setObjectName("TransparentBox")
        self.friends_scroll.viewport().setAutoFillBackground(False)

        friends_container = QWidget()
        friends_container.setObjectName("TransparentBox")
        self._friends_lay = QVBoxLayout(friends_container)
        self._friends_lay.setContentsMargins(0, 0, 0, 0)
        self._friends_lay.setSpacing(6)

        self.no_friends_box = QWidget()
        self.no_friends_box.setObjectName("TransparentBox")
        nf_lay = QVBoxLayout(self.no_friends_box)
        nf_lay.setContentsMargins(8, 22, 8, 12)
        nf_lay.setSpacing(10)
        self.no_friends_lbl = QLabel(
            "Todavía no agregaste amigos en WatchParty.\n"
            "Agrega el código de tu amigo en WatchParty para chatear."
        )
        self.no_friends_lbl.setObjectName("MutedLabel")
        self.no_friends_lbl.setWordWrap(True)
        self.no_friends_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nf_lay.addWidget(self.no_friends_lbl)
        self.add_friends_cta_btn = QPushButton("Ir a WatchParty")
        self.add_friends_cta_btn.setObjectName("PillBtn")
        self.add_friends_cta_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_friends_cta_btn.clicked.connect(self.open_watchparty_requested.emit)
        nf_lay.addWidget(self.add_friends_cta_btn, 0, Qt.AlignmentFlag.AlignCenter)
        self._friends_lay.addWidget(self.no_friends_box)
        self._friends_lay.addStretch(1)

        self.friends_scroll.setWidget(friends_container)
        left_lay.addWidget(self.friends_scroll, 1)
        body.addWidget(left_card)

        # Columna derecha: conversación
        self.chat_card = QFrame()
        self.chat_card.setObjectName("DiscordChatCard")
        chat_lay = QVBoxLayout(self.chat_card)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        self.select_friend_lbl = QLabel(
            "Selecciona un amigo de WatchParty a la izquierda para empezar a chatear."
        )
        self.select_friend_lbl.setObjectName("MutedLabel")
        self.select_friend_lbl.setWordWrap(True)
        self.select_friend_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.select_friend_lbl.setContentsMargins(24, 48, 24, 48)
        chat_lay.addWidget(self.select_friend_lbl, 1)

        self.chat_content_box = QWidget()
        self.chat_content_box.setObjectName("TransparentBox")
        self.chat_content_box.setVisible(False)
        cc_lay = QVBoxLayout(self.chat_content_box)
        cc_lay.setContentsMargins(0, 0, 0, 0)
        cc_lay.setSpacing(0)

        chat_hdr = QFrame()
        chat_hdr.setObjectName("DiscordChatHeader")
        ch_lay = QHBoxLayout(chat_hdr)
        ch_lay.setContentsMargins(14, 10, 14, 10)
        ch_lay.setSpacing(8)

        self.chat_status_dot = QLabel()
        self.chat_status_dot.setFixedSize(10, 10)
        ch_lay.addWidget(self.chat_status_dot)

        hdr_meta = QVBoxLayout()
        hdr_meta.setContentsMargins(0, 0, 0, 0)
        hdr_meta.setSpacing(1)
        self.chat_title_lbl = QLabel("Chat")
        self.chat_title_lbl.setStyleSheet("font-size: 13.5px; font-weight: 700; background: transparent;")
        hdr_meta.addWidget(self.chat_title_lbl)
        self.chat_sub_lbl = QLabel("")
        self.chat_sub_lbl.setObjectName("MutedLabel")
        self.chat_sub_lbl.setStyleSheet("font-size: 11px; background: transparent;")
        hdr_meta.addWidget(self.chat_sub_lbl)
        ch_lay.addLayout(hdr_meta, 1)

        self.chat_close_btn = QPushButton()
        self.chat_close_btn.setObjectName("IconBtn")
        self.chat_close_btn.setFixedSize(26, 26)
        self.chat_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_close_btn.setToolTip("Cerrar conversación")
        self.chat_close_btn.clicked.connect(self.close_chat)
        ch_lay.addWidget(self.chat_close_btn)
        cc_lay.addWidget(chat_hdr)

        self.chat_hint_lbl = QLabel("")
        self.chat_hint_lbl.setObjectName("MutedLabel")
        self.chat_hint_lbl.setWordWrap(True)
        self.chat_hint_lbl.setContentsMargins(14, 8, 14, 4)
        self.chat_hint_lbl.setStyleSheet("font-size: 11px; background: transparent;")
        self.chat_hint_lbl.setVisible(False)
        cc_lay.addWidget(self.chat_hint_lbl)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("TransparentScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_scroll.viewport().setObjectName("TransparentBox")
        self.chat_scroll.viewport().setAutoFillBackground(False)

        msgs_box = QWidget()
        msgs_box.setObjectName("TransparentBox")
        self._chat_msgs_lay = QVBoxLayout(msgs_box)
        self._chat_msgs_lay.setContentsMargins(14, 12, 14, 12)
        self._chat_msgs_lay.setSpacing(8)

        self.chat_empty_lbl = QLabel(
            "Todavía no hay mensajes con este amigo.\n"
            "Escríbele abajo para conversar."
        )
        self.chat_empty_lbl.setObjectName("MutedLabel")
        self.chat_empty_lbl.setWordWrap(True)
        self.chat_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_empty_lbl.setContentsMargins(12, 24, 12, 24)
        self._chat_msgs_lay.addWidget(self.chat_empty_lbl)
        self._chat_msgs_lay.addStretch(1)

        self.chat_scroll.setWidget(msgs_box)
        cc_lay.addWidget(self.chat_scroll, 1)

        self.pending_file_bar = QFrame()
        self.pending_file_bar.setObjectName("WatchPartySubCard")
        self.pending_file_bar.setVisible(False)
        pf_lay = QHBoxLayout(self.pending_file_bar)
        pf_lay.setContentsMargins(10, 6, 10, 6)
        pf_lay.setSpacing(8)
        self.pending_file_lbl = QLabel("")
        self.pending_file_lbl.setStyleSheet("font-size: 12px; background: transparent;")
        pf_lay.addWidget(self.pending_file_lbl, 1)
        self.pending_file_clear_btn = QPushButton()
        self.pending_file_clear_btn.setObjectName("IconBtn")
        self.pending_file_clear_btn.setFixedSize(22, 22)
        self.pending_file_clear_btn.setToolTip("Quitar archivo adjunto")
        self.pending_file_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pending_file_clear_btn.clicked.connect(self._clear_pending_file)
        pf_lay.addWidget(self.pending_file_clear_btn)
        cc_lay.addWidget(self.pending_file_bar)

        input_wrap = QWidget()
        input_wrap.setObjectName("TransparentBox")
        in_lay = QHBoxLayout(input_wrap)
        in_lay.setContentsMargins(12, 8, 12, 12)
        in_lay.setSpacing(8)

        self.chat_attach_btn = QPushButton()
        self.chat_attach_btn.setObjectName("IconBtn")
        self.chat_attach_btn.setFixedSize(32, 32)
        self.chat_attach_btn.setToolTip("Adjuntar archivo (hasta 8 MB; máx. 80 KB si está desconectado)")
        self.chat_attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_attach_btn.clicked.connect(self._on_attach_clicked)
        in_lay.addWidget(self.chat_attach_btn)

        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("DiscordChatInput")
        self.chat_input.setPlaceholderText("Escribe un mensaje o adjunta un archivo...")
        self.chat_input.setMaxLength(1000)
        self.chat_input.returnPressed.connect(self._on_send_clicked)
        in_lay.addWidget(self.chat_input, 1)

        self.chat_send_btn = QPushButton(" Enviar")
        self.chat_send_btn.setObjectName("PillBtn")
        self.chat_send_btn.setFixedHeight(32)
        self.chat_send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_send_btn.clicked.connect(self._on_send_clicked)
        in_lay.addWidget(self.chat_send_btn)
        cc_lay.addWidget(input_wrap)

        chat_lay.addWidget(self.chat_content_box, 1)
        body.addWidget(self.chat_card, 1)
        root.addLayout(body, 1)

        if self.thumb_cache is not None:
            self.thumb_cache.ready.connect(self._on_thumb_ready)

        self.update_theme(palette)
        self.set_friends_data([], previews={})

    @property
    def active_chat_code(self) -> str:
        return self._active_chat_code

    def is_viewing_chat(self, friend_code: str) -> bool:
        fc = str(friend_code or "").strip().upper()
        return bool(fc and self.chat_content_box.isVisible() and self._active_chat_code == fc)

    def _on_thumb_ready(self, key: str, pix: QPixmap) -> None:
        for row in self._rows.values():
            row.handle_thumb_ready(key, pix)

    def set_connection_state(
        self,
        state: str,
        detail: str = "",
        server_supports_dm: bool | None = None,
    ) -> None:
        self._conn_state = str(state or "disconnected")
        self._conn_detail = str(detail or "")
        if server_supports_dm is not None or self._conn_state != "connected":
            self._server_supports_dm = server_supports_dm if self._conn_state == "connected" else None

        if not self.cfg.watchparty_enabled:
            self.status_pill.setText("Desactivado")
            self.status_pill.setStyleSheet(
                f"color: {self.palette.text2}; background: {self.palette.surface2};"
            )
        elif not self.cfg.watchparty_relay_url:
            self.status_pill.setText("Sin servidor")
            self.status_pill.setStyleSheet(
                f"color: {self.palette.text2}; background: {self.palette.surface2};"
            )
        elif self._conn_state == "connected":
            if self._server_supports_dm is False:
                self.status_pill.setText("Servidor desactualizado")
                self.status_pill.setStyleSheet(
                    f"color: #FACC15; background: {self.palette.surface2};"
                )
            else:
                self.status_pill.setText("● En línea")
                self.status_pill.setStyleSheet(
                    f"color: #4ADE80; background: {self.palette.surface2};"
                )
        elif self._conn_state == "connecting":
            self.status_pill.setText("Conectando...")
            self.status_pill.setStyleSheet(
                f"color: #FACC15; background: {self.palette.surface2};"
            )
        else:
            txt = f"Sin conexión ({self._conn_detail})" if self._conn_detail else "Sin conexión"
            self.status_pill.setText(txt[:44])
            self.status_pill.setStyleSheet(
                f"color: #F87171; background: {self.palette.surface2};"
            )

        show_warn = bool(self._conn_state == "connected" and self._server_supports_dm is False)
        self.relay_warn_card.setVisible(show_warn)

        if self._active_chat_code:
            f_row = next(
                (f for f in self._friends if str(f.get("code") or "").upper() == self._active_chat_code),
                None,
            )
            name = str(f_row.get("name") or self._active_chat_code) if f_row else self._active_chat_code
            online = bool(f_row and str(f_row.get("status") or "offline").lower() != "offline")
            self.sync_chat_peer_status(name, online=online, connected=(self._conn_state == "connected"))

    def set_friends_data(
        self,
        friends: list[dict[str, Any]],
        *,
        previews: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if previews is not None:
            self._previews = {str(k).upper(): dict(v) for k, v in previews.items()}
        for row in friends or []:
            if isinstance(row, dict) and row.get("code"):
                self._live_by_code[str(row["code"]).strip().upper()] = dict(row)

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in self.cfg.watchparty_friends or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            live = self._live_by_code.get(code)
            if live:
                merged.append(
                    {
                        "code": code,
                        "name": str(live.get("name") or entry.get("name") or code),
                        "avatar_url": str(live.get("avatar_url") or entry.get("avatar_url") or ""),
                        "status": str(live.get("status") or "offline"),
                    }
                )
            else:
                merged.append(
                    {
                        "code": code,
                        "name": str(entry.get("name") or code),
                        "avatar_url": str(entry.get("avatar_url") or ""),
                        "status": "offline",
                    }
                )

        self._friends = sorted(
            merged,
            key=lambda it: (
                0 if self._unread_counts.get(it["code"], 0) > 0 else 1,
                _STATUS_SORT_ORDER.get(str(it.get("status") or "offline"), 2),
                -float((self._previews.get(it["code"]) or {}).get("created_ts") or 0.0),
                str(it.get("name") or it["code"]).lower(),
            ),
        )
        self.friends_count_lbl.setText(f"Amigos ({len(self._friends)})")
        if self._active_chat_code and self._active_chat_code not in seen:
            self.close_chat()
        self._render_friends_list()

    def set_previews(self, previews: dict[str, dict[str, Any]]) -> None:
        self._previews = {str(k).upper(): dict(v) for k, v in (previews or {}).items()}
        self.set_friends_data([])

    def set_unread_counts(self, counts: dict[str, int]) -> None:
        clean: dict[str, int] = {}
        for k, v in (counts or {}).items():
            c = str(k or "").strip().upper()
            n = max(0, int(v or 0))
            if c and n > 0:
                clean[c] = n
        self._unread_counts = clean
        for c, row in self._rows.items():
            row.set_unread_count(clean.get(c, 0))

    def _clear_friend_rows(self) -> None:
        for c in list(self._rows.keys()):
            w = self._rows.pop(c)
            self._friends_lay.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()

    def _render_friends_list(self) -> None:
        filt = str(self.friend_filter_input.text() or "").strip().lower()
        visible = [
            f
            for f in self._friends
            if not filt
            or filt in str(f.get("name") or "").lower()
            or filt in str(f.get("code") or "").lower()
        ]
        self.no_friends_box.setVisible(len(self._friends) == 0)

        new_order = [f["code"] for f in visible]
        if new_order == list(self._rows.keys()):
            for f in visible:
                c = f["code"]
                self._rows[c].update_data(
                    f,
                    preview=self._previews.get(c),
                    unread_count=self._unread_counts.get(c, 0),
                    active=(c == self._active_chat_code),
                )
            return

        self._clear_friend_rows()
        insert_idx = 1
        for f in visible:
            c = f["code"]
            row = _ChatFriendRow(
                f,
                self.palette,
                self.thumb_cache,
                preview=self._previews.get(c),
                unread_count=self._unread_counts.get(c, 0),
                active=(c == self._active_chat_code),
                parent=self,
            )
            row.clicked.connect(self.chat_opened.emit)
            self._rows[c] = row
            self._friends_lay.insertWidget(insert_idx, row)
            insert_idx += 1

    def _clear_chat_bubbles(self) -> None:
        self._chat_rendered_ids.clear()
        self._msg_status_labels.clear()
        for i in range(self._chat_msgs_lay.count() - 1, -1, -1):
            item = self._chat_msgs_lay.itemAt(i)
            w = item.widget() if item is not None else None
            if w is not None and w is not self.chat_empty_lbl:
                self._chat_msgs_lay.takeAt(i)
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _scroll_chat_to_bottom(self) -> None:
        sb = self.chat_scroll.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _create_chat_bubble(
        self, msg: dict[str, Any], delivery_status: str = ""
    ) -> tuple[QWidget, QLabel | None]:
        is_self = bool(msg.get("is_self", False))
        sender_name = "Tú" if is_self else str(msg.get("sender_name") or msg.get("from_name") or "Amigo")
        ts = str(msg.get("timestamp") or "").strip()
        content = str(msg.get("content") or msg.get("text") or "").strip()

        row = QWidget()
        row.setObjectName("TransparentBox")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(6)
        if is_self:
            row_lay.addStretch(1)

        bubble = QFrame()
        bubble.setObjectName("WatchPartyMsgSelf" if is_self else "WatchPartyMsgPeer")
        bubble.setMaximumWidth(420)
        bubble_lay = QVBoxLayout(bubble)
        bubble_lay.setContentsMargins(11, 7, 11, 7)
        bubble_lay.setSpacing(3)

        meta_lay = QHBoxLayout()
        meta_lay.setContentsMargins(0, 0, 0, 0)
        meta_lay.setSpacing(8)
        author_lbl = QLabel(sender_name)
        author_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {self.palette.accent if is_self else self.palette.text}; "
            "background: transparent;"
        )
        meta_lay.addWidget(author_lbl)
        meta_lay.addStretch(1)
        if ts:
            ts_lbl = QLabel(ts)
            ts_lbl.setObjectName("MutedLabel")
            ts_lbl.setStyleSheet("font-size: 10px; background: transparent;")
            meta_lay.addWidget(ts_lbl)
        bubble_lay.addLayout(meta_lay)

        att = msg.get("attachment") if isinstance(msg.get("attachment"), dict) else None
        if att and str(att.get("name") or "").strip():
            att_name = str(att.get("name") or "archivo").strip()
            att_size = int(att.get("size") or 0)
            local_path = str(att.get("local_path") or "").strip()
            lp_obj = Path(local_path) if local_path else None
            exists_disk = bool(lp_obj is not None and lp_obj.is_file())

            if exists_disk and lp_obj is not None and lp_obj.suffix.lower() in _IMAGE_EXTS:
                pm = QPixmap(str(lp_obj))
                if not pm.isNull():
                    scaled = pm.scaled(
                        240,
                        160,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    img_lbl = QLabel()
                    img_lbl.setPixmap(scaled)
                    img_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                    img_lbl.setStyleSheet("background: transparent;")
                    bubble_lay.addWidget(img_lbl)

            att_box = QFrame()
            att_box.setObjectName("WatchPartySubCard")
            att_lay = QHBoxLayout(att_box)
            att_lay.setContentsMargins(8, 6, 8, 6)
            att_lay.setSpacing(8)

            att_meta = QVBoxLayout()
            att_meta.setContentsMargins(0, 0, 0, 0)
            att_meta.setSpacing(1)
            att_title = QLabel(att_name)
            att_title.setStyleSheet("font-size: 12px; font-weight: 600; background: transparent;")
            att_meta.addWidget(att_title)
            sub_txt = _format_file_size(att_size)
            if not exists_disk:
                sub_txt = f"{sub_txt} · no disponible en este equipo" if att_size > 0 else "No disponible en disco"
            att_sub = QLabel(sub_txt)
            att_sub.setObjectName("MutedLabel")
            att_sub.setStyleSheet("font-size: 10.5px; background: transparent;")
            att_meta.addWidget(att_sub)
            att_lay.addLayout(att_meta, 1)

            open_btn = QPushButton("Abrir")
            open_btn.setObjectName("PillBtn")
            open_btn.setEnabled(exists_disk)
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.clicked.connect(lambda _c=False, p_str=local_path: self._open_local_file(p_str))
            att_lay.addWidget(open_btn)

            if exists_disk:
                dir_btn = QPushButton("Carpeta")
                dir_btn.setObjectName("PillBtn")
                dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                dir_btn.clicked.connect(lambda _c=False, p_str=local_path: self._reveal_local_file(p_str))
                att_lay.addWidget(dir_btn)

            bubble_lay.addWidget(att_box)

        if content:
            body_lbl = QLabel(content)
            body_lbl.setWordWrap(True)
            body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body_lbl.setStyleSheet("font-size: 12.5px; background: transparent;")
            bubble_lay.addWidget(body_lbl)

        status_lbl: QLabel | None = None
        if is_self:
            status_lbl = QLabel(delivery_status)
            status_lbl.setObjectName("MutedLabel")
            status_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            status_lbl.setStyleSheet("font-size: 10px; background: transparent;")
            status_lbl.setVisible(bool(delivery_status))
            bubble_lay.addWidget(status_lbl)

        row_lay.addWidget(bubble)
        if not is_self:
            row_lay.addStretch(1)
        return row, status_lbl

    @staticmethod
    def _open_local_file(file_path: str) -> None:
        p = Path(str(file_path or "").strip())
        if p.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    @staticmethod
    def _reveal_local_file(file_path: str) -> None:
        p = Path(str(file_path or "").strip())
        if not p.exists():
            return
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer", "/select,", str(p)])
                return
            except Exception:
                pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

    def sync_chat_peer_status(self, friend_name: str, online: bool, connected: bool) -> None:
        if not self._active_chat_code:
            return
        name = str(friend_name or self._active_chat_code).strip() or self._active_chat_code
        self.chat_title_lbl.setText(name)
        dot_hex = "#4ADE80" if online else "#6B7280"
        pm = QPixmap(10, 10)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(dot_hex))
        painter.drawEllipse(1, 1, 8, 8)
        painter.end()
        self.chat_status_dot.setPixmap(pm)

        status_txt = "En línea" if online else "Desconectado"
        self.chat_sub_lbl.setText(f"{self._active_chat_code} · {status_txt}")
        if not self.cfg.watchparty_relay_url or not connected:
            self.chat_hint_lbl.setText(
                "Sin conexión al servidor WatchParty: conecta para enviar o recibir mensajes."
            )
            self.chat_hint_lbl.setVisible(True)
        elif not online:
            self.chat_hint_lbl.setText(
                "Tu amigo está desconectado: se guardará en buzón hasta que se conecte."
            )
            self.chat_hint_lbl.setVisible(True)
        else:
            self.chat_hint_lbl.setVisible(False)
        self.chat_hint_lbl.setVisible(self.chat_hint_lbl.isVisible())
        self.chat_send_btn.setEnabled(bool(connected))
        self.chat_attach_btn.setEnabled(bool(connected))

    def open_chat_for(
        self,
        friend_code: str,
        messages: list[dict[str, Any]],
        *,
        friend_name: str = "",
        online: bool = False,
        connected: bool = True,
    ) -> None:
        fc = str(friend_code or "").strip().upper()
        if not fc:
            return
        self._active_chat_code = fc
        self._clear_pending_file()
        self._clear_chat_bubbles()
        for c, row in self._rows.items():
            row.set_active(c == fc)
        self.sync_chat_peer_status(friend_name or fc, online=online, connected=connected)
        for m in messages or []:
            if isinstance(m, dict):
                self._insert_chat_bubble(m)
        self.chat_empty_lbl.setVisible(len(self._chat_rendered_ids) == 0)
        self.select_friend_lbl.setVisible(False)
        self.chat_content_box.setVisible(True)
        self.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

    def _insert_chat_bubble(
        self, msg: dict[str, Any], delivery_status: str = ""
    ) -> None:
        mid = str(msg.get("id") or "").strip()
        if mid and mid in self._chat_rendered_ids:
            return
        if mid:
            self._chat_rendered_ids.add(mid)
        else:
            self._chat_rendered_ids.add(f"_anon_{self._chat_msgs_lay.count()}")
        bubble, status_lbl = self._create_chat_bubble(msg, delivery_status=delivery_status)
        if mid and status_lbl is not None:
            self._msg_status_labels[mid] = status_lbl
        insert_pos = max(1, self._chat_msgs_lay.count() - 1)
        self._chat_msgs_lay.insertWidget(insert_pos, bubble)

    def append_chat_message(
        self,
        friend_code: str,
        msg: dict[str, Any],
        *,
        delivery_status: str = "",
    ) -> None:
        fc = str(friend_code or "").strip().upper()
        if not fc or fc != self._active_chat_code or not self.chat_content_box.isVisible():
            return
        self._insert_chat_bubble(msg, delivery_status=delivery_status)
        self.chat_empty_lbl.setVisible(len(self._chat_rendered_ids) == 0)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

    def update_message_status(
        self, msg_id: str, status_text: str, *, is_error: bool = False
    ) -> None:
        mid = str(msg_id or "").strip()
        lbl = self._msg_status_labels.get(mid)
        if lbl is None:
            return
        lbl.setText(str(status_text or ""))
        lbl.setVisible(bool(status_text))
        if is_error:
            lbl.setStyleSheet("font-size: 10px; color: #F87171; background: transparent;")
        else:
            lbl.setStyleSheet(
                f"font-size: 10px; color: {self.palette.text2}; background: transparent;"
            )

    def close_chat(self) -> None:
        self._active_chat_code = ""
        self._clear_pending_file()
        self._clear_chat_bubbles()
        self.chat_content_box.setVisible(False)
        self.select_friend_lbl.setVisible(True)
        for row in self._rows.values():
            row.set_active(False)

    def _clear_pending_file(self) -> None:
        self._pending_file_path = ""
        self.pending_file_lbl.setText("")
        self.pending_file_bar.setVisible(False)

    def set_pending_file(self, file_path: str) -> bool:
        p = Path(str(file_path or "").strip())
        if not p.is_file():
            self.toast_requested.emit("El archivo seleccionado no existe.")
            return False
        try:
            size = int(p.stat().st_size)
        except Exception:
            size = 0
        if size <= 0:
            self.toast_requested.emit("El archivo está vacío.")
            return False
        if size > MAX_WP_FILE_BYTES:
            self.toast_requested.emit("El archivo supera el límite permitido de 8 MB.")
            return False
        self._pending_file_path = str(p)
        self.pending_file_lbl.setText(f"📎 {p.name} ({_format_file_size(size)})")
        self.pending_file_bar.setVisible(True)
        return True

    def _on_attach_clicked(self) -> None:
        if not self._active_chat_code:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleccionar archivo para enviar (máx. 8 MB)",
            "",
            "Todos los archivos (*.*)",
        )
        if file_path and self.set_pending_file(file_path):
            self.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_send_clicked(self) -> None:
        if not self._active_chat_code:
            return
        txt = str(self.chat_input.text() or "").strip()
        att_path = str(self._pending_file_path or "").strip()
        if not txt and not att_path:
            return
        self.chat_input.clear()
        self._clear_pending_file()
        self.send_message_requested.emit(self._active_chat_code, txt, att_path)

    def update_theme(self, palette: ThemePalette) -> None:
        self.palette = palette
        self.refresh_btn.setIcon(render_icon("refresh", palette.text, 14))
        self.chat_close_btn.setIcon(render_icon("close", palette.text2, 13))
        self.chat_send_btn.setIcon(render_icon("send", palette.text, 14))
        self.chat_attach_btn.setIcon(render_icon("paperclip", palette.text, 15))
        self.pending_file_clear_btn.setIcon(render_icon("close", palette.text2, 12))
        for row in self._rows.values():
            row.update_theme(palette)
        self.set_connection_state(
            self._conn_state,
            self._conn_detail,
            server_supports_dm=self._server_supports_dm,
        )
