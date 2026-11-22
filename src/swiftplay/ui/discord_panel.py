"""Panel nativo de Discord Voz y Chat (Bot puente aislado, 0 MB en reposo)."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import html
from typing import Any, Literal

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig, save_config
from swiftplay.discord_service import DiscordService
from swiftplay.security import has_discord_token
from swiftplay.storage import LocalLibrary
from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon

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
    """Avatar circular con foto de perfil real (o inicial fallback) y anillo de habla."""

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
        self._bg_hex = _avatar_tint_for(self._name)
        self._pixmap: QPixmap | None = None
        self.setFixedSize(size_px, size_px)

    def set_speaking(self, speaking: bool) -> None:
        val = bool(speaking)
        if val != self._speaking:
            self._speaking = val
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

        ring_color = QColor("#4ADE80") if self._speaking else QColor(self._palette.border_bright)
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

        name = str(part.get("name") or self._uid)
        is_bot = bool(part.get("is_bot", False))
        self._name = name
        self._is_bot = is_bot
        self._card_bg_hex, self._card_border_hex = _default_card_tint(name)

        self.setObjectName("DiscordParticipantCard")
        self.setMinimumWidth(195)
        self.setMinimumHeight(148)
        if not self._is_self:
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._on_context_menu)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(8)

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

        self._load_avatar()
        self.update_theme(palette)

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


class DiscordVoicePanel(QWidget):
    """Vista minimalista de Discord Voz + Estados + Chat (Bot puente aislado, 0 MB en reposo)."""

    open_token_settings_requested = Signal()
    open_shortcuts_settings_requested = Signal()
    voice_status_changed = Signal(bool, bool, bool)  # active, muted, deafened
    channel_alert_requested = Signal(dict)
    sound_event_requested = Signal(str)
    toast_requested = Signal(str)

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
        self._bot_state = "disconnected"
        self._joining_channel_id = ""
        self._user_volumes: dict[str, int] = {}
        self._participant_cards: dict[str, _ParticipantCard] = {}
        self._participant_cards_order: list[_ParticipantCard] = []
        self._last_grid_cols: int = 0
        self._populating_devices = False
        self._selected_guild_id = ""
        self._active_chat_kind = ""
        self._active_chat_id = ""
        self._active_chat_can_send = False
        self._active_dm_peer_name = ""
        self._active_dm_peer_avatar = ""
        self._chat_messages: deque[dict[str, Any]] = deque(maxlen=80)
        self._dm_threads_mem: list[dict[str, str]] = []
        self._dm_msgs_mem: dict[str, list[dict[str, Any]]] = {}
        self._members: list[dict[str, Any]] = []
        self._online_count: int = 0
        self._channel_members: list[dict[str, Any]] = []
        self._channel_online_count: int = 0
        self._channel_members_loaded: bool = False
        self._members_scope: Literal["channel", "server"] = "channel"
        self._member_row_by_url: dict[str, list[QListWidgetItem]] = {}
        self._unread_channels: set[str] = set()
        self._last_channels_sig: Any = None
        self._last_participants_sig: Any = None
        self._worker_ws_mb = 0.0
        self._worker_priv_mb = 0.0

        # Estado de acordeones y vistas (mockup limpio sin espacio muerto)
        self._channels_visible = True
        self._members_visible = False
        self._audio_drawer_open = False
        self._vc_section_open = True
        self._tc_section_open = True
        self._dm_section_open = True
        self._locked_vc_expanded = False
        self._voice_pane_visible = True
        self._chat_pane_visible = True

        self._build_ui()
        self._render_dm_threads()
        self._connect_service_signals()
        self.refresh_token_state()
        self.update_theme(self.palette)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(10)

        # --- Cabecera minimalista ---
        hdr = QHBoxLayout()
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
        root.addLayout(hdr)

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
        self.channels_scroll.setWidgetResizable(True)
        self.channels_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.channels_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        ch_container = QWidget()
        ch_container.setStyleSheet("background: transparent;")
        unified_lay = QVBoxLayout(ch_container)
        unified_lay.setContentsMargins(0, 2, 2, 4)
        unified_lay.setSpacing(6)

        self.vc_section_btn = _make_section_toggle_btn("▾ CANALES DE VOZ")
        self.vc_section_btn.clicked.connect(self._toggle_voice_channels_section)
        unified_lay.addWidget(self.vc_section_btn)

        self.vc_list_widget = QWidget()
        self.vc_list_widget.setStyleSheet("background: transparent;")
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
        self.text_channels_scroll.setStyleSheet("background: transparent;")
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
        self.dm_channels_scroll.setStyleSheet("background: transparent;")
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

        # Splitter vertical: área oscura de voz (arriba) + línea separadora + tarjeta de chat (abajo)
        self.center_splitter = QSplitter(Qt.Orientation.Vertical)
        self.center_splitter.setChildrenCollapsible(False)
        self.center_splitter.setHandleWidth(8)
        self.center_splitter.setStyleSheet(
            "QSplitter::handle { background: rgba(207, 213, 223, 38); border-radius: 2px; margin: 2px 24px; }"
        )

        # Panel superior: Fondo propio de Voz + cabecera colapsable + tarjetas de participantes
        self.voice_pane = QFrame()
        self.voice_pane.setObjectName("DiscordVoiceArea")
        vp_lay = QVBoxLayout(self.voice_pane)
        vp_lay.setContentsMargins(10, 8, 10, 10)
        vp_lay.setSpacing(8)

        voice_hdr_frame = QFrame()
        voice_hdr_frame.setObjectName("DiscordVoiceHeader")
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
        self.close_voice_btn = QPushButton()
        self.close_voice_btn.setObjectName("IconBtn")
        self.close_voice_btn.setFixedSize(24, 24)
        self.close_voice_btn.setToolTip("Ocultar panel de voz (reabrir con 'Voz')")
        self.close_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_voice_btn.clicked.connect(lambda: self._set_pane_visible("voice", False))
        vh_lay.addWidget(self.close_voice_btn)
        vp_lay.addWidget(voice_hdr_frame)

        self.participants_scroll = QScrollArea()
        self.participants_scroll.setWidgetResizable(True)
        self.participants_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.participants_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        part_container = QWidget()
        part_container.setStyleSheet("background: transparent;")
        part_outer = QVBoxLayout(part_container)
        part_outer.setContentsMargins(0, 0, 0, 0)
        part_outer.setSpacing(8)

        self.participants_grid = QGridLayout()
        self.participants_grid.setContentsMargins(0, 0, 0, 0)
        self.participants_grid.setSpacing(10)
        part_outer.addLayout(self.participants_grid)

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
        vp_lay.addWidget(self.participants_scroll, 1)
        self.center_splitter.addWidget(self.voice_pane)

        # Panel inferior: Chat con cabecera propia y fondo diferenciado
        self.chat_pane = QFrame()
        self.chat_pane.setObjectName("DiscordChatCard")
        chat_lay = QVBoxLayout(self.chat_pane)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        chat_hdr_frame = QFrame()
        chat_hdr_frame.setObjectName("DiscordChatHeader")
        chat_hdr = QHBoxLayout(chat_hdr_frame)
        chat_hdr.setContentsMargins(14, 8, 14, 8)
        chat_hdr.setSpacing(6)
        self.chat_title_lbl = QLabel("Sin chat seleccionado")
        self.chat_title_lbl.setStyleSheet("font-size: 14px; font-weight: 700;")
        chat_hdr.addWidget(self.chat_title_lbl)

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
        self.close_chat_btn.setToolTip("Ocultar panel de chat (reabrir con 'Chat')")
        self.close_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_chat_btn.clicked.connect(lambda: self._set_pane_visible("chat", False))
        chat_hdr.addWidget(self.close_chat_btn)
        chat_lay.addWidget(chat_hdr_frame)

        chat_body = QWidget()
        chat_body.setStyleSheet("background: transparent;")
        chat_body_lay = QVBoxLayout(chat_body)
        chat_body_lay.setContentsMargins(14, 8, 14, 10)
        chat_body_lay.setSpacing(8)

        self.chat_browser = QTextBrowser()
        self.chat_browser.setReadOnly(True)
        self.chat_browser.setOpenExternalLinks(True)
        self.chat_browser.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; padding: 2px; font-size: 13px; }"
        )
        self.chat_browser.setHtml(
            "<p style='color:#9299A8;'>Selecciona un canal de texto a la izquierda para ver y enviar mensajes aquí.</p>"
        )
        chat_body_lay.addWidget(self.chat_browser, 1)

        chat_input_row = QHBoxLayout()
        chat_input_row.setSpacing(8)
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("DiscordChatInput")
        self.chat_input.setPlaceholderText("Selecciona un canal #texto para escribir... [Enter]")
        self.chat_input.setEnabled(False)
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

        self.center_splitter.addWidget(self.chat_pane)
        self.center_splitter.setStretchFactor(0, 4)
        self.center_splitter.setStretchFactor(1, 5)
        self.center_splitter.setSizes([215, 275])

        right_col.addWidget(self.center_splitter, 1)

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

        # Deck inferior de audio centrado con superficie propia
        deck = QFrame()
        deck.setObjectName("DiscordDeckCard")
        deck_lay = QVBoxLayout(deck)
        deck_lay.setContentsMargins(12, 8, 12, 8)
        deck_lay.setSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self.mute_btn = QPushButton(" Silenciar")
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute_btn.clicked.connect(self._toggle_mute)
        btn_row.addWidget(self.mute_btn)

        self.deaf_btn = QPushButton(" Ensordecer")
        self.deaf_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.deaf_btn.clicked.connect(self._toggle_deaf)
        btn_row.addWidget(self.deaf_btn)

        self.vu_lbl = QLabel("Mic  0%")
        self.vu_lbl.setObjectName("MutedLabel")
        self.vu_lbl.setFixedWidth(54)
        btn_row.addWidget(self.vu_lbl)

        self.mic_vu_bar = QProgressBar()
        self.mic_vu_bar.setObjectName("HudProgress")
        self.mic_vu_bar.setRange(0, 100)
        self.mic_vu_bar.setValue(0)
        self.mic_vu_bar.setTextVisible(False)
        self.mic_vu_bar.setFixedHeight(7)
        self.mic_vu_bar.setFixedWidth(84)
        btn_row.addWidget(self.mic_vu_bar)

        self.audio_toggle_btn = QPushButton("Audio ▾")
        self.audio_toggle_btn.setObjectName("PillBtn")
        self.audio_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.audio_toggle_btn.setToolTip("Mostrar u ocultar volúmenes, dispositivos, VAD y supresión IA")
        self.audio_toggle_btn.clicked.connect(self._toggle_audio_drawer)
        btn_row.addWidget(self.audio_toggle_btn)

        self.hangup_btn = QPushButton(" Colgar")
        self.hangup_btn.setObjectName("DangerBtn")
        self.hangup_btn.setToolTip("Salir del canal de voz actual")
        self.hangup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hangup_btn.clicked.connect(self.svc.leave_channel)
        self.hangup_btn.setVisible(False)
        btn_row.addWidget(self.hangup_btn)

        btn_row.addStretch(1)
        deck_lay.addLayout(btn_row)

        self.audio_advanced_box = QWidget()
        adv_lay = QVBoxLayout(self.audio_advanced_box)
        adv_lay.setContentsMargins(0, 4, 0, 0)
        adv_lay.setSpacing(8)

        sliders_row = QHBoxLayout()
        sliders_row.setSpacing(14)

        out_col = QVBoxLayout()
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
        sliders_row.addLayout(out_col, 1)

        mic_col = QVBoxLayout()
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
        sliders_row.addLayout(mic_col, 1)

        vad_col = QVBoxLayout()
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
        sliders_row.addLayout(vad_col, 1)
        adv_lay.addLayout(sliders_row)

        dev_row = QHBoxLayout()
        dev_row.setSpacing(8)
        dev_row.addWidget(QLabel("Micrófono:"))
        self.input_combo = QComboBox()
        self.input_combo.addItem("Predeterminado del sistema", "")
        self.input_combo.currentIndexChanged.connect(self._on_devices_combo_changed)
        dev_row.addWidget(self.input_combo, 1)

        dev_row.addWidget(QLabel("Salida:"))
        self.output_combo = QComboBox()
        self.output_combo.addItem("Predeterminado del sistema", "")
        self.output_combo.currentIndexChanged.connect(self._on_devices_combo_changed)
        dev_row.addWidget(self.output_combo, 1)

        self.refresh_dev_btn = QPushButton()
        self.refresh_dev_btn.setObjectName("IconBtn")
        self.refresh_dev_btn.setFixedSize(28, 28)
        self.refresh_dev_btn.setToolTip("Detectar micrófonos y salidas de audio")
        self.refresh_dev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_dev_btn.clicked.connect(self._on_refresh_devices_clicked)
        dev_row.addWidget(self.refresh_dev_btn)
        adv_lay.addLayout(dev_row)

        ns_row = QHBoxLayout()
        ns_row.setSpacing(8)
        ns_row.addWidget(QLabel("Supresión ruido:"))
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
        ns_row.addWidget(self.ns_combo)

        self.agc_check = QCheckBox("Auto-ganancia")
        self.agc_check.setChecked(self.cfg.discord_mic_agc)
        self.agc_check.setToolTip(
            "Ajusta automáticamente el volumen de tu micrófono (AGC con limitador anti-saturación)"
        )
        self.agc_check.toggled.connect(self._on_agc_toggled)
        ns_row.addWidget(self.agc_check)
        ns_row.addStretch()
        adv_lay.addLayout(ns_row)

        self.audio_advanced_box.setVisible(False)
        deck_lay.addWidget(self.audio_advanced_box)

        right_col.addWidget(deck)
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
            "QListWidget::item { padding: 5px 4px; border-radius: 6px; }"
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
        self.svc.chat_history_loaded.connect(self._on_chat_history_loaded)
        self.svc.chat_message_received.connect(self._on_chat_message_received)
        self.svc.channel_message_incoming.connect(self._on_channel_message_incoming)
        self.svc.dm_incoming.connect(self._on_dm_incoming)
        self.svc.voice_state_updated.connect(self._on_voice_state_updated)
        self.svc.speaking_updated.connect(self._on_speaking_updated)
        self.svc.mic_level_updated.connect(self._on_mic_level_updated)
        self.svc.devices_updated.connect(self._on_devices_updated)
        self.svc.worker_ram_updated.connect(self._on_worker_ram_updated)
        self.svc.error_occurred.connect(self._on_service_error)
        self.svc.warning_occurred.connect(lambda msg: self.toast_requested.emit(msg))
        if self._thumb_cache is not None:
            self._thumb_cache.ready.connect(self._on_member_avatar_ready)

    def _emit_voice_status(self) -> None:
        active = bool(
            self._voice_state.get("in_voice", False)
            or self._bot_state == "connected"
            or self.svc.is_connected
        )
        self.voice_status_changed.emit(active, self._muted, self._deafened)

    def sync_notify_channels(self) -> None:
        self.svc.set_notify_channels(self.cfg.discord_notify_channels)

    def is_viewing_channel(self, channel_id: str) -> bool:
        return (
            self._active_chat_kind == "channel"
            and self._active_chat_id == str(channel_id)
            and self._chat_pane_visible
        )

    def refresh_ui_preferences(self) -> None:
        """Aplica cambios de preferencias visuales (canales sin permiso, avisos, etc.)."""
        self._render_channels(force=True)
        self._emit_voice_status()

    def _refresh_header_toggles(self) -> None:
        self.toggle_channels_btn.setProperty("active", "true" if self._channels_visible else "false")
        self.toggle_members_btn.setProperty("active", "true" if self._members_visible else "false")
        self.audio_toggle_btn.setProperty("active", "true" if self._audio_drawer_open else "false")
        for b in (self.toggle_channels_btn, self.toggle_members_btn, self.audio_toggle_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        self.audio_toggle_btn.setText("Audio ▴" if self._audio_drawer_open else "Audio ▾")

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
        self.tc_section_btn.setText(f"{arrow} CANALES DE TEXTO{suffix}")

    def _sync_center_panes(self) -> None:
        self.voice_pane.setVisible(self._voice_pane_visible)
        self.chat_pane.setVisible(self._chat_pane_visible)
        any_center = bool(self._voice_pane_visible or self._chat_pane_visible)
        self.center_splitter.setVisible(any_center)
        self.center_collapsed_card.setVisible(not any_center)
        self.tab_voice_btn.setProperty("active", "true" if self._voice_pane_visible else "false")
        self.tab_chat_btn.setProperty("active", "true" if self._chat_pane_visible else "false")
        for b in (self.tab_voice_btn, self.tab_chat_btn):
            b.style().unpolish(b)
            b.style().polish(b)

    def _set_pane_visible(self, which: Literal["voice", "chat"], visible: bool) -> None:
        if which == "voice":
            self._voice_pane_visible = bool(visible)
        else:
            self._chat_pane_visible = bool(visible)
        self._sync_center_panes()

    def _on_center_tab_toggled(self, which: Literal["voice", "chat"]) -> None:
        if which == "voice":
            self._voice_pane_visible = not self._voice_pane_visible
        else:
            self._chat_pane_visible = not self._chat_pane_visible
        self._sync_center_panes()

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
            self._chat_pane_visible = True
            self._sync_center_panes()

    def _set_center_tab(self, idx: int) -> None:
        """Compatibilidad: 0 = solo voz, 1 = solo chat, 2 = vista dividida."""
        val = int(idx)
        if val == 0:
            self._voice_pane_visible = True
            self._chat_pane_visible = False
        elif val == 1:
            self._voice_pane_visible = False
            self._chat_pane_visible = True
        else:
            self._voice_pane_visible = True
            self._chat_pane_visible = True
        self._sync_center_panes()

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
            self.info_pill.setToolTip(
                f"{dave_line}\n"
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
        self.hangup_btn.setIcon(render_icon("phone_off", "#F87171", 15))
        close_ico = render_icon("close", p.text2, 13)
        for btn in (
            self.close_channels_btn,
            self.intents_close_btn,
            self.close_voice_btn,
            self.close_chat_btn,
            self.close_members_btn,
        ):
            btn.setIcon(close_ico)
        self._update_info_pill()
        self._refresh_mute_deaf_buttons()
        for card in self._participant_cards_order:
            card.update_theme(p)

    def _refresh_mute_deaf_buttons(self) -> None:
        p = self.palette
        if self._muted:
            self.mute_btn.setIcon(render_icon("mic_off", "#F87171", 16))
            self.mute_btn.setText(" Silenciado")
            self.mute_btn.setProperty("active", "true")
        else:
            self.mute_btn.setIcon(render_icon("mic", p.text, 16))
            self.mute_btn.setText(" Silenciar")
            self.mute_btn.setProperty("active", "false")

        if self._deafened:
            self.deaf_btn.setIcon(render_icon("headphones_off", "#F87171", 16))
            self.deaf_btn.setText(" Ensordecido")
            self.deaf_btn.setProperty("active", "true")
        else:
            self.deaf_btn.setIcon(render_icon("headphones", p.text, 16))
            self.deaf_btn.setText(" Ensordecer")
            self.deaf_btn.setProperty("active", "false")

        for b in (self.mute_btn, self.deaf_btn):
            b.style().unpolish(b)
            b.style().polish(b)

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
            self.status_pill.setText("Conectando...")
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
            self._update_info_pill()
            self.sync_notify_channels()
        elif state == "error":
            self._joining_channel_id = ""
            self.status_pill.setText("Error")
            self.connect_btn.setText(
                "Desconectar" if self.svc.is_worker_running else "Conectar Bot"
            )
            self.connect_btn.setObjectName(
                "DangerBtn" if self.svc.is_worker_running else "AccentBtn"
            )
            self.intents_banner.setVisible(False)
            self.info_pill.setVisible(False)
        else:
            self._joining_channel_id = ""
            self._selected_guild_id = ""
            self._active_chat_kind = ""
            self._active_chat_id = ""
            self._active_chat_can_send = False
            self._active_dm_peer_name = ""
            self._active_dm_peer_avatar = ""
            self._unread_channels.clear()
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
            self.members_list.clear()
            self.members_online_lbl.setText("0 en línea")
            self.toggle_members_btn.setText("Miembros")
            self.dm_btn.setEnabled(False)
            self.dm_btn.setVisible(False)
            self.dm_hint_lbl.setVisible(False)
            self.chat_title_lbl.setText("Sin chat seleccionado")
            self.chat_input.clear()
            self.chat_input.setPlaceholderText("Selecciona un canal #texto para escribir... [Enter]")
            self.chat_input.setEnabled(False)
            self.chat_send_btn.setEnabled(False)
            self.chat_browser.setHtml(
                "<p style='color:#9299A8;'>Selecciona un canal de texto a la izquierda para ver y enviar mensajes aquí.</p>"
            )
            self.intents_banner.setVisible(False)
            self.info_pill.setVisible(False)
            self.status_pill.setText("Desconectado")
            self.connect_btn.setText("Conectar Bot")
            self.connect_btn.setObjectName("AccentBtn")
            self.hangup_btn.setVisible(False)
            self._guilds = []
            self._render_channels(force=True)
            self._render_dm_threads()
            self._voice_state = {"in_voice": False, "participants": []}
            self._render_participants(force=True)

        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)
        self._emit_voice_status()

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
            self._unread_channels.discard(cid)
            self.chat_title_lbl.setText(f"# {cname}")
            self.chat_input.setPlaceholderText(f"Enviar mensaje a #{cname}... [Enter]")
            self._render_channels(force=True)
            self.svc.open_text_channel(gid, cid)

    def _on_guilds_updated(self, guilds: list[dict[str, Any]]) -> None:
        self._guilds = guilds
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
        gid = str(self.guild_combo.currentData() or "")
        if gid and self.svc.is_connected and gid != self._selected_guild_id:
            self._selected_guild_id = gid
            self.svc.select_guild(gid)
        if gid:
            self._auto_open_first_text_channel(gid)

    def _on_guild_combo_changed(self, _idx: int) -> None:
        self._active_chat_kind = ""
        self._active_chat_id = ""
        self._active_chat_can_send = False
        self._channel_members = []
        self._channel_online_count = 0
        self._channel_members_loaded = False
        self._chat_messages.clear()
        self._render_channels(force=True)
        gid = str(self.guild_combo.currentData() or "")
        if gid and self.svc.is_connected and gid != self._selected_guild_id:
            self._selected_guild_id = gid
            self._member_row_by_url.clear()
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

        row_frame = QFrame()
        row_frame.setObjectName("DiscordChannelRow")
        row_frame.setProperty("active", "true" if is_cur else "false")
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
        elif members:
            cnt_lbl = QLabel(f"{len(members)}")
            cnt_lbl.setObjectName("DeckTag")
            top.addWidget(cnt_lbl)

        if is_cur:
            btn = QPushButton("Salir")
            btn.setObjectName("DangerBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(self.svc.leave_channel)
        elif is_joining:
            btn = QPushButton("...")
            btn.setEnabled(False)
        elif not can_connect:
            btn = QPushButton("Bloq.")
            btn.setEnabled(False)
            btn.setToolTip("El bot no tiene permiso 'Conectar' en este canal.")
        else:
            btn = QPushButton("Entrar")
            btn.setObjectName("AccentBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _c=False, g=gid, c=cid, n=cname: self._on_join_channel_clicked(g, c, n)
            )
        top.addWidget(btn)
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
                mrow_w.setStyleSheet("background: transparent;")
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
                dot.setStyleSheet(
                    "color: #4ADE80; font-size: 9px;"
                    if is_cur
                    else f"color: {self.palette.text2}; font-size: 9px;"
                )
                m_row.addWidget(dot)

                mlbl = QLabel(m_name)
                mlbl.setObjectName("MutedLabel")
                mlbl.setStyleSheet("font-size: 11px;")
                m_row.addWidget(mlbl, 1)

                if m_bot:
                    btag = QLabel("APP")
                    btag.setObjectName("DeckTag")
                    m_row.addWidget(btag)
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
            tuple(sorted(self._unread_channels)),
        )

    def _render_channels(self, force: bool = False) -> None:
        gid = str(self.guild_combo.currentData() or "")
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
        sig = self._channels_signature(gid, guild)
        if not force and sig == self._last_channels_sig:
            return
        self._last_channels_sig = sig

        _clear_layout(self.channels_lay)
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
                has_unread = cid in self._unread_channels

                row_w = QWidget()
                row_w.setStyleSheet("background: transparent;")
                tc_row = QHBoxLayout(row_w)
                tc_row.setContentsMargins(0, 0, 0, 0)
                tc_row.setSpacing(4)

                prefix = "● " if has_unread else ""
                tbtn = QPushButton(f"  {prefix}# {cname}")
                tbtn.setObjectName("NavBtn")
                tbtn.setProperty("active", "true" if is_active_tc else "false")
                if has_unread and not is_active_tc:
                    tbtn.setStyleSheet("color: #F1F3F6; font-weight: 700;")
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

    def _open_text_channel_clicked(self, guild_id: str, channel_id: str, name: str) -> None:
        self._active_chat_kind = "channel"
        self._active_chat_id = channel_id
        self._unread_channels.discard(channel_id)
        self.chat_title_lbl.setText(f"# {name}")
        self.chat_input.setPlaceholderText(f"Enviar mensaje a #{name}... [Enter]")
        self._ensure_chat_pane_visible()
        self._render_channels(force=True)
        self.svc.open_text_channel(guild_id, channel_id)

    def _on_channel_message_incoming(self, data: dict[str, Any]) -> None:
        if not self.cfg.discord_notify_chat_enabled:
            return
        cid = str(data.get("channel_id") or "")
        if not cid or cid not in self.cfg.discord_notify_channels:
            return
        if not self.is_viewing_channel(cid):
            self._unread_channels.add(cid)
            self._render_channels(force=True)
        self.channel_alert_requested.emit(data)

    def _on_guild_members_updated(self, data: dict[str, Any]) -> None:
        cur_gid = str(self.guild_combo.currentData() or "")
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
            st = str(m.get("status") or "offline")
            full_act = str(m.get("activity") or "").strip()
            short_act = full_act[:33] + "..." if len(full_act) > 36 else full_act
            dot = status_dots.get(st, "⚪")
            tag = " [APP]" if is_bot else ""
            text = f"{dot} {name}{tag}"
            if short_act:
                text += f"\n    {short_act}"
            item = QListWidgetItem(text)
            item.setData(
                Qt.ItemDataRole.UserRole,
                {"id": uid, "name": name, "is_bot": is_bot, "avatar_url": av_url},
            )
            st_lbl = status_names.get(st, st)
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

    def _upsert_dm_thread_local(
        self, uid: str, name: str, avatar_url: str = "", preview: str = ""
    ) -> None:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return
        clean_name = str(name or clean_id).strip() or clean_id
        clean_av = str(avatar_url or "").strip()
        clean_prev = str(preview or "").strip()[:120]
        if self._db is not None:
            self._db.upsert_discord_dm_thread(
                clean_id, clean_name, avatar_url=clean_av, last_preview=clean_prev
            )
            return
        prev_entry = next((t for t in self._dm_threads_mem if t["user_id"] == clean_id), None)
        self._dm_threads_mem = [t for t in self._dm_threads_mem if t["user_id"] != clean_id]
        self._dm_threads_mem.insert(
            0,
            {
                "user_id": clean_id,
                "user_name": clean_name or (prev_entry["user_name"] if prev_entry else clean_id),
                "avatar_url": clean_av or (prev_entry["avatar_url"] if prev_entry else ""),
                "last_preview": clean_prev or (prev_entry["last_preview"] if prev_entry else ""),
            },
        )

    def _save_dm_message_local(
        self, uid: str, msg: dict[str, Any], peer_name: str = "", peer_avatar_url: str = ""
    ) -> None:
        clean_id = str(uid or "").strip()
        if not clean_id or not isinstance(msg, dict):
            return
        if self._db is not None:
            self._db.save_discord_dm_message(
                clean_id, msg, peer_name=peer_name, peer_avatar_url=peer_avatar_url
            )
            return
        bucket = self._dm_msgs_mem.setdefault(clean_id, [])
        mid = str(msg.get("id") or "")
        if not mid or not any(str(x.get("id") or "") == mid for x in bucket):
            bucket.append(dict(msg))
            if len(bucket) > 80:
                del bucket[:-80]
        prev = str(msg.get("content") or "").strip()
        self._upsert_dm_thread_local(clean_id, peer_name, peer_avatar_url, prev)

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
        if self._active_chat_kind == "dm" and self._active_chat_id == clean_id:
            self._active_chat_kind = ""
            self._active_chat_id = ""
            self._active_dm_peer_name = ""
            self._active_dm_peer_avatar = ""
            self.chat_title_lbl.setText("Sin chat seleccionado")
        self._render_dm_threads()

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
        arrow = "▾" if self._dm_section_open else "▸"
        suffix = f" ({len(threads)})" if threads else ""
        self.dm_section_btn.setText(f"{arrow} MENSAJES DIRECTOS{suffix}")
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
            btn = QPushButton(f"  @ {uname}")
            btn.setObjectName("NavBtn")
            btn.setProperty("active", "true" if is_act else "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            tip = f"DM con {uname}" + (f"\nÚltimo: {prev}" if prev else "")
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
            self.dm_channels_lay.addWidget(btn)

    def _open_dm_with_user(self, uid: str, name: str, avatar_url: str = "") -> None:
        clean_id = str(uid or "").strip()
        if not clean_id:
            return
        peer_name = str(name or clean_id).strip() or clean_id
        self._active_chat_kind = "dm"
        self._active_chat_id = clean_id
        self._active_dm_peer_name = peer_name
        self._active_dm_peer_avatar = str(avatar_url or "").strip()
        self.chat_title_lbl.setText(f"DM · {peer_name}")

        self._upsert_dm_thread_local(clean_id, peer_name, self._active_dm_peer_avatar)
        saved_msgs = self._load_dm_messages_local(clean_id)
        self._chat_messages.clear()
        for sm in saved_msgs[-80:]:
            self._chat_messages.append(sm)

        can_send = self.svc.is_connected
        self._active_chat_can_send = can_send
        self.chat_input.setEnabled(can_send)
        self.chat_send_btn.setEnabled(can_send)
        self.chat_input.setPlaceholderText(
            f"Enviar mensaje directo a {peer_name}... [Enter]"
            if can_send
            else "Conecta el Bot para enviar nuevos mensajes (historial guardado visible)."
        )
        self._ensure_chat_pane_visible()
        self._render_channels(force=True)
        self._render_dm_threads()
        self._render_chat_messages(can_read=True)
        if can_send:
            self.svc.open_dm(clean_id)

    def _on_chat_history_loaded(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        if not kind or not tid:
            return
        self._active_chat_kind = kind
        self._active_chat_id = tid
        self._active_chat_can_send = bool(data.get("can_send", False))
        if kind == "channel":
            self._unread_channels.discard(tid)
        raw_title = str(data.get("title") or "Chat")
        clean_title = (
            f"# {raw_title[1:].strip()}"
            if raw_title.startswith("#") and not raw_title.startswith("# ")
            else raw_title
        )
        self.chat_title_lbl.setText(clean_title)
        live_msgs = list(data.get("messages") or [])[-80:]
        if kind == "dm":
            peer_name = str(
                data.get("peer_name")
                or clean_title.replace("DM · ", "").strip()
                or self._active_dm_peer_name
                or tid
            )
            peer_av = str(data.get("peer_avatar_url") or self._active_dm_peer_avatar or "")
            self._active_dm_peer_name = peer_name
            self._active_dm_peer_avatar = peer_av
            self._upsert_dm_thread_local(tid, peer_name, peer_av)
            for m in live_msgs:
                if isinstance(m, dict):
                    self._save_dm_message_local(tid, m, peer_name=peer_name, peer_avatar_url=peer_av)
            if not live_msgs:
                live_msgs = self._load_dm_messages_local(tid)
            self._render_dm_threads()

        self._chat_messages.clear()
        for m in live_msgs[-80:]:
            self._chat_messages.append(m)

        if kind == "channel" and "channel_members" in data:
            self._channel_members = list(data.get("channel_members") or [])
            self._channel_online_count = int(data.get("channel_online_count") or 0)
            self._channel_members_loaded = True
            self._refresh_members_list()

        can_read = bool(data.get("can_read", True))
        self.chat_input.setEnabled(self._active_chat_can_send)
        self.chat_send_btn.setEnabled(self._active_chat_can_send)
        if self._active_chat_can_send:
            target_label = clean_title.replace("# ", "#")
            self.chat_input.setPlaceholderText(f"Enviar mensaje a {target_label}... [Enter]")
        else:
            self.chat_input.setPlaceholderText("Sin permiso para enviar mensajes en este canal.")
        self._ensure_chat_pane_visible()
        self._render_channels()
        self._render_chat_messages(can_read=can_read)

    def _on_chat_message_received(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        msg = data.get("message")
        if not isinstance(msg, dict):
            return
        if kind == "dm" and tid:
            peer_name = str(
                data.get("peer_name") or self._active_dm_peer_name or tid
            )
            peer_av = str(data.get("peer_avatar_url") or self._active_dm_peer_avatar or "")
            self._save_dm_message_local(tid, msg, peer_name=peer_name, peer_avatar_url=peer_av)
            self._render_dm_threads()
        if kind != self._active_chat_kind or tid != self._active_chat_id:
            return
        mid = str(msg.get("id") or "")
        if mid and any(str(x.get("id") or "") == mid for x in self._chat_messages):
            return
        self._chat_messages.append(msg)
        self._render_chat_messages(can_read=True)

    def _on_dm_incoming(self, data: dict[str, Any]) -> None:
        uid = str(data.get("user_id") or "")
        author = str(data.get("author_name") or "Usuario")
        peer_name = str(data.get("peer_name") or author)
        peer_av = str(data.get("peer_avatar_url") or "")
        preview = str(data.get("preview") or "").strip()
        raw_msg = data.get("message")
        if uid and isinstance(raw_msg, dict):
            self._save_dm_message_local(uid, raw_msg, peer_name=peer_name, peer_avatar_url=peer_av)
        elif uid:
            self._upsert_dm_thread_local(uid, peer_name, peer_av, preview)
        self._render_dm_threads()
        msg = f"DM de {author}: {preview}" if preview else f"Nuevo mensaje directo de {author}"
        self.toast_requested.emit(msg)
        self.sound_event_requested.emit("message")

    def _on_send_chat_clicked(self) -> None:
        if not self._active_chat_kind or not self._active_chat_id or not self._active_chat_can_send:
            return
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        if self._active_chat_kind == "dm":
            self._upsert_dm_thread_local(
                self._active_chat_id,
                self._active_dm_peer_name or self._active_chat_id,
                self._active_dm_peer_avatar,
                preview=text[:120],
            )
            self._render_dm_threads()
        self.svc.send_chat_message(self._active_chat_kind, self._active_chat_id, text)

    def _render_chat_messages(self, *, can_read: bool = True) -> None:
        if not can_read and not self._chat_messages:
            self.chat_browser.setHtml(
                "<p style='color:#9299A8;'>El bot no tiene permiso <b>Read Message History</b> en este canal. Los mensajes nuevos en vivo sí aparecerán aquí.</p>"
            )
            return
        if not self._chat_messages:
            self.chat_browser.setHtml(
                "<p style='color:#9299A8;'>Aún no hay mensajes recientes en este chat.</p>"
            )
            return

        blocks: list[str] = []
        last_date = ""
        for msg in self._chat_messages:
            dlabel = str(msg.get("date_label") or "").strip()
            if dlabel and dlabel != last_date:
                last_date = dlabel
                d_esc = html.escape(dlabel)
                blocks.append(
                    "<table width='100%' cellspacing='0' cellpadding='0' style='margin: 6px 0 6px 0;'>"
                    "<tr>"
                    "<td width='45%' valign='middle'><hr color='#262B36'></td>"
                    f"<td align='center' valign='middle' style='color:#9299A8; font-size:11px; padding:0 8px; white-space:nowrap;'>{d_esc}</td>"
                    "<td width='45%' valign='middle'><hr color='#262B36'></td>"
                    "</tr></table>"
                )

            raw_author = str(msg.get("author_name") or "Usuario")
            author = html.escape(raw_author)
            initial = html.escape((raw_author.strip()[:1] or "?").upper())
            av_bg = _avatar_tint_for(raw_author)
            ts = html.escape(str(msg.get("timestamp") or ""))
            is_self = bool(msg.get("is_self", False))
            is_bot = bool(msg.get("is_bot", False))
            color = "#CFD5DF" if is_self else "#F1F3F6"
            bot_tag = (
                " <span style='background-color:#262B36; color:#CFD5DF; font-size:10px; font-weight:700;'> APP </span>"
                if is_bot
                else ""
            )
            ts_html = (
                f" <span style='color:#9299A8; font-size:11px;'>{ts}</span>" if ts else ""
            )
            raw_content = str(msg.get("content") or "")
            body_html = html.escape(raw_content).replace("\n", "<br>") if raw_content else ""
            atts = list(msg.get("attachments") or [])
            att_html = ""
            if atts:
                links = []
                for a in atts:
                    aname = html.escape(str(a.get("name") or "archivo"))
                    aurl = html.escape(str(a.get("url") or ""))
                    if aurl:
                        links.append(
                            f"<a href='{aurl}' style='color:#93C5FD;'>[{aname}]</a>"
                        )
                    else:
                        links.append(f"[{aname}]")
                att_html = ("<br>" if body_html else "") + " ".join(links)
            if not body_html and not att_html:
                body_html = "<span style='color:#9299A8; font-style:italic;'>(contenido multimedia o embed)</span>"

            blocks.append(
                "<table width='100%' cellspacing='0' cellpadding='2' style='margin-bottom:4px;'>"
                "<tr>"
                f"<td width='34' valign='top' align='center' style='padding-top:2px;'>"
                f"<span style='background-color:{av_bg}; color:#F1F3F6; font-weight:700; font-size:12px;'>&nbsp;{initial}&nbsp;</span>"
                "</td>"
                "<td valign='top' style='line-height:1.35;'>"
                f"<b style='color:{color};'>{author}</b>{bot_tag}{ts_html}<br>"
                f"<span style='color:#D8DEE9;'>{body_html}{att_html}</span>"
                "</td>"
                "</tr></table>"
            )
        self.chat_browser.setHtml("".join(blocks))
        sb = self.chat_browser.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

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

    def _on_join_channel_clicked(self, guild_id: str, channel_id: str, channel_name: str) -> None:
        self._joining_channel_id = channel_id
        self.status_pill.setText(f"Conectando a {channel_name}...")
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
        self.hangup_btn.setVisible(in_voice)

        if in_voice:
            ch_name = str(state.get("channel_name") or "Voz")
            g_name = str(state.get("guild_name") or "")
            self.active_channel_lbl.setText(f"{ch_name} · {g_name}" if g_name else ch_name)
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
        if w >= 660:
            return 3
        if w >= 420:
            return 2
        return 1

    def _relayout_participant_cards(self) -> None:
        if not self._participant_cards_order:
            return
        cols = self._compute_grid_cols()
        self._last_grid_cols = cols
        _clear_grid_keep_widgets(self.participants_grid)
        for c in range(3):
            self.participants_grid.setColumnStretch(c, 1 if c < cols else 0)
        for idx, card in enumerate(self._participant_cards_order):
            r, c = divmod(idx, cols)
            self.participants_grid.addWidget(card, r, c)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._participant_cards_order and self._compute_grid_cols() != self._last_grid_cols:
            self._relayout_participant_cards()

    def _participants_signature(self) -> Any:
        in_voice = bool(self._voice_state.get("in_voice", False))
        parts = self._voice_state.get("participants") or []
        return (
            in_voice,
            tuple(
                (
                    str(p.get("id") or ""),
                    str(p.get("name") or ""),
                    bool(p.get("is_bot", False)),
                    bool(p.get("is_self", False)),
                    bool(p.get("muted", False)),
                    bool(p.get("deafened", False)),
                    str(p.get("avatar_url") or ""),
                )
                for p in parts
            ),
        )

    def _render_participants(self, force: bool = False) -> None:
        sig = self._participants_signature()
        if not force and sig == self._last_participants_sig:
            return
        self._last_participants_sig = sig

        self._participant_cards.clear()
        self._participant_cards_order.clear()
        _clear_layout(self.participants_grid)
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
            card = _ParticipantCard(
                part,
                vol,
                speaking,
                self.palette,
                thumb_cache=self._thumb_cache,
            )
            card.volume_changed.connect(self._on_user_vol_value_changed)
            card.dm_requested.connect(self._open_dm_with_user)
            self._participant_cards[key] = card
            self._participant_cards_order.append(card)
        self._relayout_participant_cards()

    def _on_speaking_updated(self, speaking_map: dict[str, bool]) -> None:
        self._speaking = dict(speaking_map)
        for key, card in self._participant_cards.items():
            card.set_speaking(bool(self._speaking.get(key, False)))

    def _on_mic_level_updated(self, rms: int, transmitting: bool) -> None:
        val = max(0, min(100, int(rms)))
        self.vu_lbl.setText(f"Mic {val:2d}%")
        self.mic_vu_bar.setValue(val)
        if transmitting:
            self.mic_vu_bar.setStyleSheet("QProgressBar::chunk { background-color: #4ADE80; }")
        else:
            self.mic_vu_bar.setStyleSheet("")

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

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._refresh_mute_deaf_buttons()
        self.svc.set_mute(self._muted)
        self._emit_voice_status()
        self.sound_event_requested.emit("mute_on" if self._muted else "mute_off")

    def _toggle_deaf(self) -> None:
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
