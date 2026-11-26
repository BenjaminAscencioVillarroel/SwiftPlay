"""Panel nativo de WatchParty: amigos en vivo, sub-cards YouTube/Twitch/Music y control cruzado."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig, normalize_watchparty_relay_url, save_config
from swiftplay.models import format_duration
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.watchparty_service import compute_live_position

_STATUS_SORT_ORDER = {"active": 0, "idle": 1, "offline": 2}
_STATUS_META: dict[str, tuple[str, str]] = {
    "active": ("#4ADE80", "Activo"),
    "idle": ("#FACC15", "Inactivo"),
    "offline": ("#6B7280", "Desconectado"),
}


def _clear_layout_safe(lay: QVBoxLayout | QHBoxLayout) -> None:
    """Limpia widgets hijos sin ventanas fantasma (hide + setParent(None) + deleteLater)."""
    while lay.count() > 0:
        item = lay.takeAt(0)
        if item is None:
            continue
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        sub = item.layout()
        if sub is not None and isinstance(sub, (QVBoxLayout, QHBoxLayout)):
            _clear_layout_safe(sub)


def _rounded_pixmap(pix: QPixmap, width: int, height: int, radius: int) -> QPixmap:
    if pix.isNull():
        return QPixmap()
    out = QPixmap(width, height)
    out.fill(Qt.GlobalColor.transparent)
    scaled = pix.scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    sx = max(0, (scaled.width() - width) // 2)
    sy = max(0, (scaled.height() - height) // 2)
    cropped = scaled.copy(sx, sy, width, height)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(0.0, 0.0, float(width), float(height), float(radius), float(radius))
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return out


class _FriendHeaderRow(QWidget):
    """Cabecera clicable de card de amigo para expandir o colapsar sus 3 sub-cards."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _WatchPartySubCard(QFrame):
    """Sub-card de actividad individual (YouTube, Twitch o YT Music) dentro de un amigo."""

    action_clicked = Signal(str)  # "video" | "music"

    def __init__(
        self,
        *,
        slot_kind: str,  # "youtube" | "twitch" | "music"
        deck: str,
        title_badge: str,
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.slot_kind = slot_kind
        self.deck = deck
        self._palette = palette
        self._thumb_cache = thumb_cache
        self._state: dict[str, Any] | None = None
        self._thumb_url = ""
        self._thumb_key = ""
        self._joined = False
        self._friend_online = False

        self.setObjectName("WatchPartySubCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.ico_lbl = QLabel()
        self.ico_lbl.setFixedSize(16, 16)
        top.addWidget(self.ico_lbl)

        self.badge_lbl = QLabel(title_badge)
        self.badge_lbl.setStyleSheet("font-size: 12px; font-weight: 700; background: transparent;")
        top.addWidget(self.badge_lbl)
        top.addStretch(1)

        self.live_lbl = QLabel("EN VIVO")
        self.live_lbl.setObjectName("ScreenShareLiveBadge")
        self.live_lbl.setVisible(False)
        top.addWidget(self.live_lbl)
        lay.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(10)
        self.thumb_lbl = QLabel()
        self.thumb_lbl.setFixedSize(76, 46)
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mid.addWidget(self.thumb_lbl)

        meta = QVBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(2)
        self.title_lbl = QLabel("Sin actividad")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setMaximumHeight(36)
        self.title_lbl.setStyleSheet("font-size: 12px; font-weight: 600; background: transparent;")
        meta.addWidget(self.title_lbl)

        self.sub_lbl = QLabel("")
        self.sub_lbl.setObjectName("MutedLabel")
        self.sub_lbl.setStyleSheet("font-size: 11px; background: transparent;")
        meta.addWidget(self.sub_lbl)
        mid.addLayout(meta, 1)
        lay.addLayout(mid)

        bot = QHBoxLayout()
        bot.setSpacing(8)
        self.time_lbl = QLabel("")
        self.time_lbl.setObjectName("MutedLabel")
        self.time_lbl.setStyleSheet("font-size: 11px; background: transparent;")
        bot.addWidget(self.time_lbl, 1)

        self.join_btn = QPushButton("Unirse")
        self.join_btn.setObjectName("PillBtn")
        self.join_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.join_btn.clicked.connect(lambda: self.action_clicked.emit(self.deck))
        bot.addWidget(self.join_btn)
        lay.addLayout(bot)

        self.update_theme(palette)

    def handle_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url and url == self._thumb_url and not pix.isNull():
            self.thumb_lbl.setPixmap(_rounded_pixmap(pix, 76, 46, 7))

    def set_data(
        self,
        *,
        friend_online: bool,
        state: dict[str, Any] | None,
        joined_this_deck: bool,
    ) -> None:
        self._friend_online = bool(friend_online)
        self._state = dict(state) if isinstance(state, dict) else None
        self._joined = bool(joined_this_deck)
        has_media = self._state is not None and bool(
            self._state.get("video_id") or self._state.get("title")
        )

        self.live_lbl.setVisible(self.slot_kind == "twitch" and has_media)

        if not has_media:
            empty_msg = {
                "youtube": "Sin video en YouTube",
                "twitch": "Sin directo en Twitch",
                "music": "Sin música sonando",
            }[self.slot_kind]
            self.title_lbl.setText(empty_msg)
            self.sub_lbl.setText("Desconectado" if not friend_online else "Nada reproduciendo")
            self.time_lbl.setText("")
            self._thumb_url = ""
            self._thumb_key = ""
            self.thumb_lbl.clear()
            self.thumb_lbl.setText("—")
            self.join_btn.setVisible(friend_online and joined_this_deck)
            self.join_btn.setText("Salir")
            self.join_btn.setProperty("active", "true" if joined_this_deck else "false")
            self.join_btn.style().unpolish(self.join_btn)
            self.join_btn.style().polish(self.join_btn)
            return

        title = str(self._state.get("title") or "Sin título")
        author = str(self._state.get("author") or "")
        game = str(self._state.get("game_name") or "")
        self.title_lbl.setText(title)
        if self.slot_kind == "twitch":
            sub = f"{author} · {game}" if game and author else (author or game or "Directo en Twitch")
        else:
            sub = author or ("YouTube" if self.slot_kind == "youtube" else "YT Music")
        self.sub_lbl.setText(sub)

        thumb = str(self._state.get("thumb_url") or "").strip()
        if thumb != self._thumb_url:
            self._thumb_url = thumb
            self._thumb_key = ""
            self.thumb_lbl.clear()
            if thumb and self._thumb_cache is not None:
                sz = QSize(76, 46)
                self._thumb_key = self._thumb_cache._make_key(thumb, sz, False, radius=7)
                pix = self._thumb_cache.get_or_fetch(thumb, sz, circular=False, radius=7)
                if pix is not None and not pix.isNull():
                    self.thumb_lbl.setPixmap(pix)

        self.join_btn.setVisible(friend_online)
        if joined_this_deck:
            self.join_btn.setText("Salir de sala")
        elif self.slot_kind == "twitch":
            self.join_btn.setText("Ver juntos")
        elif self.slot_kind == "music":
            self.join_btn.setText("Escuchar juntos")
        else:
            self.join_btn.setText("Unirse")
        self.join_btn.setProperty("active", "true" if joined_this_deck else "false")
        self.join_btn.style().unpolish(self.join_btn)
        self.join_btn.style().polish(self.join_btn)
        self.refresh_live_time()

    def refresh_live_time(self) -> None:
        if not self._state:
            return
        if self.slot_kind == "twitch":
            vc = int(self._state.get("viewer_count") or 0)
            paused = bool(self._state.get("paused"))
            if paused:
                self.time_lbl.setText("En pausa")
            elif vc > 0:
                self.time_lbl.setText(f"En vivo · {vc:,} espectadores".replace(",", "."))
            else:
                self.time_lbl.setText("En vivo ahora")
            return
        pos = compute_live_position(self._state)
        dur = float(self._state.get("duration_sec") or 0.0)
        pos_txt = format_duration(pos) or "0:00"
        dur_txt = format_duration(dur) if dur > 0 else ""
        prefix = "Pausado" if self._state.get("paused") else "Reproduciendo"
        self.time_lbl.setText(
            f"{prefix} · {pos_txt} / {dur_txt}" if dur_txt else f"{prefix} · {pos_txt}"
        )

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        ico_name = {"youtube": "video", "twitch": "live", "music": "music"}.get(
            self.slot_kind, "video"
        )
        ico_color = palette.accent if self.slot_kind != "twitch" else "#A855F7"
        self.ico_lbl.setPixmap(render_icon(ico_name, ico_color, 15).pixmap(15, 15))
        self.thumb_lbl.setStyleSheet(
            f"background-color: {palette.surface1}; color: {palette.text2}; "
            f"border: 1px solid {palette.border}; border-radius: 7px; font-size: 11px;"
        )


class _WatchPartyFriendCard(QFrame):
    """Card expandible por amigo: muestra estado + resumen y al abrir revela YT / Twitch / Music."""

    toggle_expanded_requested = Signal(str, bool)
    chat_requested = Signal(str)  # friend_code
    join_requested = Signal(str, str)  # host_code, deck
    leave_requested = Signal(str)  # deck
    remove_requested = Signal(str)  # friend_code

    def __init__(
        self,
        friend: dict[str, Any],
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None,
        *,
        expanded: bool = False,
        joined_video_host: str = "",
        joined_music_host: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.friend_code = str(friend.get("code") or "").upper()
        self._friend = dict(friend)
        self._palette = palette
        self._thumb_cache = thumb_cache
        self._avatar_url = ""
        self._avatar_key = ""
        self._expanded = bool(expanded)
        self._unread_count = 0
        self._chat_active = False

        self.setObjectName("WatchPartyFriendCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.header_row = _FriendHeaderRow()
        self.header_row.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_row.clicked.connect(self._on_header_clicked)
        hdr = QHBoxLayout(self.header_row)
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(10)

        self.avatar_lbl = QLabel()
        self.avatar_lbl.setFixedSize(40, 40)
        self.avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr.addWidget(self.avatar_lbl)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(10, 10)
        name_row.addWidget(self.status_dot)

        self.name_lbl = QLabel(str(friend.get("name") or self.friend_code))
        self.name_lbl.setStyleSheet("font-size: 14px; font-weight: 700; background: transparent;")
        name_row.addWidget(self.name_lbl)

        self.code_lbl = QLabel(self.friend_code)
        self.code_lbl.setObjectName("DeckTag")
        name_row.addWidget(self.code_lbl)

        self.status_lbl = QLabel("Desconectado")
        self.status_lbl.setStyleSheet("font-size: 11px; font-weight: 600; background: transparent;")
        name_row.addWidget(self.status_lbl)
        name_row.addStretch(1)
        info_col.addLayout(name_row)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("MutedLabel")
        self.summary_lbl.setStyleSheet("font-size: 12px; background: transparent;")
        info_col.addWidget(self.summary_lbl)
        hdr.addLayout(info_col, 1)

        self.yt_pill = QLabel("YT")
        self.yt_pill.setObjectName("DeckTag")
        hdr.addWidget(self.yt_pill)

        self.tw_pill = QLabel("Twitch")
        self.tw_pill.setObjectName("DeckTag")
        hdr.addWidget(self.tw_pill)

        self.mus_pill = QLabel("Music")
        self.mus_pill.setObjectName("DeckTag")
        hdr.addWidget(self.mus_pill)

        self.chat_btn = QPushButton(" Chat")
        self.chat_btn.setObjectName("PillBtn")
        self.chat_btn.setFixedHeight(28)
        self.chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_btn.setToolTip(f"Enviar mensaje directo a {self.friend_code}")
        self.chat_btn.clicked.connect(lambda: self.chat_requested.emit(self.friend_code))
        hdr.addWidget(self.chat_btn)

        self.expand_btn = QPushButton()
        self.expand_btn.setObjectName("IconBtn")
        self.expand_btn.setFixedSize(28, 28)
        self.expand_btn.setToolTip("Expandir actividades (YouTube / Twitch / YT Music)")
        self.expand_btn.clicked.connect(self._on_header_clicked)
        hdr.addWidget(self.expand_btn)

        self.remove_btn = QPushButton()
        self.remove_btn.setObjectName("IconBtn")
        self.remove_btn.setFixedSize(26, 26)
        self.remove_btn.setToolTip(f"Quitar a {self.friend_code} de WatchParty")
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.friend_code))
        hdr.addWidget(self.remove_btn)
        root.addWidget(self.header_row)

        # Cuerpo expandible con 3 sub-cards: YouTube | Twitch | YT Music
        self.subcards_box = QWidget()
        self.subcards_box.setObjectName("TransparentBox")
        sc_lay = QHBoxLayout(self.subcards_box)
        sc_lay.setContentsMargins(0, 2, 0, 0)
        sc_lay.setSpacing(10)

        self.yt_sub = _WatchPartySubCard(
            slot_kind="youtube",
            deck="video",
            title_badge="YouTube",
            palette=palette,
            thumb_cache=thumb_cache,
        )
        self.tw_sub = _WatchPartySubCard(
            slot_kind="twitch",
            deck="video",
            title_badge="Twitch",
            palette=palette,
            thumb_cache=thumb_cache,
        )
        self.mus_sub = _WatchPartySubCard(
            slot_kind="music",
            deck="music",
            title_badge="YT Music",
            palette=palette,
            thumb_cache=thumb_cache,
        )

        self.yt_sub.action_clicked.connect(lambda dk: self._handle_slot_action("youtube", dk))
        self.tw_sub.action_clicked.connect(lambda dk: self._handle_slot_action("twitch", dk))
        self.mus_sub.action_clicked.connect(lambda dk: self._handle_slot_action("music", dk))

        sc_lay.addWidget(self.yt_sub, 1)
        sc_lay.addWidget(self.tw_sub, 1)
        sc_lay.addWidget(self.mus_sub, 1)
        root.addWidget(self.subcards_box)

        self._joined_video_host = joined_video_host
        self._joined_music_host = joined_music_host
        self.update_friend(
            friend,
            joined_video_host=joined_video_host,
            joined_music_host=joined_music_host,
        )
        self.set_expanded(self._expanded)

    def _on_header_clicked(self) -> None:
        self.set_expanded(not self._expanded)
        self.toggle_expanded_requested.emit(self.friend_code, self._expanded)

    def _handle_slot_action(self, slot_kind: str, deck: str) -> None:
        if deck == "music":
            if self._joined_music_host == self.friend_code:
                self.leave_requested.emit("music")
            else:
                self.join_requested.emit(self.friend_code, "music")
            return
        if self._joined_video_host == self.friend_code:
            vid = self._friend.get("video") or {}
            cur_kind = "twitch" if vid.get("kind") == "twitch" else "youtube"
            if cur_kind == slot_kind or not vid:
                self.leave_requested.emit("video")
                return
        self.join_requested.emit(self.friend_code, "video")

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.subcards_box.setVisible(self._expanded)
        ico = "chevron_down" if self._expanded else "chevron_right"
        self.expand_btn.setIcon(render_icon(ico, self._palette.text, 15))
        if self._expanded:
            self.refresh_live_timers()

    def refresh_live_timers(self) -> None:
        if not self._expanded:
            return
        self.yt_sub.refresh_live_time()
        self.tw_sub.refresh_live_time()
        self.mus_sub.refresh_live_time()

    def update_friend(
        self,
        friend: dict[str, Any],
        *,
        joined_video_host: str = "",
        joined_music_host: str = "",
    ) -> None:
        self._friend = dict(friend)
        self._joined_video_host = joined_video_host
        self._joined_music_host = joined_music_host

        name = str(friend.get("name") or self.friend_code)
        status = str(friend.get("status") or "offline").lower()
        if status not in _STATUS_META:
            status = "offline"
        dot_hex, status_txt = _STATUS_META[status]

        self.name_lbl.setText(name)
        pm = QPixmap(10, 10)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(dot_hex))
        painter.drawEllipse(1, 1, 8, 8)
        painter.end()
        self.status_dot.setPixmap(pm)

        self.status_lbl.setText(status_txt)
        self.status_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {dot_hex}; background: transparent;"
        )

        # Avatar con inicial (no requiere imagen externa si no hay avatar_url)
        av_url = str(friend.get("avatar_url") or "").strip()
        self._avatar_url = av_url
        av_sz = QSize(40, 40)
        self._avatar_key = (
            self._thumb_cache._make_key(av_url, av_sz, True)
            if (av_url and self._thumb_cache)
            else ""
        )
        av_pix = (
            self._thumb_cache.get_or_fetch(av_url, av_sz, circular=True)
            if (av_url and self._thumb_cache)
            else None
        )
        if av_pix is not None and not av_pix.isNull():
            self.avatar_lbl.setPixmap(_rounded_pixmap(av_pix, 40, 40, 20))
        else:
            initial = (name[:1] or "?").upper()
            self.avatar_lbl.setText(initial)
            self.avatar_lbl.setStyleSheet(
                f"background-color: {self._palette.surface2}; color: {self._palette.text}; "
                f"border: 1px solid {dot_hex if status != 'offline' else self._palette.border}; "
                "border-radius: 20px; font-size: 15px; font-weight: 700;"
            )

        vid = friend.get("video") if isinstance(friend.get("video"), dict) else None
        mus = friend.get("music") if isinstance(friend.get("music"), dict) else None
        online = status != "offline"

        is_yt = bool(online and vid and vid.get("kind") != "twitch" and (vid.get("video_id") or vid.get("title")))
        is_tw = bool(online and vid and vid.get("kind") == "twitch" and (vid.get("video_id") or vid.get("title")))
        is_mus = bool(online and mus and (mus.get("video_id") or mus.get("title")))

        self.yt_pill.setVisible(is_yt)
        self.tw_pill.setVisible(is_tw)
        self.mus_pill.setVisible(is_mus)

        parts: list[str] = []
        if is_tw and vid:
            parts.append(f"Twitch: {vid.get('author') or vid.get('title')}")
        elif is_yt and vid:
            parts.append(f"YouTube: {vid.get('title')}")
        if is_mus and mus:
            parts.append(f"Música: {mus.get('title')}")
        if not parts:
            if not online:
                parts.append("SwiftPlay cerrado")
            elif friend.get("share_enabled") is False:
                parts.append("Estado privado")
            elif status == "idle":
                parts.append("En SwiftPlay sin actividad reciente")
            else:
                parts.append("En línea en SwiftPlay")
        self.summary_lbl.setText("  ·  ".join(parts))

        vid_joined = joined_video_host == self.friend_code
        mus_joined = joined_music_host == self.friend_code
        self.yt_sub.set_data(
            friend_online=online,
            state=vid if is_yt else None,
            joined_this_deck=vid_joined and is_yt,
        )
        self.tw_sub.set_data(
            friend_online=online,
            state=vid if is_tw else None,
            joined_this_deck=vid_joined and is_tw,
        )
        self.mus_sub.set_data(
            friend_online=online,
            state=mus if is_mus else None,
            joined_this_deck=mus_joined,
        )
        self.remove_btn.setIcon(render_icon("close", self._palette.text2, 13))
        self._refresh_chat_btn_ui()

    def _refresh_chat_btn_ui(self) -> None:
        if self._unread_count > 0:
            self.chat_btn.setText(f" Chat ({self._unread_count})")
            self.chat_btn.setStyleSheet(
                "background-color: #EF4444; color: #FFFFFF; border: 1px solid #EF4444; "
                "border-radius: 10px; font-weight: 700; padding: 4px 10px;"
            )
            self.chat_btn.setIcon(render_icon("chat", "#FFFFFF", 13))
        else:
            self.chat_btn.setText(" Chat")
            self.chat_btn.setStyleSheet("")
            self.chat_btn.setProperty("active", "true" if self._chat_active else "false")
            self.chat_btn.style().unpolish(self.chat_btn)
            self.chat_btn.style().polish(self.chat_btn)
            self.chat_btn.setIcon(render_icon("chat", self._palette.text, 13))

    def set_unread_count(self, count: int) -> None:
        val = max(0, int(count or 0))
        if val == self._unread_count:
            return
        self._unread_count = val
        self._refresh_chat_btn_ui()

    def set_chat_active(self, active: bool) -> None:
        val = bool(active)
        if val == self._chat_active:
            return
        self._chat_active = val
        self._refresh_chat_btn_ui()

    def handle_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if url and url == self._avatar_url and not pix.isNull():
            self.avatar_lbl.setPixmap(_rounded_pixmap(pix, 40, 40, 20))
        self.yt_sub.handle_thumb_ready(url, pix)
        self.tw_sub.handle_thumb_ready(url, pix)
        self.mus_sub.handle_thumb_ready(url, pix)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.yt_sub.update_theme(palette)
        self.tw_sub.update_theme(palette)
        self.mus_sub.update_theme(palette)
        self.set_expanded(self._expanded)
        self.update_friend(
            self._friend,
            joined_video_host=self._joined_video_host,
            joined_music_host=self._joined_music_host,
        )


