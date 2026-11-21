"""Panel nativo de Discord Voz y Chat (Bot puente aislado, 0 MB en reposo)."""

from __future__ import annotations

from collections import deque
import html
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig, save_config
from swiftplay.discord_service import DiscordService
from swiftplay.security import has_discord_token
from swiftplay.themes import ThemePalette
from swiftplay.ui.icons import render_icon


def _clear_layout(layout: QLayout) -> None:
    while layout.count() > 0:
        item = layout.takeAt(0)
        if item is None:
            continue
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)


def _make_section_toggle_btn(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("SectionHeader")
    btn.setFlat(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        "QPushButton { border: none; background: transparent; text-align: left; padding: 2px 2px; }"
    )
    return btn


class DiscordVoicePanel(QWidget):
    """Vista minimalista de Discord Voz + Estados + Chat (Bot puente aislado, 0 MB en reposo)."""

    open_token_settings_requested = Signal()
    open_shortcuts_settings_requested = Signal()
    toast_requested = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        service: DiscordService,
        palette: ThemePalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = config
        self.svc = service
        self.palette = palette

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
        self._participant_dots: dict[str, QLabel] = {}
        self._populating_devices = False
        self._selected_guild_id = ""
        self._active_chat_kind = ""
        self._active_chat_id = ""
        self._active_chat_can_send = False
        self._chat_messages: deque[dict[str, Any]] = deque(maxlen=80)
        self._members: list[dict[str, Any]] = []
        self._worker_ws_mb = 0.0
        self._worker_priv_mb = 0.0

        # Estado de acordeones y cajones plegables (interfaz limpia por defecto)
        self._channels_visible = True
        self._members_visible = False
        self._audio_drawer_open = False
        self._vc_section_open = True
        self._tc_section_open = False
        self._locked_vc_expanded = False

        self._build_ui()
        self._connect_service_signals()
        self.refresh_token_state()
        self.update_theme(self.palette)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
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
        self.toggle_members_btn.setToolTip("Mostrar u ocultar miembros conectados del servidor")
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
        self.setup_card.setObjectName("MediaCard")
        setup_lay = QVBoxLayout(self.setup_card)
        setup_lay.setContentsMargins(18, 16, 18, 16)
        setup_lay.setSpacing(10)

        setup_title = QLabel("Puente ultraliviano con Bot oficial (0 MB en reposo)")
        setup_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        setup_lay.addWidget(setup_title)

        setup_desc = QLabel(
            "Conecta voz con DAVE (E2EE), mira el estado de miembros del servidor y chatea sin la pesada "
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

        # Columna izquierda plegable: Selector de servidor + acordeones Voz / Texto
        self.left_card = QFrame()
        self.left_card.setObjectName("MediaCard")
        self.left_card.setMinimumWidth(235)
        self.left_card.setMaximumWidth(275)
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
        left_lay.addLayout(srv_row)

        # Acordeón Canales de Voz (abierto por defecto)
        self.vc_section_btn = _make_section_toggle_btn("▾ CANALES DE VOZ")
        self.vc_section_btn.clicked.connect(self._toggle_voice_channels_section)
        left_lay.addWidget(self.vc_section_btn)

        self.channels_scroll = QScrollArea()
        self.channels_scroll.setWidgetResizable(True)
        self.channels_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ch_container = QWidget()
        self.channels_lay = QVBoxLayout(ch_container)
        self.channels_lay.setContentsMargins(0, 2, 2, 4)
        self.channels_lay.setSpacing(5)
        self.channels_empty_lbl = QLabel("Pulsa 'Conectar Bot' para ver tus canales.")
        self.channels_empty_lbl.setObjectName("MutedLabel")
        self.channels_empty_lbl.setWordWrap(True)
        self.channels_lay.addWidget(self.channels_empty_lbl)
        self.channels_lay.addStretch()
        self.channels_scroll.setWidget(ch_container)
        left_lay.addWidget(self.channels_scroll, 2)

        # Acordeón Canales de Texto (plegado por defecto)
        self.tc_section_btn = _make_section_toggle_btn("▸ CANALES DE TEXTO")
        self.tc_section_btn.clicked.connect(self._toggle_text_channels_section)
        left_lay.addWidget(self.tc_section_btn)

        self.text_channels_scroll = QScrollArea()
        self.text_channels_scroll.setWidgetResizable(True)
        self.text_channels_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        tc_container = QWidget()
        self.text_channels_lay = QVBoxLayout(tc_container)
        self.text_channels_lay.setContentsMargins(0, 2, 2, 4)
        self.text_channels_lay.setSpacing(4)
        tc_empty = QLabel("Conecta el bot para ver canales de texto.")
        tc_empty.setObjectName("MutedLabel")
        tc_empty.setWordWrap(True)
        self.text_channels_lay.addWidget(tc_empty)
        self.text_channels_lay.addStretch()
        self.text_channels_scroll.setWidget(tc_container)
        self.text_channels_scroll.setVisible(False)
        left_lay.addWidget(self.text_channels_scroll, 1)

        left_lay.addStretch(0)
        ws_lay.addWidget(self.left_card)

        # Columna central: Banner intents + Voz/Chat + Barra compacta de audio
        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        self.intents_banner = QFrame()
        self.intents_banner.setObjectName("MediaCard")
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
        ib_close = QPushButton("×")
        ib_close.setFixedSize(22, 22)
        ib_close.setToolTip("Ocultar aviso")
        ib_close.clicked.connect(lambda: self.intents_banner.setVisible(False))
        ib_lay.addWidget(ib_close)
        self.intents_banner.setVisible(False)
        right_col.addWidget(self.intents_banner)

        call_card = QFrame()
        call_card.setObjectName("MediaCard")
        call_lay = QVBoxLayout(call_card)
        call_lay.setContentsMargins(14, 10, 14, 10)
        call_lay.setSpacing(8)

        call_hdr = QHBoxLayout()
        call_hdr.setSpacing(6)
        self.tab_voice_btn = QPushButton("Voz")
        self.tab_voice_btn.setObjectName("PillBtn")
        self.tab_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tab_voice_btn.clicked.connect(lambda: self._set_center_tab(0))
        call_hdr.addWidget(self.tab_voice_btn)

        self.tab_chat_btn = QPushButton("Chat")
        self.tab_chat_btn.setObjectName("PillBtn")
        self.tab_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tab_chat_btn.clicked.connect(lambda: self._set_center_tab(1))
        call_hdr.addWidget(self.tab_chat_btn)

        self.active_channel_lbl = QLabel("Sin canal activo")
        self.active_channel_lbl.setStyleSheet("font-size: 14px; font-weight: 700; margin-left: 6px;")
        call_hdr.addWidget(self.active_channel_lbl)

        self.chat_title_lbl = QLabel("Sin chat seleccionado")
        self.chat_title_lbl.setStyleSheet("font-size: 14px; font-weight: 700; margin-left: 6px;")
        self.chat_title_lbl.setVisible(False)
        call_hdr.addWidget(self.chat_title_lbl)

        self.bot_chat_badge = QLabel("Envía como Bot")
        self.bot_chat_badge.setObjectName("DeckTag")
        self.bot_chat_badge.setToolTip(
            "Los mensajes escritos aquí se envían usando la cuenta del Bot oficial."
        )
        self.bot_chat_badge.setVisible(False)
        call_hdr.addWidget(self.bot_chat_badge)
        call_hdr.addStretch()
        call_lay.addLayout(call_hdr)

        self.center_stack = QStackedWidget()

        # Página 0: Participantes en llamada de voz
        self.participants_scroll = QScrollArea()
        self.participants_scroll.setWidgetResizable(True)
        self.participants_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        part_container = QWidget()
        self.participants_lay = QVBoxLayout(part_container)
        self.participants_lay.setContentsMargins(0, 4, 4, 4)
        self.participants_lay.setSpacing(6)
        self.participants_empty_lbl = QLabel(
            "Elige un canal de voz a la izquierda para conectarte y hablar en tiempo real."
        )
        self.participants_empty_lbl.setObjectName("MutedLabel")
        self.participants_empty_lbl.setWordWrap(True)
        self.participants_lay.addWidget(self.participants_empty_lbl)
        self.participants_lay.addStretch()
        self.participants_scroll.setWidget(part_container)
        self.center_stack.addWidget(self.participants_scroll)

        # Página 1: Chat de canal de texto o DM
        chat_page = QWidget()
        chat_lay = QVBoxLayout(chat_page)
        chat_lay.setContentsMargins(0, 2, 0, 0)
        chat_lay.setSpacing(8)

        self.chat_browser = QTextBrowser()
        self.chat_browser.setReadOnly(True)
        self.chat_browser.setOpenExternalLinks(True)
        self.chat_browser.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; padding: 2px; font-size: 13px; }"
        )
        self.chat_browser.setHtml(
            "<p style='color:#94A3B8;'>Abre <b>Canales de texto</b> o pulsa <b>Miembros</b> en la barra superior para iniciar un chat o DM.</p>"
        )
        chat_lay.addWidget(self.chat_browser, 1)

        chat_input_row = QHBoxLayout()
        chat_input_row.setSpacing(8)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Selecciona un canal #texto o miembro para escribir... [Enter]")
        self.chat_input.setEnabled(False)
        self.chat_input.returnPressed.connect(self._on_send_chat_clicked)
        chat_input_row.addWidget(self.chat_input, 1)

        self.chat_send_btn = QPushButton("Enviar")
        self.chat_send_btn.setObjectName("AccentBtn")
        self.chat_send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_send_btn.setEnabled(False)
        self.chat_send_btn.clicked.connect(self._on_send_chat_clicked)
        chat_input_row.addWidget(self.chat_send_btn)
        chat_lay.addLayout(chat_input_row)

        self.center_stack.addWidget(chat_page)
        call_lay.addWidget(self.center_stack, 1)
        self._set_center_tab(0)

        right_col.addWidget(call_card, 1)

        # Deck inferior de audio: barra esencial 1 fila + cajón desplegable (oculto por defecto)
        deck = QFrame()
        deck.setObjectName("MediaCard")
        deck_lay = QVBoxLayout(deck)
        deck_lay.setContentsMargins(12, 9, 12, 9)
        deck_lay.setSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

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
        self.mic_vu_bar.setFixedHeight(8)
        self.mic_vu_bar.setMinimumWidth(90)
        btn_row.addWidget(self.mic_vu_bar, 1)

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
        deck_lay.addLayout(btn_row)

        # Cajón avanzado oculto por defecto
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

        # Columna derecha desplegable: Miembros del servidor (OCULTA por defecto)
        self.members_card = QFrame()
        self.members_card.setObjectName("MediaCard")
        self.members_card.setMinimumWidth(210)
        self.members_card.setMaximumWidth(245)
        m_lay = QVBoxLayout(self.members_card)
        m_lay.setContentsMargins(10, 10, 10, 10)
        m_lay.setSpacing(6)

        m_hdr = QHBoxLayout()
        m_title = QLabel("MIEMBROS")
        m_title.setObjectName("SectionHeader")
        m_hdr.addWidget(m_title)
        m_hdr.addStretch()
        self.members_online_lbl = QLabel("0 en línea")
        self.members_online_lbl.setObjectName("MutedLabel")
        m_hdr.addWidget(self.members_online_lbl)

        m_close = QPushButton("×")
        m_close.setFixedSize(22, 22)
        m_close.setToolTip("Ocultar panel de miembros")
        m_close.setCursor(Qt.CursorShape.PointingHandCursor)
        m_close.clicked.connect(self._toggle_members_panel)
        m_hdr.addWidget(m_close)
        m_lay.addLayout(m_hdr)

        self.members_list = QListWidget()
        self.members_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.members_list.setWordWrap(True)
        self.members_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { padding: 5px 4px; border-radius: 6px; }"
        )
        self.members_list.setToolTip("Haz doble clic en un miembro para abrir un chat directo (DM)")
        self.members_list.itemDoubleClicked.connect(lambda _it: self._on_open_selected_dm())
        m_lay.addWidget(self.members_list, 1)

        self.dm_btn = QPushButton("Enviar DM")
        self.dm_btn.setToolTip(
            "Abrir conversación directa con la persona seleccionada (se envía como tu Bot)"
        )
        self.dm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dm_btn.setEnabled(False)
        self.dm_btn.clicked.connect(self._on_open_selected_dm)
        self.members_list.itemSelectionChanged.connect(self._on_member_selection_changed)
        m_lay.addWidget(self.dm_btn)

        self.members_card.setVisible(False)
        ws_lay.addWidget(self.members_card)

        root.addWidget(self.workspace_widget, 1)
        self._refresh_header_toggles()

    def _connect_service_signals(self) -> None:
        self.svc.state_changed.connect(self._on_bot_state_changed)
        self.svc.guilds_updated.connect(self._on_guilds_updated)
        self.svc.guild_members_updated.connect(self._on_guild_members_updated)
        self.svc.chat_history_loaded.connect(self._on_chat_history_loaded)
        self.svc.chat_message_received.connect(self._on_chat_message_received)
        self.svc.dm_incoming.connect(self._on_dm_incoming)
        self.svc.voice_state_updated.connect(self._on_voice_state_updated)
        self.svc.speaking_updated.connect(self._on_speaking_updated)
        self.svc.mic_level_updated.connect(self._on_mic_level_updated)
        self.svc.devices_updated.connect(self._on_devices_updated)
        self.svc.worker_ram_updated.connect(self._on_worker_ram_updated)
        self.svc.error_occurred.connect(self._on_service_error)
        self.svc.warning_occurred.connect(lambda msg: self.toast_requested.emit(msg))

    def _refresh_header_toggles(self) -> None:
        self.toggle_channels_btn.setProperty("active", "true" if self._channels_visible else "false")
        self.toggle_members_btn.setProperty("active", "true" if self._members_visible else "false")
        self.audio_toggle_btn.setProperty("active", "true" if self._audio_drawer_open else "false")
        for b in (self.toggle_channels_btn, self.toggle_members_btn, self.audio_toggle_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        self.audio_toggle_btn.setText("Audio ▴" if self._audio_drawer_open else "Audio ▾")

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
        self.channels_scroll.setVisible(self._vc_section_open)
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
        tc_cnt = len((guild or {}).get("text_channels") or ())
        arrow = "▾" if self._tc_section_open else "▸"
        suffix = f" ({tc_cnt})" if tc_cnt > 0 else ""
        self.tc_section_btn.setText(f"{arrow} CANALES DE TEXTO{suffix}")

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
            self.info_pill.setIcon(render_icon("shield", "#4ADE80" if dave_active else "#FBBF24", 13))
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
            self._render_channels()
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
        self._update_info_pill()
        self._refresh_mute_deaf_buttons()

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
        elif state == "error":
            self._joining_channel_id = ""
            self.status_pill.setText("Error")
            self.connect_btn.setText("Desconectar" if self.svc.is_worker_running else "Conectar Bot")
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
            self._worker_ws_mb = 0.0
            self._worker_priv_mb = 0.0
            self._chat_messages.clear()
            self._members = []
            self.members_list.clear()
            self.members_online_lbl.setText("0 en línea")
            self.toggle_members_btn.setText("Miembros")
            self.dm_btn.setEnabled(False)
            self.chat_title_lbl.setText("Sin chat seleccionado")
            self.chat_input.clear()
            self.chat_input.setEnabled(False)
            self.chat_send_btn.setEnabled(False)
            self.chat_browser.setHtml(
                "<p style='color:#94A3B8;'>Abre <b>Canales de texto</b> o pulsa <b>Miembros</b> en la barra superior para iniciar un chat o DM.</p>"
            )
            self.intents_banner.setVisible(False)
            self.info_pill.setVisible(False)
            self.status_pill.setText("Desconectado")
            self.connect_btn.setText("Conectar Bot")
            self.connect_btn.setObjectName("AccentBtn")
            self.hangup_btn.setVisible(False)
            self._guilds = []
            self._render_channels()
            self._voice_state = {"in_voice": False, "participants": []}
            self._render_participants()

        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)

    def _on_worker_ram_updated(self, ws_mb: float, priv_mb: float) -> None:
        self._worker_ws_mb = float(ws_mb)
        self._worker_priv_mb = float(priv_mb)
        self._update_info_pill()

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

    def _on_guild_combo_changed(self, _idx: int) -> None:
        self._render_channels()
        gid = str(self.guild_combo.currentData() or "")
        if gid and self.svc.is_connected and gid != self._selected_guild_id:
            self._selected_guild_id = gid
            self.members_list.clear()
            self.members_online_lbl.setText("Cargando...")
            self.dm_btn.setEnabled(False)
            self.svc.select_guild(gid)

    def _set_center_tab(self, idx: int) -> None:
        tab = 1 if int(idx) == 1 else 0
        self.center_stack.setCurrentIndex(tab)
        self.tab_voice_btn.setProperty("active", "true" if tab == 0 else "false")
        self.tab_chat_btn.setProperty("active", "true" if tab == 1 else "false")
        for b in (self.tab_voice_btn, self.tab_chat_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        self.active_channel_lbl.setVisible(tab == 0)
        self.chat_title_lbl.setVisible(tab == 1)
        self.bot_chat_badge.setVisible(tab == 1)

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
        row_frame.setObjectName("MediaCard")
        row_lay = QVBoxLayout(row_frame)
        row_lay.setContentsMargins(9, 6, 9, 6)
        row_lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(6)
        name_lbl = QLabel(f"🔊 {cname}")
        name_lbl.setStyleSheet("font-weight: 600;" if is_cur else "font-weight: 500;")
        top.addWidget(name_lbl, 1)

        if members:
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
            btn.setToolTip(
                "El bot no tiene permiso 'Conectar' en este canal. Ajusta los permisos o el rol del bot en Discord."
            )
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
            names = ", ".join(str(m.get("name") or "") for m in members[:5])
            if len(members) > 5:
                names += f" +{len(members) - 5}"
            sub = QLabel(names)
            sub.setObjectName("MutedLabel")
            sub.setWordWrap(True)
            row_lay.addWidget(sub)

        return row_frame

    def _toggle_locked_vc_group(self) -> None:
        self._locked_vc_expanded = not self._locked_vc_expanded
        self._render_channels()

    def _render_channels(self) -> None:
        _clear_layout(self.channels_lay)
        _clear_layout(self.text_channels_lay)
        self._update_tc_section_label()

        gid = str(self.guild_combo.currentData() or "")
        guild = next((g for g in self._guilds if str(g.get("id")) == gid), None)
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
            self.channels_lay.addStretch()

            tlbl = QLabel("Conecta el bot para ver canales de texto.")
            tlbl.setObjectName("MutedLabel")
            tlbl.setWordWrap(True)
            self.text_channels_lay.addWidget(tlbl)
            self.text_channels_lay.addStretch()
            return

        voice_channels = guild.get("voice_channels") or []
        if not voice_channels:
            lbl = QLabel("No se encontraron canales de voz en este servidor.")
            lbl.setObjectName("MutedLabel")
            lbl.setWordWrap(True)
            self.channels_lay.addWidget(lbl)
        else:
            active_cid = str(self._voice_state.get("channel_id") or "")
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

        self.channels_lay.addStretch()

        text_channels = guild.get("text_channels") or []
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
                tbtn = QPushButton(f"  # {cname}")
                tbtn.setObjectName("NavBtn")
                tbtn.setProperty("active", "true" if is_active_tc else "false")
                tbtn.setCursor(Qt.CursorShape.PointingHandCursor)
                tbtn.clicked.connect(
                    lambda _c=False, g=gid, c=cid, n=cname: self._open_text_channel_clicked(g, c, n)
                )
                self.text_channels_lay.addWidget(tbtn)
        self.text_channels_lay.addStretch()

    def _open_text_channel_clicked(self, guild_id: str, channel_id: str, name: str) -> None:
        self._active_chat_kind = "channel"
        self._active_chat_id = channel_id
        self.chat_title_lbl.setText(f"#{name}")
        self._set_center_tab(1)
        self._render_channels()
        self.svc.open_text_channel(guild_id, channel_id)

    def _on_guild_members_updated(self, data: dict[str, Any]) -> None:
        cur_gid = str(self.guild_combo.currentData() or "")
        if cur_gid and str(data.get("guild_id") or "") != cur_gid:
            return
        members = list(data.get("members") or [])
        self._members = members
        online_count = int(data.get("online_count") or 0)
        self.members_online_lbl.setText(f"{online_count} en línea")
        self.toggle_members_btn.setText(
            f"Miembros · {online_count}" if online_count > 0 else "Miembros"
        )

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
        for m in members:
            uid = str(m.get("id") or "")
            name = str(m.get("name") or uid)
            is_bot = bool(m.get("is_bot", False))
            st = str(m.get("status") or "offline")
            full_act = str(m.get("activity") or "").strip()
            short_act = full_act[:33] + "..." if len(full_act) > 36 else full_act
            dot = status_dots.get(st, "⚪")
            tag = " [APP]" if is_bot else ""
            text = f"{dot} {name}{tag}"
            if short_act:
                text += f"\n    {short_act}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, {"id": uid, "name": name, "is_bot": is_bot})
            st_lbl = status_names.get(st, st)
            item.setToolTip(f"{name} ({st_lbl})" + (f" · {full_act}" if full_act else ""))
            self.members_list.addItem(item)
        self._on_member_selection_changed()

    def _on_member_selection_changed(self) -> None:
        items = self.members_list.selectedItems()
        if not items:
            self.dm_btn.setEnabled(False)
            return
        meta = items[0].data(Qt.ItemDataRole.UserRole) or {}
        self.dm_btn.setEnabled(bool(meta.get("id")) and not bool(meta.get("is_bot", False)))

    def _on_open_selected_dm(self) -> None:
        items = self.members_list.selectedItems()
        if not items:
            return
        meta = items[0].data(Qt.ItemDataRole.UserRole) or {}
        uid = str(meta.get("id") or "")
        name = str(meta.get("name") or uid)
        if not uid or bool(meta.get("is_bot", False)):
            return
        self._active_chat_kind = "dm"
        self._active_chat_id = uid
        self.chat_title_lbl.setText(f"DM · {name}")
        self._set_center_tab(1)
        self._render_channels()
        self.svc.open_dm(uid)

    def _on_chat_history_loaded(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        if not kind or not tid:
            return
        self._active_chat_kind = kind
        self._active_chat_id = tid
        self._active_chat_can_send = bool(data.get("can_send", False))
        self.chat_title_lbl.setText(str(data.get("title") or "Chat"))
        self._chat_messages.clear()
        for m in list(data.get("messages") or [])[-80:]:
            self._chat_messages.append(m)

        can_read = bool(data.get("can_read", True))
        self.chat_input.setEnabled(self._active_chat_can_send)
        self.chat_send_btn.setEnabled(self._active_chat_can_send)
        if self._active_chat_can_send:
            self.chat_input.setPlaceholderText("Escribe un mensaje (se envía como tu Bot)... [Enter]")
            self.chat_input.setFocus()
        else:
            self.chat_input.setPlaceholderText("Sin permiso para enviar mensajes en este canal.")
        self._set_center_tab(1)
        self._render_chat_messages(can_read=can_read)

    def _on_chat_message_received(self, data: dict[str, Any]) -> None:
        kind = str(data.get("kind") or "")
        tid = str(data.get("target_id") or "")
        msg = data.get("message")
        if kind != self._active_chat_kind or tid != self._active_chat_id or not isinstance(msg, dict):
            return
        self._chat_messages.append(msg)
        self._render_chat_messages(can_read=True)

    def _on_dm_incoming(self, data: dict[str, Any]) -> None:
        author = str(data.get("author_name") or "Usuario")
        preview = str(data.get("preview") or "").strip()
        msg = f"DM de {author}: {preview}" if preview else f"Nuevo mensaje directo de {author}"
        self.toast_requested.emit(msg)

    def _on_send_chat_clicked(self) -> None:
        if not self._active_chat_kind or not self._active_chat_id or not self._active_chat_can_send:
            return
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        self.svc.send_chat_message(self._active_chat_kind, self._active_chat_id, text)

    def _render_chat_messages(self, *, can_read: bool = True) -> None:
        if not can_read and not self._chat_messages:
            self.chat_browser.setHtml(
                "<p style='color:#94A3B8;'>El bot no tiene permiso <b>Read Message History</b> en este canal. Los mensajes nuevos en vivo sí aparecerán aquí.</p>"
            )
            return
        if not self._chat_messages:
            self.chat_browser.setHtml(
                "<p style='color:#94A3B8;'>Aún no hay mensajes recientes en este chat.</p>"
            )
            return

        blocks: list[str] = []
        for msg in self._chat_messages:
            author = html.escape(str(msg.get("author_name") or "Usuario"))
            ts = html.escape(str(msg.get("timestamp") or ""))
            is_self = bool(msg.get("is_self", False))
            is_bot = bool(msg.get("is_bot", False))
            color = "#93C5FD" if is_self else "#E2E8F0"
            bot_tag = (
                " <span style='color:#94A3B8; font-size:10px;'>[APP]</span>" if is_bot else ""
            )
            ts_html = (
                f" <span style='color:#64748B; font-size:11px;'>{ts}</span>" if ts else ""
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
                            f"<a href='{aurl}' style='color:#60A5FA;'>[{aname}]</a>"
                        )
                    else:
                        links.append(f"[{aname}]")
                att_html = ("<br>" if body_html else "") + " ".join(links)
            if not body_html and not att_html:
                body_html = "<span style='color:#64748B; font-style:italic;'>(contenido multimedia o embed)</span>"
            blocks.append(
                f"<div style='margin-bottom:7px; line-height:1.35;'>"
                f"<b style='color:{color};'>{author}</b>{bot_tag}{ts_html}<br>"
                f"<span style='color:#CBD5E1;'>{body_html}{att_html}</span>"
                f"</div>"
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
        self._render_channels()
        self.svc.sync_audio_config(self.cfg, muted=self._muted, deafened=self._deafened)
        self.svc.join_channel(guild_id, channel_id)

    def _on_voice_state_updated(self, state: dict[str, Any]) -> None:
        self._voice_state = state
        self._joining_channel_id = str(state.get("joining_channel_id") or "")
        in_voice = bool(state.get("in_voice", False))
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

    def _render_participants(self) -> None:
        self._participant_dots.clear()
        _clear_layout(self.participants_lay)
        parts = self._voice_state.get("participants") or []
        if not self._voice_state.get("in_voice") or not parts:
            lbl = QLabel(
                "Elige un canal de voz a la izquierda para conectarte y hablar en tiempo real."
            )
            lbl.setObjectName("MutedLabel")
            lbl.setWordWrap(True)
            self.participants_lay.addWidget(lbl)
            self.participants_lay.addStretch()
            return

        for part in parts:
            uid = str(part.get("id") or "")
            name = str(part.get("name") or uid)
            is_self = bool(part.get("is_self", False))
            is_bot = bool(part.get("is_bot", False))
            muted = bool(part.get("muted", False))
            deaf = bool(part.get("deafened", False))

            row = QFrame()
            row.setObjectName("MediaCard")
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(10, 7, 10, 7)
            rlay.setSpacing(8)

            dot = QLabel()
            dot.setFixedSize(10, 10)
            key = "local" if is_self else uid
            speaking = bool(self._speaking.get(key, False))
            dot.setStyleSheet(self._dot_style(speaking))
            self._participant_dots[key] = dot
            rlay.addWidget(dot)

            name_lbl = QLabel(name)
            name_lbl.setStyleSheet("font-weight: 600;" if is_self else "")
            rlay.addWidget(name_lbl)

            if is_bot:
                tag = QLabel("APP")
                tag.setObjectName("DeckTag")
                rlay.addWidget(tag)
            if muted:
                m_lbl = QLabel("Silenciado")
                m_lbl.setObjectName("MutedLabel")
                rlay.addWidget(m_lbl)
            if deaf:
                d_lbl = QLabel("Ensordecido")
                d_lbl.setObjectName("MutedLabel")
                rlay.addWidget(d_lbl)

            rlay.addStretch()

            if not is_self:
                vol = self._user_volumes.get(uid, 100)
                val_lbl = QLabel(f"{vol}%")
                val_lbl.setObjectName("MutedLabel")
                val_lbl.setFixedWidth(38)
                val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

                sld = QSlider(Qt.Orientation.Horizontal)
                sld.setRange(0, 200)
                sld.setValue(vol)
                sld.setFixedWidth(115)
                sld.valueChanged.connect(
                    lambda v, u=uid, l=val_lbl: self._on_user_vol_changed(u, v, l)
                )
                rlay.addWidget(sld)
                rlay.addWidget(val_lbl)

            self.participants_lay.addWidget(row)

        self.participants_lay.addStretch()

    def _dot_style(self, speaking: bool) -> str:
        color = "#4ADE80" if speaking else self.palette.border_bright
        return f"background-color: {color}; border-radius: 5px;"

    def _on_speaking_updated(self, speaking_map: dict[str, bool]) -> None:
        self._speaking = dict(speaking_map)
        for key, dot in self._participant_dots.items():
            dot.setStyleSheet(self._dot_style(bool(self._speaking.get(key, False))))

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

    def _toggle_deaf(self) -> None:
        self._deafened = not self._deafened
        self._refresh_mute_deaf_buttons()
        self.svc.set_deaf(self._deafened)

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

    def _on_user_vol_changed(self, uid: str, value: int, label: QLabel) -> None:
        self._user_volumes[uid] = value
        label.setText(f"{value}%")
        self.svc.set_user_volume(uid, value)