class WatchPartyPanel(QWidget):
    """Página principal de WatchParty: amigos siempre activos, identidad y 3 sub-cards."""

    join_party_requested = Signal(str, str)  # host_code, deck ("video"|"music")
    leave_party_requested = Signal(str)  # deck
    add_friend_requested = Signal(str)  # code
    remove_friend_requested = Signal(str)  # code
    refresh_requested = Signal()
    config_changed = Signal()
    overlay_settings_changed = Signal()
    overlay_preview_requested = Signal(int)
    overlay_edit_requested = Signal()
    toast_requested = Signal(str)
    chat_opened = Signal(str)  # friend_code
    chat_closed = Signal()
    send_message_requested = Signal(str, str)  # friend_code, text

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

        self._friends: list[dict[str, Any]] = []
        self._expanded_codes: set[str] = set()
        self._cards: dict[str, _WatchPartyFriendCard] = {}
        self._rendered_order: list[str] = []
        self._friends_dirty: bool = False
        self._joined_video_host: str = ""
        self._joined_music_host: str = ""
        self._conn_state: str = "disconnected"
        self._conn_detail: str = ""
        self._active_chat_code: str = ""
        self._chat_rendered_ids: set[str] = set()
        self._unread_counts: dict[str, int] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(12)

        # Cabecera principal
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        title = QLabel("WatchParty")
        title.setObjectName("PageTitle")
        hdr.addWidget(title)

        self.status_pill = QLabel("Sin servidor")
        self.status_pill.setObjectName("DeckTag")
        hdr.addWidget(self.status_pill)

        self.power_toggle_btn = QPushButton("Desconectar")
        self.power_toggle_btn.setObjectName("PillBtn")
        self.power_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_toggle_btn.clicked.connect(self._on_power_toggled)
        hdr.addWidget(self.power_toggle_btn)

        hdr.addStretch(1)

        code_lbl_hint = QLabel("Tu código:")
        code_lbl_hint.setObjectName("MutedLabel")
        hdr.addWidget(code_lbl_hint)

        self.my_code_lbl = QLabel(self.cfg.watchparty_code)
        self.my_code_lbl.setObjectName("WatchPartyCodePill")
        self.my_code_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hdr.addWidget(self.my_code_lbl)

        self.copy_code_btn = QPushButton("Copiar")
        self.copy_code_btn.setObjectName("PillBtn")
        self.copy_code_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_code_btn.clicked.connect(self._copy_my_code)
        hdr.addWidget(self.copy_code_btn)

        self.server_toggle_btn = QPushButton("Servidor / Perfil")
        self.server_toggle_btn.setObjectName("PillBtn")
        self.server_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.server_toggle_btn.clicked.connect(self._toggle_setup_box)
        hdr.addWidget(self.server_toggle_btn)

        self.refresh_btn = QPushButton()
        self.refresh_btn.setObjectName("IconBtn")
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.setToolTip("Actualizar estado de amigos y reconectar si hace falta")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        hdr.addWidget(self.refresh_btn)
        root.addLayout(hdr)

        # Switches rápidos de privacidad y control cruzado
        toggles_row = QHBoxLayout()
        toggles_row.setSpacing(14)

        self.share_chk = QCheckBox("Compartir qué estoy viendo/escuchando")
        self.share_chk.setChecked(bool(self.cfg.watchparty_share_activity))
        self.share_chk.toggled.connect(self._on_quick_toggle)
        toggles_row.addWidget(self.share_chk)

        self.guest_ctrl_chk = QCheckBox("Permitir control cruzado en mi sala")
        self.guest_ctrl_chk.setChecked(bool(self.cfg.watchparty_allow_guest_control))
        self.guest_ctrl_chk.toggled.connect(self._on_quick_toggle)
        toggles_row.addWidget(self.guest_ctrl_chk)

        toggles_row.addStretch(1)
        root.addLayout(toggles_row)

        # Controles del overlay flotante unificado (Sin overlay / Simple / Extendido + opacidad + mover)
        overlay_row = QHBoxLayout()
        overlay_row.setSpacing(10)
        ov_lbl = QLabel("Overlay flotante:")
        ov_lbl.setObjectName("MutedLabel")
        overlay_row.addWidget(ov_lbl)

        self.overlay_mode_combo = QComboBox()
        self.overlay_mode_combo.addItem("Sin overlay", "off")
        self.overlay_mode_combo.addItem("Simple (ojito / estado)", "status")
        self.overlay_mode_combo.addItem("Extendido (personas + íconos)", "full")
        ov_idx = self.overlay_mode_combo.findData(self.cfg.discord_overlay_mode)
        self.overlay_mode_combo.setCurrentIndex(max(0, ov_idx))
        self.overlay_mode_combo.currentIndexChanged.connect(self._on_overlay_mode_changed)
        overlay_row.addWidget(self.overlay_mode_combo)

        op_lbl = QLabel("Opacidad:")
        op_lbl.setObjectName("MutedLabel")
        overlay_row.addWidget(op_lbl)

        self.overlay_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.overlay_opacity_slider.setRange(10, 100)
        self.overlay_opacity_slider.setSingleStep(5)
        self.overlay_opacity_slider.setPageStep(10)
        self.overlay_opacity_slider.setFixedWidth(104)
        self.overlay_opacity_slider.setValue(int(self.cfg.discord_overlay_opacity))
        self.overlay_opacity_slider.setEnabled(self.cfg.discord_overlay_mode != "off")
        self.overlay_opacity_slider.valueChanged.connect(self._on_overlay_opacity_changed)
        overlay_row.addWidget(self.overlay_opacity_slider)

        self.overlay_opacity_lbl = QLabel(f"{int(self.cfg.discord_overlay_opacity)}%")
        self.overlay_opacity_lbl.setFixedWidth(36)
        self.overlay_opacity_lbl.setObjectName("MutedLabel")
        overlay_row.addWidget(self.overlay_opacity_lbl)

        self.overlay_move_btn = QPushButton("Mover")
        self.overlay_move_btn.setObjectName("PillBtn")
        self.overlay_move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_move_btn.setEnabled(self.cfg.discord_overlay_mode != "off")
        self.overlay_move_btn.setToolTip("Arrastra el overlay unificado donde quieras en tu pantalla")
        self.overlay_move_btn.clicked.connect(self.overlay_edit_requested.emit)
        overlay_row.addWidget(self.overlay_move_btn)
        overlay_row.addStretch(1)
        root.addLayout(overlay_row)

        # Panel desplegable de configuración del relay Cloudflare + Nombre + AFK
        self.setup_card = QFrame()
        self.setup_card.setObjectName("MediaCard")
        sc = QVBoxLayout(self.setup_card)
        sc.setContentsMargins(14, 12, 14, 12)
        sc.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(QLabel("Servidor Cloudflare:"))
        self.relay_input = QLineEdit(self.cfg.watchparty_relay_url)
        self.relay_input.setPlaceholderText("wss://swiftplay-watchparty.<tu-usuario>.workers.dev/ws")
        row1.addWidget(self.relay_input, 1)
        sc.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("Tu nombre:"))
        self.name_input = QLineEdit(self.cfg.watchparty_display_name)
        self.name_input.setPlaceholderText("Ej: Benja")
        self.name_input.setMaximumWidth(220)
        row2.addWidget(self.name_input)

        row2.addWidget(QLabel("AFK inactivo (min):"))
        self.afk_spin = QSpinBox()
        self.afk_spin.setRange(1, 60)
        self.afk_spin.setValue(int(self.cfg.watchparty_afk_minutes or 8))
        self.afk_spin.setFixedWidth(86)
        row2.addWidget(self.afk_spin)

        row2.addStretch(1)
        self.save_setup_btn = QPushButton("Guardar y conectar")
        self.save_setup_btn.setObjectName("PillBtn")
        self.save_setup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_setup_btn.clicked.connect(self._save_setup)
        row2.addWidget(self.save_setup_btn)
        sc.addLayout(row2)

        setup_hint = QLabel(
            "Despliega gratis `watchparty_relay` con `npx wrangler deploy` y pega la URL acá. "
            "Todos los amigos que usen el mismo servidor se verán en tiempo real sin depender de Discord."
        )
        setup_hint.setObjectName("MutedLabel")
        setup_hint.setWordWrap(True)
        sc.addWidget(setup_hint)

        self.setup_card.setVisible(not bool(self.cfg.watchparty_relay_url))
        root.addWidget(self.setup_card)

        # Barra para agregar amigo por código
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.add_code_input = QLineEdit()
        self.add_code_input.setPlaceholderText("Código de amigo (ej: FINZ-7K2M)")
        self.add_code_input.returnPressed.connect(self._on_add_friend)
        add_row.addWidget(self.add_code_input, 1)

        self.add_friend_btn = QPushButton(" Agregar amigo")
        self.add_friend_btn.setObjectName("PillBtn")
        self.add_friend_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_friend_btn.clicked.connect(self._on_add_friend)
        add_row.addWidget(self.add_friend_btn)
        root.addLayout(add_row)

        # Lista scrollable de amigos
        self.scroll = QScrollArea()
        self.scroll.setObjectName("TransparentScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setObjectName("TransparentBox")
        self.scroll.viewport().setAutoFillBackground(False)

        container = QWidget()
        container.setObjectName("TransparentBox")
        self._list_lay = QVBoxLayout(container)
        self._list_lay.setContentsMargins(0, 2, 4, 12)
        self._list_lay.setSpacing(10)

        self.empty_lbl = QLabel(
            "Todavía no agregaste socios a WatchParty.\n"
            "Comparte tu código o pega el de tu amigo arriba para ver en qué anda "
            "(YouTube, Twitch o YT Music) y unirte al instante."
        )
        self.empty_lbl.setObjectName("MutedLabel")
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_lbl.setContentsMargins(24, 42, 24, 42)
        self._list_lay.addWidget(self.empty_lbl)
        self._list_lay.addStretch(1)

        self.scroll.setWidget(container)

        # Panel lateral derecho de chat directo con amigo seleccionado
        self.chat_card = QFrame()
        self.chat_card.setObjectName("DiscordChatCard")
        self.chat_card.setMinimumWidth(290)
        self.chat_card.setMaximumWidth(420)
        self.chat_card.setVisible(False)
        chat_lay = QVBoxLayout(self.chat_card)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        chat_hdr = QFrame()
        chat_hdr.setObjectName("DiscordChatHeader")
        chat_hdr_lay = QHBoxLayout(chat_hdr)
        chat_hdr_lay.setContentsMargins(12, 10, 10, 10)
        chat_hdr_lay.setSpacing(8)

        self.chat_status_dot = QLabel()
        self.chat_status_dot.setFixedSize(10, 10)
        chat_hdr_lay.addWidget(self.chat_status_dot)

        chat_hdr_meta = QVBoxLayout()
        chat_hdr_meta.setContentsMargins(0, 0, 0, 0)
        chat_hdr_meta.setSpacing(1)
        self.chat_title_lbl = QLabel("Chat")
        self.chat_title_lbl.setStyleSheet("font-size: 13px; font-weight: 700; background: transparent;")
        chat_hdr_meta.addWidget(self.chat_title_lbl)
        self.chat_sub_lbl = QLabel("")
        self.chat_sub_lbl.setObjectName("MutedLabel")
        self.chat_sub_lbl.setStyleSheet("font-size: 11px; background: transparent;")
        chat_hdr_meta.addWidget(self.chat_sub_lbl)
        chat_hdr_lay.addLayout(chat_hdr_meta, 1)

        self.chat_close_btn = QPushButton()
        self.chat_close_btn.setObjectName("IconBtn")
        self.chat_close_btn.setFixedSize(26, 26)
        self.chat_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_close_btn.setToolTip("Cerrar chat")
        self.chat_close_btn.clicked.connect(self.close_chat)
        chat_hdr_lay.addWidget(self.chat_close_btn)
        chat_lay.addWidget(chat_hdr)

        self.chat_hint_lbl = QLabel("")
        self.chat_hint_lbl.setObjectName("MutedLabel")
        self.chat_hint_lbl.setWordWrap(True)
        self.chat_hint_lbl.setContentsMargins(12, 8, 12, 4)
        self.chat_hint_lbl.setStyleSheet("font-size: 11px; background: transparent;")
        self.chat_hint_lbl.setVisible(False)
        chat_lay.addWidget(self.chat_hint_lbl)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("TransparentScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat_scroll.viewport().setObjectName("TransparentBox")
        self.chat_scroll.viewport().setAutoFillBackground(False)

        chat_msgs_box = QWidget()
        chat_msgs_box.setObjectName("TransparentBox")
        self._chat_msgs_lay = QVBoxLayout(chat_msgs_box)
        self._chat_msgs_lay.setContentsMargins(12, 10, 12, 10)
        self._chat_msgs_lay.setSpacing(8)

        self.chat_empty_lbl = QLabel(
            "Todavía no hay mensajes con este amigo.\n"
            "Escríbele abajo para coordinar qué ver o escuchar."
        )
        self.chat_empty_lbl.setObjectName("MutedLabel")
        self.chat_empty_lbl.setWordWrap(True)
        self.chat_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chat_empty_lbl.setContentsMargins(12, 24, 12, 24)
        self._chat_msgs_lay.addWidget(self.chat_empty_lbl)
        self._chat_msgs_lay.addStretch(1)

        self.chat_scroll.setWidget(chat_msgs_box)
        chat_lay.addWidget(self.chat_scroll, 1)

        chat_input_wrap = QWidget()
        chat_input_wrap.setObjectName("TransparentBox")
        chat_input_lay = QHBoxLayout(chat_input_wrap)
        chat_input_lay.setContentsMargins(10, 8, 10, 10)
        chat_input_lay.setSpacing(8)

        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("DiscordChatInput")
        self.chat_input.setPlaceholderText("Escribe un mensaje...")
        self.chat_input.setMaxLength(1000)
        self.chat_input.returnPressed.connect(self._on_send_chat_clicked)
        chat_input_lay.addWidget(self.chat_input, 1)

        self.chat_send_btn = QPushButton(" Enviar")
        self.chat_send_btn.setObjectName("PillBtn")
        self.chat_send_btn.setFixedHeight(32)
        self.chat_send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_send_btn.clicked.connect(self._on_send_chat_clicked)
        chat_input_lay.addWidget(self.chat_send_btn)
        chat_lay.addWidget(chat_input_wrap)

        body_lay = QHBoxLayout()
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(12)
        body_lay.addWidget(self.scroll, 3)
        body_lay.addWidget(self.chat_card, 2)
        root.addLayout(body_lay, 1)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_live_tick)

        if self.thumb_cache is not None:
            self.thumb_cache.ready.connect(self._on_thumb_ready)

        self.update_theme(palette)
        self._sync_power_toggle_ui()
        self._rebuild_from_offline_config()

    def _on_thumb_ready(self, key: str, pix: QPixmap) -> None:
        for card in self._cards.values():
            card.handle_thumb_ready(key, pix)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if self._friends_dirty:
            self._friends_dirty = False
            self._render_friends_list()
        self._refresh_tick_timer()

    def hideEvent(self, event: Any) -> None:
        self._tick_timer.stop()
        super().hideEvent(event)

    def _refresh_tick_timer(self) -> None:
        if self.isVisible() and bool(self._expanded_codes):
            if not self._tick_timer.isActive():
                self._tick_timer.start()
        else:
            self._tick_timer.stop()

    def _on_live_tick(self) -> None:
        for code in list(self._expanded_codes):
            card = self._cards.get(code)
            if card is not None:
                card.refresh_live_timers()

    def _copy_my_code(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self.cfg.watchparty_code)
            self.toast_requested.emit(f"Código WatchParty copiado: {self.cfg.watchparty_code}")

    def _toggle_setup_box(self) -> None:
        self.setup_card.setVisible(not self.setup_card.isVisible())

    def _on_quick_toggle(self) -> None:
        self.cfg.watchparty_share_activity = bool(self.share_chk.isChecked())
        self.cfg.watchparty_allow_guest_control = bool(self.guest_ctrl_chk.isChecked())
        save_config(self.cfg)
        self.config_changed.emit()

    def _on_overlay_mode_changed(self, _idx: int) -> None:
        mode = str(self.overlay_mode_combo.currentData() or "status")
        if mode not in ("off", "status", "full"):
            mode = "status"
        self.cfg.discord_overlay_mode = mode
        self.cfg.discord_overlay_enabled = mode != "off"
        enabled = mode != "off"
        self.overlay_opacity_slider.setEnabled(enabled)
        self.overlay_move_btn.setEnabled(enabled)
        save_config(self.cfg)
        self.overlay_settings_changed.emit()
        if enabled:
            self.overlay_preview_requested.emit(int(self.cfg.discord_overlay_opacity))

    def _on_overlay_opacity_changed(self, value: int) -> None:
        val = max(10, min(100, int(value)))
        self.cfg.discord_overlay_opacity = val
        self.overlay_opacity_lbl.setText(f"{val}%")
        save_config(self.cfg)
        self.overlay_settings_changed.emit()
        if self.cfg.discord_overlay_mode != "off":
            self.overlay_preview_requested.emit(val)

    def sync_overlay_controls(self) -> None:
        self.overlay_mode_combo.blockSignals(True)
        idx = self.overlay_mode_combo.findData(self.cfg.discord_overlay_mode)
        self.overlay_mode_combo.setCurrentIndex(max(0, idx))
        self.overlay_mode_combo.blockSignals(False)

        self.overlay_opacity_slider.blockSignals(True)
        self.overlay_opacity_slider.setValue(int(self.cfg.discord_overlay_opacity))
        self.overlay_opacity_slider.blockSignals(False)
        self.overlay_opacity_lbl.setText(f"{int(self.cfg.discord_overlay_opacity)}%")
        enabled = self.cfg.discord_overlay_mode != "off"
        self.overlay_opacity_slider.setEnabled(enabled)
        self.overlay_move_btn.setEnabled(enabled)

    def _save_setup(self) -> None:
        self.cfg.watchparty_relay_url = normalize_watchparty_relay_url(self.relay_input.text())
        self.cfg.watchparty_display_name = str(self.name_input.text() or "").strip()[:64]
        self.cfg.watchparty_afk_minutes = int(self.afk_spin.value())
        save_config(self.cfg)
        self.relay_input.setText(self.cfg.watchparty_relay_url)
        self.setup_card.setVisible(False)
        self.config_changed.emit()
        self.toast_requested.emit("Configuración de WatchParty guardada.")

    def _on_add_friend(self) -> None:
        code = str(self.add_code_input.text() or "").strip().upper()
        if not code:
            return
        if code == self.cfg.watchparty_code:
            self.toast_requested.emit("Ese es tu propio código WatchParty.")
            return
        self.add_code_input.clear()
        self.add_friend_requested.emit(code)

    def _sync_power_toggle_ui(self) -> None:
        enabled = bool(self.cfg.watchparty_enabled)
        if enabled:
            self.power_toggle_btn.setText("Desconectar")
            self.power_toggle_btn.setProperty("active", "false")
            self.power_toggle_btn.setToolTip(
                "Desconectar WatchParty por completo (detiene WebSocket y presencia en segundo plano)"
            )
        else:
            self.power_toggle_btn.setText("Conectar")
            self.power_toggle_btn.setProperty("active", "true")
            self.power_toggle_btn.setToolTip("Activar y conectar WatchParty")
        self.power_toggle_btn.style().unpolish(self.power_toggle_btn)
        self.power_toggle_btn.style().polish(self.power_toggle_btn)
        self.refresh_btn.setEnabled(enabled)

    def _on_power_toggled(self) -> None:
        self.cfg.watchparty_enabled = not bool(self.cfg.watchparty_enabled)
        save_config(self.cfg)
        self._sync_power_toggle_ui()
        if not self.cfg.watchparty_enabled:
            self.set_connection_state("disconnected", "")
            self.toast_requested.emit("WatchParty desconectado.")
        else:
            self.toast_requested.emit("WatchParty activado.")
        self.config_changed.emit()

    def set_connection_state(self, state: str, detail: str = "") -> None:
        self._conn_state = str(state or "disconnected")
        self._conn_detail = str(detail or "")
        if not self.cfg.watchparty_enabled:
            self.status_pill.setText("Desactivado")
            self.status_pill.setStyleSheet(
                f"color: {self.palette.text2}; background: {self.palette.surface2};"
            )
            return
        if not self.cfg.watchparty_relay_url:
            self.status_pill.setText("Sin servidor")
            self.status_pill.setStyleSheet(
                f"color: {self.palette.text2}; background: {self.palette.surface2};"
            )
            return
        if state == "connected":
            self.status_pill.setText("● En línea")
            self.status_pill.setStyleSheet(
                f"color: #4ADE80; background: {self.palette.surface2};"
            )
        elif state == "connecting":
            self.status_pill.setText("Conectando...")
            self.status_pill.setStyleSheet(
                f"color: #FACC15; background: {self.palette.surface2};"
            )
        else:
            txt = f"Sin conexión ({detail})" if detail else "Sin conexión"
            self.status_pill.setText(txt[:44])
            self.status_pill.setStyleSheet(
                f"color: #F87171; background: {self.palette.surface2};"
            )

    def set_joined_hosts(self, *, video_host: str = "", music_host: str = "") -> None:
        vh = str(video_host or "").upper()
        mh = str(music_host or "").upper()
        if vh == self._joined_video_host and mh == self._joined_music_host:
            return
        self._joined_video_host = vh
        self._joined_music_host = mh
        if not self.isVisible():
            self._friends_dirty = True
            return
        for f in self._friends:
            c = str(f.get("code") or "").upper()
            card = self._cards.get(c)
            if card is not None and (
                card._joined_video_host != vh or card._joined_music_host != mh
            ):
                card.update_friend(
                    f,
                    joined_video_host=self._joined_video_host,
                    joined_music_host=self._joined_music_host,
                )

    def set_friends_data(self, friends: list[dict[str, Any]]) -> None:
        merged_by_code: dict[str, dict[str, Any]] = {
            str(f.get("code") or "").upper(): {
                "code": str(f.get("code") or "").upper(),
                "name": str(f.get("name") or f.get("code") or ""),
                "avatar_url": str(f.get("avatar_url") or ""),
                "status": "offline",
                "video": None,
                "music": None,
            }
            for f in (self.cfg.watchparty_friends or [])
            if isinstance(f, dict) and f.get("code")
        }
        for row in friends or []:
            if isinstance(row, dict) and row.get("code"):
                c = str(row["code"]).upper()
                merged_by_code[c] = dict(row)

        sorted_list = sorted(
            merged_by_code.values(),
            key=lambda it: (
                _STATUS_SORT_ORDER.get(str(it.get("status") or "offline"), 2),
                str(it.get("name") or it.get("code") or "").lower(),
            ),
        )
        if (
            sorted_list == self._friends
            and not self._friends_dirty
            and len(self._cards) == len(sorted_list)
        ):
            return
        self._friends = sorted_list
        if not self.isVisible():
            self._friends_dirty = True
            return
        self._render_friends_list()

    def _rebuild_from_offline_config(self) -> None:
        self.set_friends_data([])

    def _render_friends_list(self) -> None:
        self._friends_dirty = False
        self.empty_lbl.setVisible(len(self._friends) == 0)

        new_order = [str(f.get("code") or "").upper() for f in self._friends]
        if new_order == self._rendered_order and all(c in self._cards for c in new_order):
            for f in self._friends:
                code = str(f.get("code") or "").upper()
                card = self._cards[code]
                if (
                    card._friend != f
                    or card._joined_video_host != self._joined_video_host
                    or card._joined_music_host != self._joined_music_host
                ):
                    card.update_friend(
                        f,
                        joined_video_host=self._joined_video_host,
                        joined_music_host=self._joined_music_host,
                    )
            self._refresh_tick_timer()
            return

        # Remover tarjetas anteriores que ya no existen o cambiar orden limpiamente
        for c in list(self._cards.keys()):
            w = self._cards.pop(c)
            self._list_lay.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()

        # Mantener empty_lbl al inicio y el stretch al final
        insert_idx = 1
        for f in self._friends:
            code = str(f.get("code") or "").upper()
            card = _WatchPartyFriendCard(
                f,
                self.palette,
                self.thumb_cache,
                expanded=(code in self._expanded_codes),
                joined_video_host=self._joined_video_host,
                joined_music_host=self._joined_music_host,
                parent=self,
            )
            card.toggle_expanded_requested.connect(self._on_card_expanded_toggled)
            card.chat_requested.connect(self.chat_opened.emit)
            card.join_requested.connect(self.join_party_requested.emit)
            card.leave_requested.connect(self.leave_party_requested.emit)
            card.remove_requested.connect(self.remove_friend_requested.emit)
            card.set_unread_count(self._unread_counts.get(code, 0))
            card.set_chat_active(bool(self._active_chat_code and code == self._active_chat_code))
            self._cards[code] = card
            self._list_lay.insertWidget(insert_idx, card)
            insert_idx += 1

        self._rendered_order = new_order
        self._refresh_tick_timer()

    def _on_card_expanded_toggled(self, code: str, expanded: bool) -> None:
        c = str(code or "").upper()
        if expanded:
            self._expanded_codes.add(c)
        else:
            self._expanded_codes.discard(c)
        self._refresh_tick_timer()

    def refresh_from_config(self, conn_state: tuple[str, str] | None = None) -> None:
        self.my_code_lbl.setText(self.cfg.watchparty_code)
        self.relay_input.setText(self.cfg.watchparty_relay_url)
        self.name_input.setText(self.cfg.watchparty_display_name)
        self.afk_spin.setValue(int(self.cfg.watchparty_afk_minutes or 8))
        self.share_chk.blockSignals(True)
        self.share_chk.setChecked(bool(self.cfg.watchparty_share_activity))
        self.share_chk.blockSignals(False)
        self.guest_ctrl_chk.blockSignals(True)
        self.guest_ctrl_chk.setChecked(bool(self.cfg.watchparty_allow_guest_control))
        self.guest_ctrl_chk.blockSignals(False)
        self.sync_overlay_controls()
        self._sync_power_toggle_ui()
        if conn_state is not None:
            self.set_connection_state(conn_state[0], conn_state[1])
        elif not self.cfg.watchparty_relay_url:
            self.set_connection_state("disconnected", "")
        else:
            self.set_connection_state(self._conn_state, self._conn_detail)
        if not self._friends:
            self._rebuild_from_offline_config()

    @property
    def active_chat_code(self) -> str:
        return self._active_chat_code

    def is_viewing_chat(self, friend_code: str) -> bool:
        fc = str(friend_code or "").strip().upper()
        return bool(fc and self.chat_card.isVisible() and self._active_chat_code == fc)

    def _clear_chat_bubbles(self) -> None:
        self._chat_rendered_ids.clear()
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

    def _create_chat_bubble(self, msg: dict[str, Any]) -> QWidget:
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
        bubble.setMaximumWidth(290)
        bubble_lay = QVBoxLayout(bubble)
        bubble_lay.setContentsMargins(10, 7, 10, 7)
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

        body_lbl = QLabel(content)
        body_lbl.setWordWrap(True)
        body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body_lbl.setStyleSheet("font-size: 12.5px; background: transparent;")
        bubble_lay.addWidget(body_lbl)

        row_lay.addWidget(bubble)
        if not is_self:
            row_lay.addStretch(1)
        return row

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
                "Tu amigo está desconectado: el mensaje se entregará cuando se conecte."
            )
            self.chat_hint_lbl.setVisible(True)
        else:
            self.chat_hint_lbl.setVisible(False)
        self.chat_send_btn.setEnabled(bool(connected))

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
        self._clear_chat_bubbles()
        for c, card in self._cards.items():
            card.set_chat_active(c == fc)
        self.sync_chat_peer_status(friend_name or fc, online=online, connected=connected)
        for m in messages or []:
            if isinstance(m, dict):
                self._insert_chat_bubble(m)
        self.chat_empty_lbl.setVisible(len(self._chat_rendered_ids) == 0)
        self.chat_card.setVisible(True)
        self.chat_input.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

    def _insert_chat_bubble(self, msg: dict[str, Any]) -> None:
        mid = str(msg.get("id") or "").strip()
        if mid and mid in self._chat_rendered_ids:
            return
        if mid:
            self._chat_rendered_ids.add(mid)
        else:
            self._chat_rendered_ids.add(f"_anon_{self._chat_msgs_lay.count()}")
        bubble = self._create_chat_bubble(msg)
        insert_pos = max(1, self._chat_msgs_lay.count() - 1)
        self._chat_msgs_lay.insertWidget(insert_pos, bubble)

    def append_chat_message(self, friend_code: str, msg: dict[str, Any]) -> None:
        fc = str(friend_code or "").strip().upper()
        if not fc or fc != self._active_chat_code or not self.chat_card.isVisible():
            return
        self._insert_chat_bubble(msg)
        self.chat_empty_lbl.setVisible(len(self._chat_rendered_ids) == 0)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

    def close_chat(self) -> None:
        self._active_chat_code = ""
        self._clear_chat_bubbles()
        self.chat_card.setVisible(False)
        for card in self._cards.values():
            card.set_chat_active(False)
        self.chat_closed.emit()

    def set_unread_counts(self, counts: dict[str, int]) -> None:
        clean: dict[str, int] = {}
        for k, v in (counts or {}).items():
            c = str(k or "").strip().upper()
            n = max(0, int(v or 0))
            if c and n > 0:
                clean[c] = n
        self._unread_counts = clean
        for c, card in self._cards.items():
            card.set_unread_count(clean.get(c, 0))

    def _on_send_chat_clicked(self) -> None:
        if not self._active_chat_code:
            return
        txt = str(self.chat_input.text() or "").strip()
        if not txt:
            return
        self.chat_input.clear()
        self.send_message_requested.emit(self._active_chat_code, txt)

    def update_theme(self, palette: ThemePalette) -> None:
        self.palette = palette
        self.add_friend_btn.setIcon(render_icon("plus", palette.text, 14))
        self.refresh_btn.setIcon(render_icon("refresh", palette.text, 14))
        self.chat_close_btn.setIcon(render_icon("close", palette.text2, 13))
        self.chat_send_btn.setIcon(render_icon("send", palette.text, 14))
        for card in self._cards.values():
            card.update_theme(palette)
        self.set_connection_state(self._conn_state, self._conn_detail)
