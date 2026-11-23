"""Ventana principal de SwiftPlay con Reproductor Dual, Barra Inteligente y Fullscreen 100%."""

from __future__ import annotations

from collections import deque
import random
import time
from pathlib import Path
from typing import Callable, Literal

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRunnable,
    QSize,
    QStringListModel,
    Qt,
    QThreadPool,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QCursor,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPixmap,
    QPixmapCache,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig, load_config, save_config
from swiftplay.discord_service import DiscordService
from swiftplay.media_keys import GlobalMediaKeyFilter
from swiftplay.models import ChannelInfo, FeedSection, MediaItem, ResolvedStream, format_duration
from swiftplay.paths import APP_NAME, ICON_ICO_PATH, ICON_PNG_PATH, RAM_LOG_PATH, SESSION_STATE_PATH
from swiftplay.player import MediaPlayer
from swiftplay.ram_logger import RamLogger
from swiftplay.security import has_discord_token, has_saved_session, load_account_profile
from swiftplay.session_state import SessionState, load_session_state, save_session_state
from swiftplay.sound_effects import SoundEffectPlayer
from swiftplay.storage import LocalLibrary
from swiftplay.themes import (
    ThemePalette,
    apply_windows_dark_titlebar,
    build_stylesheet,
    get_notification_color,
    get_theme,
)
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.twitch_service import TwitchService, format_viewers
from swiftplay.ui.discord_notification import DiscordMessageToast
from swiftplay.ui.discord_overlay import DiscordStatusOverlay
from swiftplay.ui.discord_panel import DiscordVoicePanel
from swiftplay.ui.icons import render_icon
from swiftplay.ui.mini_bar import MiniBarWindow
from swiftplay.ui.music_player_panel import MusicNowPlayingPanel
from swiftplay.ui.settings_dialog import SettingsDialog
from swiftplay.ui.shortcuts_dialog import ShortcutsDialog
from swiftplay.ui.video_overlays import (
    FullscreenOverlayBar,
    PlaybackFeedbackHud,
    VolumeHudOverlay,
)
from swiftplay.ui.video_surface import VideoPlayerPanel
from swiftplay.ui.widgets import (
    AnimatedToastWidget,
    ClickableSlider,
    ContinueWatchingBanner,
    ElidedLabel,
    LoadingShimmerBar,
    MusicRowWidget,
    MusicShelfWidget,
    MusicTileWidget,
    ResponsiveGridWidget,
    RoundedThumbWidget,
    SectionTitleWidget,
    VideoCardWidget,
    VideoGridCard,
)
from swiftplay.windows_os import (
    SystemResourceMonitor,
    apply_window_taskbar_properties,
    bring_window_to_front,
    clear_window_taskbar_properties,
    flash_window_taskbar,
    get_process_memory_mb,
    is_win32_disabled_parent_click,
    trim_process_memory,
)
from swiftplay.youtube_service import YouTubeService


class _TaskSignals(QObject):
    done = Signal(int, object)
    error = Signal(int, str)


class _AsyncTask(QRunnable):
    def __init__(self, task_id: int, fn: Callable, *args, **kwargs) -> None:
        super().__init__()
        self.task_id = task_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            res = self.fn(*self.args, **self.kwargs)
            self.signals.done.emit(self.task_id, res)
        except Exception as exc:
            self.signals.error.emit(self.task_id, str(exc))


class _BottomBarContainer(QWidget):
    """Contenedor recortador de altura fija para deslizar la barra inferior sin deformarla."""

    def __init__(
        self, inner: QWidget, target_height: int = 74, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._inner = inner
        self._target_h = max(1, int(target_height))
        self._anim_h = self._target_h
        self.setObjectName("BottomBarContainer")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self._target_h)
        inner.setParent(self)
        inner.setFixedHeight(self._target_h)

    def set_reveal_height(self, height: int) -> None:
        h = max(0, min(self._target_h, int(height)))
        self._anim_h = h
        self.setFixedHeight(h)
        self._position_inner()

    def _position_inner(self) -> None:
        w = max(0, self.width())
        self._inner.setGeometry(0, 0, w, self._target_h)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_inner()


class _ClickableFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    """Ventana principal de SwiftPlay con Reproductor Dual Independiente."""

    PAGE_VIDEO_SEARCH = 0
    PAGE_VIDEO_PLAYER = 1
    PAGE_WATCH_LATER = 2
    PAGE_MUSIC_SEARCH = 3
    PAGE_MY_MUSIC = 4
    PAGE_MUSIC_PLAYER = 5
    PAGE_QUEUE = PAGE_MUSIC_PLAYER
    PAGE_HISTORY = 6
    PAGE_MUSIC_HISTORY = 7
    PAGE_TWITCH = 8
    PAGE_DISCORD = 9
    PAGE_CHANNEL = 10
    MAX_FEED_ITEMS = 120

    def __init__(
        self,
        session_state_path: Path | None = None,
        ram_log_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._session_state_path: Path = (
            Path(session_state_path) if session_state_path is not None else SESSION_STATE_PATH
        )
        self._ram_logger = RamLogger(ram_log_path if ram_log_path is not None else RAM_LOG_PATH)
        self._discord_ws_mb: float = 0.0
        self._discord_priv_mb: float = 0.0
        self._screen_share_ws_mb: float = 0.0
        self._screen_share_priv_mb: float = 0.0
        self._last_gui_ws_mb: float = 0.0
        self._ram_log_tick_count: int = 0
        self.sound_fx = SoundEffectPlayer()
        self._was_discord_connected: bool = False
        self._was_discord_in_voice: bool = False
        self._discord_bot_name: str = ""
        self._discord_bot_avatar_url: str = ""
        self._discord_unread_count: int = 0
        self.cfg: AppConfig = load_config()
        self.palette: ThemePalette = get_theme(self.cfg.theme)
        self.db = LocalLibrary()
        self.yt = YouTubeService()
        self.twitch = TwitchService()
        self.discord_service = DiscordService(self)
        QPixmapCache.setCacheLimit(2048)
        self.thumb_cache = ThumbnailCache(max_items=32, parent=self)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)

        self._inflight: dict[int, _TaskSignals] = {}
        self._task_seq = 0
        self._search_gen = 0
        self._video_feed_gen = 0
        self._video_feed_shown_ids: deque[str] = deque(maxlen=160)
        self._video_feed_loading_more = False
        self._video_feed_exhausted = False
        self._music_home_gen = 0
        self._music_home_loading_more = False
        self._music_home_exhausted = False
        self._channel_gen = 0
        self._channel_return_page: int = self.PAGE_VIDEO_SEARCH
        self._channel_id: str = ""
        self._channel_info: ChannelInfo | None = None
        self._channel_tab: Literal["videos", "streams"] = "videos"
        self._channel_items: list[MediaItem] = []
        self._channel_continuation: str | None = None
        self._channel_loading_more: bool = False
        self._channel_cards: list[VideoGridCard] = []
        self._channel_page_dirty: bool = True
        self._channel_avatar_url: str = ""
        self._video_play_gen = 0
        self._music_play_gen = 0
        self._radio_gen = 0
        self._radio_fetching = False
        self._radio_seen_ids: deque[str] = deque(maxlen=300)
        self._pending_advance_after_radio = False
        self._force_quit = False
        self._is_fullscreen = False
        self._sidebar_hidden = False
        self._pre_fs_was_maximized = False
        self._pre_fs_geometry: QRect | None = None
        self._tray_notified = False
        self._seeking_bottom = False
        self._music_home_loaded = False
        self._video_showing_search = False
        self._music_showing_search = False
        self._music_filter: str = "songs"
        self._account_avatar_url = ""
        self._bb_thumb_url = ""
        self._cleaned_up = False

        self._video_viewport_timer = QTimer(self)
        self._video_viewport_timer.setSingleShot(True)
        self._video_viewport_timer.setInterval(140)
        self._video_viewport_timer.timeout.connect(
            lambda: self._refresh_offscreen_grid_thumbs(self.video_scroll, self._video_cards)
        )

        self._channel_viewport_timer = QTimer(self)
        self._channel_viewport_timer.setSingleShot(True)
        self._channel_viewport_timer.setInterval(140)
        self._channel_viewport_timer.timeout.connect(
            lambda: self._refresh_offscreen_grid_thumbs(self.channel_scroll, self._channel_cards)
        )

        # Debounce clic simple vs doble clic en la superficie de video
        self._video_click_timer = QTimer(self)
        self._video_click_timer.setSingleShot(True)
        self._video_click_timer.setInterval(210)
        self._video_click_timer.timeout.connect(self._on_video_single_click_commit)
        self._last_video_dbl_ts: float = 0.0

        # Pre-resolución de la siguiente pista de música
        self._prefetch_inflight: int = 0

        # Descarga automática de libmpv en reposo para mantener RAM mínima
        self._video_idle_unload_timer = QTimer(self)
        self._video_idle_unload_timer.setSingleShot(True)
        self._video_idle_unload_timer.setInterval(20_000)
        self._video_idle_unload_timer.timeout.connect(self._unload_idle_video_player)

        self._music_idle_unload_timer = QTimer(self)
        self._music_idle_unload_timer.setSingleShot(True)
        self._music_idle_unload_timer.setInterval(45_000)
        self._music_idle_unload_timer.timeout.connect(self._unload_idle_music_player)

        self._sidebar_hiding = False
        self._bottom_bar_hiding = False

        # Estado independiente del Reproductor Dual
        self._search_mode: Literal["video", "music", "twitch"] = "video"
        self._active_deck: Literal["video", "music"] = "video"
        self._last_video_page: int = self.PAGE_VIDEO_SEARCH
        self._last_music_page: int = self.PAGE_MUSIC_SEARCH
        self._music_return_page: int = self.PAGE_MUSIC_SEARCH

        self._twitch_tab: Literal["ES", "ALL", "FAV"] = "ES"
        self._twitch_showing_search = False
        self._twitch_gen = 0
        self._twitch_cards: list[VideoGridCard] = []
        self._twitch_results: list[MediaItem] = []
        self._twitch_loaded = False

        self._video_item: MediaItem | None = None
        self._video_pos: float = 0.0
        self._video_dur: float = 0.0
        self._video_live_start_ts: float = 0.0
        self._video_paused: bool = True
        self._last_saved_video_pos: float = 0.0
        self._last_saved_video_ts: float = 0.0
        self._last_saved_session_ts: float = 0.0
        self._prev_video_vol: int = max(10, self.cfg.video_volume)

        self._music_item: MediaItem | None = None
        self._music_pos: float = 0.0
        self._music_dur: float = 0.0
        self._music_paused: bool = True
        self._prev_music_vol: int = max(10, self.cfg.music_volume)
        self._music_queue: list[MediaItem] = []
        self._queue_index: int = -1

        self._video_feed_items: list[MediaItem] = []
        self._video_results: list[MediaItem] = []
        self._music_home_sections: list[FeedSection] = []
        self._music_results: list[MediaItem] = []
        self._video_cards: list[VideoGridCard | VideoCardWidget] = []
        self._music_rows: list[MusicRowWidget] = []
        self._music_tiles: list[MusicTileWidget] = []
        self._music_shelves: list[MusicShelfWidget] = []
        self._page_shimmers: list[LoadingShimmerBar] = []

        self._video_page_dirty: bool = True
        self._wl_page_dirty: bool = True
        self._history_page_dirty: bool = True
        self._my_music_page_dirty: bool = True
        self._music_history_page_dirty: bool = True
        self._music_search_page_dirty: bool = True
        self._twitch_page_dirty: bool = True
        self._twitch_feed_cache: dict[str, tuple[float, list[MediaItem]]] = {}
        self._last_completer_mode: str = ""

        self.setWindowTitle(APP_NAME)
        self.resize(1160, 750)
        self.setMinimumSize(860, 560)
        if ICON_ICO_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_ICO_PATH)))

        self._build_ui()

        self._bottom_bar_anim = QVariantAnimation(self)
        self._bottom_bar_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._bottom_bar_anim.valueChanged.connect(self._on_bottom_bar_anim_step)
        self._bottom_bar_anim.finished.connect(self._on_bottom_bar_anim_finished)

        self._page_fade_target: QWidget | None = None
        self._page_fade_anim = QVariantAnimation(self)
        self._page_fade_anim.setDuration(140)
        self._page_fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._page_fade_anim.setStartValue(0.86)
        self._page_fade_anim.setEndValue(1.0)
        self._page_fade_anim.valueChanged.connect(self._on_page_fade_step)
        self._page_fade_anim.finished.connect(self._on_page_fade_finished)

        # Dos instancias ligeras lazy de libmpv en paralelo (Video GPU + Música Solo-Audio)
        self.video_player = MediaPlayer(
            wid=int(self.video_panel.video_surface.winId()),
            initial_volume=self.cfg.video_volume,
            initial_speed=self.cfg.video_speed,
            normalization=self.cfg.audio_normalization,
            lazy=True,
            parent=self,
        )
        self.music_player = MediaPlayer(
            wid=None,
            initial_volume=self.cfg.music_volume,
            initial_speed=1.0,
            normalization=self.cfg.audio_normalization,
            lazy=True,
            parent=self,
        )

        # Overlays translúcidos de video
        self.fs_overlay = FullscreenOverlayBar(
            palette=self.palette,
            initial_quality=self.cfg.video_quality,
            initial_speed=self.cfg.video_speed,
            initial_volume=self.cfg.video_volume,
            initial_audio_only=self.cfg.video_audio_only,
        )
        self.vol_hud = VolumeHudOverlay(palette=self.palette)
        self.action_hud = PlaybackFeedbackHud(palette=self.palette)
        self.discord_overlay = DiscordStatusOverlay(
            palette=self.palette,
            thumb_cache=self.thumb_cache,
        )
        self.discord_overlay.set_custom_position(
            self.cfg.discord_overlay_x, self.cfg.discord_overlay_y
        )
        self.discord_overlay.position_changed.connect(
            self._on_discord_overlay_position_changed
        )
        self.discord_overlay.reset_position_requested.connect(
            self._on_discord_overlay_reset_position
        )
        self.discord_notif_toast = DiscordMessageToast(
            palette=self.palette,
            notify_color=self.cfg.discord_notify_color,
            thumb_cache=self.thumb_cache,
        )
        self.discord_notif_toast.notification_clicked.connect(
            self._on_discord_notification_clicked
        )
        self._discord_voice_active = False
        self._discord_in_voice = False
        self._discord_muted = False
        self._discord_deafened = False
        self._discord_participants: list[dict] = []
        self._discord_speaking: dict[str, bool] = {}

        self._connect_player_signals()
        self._connect_video_panel_signals()
        self._connect_fs_overlay_signals()

        self.mini_bar = MiniBarWindow(
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            always_on_top=self.cfg.mini_bar_always_on_top,
            initial_volume=self.cfg.music_volume,
        )
        self._connect_minibar_signals()
        self.discord_service.voice_state_updated.connect(self._on_discord_voice_state_changed)
        self.discord_service.speaking_updated.connect(self._on_discord_speaking_updated)
        self.discord_service.mic_level_updated.connect(self._on_discord_mic_level_updated)
        self.discord_service.worker_ram_updated.connect(self._on_discord_worker_ram_updated)
        self.discord_service.state_changed.connect(self._on_discord_state_changed_for_log)

        self._setup_tray()
        self._setup_shortcuts()
        self._setup_media_keys()

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self.thumb_cache.ready.connect(self._on_account_avatar_ready)
        self.apply_theme(self.cfg.theme)
        apply_window_taskbar_properties(int(self.winId()))
        self._refresh_badges()
        self._restore_last_session()
        self._switch_page(self.PAGE_VIDEO_SEARCH)

        self._ram_log_timer = QTimer(self)
        self._ram_log_timer.setInterval(60_000)
        self._ram_log_timer.timeout.connect(self._on_periodic_ram_tick)
        self._ram_log_timer.start()
        self._ram_logger.log_session_start()
        self._log_ram_sample("startup")

        self._resource_monitor = SystemResourceMonitor()
        self._res_sampled_once = False
        self._resource_timer = QTimer(self)
        self._resource_timer.setInterval(2500)
        self._resource_timer.timeout.connect(self._refresh_resource_monitor)
        self._resource_timer.start()
        self._refresh_resource_monitor()

        self._video_refresh_timer = QTimer(self)
        self._video_refresh_timer.setSingleShot(True)
        self._video_refresh_timer.setInterval(55)
        self._video_refresh_timer.timeout.connect(self._refresh_video_surface_output)

        self._video_post_reinit_timer = QTimer(self)
        self._video_post_reinit_timer.setSingleShot(True)
        self._video_post_reinit_timer.setInterval(150)
        self._video_post_reinit_timer.timeout.connect(self._post_reinit_video_redraw)

        QTimer.singleShot(150, self._load_video_feed)
        QTimer.singleShot(250, self._refresh_account_async_if_needed)

    @property
    def player(self) -> MediaPlayer:
        """Referencia principal al reproductor de video (compatibilidad de tests)."""
        return self.video_player

    # ------------------------------------------------------------------
    # Construcción de UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)

        # --- Barra lateral izquierda ---
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(220)
        sb_lay = QVBoxLayout(self.sidebar)
        sb_lay.setContentsMargins(10, 12, 10, 10)
        sb_lay.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(2, 0, 2, 2)
        brand_row.setSpacing(9)
        logo_lbl = QLabel()
        logo_lbl.setFixedSize(26, 26)
        if ICON_PNG_PATH.exists():
            pm = QPixmap(str(ICON_PNG_PATH)).scaled(
                26, 26, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            logo_lbl.setPixmap(pm)
        brand_row.addWidget(logo_lbl)
        brand_lbl = QLabel("SwiftPlay")
        brand_lbl.setObjectName("BrandTitle")
        brand_row.addWidget(brand_lbl)
        brand_row.addStretch()
        sb_lay.addLayout(brand_row)

        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setObjectName("SidebarScroll")
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sidebar_scroll.viewport().setObjectName("SidebarViewport")
        self.sidebar_scroll.viewport().setAutoFillBackground(False)

        nav_container = QWidget()
        nav_container.setObjectName("SidebarNavContainer")
        nav_lay = QVBoxLayout(nav_container)
        nav_lay.setContentsMargins(0, 0, 0, 0)
        nav_lay.setSpacing(2)

        vid_sec = QLabel("VIDEO")
        vid_sec.setObjectName("SectionHeader")
        nav_lay.addWidget(vid_sec)

        self.nav_yt = self._make_nav_btn(
            "YouTube",
            lambda: self._switch_page(self.PAGE_VIDEO_SEARCH),
            tooltip="Cambiar a Video / YouTube [Tab]",
        )
        self.nav_wl = self._make_nav_btn(
            "Ver más tarde",
            lambda: self._switch_page(self.PAGE_WATCH_LATER),
            tooltip="Videos guardados localmente",
        )
        self.nav_history = self._make_nav_btn(
            "Historial",
            lambda: self._switch_page(self.PAGE_HISTORY),
            tooltip="Videos vistos recientemente y minuto exacto donde quedaste",
        )
        nav_lay.addWidget(self.nav_yt)
        nav_lay.addWidget(self.nav_wl)
        nav_lay.addWidget(self.nav_history)

        mus_sec = QLabel("MÚSICA")
        mus_sec.setObjectName("SectionHeader")
        nav_lay.addWidget(mus_sec)

        self.nav_ytm = self._make_nav_btn(
            "YT Music",
            lambda: self._switch_page(self.PAGE_MUSIC_SEARCH),
            tooltip="Cambiar a Música / YT Music [Tab]",
        )
        self.nav_mymusic = self._make_nav_btn(
            "Mi música",
            lambda: self._switch_page(self.PAGE_MY_MUSIC),
            tooltip="Tus canciones locales",
        )
        self.nav_music_history = self._make_nav_btn(
            "Historial",
            lambda: self._switch_page(self.PAGE_MUSIC_HISTORY),
            tooltip="Canciones escuchadas recientemente",
        )
        nav_lay.addWidget(self.nav_ytm)
        nav_lay.addWidget(self.nav_mymusic)
        nav_lay.addWidget(self.nav_music_history)

        live_sec = QLabel("DIRECTOS")
        live_sec.setObjectName("SectionHeader")
        nav_lay.addWidget(live_sec)

        self.nav_twitch = self._make_nav_btn(
            "Twitch",
            lambda: self._switch_page(self.PAGE_TWITCH),
            tooltip="Directos de Twitch (invitado, sin chat) [Tab]",
        )
        nav_lay.addWidget(self.nav_twitch)

        voice_sec = QLabel("VOZ")
        voice_sec.setObjectName("SectionHeader")
        nav_lay.addWidget(voice_sec)

        self.nav_discord = self._make_nav_btn(
            "Discord",
            lambda: self._switch_page(self.PAGE_DISCORD),
            tooltip="Canales de voz de Discord (Bot ultraliviano, 0 MB en reposo) [Tab]",
        )
        nav_lay.addWidget(self.nav_discord)

        nav_lay.addStretch(1)

        self.minibar_btn = QPushButton(" Mini-barra")
        self.minibar_btn.setObjectName("NavBtn")
        self.minibar_btn.setToolTip("Abrir mini-barra flotante")
        self.minibar_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.minibar_btn.clicked.connect(self.toggle_mini_bar)
        nav_lay.addWidget(self.minibar_btn)

        self.shortcuts_btn = QPushButton(" Atajos [?]")
        self.shortcuts_btn.setObjectName("NavBtn")
        self.shortcuts_btn.setToolTip("Ver todos los atajos de teclado [?]")
        self.shortcuts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.shortcuts_btn.clicked.connect(self.open_shortcuts_dialog)
        nav_lay.addWidget(self.shortcuts_btn)

        self.settings_btn = QPushButton(" Ajustes")
        self.settings_btn.setObjectName("NavBtn")
        self.settings_btn.setToolTip("Configuración de reproductor dual, descargas y cuenta")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(lambda: self.open_settings(account_tab=False))
        nav_lay.addWidget(self.settings_btn)

        self.sidebar_scroll.setWidget(nav_container)
        sb_lay.addWidget(self.sidebar_scroll, 1)

        self.resource_card = QFrame()
        self.resource_card.setObjectName("ResourceMonitorCard")
        self.resource_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.resource_card.setToolTip("Consumo en vivo de SwiftPlay (RAM física, CPU y GPU)")
        res_lay = QGridLayout(self.resource_card)
        res_lay.setContentsMargins(10, 7, 10, 7)
        res_lay.setHorizontalSpacing(6)
        res_lay.setVerticalSpacing(3)
        res_lay.setColumnStretch(0, 1)
        res_lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        def _make_res_row(row_idx: int, label_key: str, init_val: str) -> QLabel:
            k_lbl = QLabel(label_key)
            k_lbl.setObjectName("MutedLabel")
            k_lbl.setMinimumHeight(15)
            k_lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            k_lbl.setStyleSheet("font-size: 10px; font-weight: 700; letter-spacing: 0.5px;")
            v_lbl = QLabel(init_val)
            v_lbl.setMinimumHeight(15)
            v_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            v_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            v_lbl.setStyleSheet("font-size: 11px; font-weight: 600; color: #E2E7F0;")
            res_lay.addWidget(k_lbl, row_idx, 0)
            res_lay.addWidget(v_lbl, row_idx, 1)
            return v_lbl

        self.res_ram_lbl = _make_res_row(0, "RAM APP", "-- MB")
        self.res_ram_total_lbl = _make_res_row(1, "TOTAL (+DS)", "-- MB")
        self.res_cpu_lbl = _make_res_row(2, "CPU", "0.0%")
        self.res_gpu_lbl = _make_res_row(3, "GPU", "0.0%")
        sb_lay.addWidget(self.resource_card)

        self.discord_mini_card = QFrame()
        self.discord_mini_card.setObjectName("DiscordSidebarMiniCard")
        self.discord_mini_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.discord_mini_card.setMinimumHeight(56)
        self.discord_mini_card.setVisible(False)
        dm_outer = QVBoxLayout(self.discord_mini_card)
        dm_outer.setContentsMargins(5, 5, 5, 5)
        dm_outer.setSpacing(4)

        dm_lay = QHBoxLayout()
        dm_lay.setContentsMargins(0, 0, 0, 0)
        dm_lay.setSpacing(2)

        self.discord_mini_avatar = QLabel("D")
        self.discord_mini_avatar.setFixedSize(22, 22)
        self.discord_mini_avatar.setScaledContents(True)
        self.discord_mini_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dm_lay.addWidget(self.discord_mini_avatar)

        dm_info = QVBoxLayout()
        dm_info.setContentsMargins(2, 0, 0, 0)
        dm_info.setSpacing(0)
        self.discord_mini_name = ElidedLabel("Discord", max_lines=1)
        self.discord_mini_name.setStyleSheet("font-size: 11px; font-weight: 700;")
        dm_info.addWidget(self.discord_mini_name)
        self.discord_mini_sub = ElidedLabel("Conectado", max_lines=1)
        self.discord_mini_sub.setObjectName("MutedLabel")
        self.discord_mini_sub.setStyleSheet("font-size: 9.5px;")
        dm_info.addWidget(self.discord_mini_sub)
        dm_lay.addLayout(dm_info, 1)

        self.discord_mini_audio_btn = QPushButton()
        self.discord_mini_audio_btn.setObjectName("IconBtn")
        self.discord_mini_audio_btn.setFixedSize(19, 19)
        self.discord_mini_audio_btn.setToolTip("Configuración de audio de Discord (dispositivos, ganancias, VAD)")
        self.discord_mini_audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_mini_audio_btn.clicked.connect(self._on_sidebar_discord_audio_clicked)
        dm_lay.addWidget(self.discord_mini_audio_btn)

        self.discord_mini_mute_btn = QPushButton()
        self.discord_mini_mute_btn.setObjectName("IconBtn")
        self.discord_mini_mute_btn.setFixedSize(19, 19)
        self.discord_mini_mute_btn.setToolTip("Silenciar / Activar micrófono")
        self.discord_mini_mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_mini_mute_btn.clicked.connect(
            self.discord_panel.toggle_mute_from_hotkey
            if hasattr(self, "discord_panel")
            else self._on_sidebar_discord_mute
        )
        dm_lay.addWidget(self.discord_mini_mute_btn)

        self.discord_mini_deaf_btn = QPushButton()
        self.discord_mini_deaf_btn.setObjectName("IconBtn")
        self.discord_mini_deaf_btn.setFixedSize(19, 19)
        self.discord_mini_deaf_btn.setToolTip("Ensordecer / Activar audio Discord")
        self.discord_mini_deaf_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_mini_deaf_btn.clicked.connect(self._on_sidebar_discord_deafen)
        dm_lay.addWidget(self.discord_mini_deaf_btn)

        self.discord_mini_hangup_btn = QPushButton()
        self.discord_mini_hangup_btn.setObjectName("IconBtn")
        self.discord_mini_hangup_btn.setFixedSize(19, 19)
        self.discord_mini_hangup_btn.setToolTip("Colgar / Salir del canal de voz")
        self.discord_mini_hangup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_mini_hangup_btn.clicked.connect(self._on_sidebar_discord_hangup)
        self.discord_mini_hangup_btn.setVisible(False)
        dm_lay.addWidget(self.discord_mini_hangup_btn)
        dm_outer.addLayout(dm_lay)

        dm_mic_row = QHBoxLayout()
        dm_mic_row.setContentsMargins(0, 0, 0, 0)
        dm_mic_row.setSpacing(6)
        self.discord_mini_vu_lbl = QLabel("Mic  0%")
        self.discord_mini_vu_lbl.setObjectName("MutedLabel")
        self.discord_mini_vu_lbl.setStyleSheet("font-size: 9.5px;")
        self.discord_mini_vu_lbl.setFixedWidth(44)
        dm_mic_row.addWidget(self.discord_mini_vu_lbl)

        self.discord_mini_vu_bar = QProgressBar()
        self.discord_mini_vu_bar.setObjectName("HudProgress")
        self.discord_mini_vu_bar.setRange(0, 100)
        self.discord_mini_vu_bar.setValue(0)
        self.discord_mini_vu_bar.setTextVisible(False)
        self.discord_mini_vu_bar.setFixedHeight(5)
        dm_mic_row.addWidget(self.discord_mini_vu_bar, 1)
        dm_outer.addLayout(dm_mic_row)

        sb_lay.addWidget(self.discord_mini_card)

        body_row.addWidget(self.sidebar)

        # --- Área principal derecha ---
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(0)

        # Barra superior de búsqueda + cuenta
        self.top_bar = QFrame()
        self.top_bar.setObjectName("TopBar")
        top_lay = QHBoxLayout(self.top_bar)
        top_lay.setContentsMargins(18, 10, 18, 10)
        top_lay.setSpacing(10)

        self.sidebar_toggle_btn = QPushButton()
        self.sidebar_toggle_btn.setObjectName("IconBtn")
        self.sidebar_toggle_btn.setFixedSize(34, 34)
        self.sidebar_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sidebar_toggle_btn.clicked.connect(self.toggle_sidebar)
        top_lay.addWidget(self.sidebar_toggle_btn)

        self.close_media_btn = QPushButton(" Cerrar video")
        self.close_media_btn.setObjectName("DangerBtn")
        self.close_media_btn.setStyleSheet("padding: 5px 10px; font-size: 12px;")
        self.close_media_btn.setVisible(False)
        self.close_media_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_media_btn.clicked.connect(lambda _c=False: self._close_active_media())
        top_lay.addWidget(self.close_media_btn)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchInput")
        self.search_input.setPlaceholderText("Buscar videos en YouTube... [Ctrl+L]")
        self.search_input.returnPressed.connect(self._on_search_submitted)
        self._search_history_model = QStringListModel([], self)
        self._search_completer = QCompleter(self._search_history_model, self)
        self._search_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._search_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.search_input.setCompleter(self._search_completer)
        top_lay.addWidget(self.search_input, 1)

        self.filter_songs_btn = QPushButton("Canciones")
        self.filter_songs_btn.setObjectName("PillBtn")
        self.filter_songs_btn.setProperty("active", "true")
        self.filter_songs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.filter_songs_btn.clicked.connect(lambda: self._set_music_filter("songs"))
        top_lay.addWidget(self.filter_songs_btn)

        self.filter_videos_btn = QPushButton("Videos musicales")
        self.filter_videos_btn.setObjectName("PillBtn")
        self.filter_videos_btn.setProperty("active", "false")
        self.filter_videos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.filter_videos_btn.clicked.connect(lambda: self._set_music_filter("videos"))
        top_lay.addWidget(self.filter_videos_btn)

        self.search_btn = QPushButton()
        self.search_btn.setObjectName("SearchSubmitBtn")
        self.search_btn.setFixedSize(36, 36)
        self.search_btn.setToolTip("Buscar")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.clicked.connect(self._on_search_submitted)
        top_lay.addWidget(self.search_btn)

        self.back_to_video_btn = QPushButton(" Video")
        self.back_to_video_btn.setObjectName("PillBtn")
        self.back_to_video_btn.setVisible(False)
        self.back_to_video_btn.setToolTip("Ir al reproductor de video [Tab]")
        self.back_to_video_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_to_video_btn.clicked.connect(lambda: self._switch_page(self.PAGE_VIDEO_PLAYER))
        top_lay.addWidget(self.back_to_video_btn)

        self.account_btn = QPushButton("Acceder")
        self.account_btn.setObjectName("PillBtn")
        self.account_btn.setIconSize(QSize(22, 22))
        self.account_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_btn.clicked.connect(lambda: self.open_settings(account_tab=True))
        top_lay.addWidget(self.account_btn)

        right_col.addWidget(self.top_bar)

        # Stack de vistas
        self.stack = QStackedWidget()

        # Pág 0: Inicio (YouTube Feed sin Shorts + Búsqueda)
        self.video_back_feed_btn: QPushButton | None = None
        self.video_refresh_btn: QPushButton | None = None
        (
            self.video_search_page,
            self.video_grid,
            self.video_empty_lbl,
            self.video_view_title,
            video_hdr_btns,
        ) = self._make_grid_page(
            "Inicio",
            "Cargando videos...",
            header_actions=[
                ("Inicio", self._show_video_feed_view, False),
                ("", lambda _c=False: self._load_video_feed(refresh=True), False),
            ],
            return_header=True,
        )
        self.video_back_feed_btn = video_hdr_btns[0]
        self.video_back_feed_btn.setVisible(False)
        self.video_refresh_btn = video_hdr_btns[1]
        self.video_refresh_btn.setObjectName("IconBtn")
        self.video_refresh_btn.setFixedSize(32, 32)
        self.video_refresh_btn.setToolTip("Actualizar")

        self.continue_banner = ContinueWatchingBanner(self.thumb_cache, self.palette)
        self.continue_banner.setVisible(False)
        self.continue_banner.resume_requested.connect(self._on_continue_banner_resume)
        v_lay = self.video_grid.parentWidget().layout()
        if v_lay is not None:
            v_lay.insertWidget(0, self.continue_banner)

        self.video_scroll: QScrollArea = self.video_search_page._scroll_area  # type: ignore[attr-defined]
        vsb = self.video_scroll.verticalScrollBar()
        if vsb is not None:
            vsb.valueChanged.connect(self._on_video_scroll_changed)

        self.stack.addWidget(self.video_search_page)

        # Pág 1: Reproductor de Video embebido
        self.video_panel = VideoPlayerPanel(
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            initial_quality=self.cfg.video_quality,
            initial_speed=self.cfg.video_speed,
            initial_volume=self.cfg.video_volume,
            initial_audio_only=self.cfg.video_audio_only,
        )
        self.stack.addWidget(self.video_panel)

        # Pág 2: Ver más tarde
        self.wl_page, self.wl_grid, self.wl_empty_lbl = self._make_grid_page(
            "Ver más tarde",
            "Aún no tienes videos guardados. Usa el menú ⋮ de cualquier video para añadirlo.",
        )
        self.stack.addWidget(self.wl_page)

        # Pág 3: Música (YT Music Inicio + Búsqueda)
        self.music_back_home_btn: QPushButton | None = None
        self.music_refresh_btn: QPushButton | None = None
        (
            self.music_search_page,
            self.music_results_lay,
            self.music_empty_lbl,
            self.music_view_title,
            music_hdr_btns,
        ) = self._make_scroll_page(
            "Música",
            "Cargando música...",
            header_actions=[
                ("Música", self._show_music_home_view, False),
                ("", lambda _c=False: self._load_music_home(refresh=True), False),
            ],
            return_header=True,
        )
        self.music_back_home_btn = music_hdr_btns[0]
        self.music_back_home_btn.setVisible(False)
        self.music_refresh_btn = music_hdr_btns[1]
        self.music_refresh_btn.setObjectName("IconBtn")
        self.music_refresh_btn.setFixedSize(32, 32)
        self.music_refresh_btn.setToolTip("Actualizar")
        self.music_scroll: QScrollArea = self.music_search_page._scroll_area  # type: ignore[attr-defined]
        msb = self.music_scroll.verticalScrollBar()
        if msb is not None:
            msb.valueChanged.connect(self._on_music_scroll_changed)
        self.stack.addWidget(self.music_search_page)

        # Pág 4: Mi música
        self.my_music_page, self.my_music_lay, self.my_music_empty_lbl = self._make_scroll_page(
            "Mi música",
            "Aún no tienes canciones guardadas. Pulsa el corazón en cualquier canción para guardarla.",
            header_actions=[
                ("Reproducir todo", self._play_all_my_music, True),
                ("Aleatorio", self._shuffle_my_music, False),
            ],
        )
        self.stack.addWidget(self.my_music_page)

        # Pág 5: Vista completa de Música (Portada grande + Cola 'A continuación')
        self.music_panel = MusicNowPlayingPanel(
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            autoplay_enabled=self.cfg.autoplay_music,
        )
        self.music_panel.back_requested.connect(self._on_music_panel_back)
        self.music_panel.play_pause_requested.connect(self._on_music_stage_clicked)
        self.music_panel.wheel_scrolled.connect(self._on_music_wheel_volume)
        self.music_panel.queue_index_requested.connect(self._play_queue_index)
        self.music_panel.remove_from_queue_requested.connect(self._remove_from_queue)
        self.music_panel.my_music_toggled.connect(self._toggle_my_music_item)
        self.music_panel.download_requested.connect(lambda it: self._download_item(it, "music"))
        self.music_panel.autoplay_toggled.connect(self._toggle_autoplay)
        self.music_panel.clear_queue_requested.connect(self._clear_queue)
        self.stack.addWidget(self.music_panel)

        # Pág 6: Historial de video con progreso exacto
        self.history_page, self.history_grid, self.history_empty_lbl = self._make_grid_page(
            "Historial",
            "Aún no hay videos en tu historial. Los videos que veas aparecerán aquí con el minuto exacto donde los dejaste.",
            header_actions=[("Limpiar", self._clear_video_history, False)],
        )
        self.stack.addWidget(self.history_page)

        # Pág 7: Historial de música
        (
            self.music_history_page,
            self.music_history_lay,
            self.music_history_empty_lbl,
        ) = self._make_scroll_page(
            "Historial",
            "Aún no hay canciones en tu historial. Las canciones que escuches aparecerán aquí.",
            header_actions=[
                ("Reproducir", self._play_all_music_history, True),
                ("Limpiar", self._clear_music_history, False),
            ],
        )
        self.stack.addWidget(self.music_history_page)

        # Pág 8: Twitch (solo directos, invitado sin chat)
        self.twitch_back_btn: QPushButton | None = None
        self.twitch_refresh_btn: QPushButton | None = None
        (
            self.twitch_page,
            self.twitch_grid,
            self.twitch_empty_lbl,
            self.twitch_view_title,
            tw_hdr_btns,
        ) = self._make_grid_page(
            "Twitch",
            "Cargando directos de Twitch...",
            header_actions=[
                ("Directos", self._show_twitch_feed_view, False),
                ("", self._reload_twitch_current_view, False),
            ],
            return_header=True,
        )
        self.twitch_back_btn = tw_hdr_btns[0]
        self.twitch_back_btn.setVisible(False)
        self.twitch_refresh_btn = tw_hdr_btns[1]
        self.twitch_refresh_btn.setObjectName("IconBtn")
        self.twitch_refresh_btn.setFixedSize(32, 32)
        self.twitch_refresh_btn.setToolTip("Actualizar")

        self.twitch_es_btn = QPushButton("Español")
        self.twitch_es_btn.setObjectName("PillBtn")
        self.twitch_es_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.twitch_es_btn.clicked.connect(lambda: self._set_twitch_tab("ES"))

        self.twitch_all_btn = QPushButton("Global")
        self.twitch_all_btn.setObjectName("PillBtn")
        self.twitch_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.twitch_all_btn.clicked.connect(lambda: self._set_twitch_tab("ALL"))

        self.twitch_fav_btn = QPushButton("Favoritos")
        self.twitch_fav_btn.setObjectName("PillBtn")
        self.twitch_fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.twitch_fav_btn.clicked.connect(lambda: self._set_twitch_tab("FAV"))

        tw_hdr = self.twitch_page.layout().itemAt(0).layout()
        if tw_hdr is not None:
            tw_hdr.insertWidget(1, self.twitch_es_btn)
            tw_hdr.insertWidget(2, self.twitch_all_btn)
            tw_hdr.insertWidget(3, self.twitch_fav_btn)
        self._refresh_twitch_tab_pills()
        self.stack.addWidget(self.twitch_page)

        # Pág 9: Discord Voz (Bot puente aislado, 0 MB en reposo)
        self.discord_panel = DiscordVoicePanel(
            config=self.cfg,
            service=self.discord_service,
            palette=self.palette,
            thumb_cache=self.thumb_cache,
            db=self.db,
            parent=self,
        )
        self.discord_panel.open_token_settings_requested.connect(
            lambda: self.open_settings(discord_tab=True)
        )
        self.discord_panel.open_shortcuts_settings_requested.connect(
            lambda: self.open_settings(discord_shortcuts_tab=True)
        )
        self.discord_panel.voice_status_changed.connect(self._on_discord_voice_status_changed)
        self.discord_panel.channel_alert_requested.connect(self._on_discord_channel_alert)
        self.discord_panel.dm_alert_requested.connect(self._on_discord_dm_alert)
        self.discord_panel.unread_count_changed.connect(self._on_discord_unread_count_changed)
        self.discord_panel.sound_event_requested.connect(self.play_discord_sound)
        self.discord_panel.toast_requested.connect(self.show_toast)
        self.discord_panel.screen_share_ram_updated.connect(self._on_screen_share_ram_updated)
        self.stack.addWidget(self.discord_panel)

        # Pág 10: Canal de YouTube (Videos y Directos sin Shorts)
        (
            self.channel_page,
            self.channel_grid,
            self.channel_empty_lbl,
            self.channel_title_lbl,
            chan_hdr_btns,
        ) = self._make_grid_page(
            "Canal",
            "Cargando canal...",
            header_actions=[("", self._reload_channel_page, False)],
            return_header=True,
        )
        self.channel_refresh_btn = chan_hdr_btns[0]
        self.channel_refresh_btn.setObjectName("IconBtn")
        self.channel_refresh_btn.setFixedSize(32, 32)
        self.channel_refresh_btn.setToolTip("Actualizar canal")

        ch_hdr = self.channel_page.layout().itemAt(0).layout()
        if ch_hdr is not None:
            self.channel_back_btn = QPushButton(" Volver")
            self.channel_back_btn.setObjectName("PillBtn")
            self.channel_back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.channel_back_btn.clicked.connect(self._on_channel_back)

            self.channel_avatar_lbl = QLabel()
            self.channel_avatar_lbl.setFixedSize(40, 40)
            self.channel_avatar_lbl.setScaledContents(True)
            self.channel_avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            meta_box = QWidget()
            meta_lay = QVBoxLayout(meta_box)
            meta_lay.setContentsMargins(0, 0, 0, 0)
            meta_lay.setSpacing(1)
            ch_hdr.removeWidget(self.channel_title_lbl)
            meta_lay.addWidget(self.channel_title_lbl)
            self.channel_meta_lbl = QLabel("")
            self.channel_meta_lbl.setObjectName("MutedLabel")
            self.channel_meta_lbl.setVisible(False)
            meta_lay.addWidget(self.channel_meta_lbl)

            self.channel_videos_btn = QPushButton("Videos")
            self.channel_videos_btn.setObjectName("PillBtn")
            self.channel_videos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.channel_videos_btn.clicked.connect(lambda: self._set_channel_tab("videos"))

            self.channel_streams_btn = QPushButton("Directos")
            self.channel_streams_btn.setObjectName("PillBtn")
            self.channel_streams_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.channel_streams_btn.clicked.connect(lambda: self._set_channel_tab("streams"))

            ch_hdr.insertWidget(0, self.channel_back_btn)
            ch_hdr.insertWidget(1, self.channel_avatar_lbl)
            ch_hdr.insertWidget(2, meta_box)
            ch_hdr.insertWidget(3, self.channel_videos_btn)
            ch_hdr.insertWidget(4, self.channel_streams_btn)
        else:
            self.channel_back_btn = QPushButton(" Volver")
            self.channel_avatar_lbl = QLabel()
            self.channel_meta_lbl = QLabel("")
            self.channel_videos_btn = QPushButton("Videos")
            self.channel_streams_btn = QPushButton("Directos")

        self.channel_scroll: QScrollArea = self.channel_page._scroll_area  # type: ignore[attr-defined]
        csb = self.channel_scroll.verticalScrollBar()
        if csb is not None:
            csb.valueChanged.connect(self._on_channel_scroll_changed)
        self.thumb_cache.ready.connect(self._on_channel_avatar_ready)
        self._refresh_channel_tab_pills()
        self.stack.addWidget(self.channel_page)

        right_col.addWidget(self.stack, 1)
        body_row.addLayout(right_col, 1)
        outer.addLayout(body_row, 1)

        # --- Barra inferior inteligente (fija en ventana; click redirige al contenido) ---
        self.bottom_bar = self._build_bottom_bar()
        self.bottom_bar_wrap = _BottomBarContainer(self.bottom_bar, target_height=74)
        self.bottom_bar_wrap.setVisible(False)
        outer.addWidget(self.bottom_bar_wrap)

        # Toast flotante animado no intrusivo
        self.toast_lbl = AnimatedToastWidget(self.palette, self)

        self._update_music_filter_visibility()
        self._refresh_search_completer()

    def _make_nav_btn(
        self, text: str, slot: Callable, tooltip: str = ""
    ) -> QPushButton:
        btn = QPushButton(f"  {text}")
        btn.setObjectName("NavBtn")
        btn.setProperty("active", "false")
        if tooltip:
            btn.setToolTip(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    def _make_scroll_page(
        self,
        title: str,
        empty_text: str,
        header_actions: list[tuple[str, Callable, bool]] | None = None,
        return_header: bool = False,
    ):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(18, 14, 18, 10)
        root.setSpacing(10)

        hdr_row = QHBoxLayout()
        title_lbl = QLabel(title)
        title_lbl.setObjectName("ViewTitle")
        hdr_row.addWidget(title_lbl)
        hdr_row.addStretch()

        created_btns: list[QPushButton] = []
        if header_actions:
            for label, fn, is_accent in header_actions:
                b = QPushButton(label)
                if is_accent:
                    b.setObjectName("AccentBtn")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(fn)
                if label.startswith("Autoplay"):
                    self.autoplay_btn_ref = b
                hdr_row.addWidget(b)
                created_btns.append(b)
        root.addLayout(hdr_row)

        shimmer = LoadingShimmerBar(self.palette.accent)
        root.addWidget(shimmer)
        self._page_shimmers.append(shimmer)
        page._shimmer_bar = shimmer  # type: ignore[attr-defined]

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setMinimumWidth(0)
        items_lay = QVBoxLayout(container)
        items_lay.setContentsMargins(0, 4, 4, 12)
        items_lay.setSpacing(8)

        empty_lbl = QLabel(empty_text)
        empty_lbl.setObjectName("MutedLabel")
        empty_lbl.setWordWrap(True)
        empty_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        items_lay.addWidget(empty_lbl)
        items_lay.addStretch()

        scroll.setWidget(container)
        root.addWidget(scroll, 1)
        page._scroll_area = scroll  # type: ignore[attr-defined]
        if return_header:
            return page, items_lay, empty_lbl, title_lbl, created_btns
        return page, items_lay, empty_lbl

    def _make_grid_page(
        self,
        title: str,
        empty_text: str,
        header_actions: list[tuple[str, Callable, bool]] | None = None,
        return_header: bool = False,
    ):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(18, 14, 18, 10)
        root.setSpacing(10)

        hdr_row = QHBoxLayout()
        title_lbl = QLabel(title)
        title_lbl.setObjectName("ViewTitle")
        hdr_row.addWidget(title_lbl)
        hdr_row.addStretch()

        created_btns: list[QPushButton] = []
        if header_actions:
            for label, fn, is_accent in header_actions:
                b = QPushButton(label)
                if is_accent:
                    b.setObjectName("AccentBtn")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(fn)
                hdr_row.addWidget(b)
                created_btns.append(b)
        root.addLayout(hdr_row)

        shimmer = LoadingShimmerBar(self.palette.accent)
        root.addWidget(shimmer)
        self._page_shimmers.append(shimmer)
        page._shimmer_bar = shimmer  # type: ignore[attr-defined]

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setMinimumWidth(0)
        items_lay = QVBoxLayout(container)
        items_lay.setContentsMargins(0, 4, 4, 12)
        items_lay.setSpacing(8)

        empty_lbl = QLabel(empty_text)
        empty_lbl.setObjectName("MutedLabel")
        empty_lbl.setWordWrap(True)
        empty_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        items_lay.addWidget(empty_lbl)

        grid = ResponsiveGridWidget(min_col_width=270, max_cols=4, spacing=16)
        items_lay.addWidget(grid)
        items_lay.addStretch()

        scroll.setWidget(container)
        root.addWidget(scroll, 1)
        page._scroll_area = scroll  # type: ignore[attr-defined]
        if return_header:
            return page, grid, empty_lbl, title_lbl, created_btns
        return page, grid, empty_lbl

    def _build_bottom_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("PlayerBar")
        bar.setFixedHeight(74)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(12)

        # Área clicable izquierda: al tocarla, lleva directo a la vista del contenido sonando
        self.bb_click_area = _ClickableFrame()
        self.bb_click_area.setObjectName("MiniClickArea")
        self.bb_click_area.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_click_area.setToolTip("Clic para ir a la vista completa del reproductor")
        self.bb_click_area.clicked.connect(self._on_bottom_bar_clicked)
        click_lay = QHBoxLayout(self.bb_click_area)
        click_lay.setContentsMargins(4, 2, 8, 2)
        click_lay.setSpacing(10)

        self.bb_thumb = RoundedThumbWidget(
            radius=8,
            enable_crossfade=True,
            bg_color=self.palette.base,
        )
        self.bb_thumb.setFixedSize(48, 48)
        click_lay.addWidget(self.bb_thumb)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        title_line = QHBoxLayout()
        title_line.setSpacing(6)
        self.bb_deck_tag = QLabel("MÚSICA")
        self.bb_deck_tag.setObjectName("DeckTag")
        title_line.addWidget(self.bb_deck_tag)

        self.bb_title = ElidedLabel("Sin reproducción", max_lines=1)
        self.bb_title.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.bb_title.setMaximumWidth(230)
        title_line.addWidget(self.bb_title, 1)
        info_col.addLayout(title_line)

        self.bb_author = ElidedLabel("", max_lines=1)
        self.bb_author.setObjectName("MutedLabel")
        self.bb_author.setMaximumWidth(270)
        info_col.addWidget(self.bb_author)
        click_lay.addLayout(info_col)
        lay.addWidget(self.bb_click_area)

        # Centro: controles + barra de progreso
        center_col = QVBoxLayout()
        center_col.setSpacing(4)
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        ctrl_row.addStretch()

        self.bb_prev = QPushButton()
        self.bb_prev.setObjectName("IconBtn")
        self.bb_prev.setFixedSize(30, 30)
        self.bb_prev.setToolTip("Anterior")
        self.bb_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_prev.clicked.connect(self._on_bb_prev)
        ctrl_row.addWidget(self.bb_prev)

        self.bb_play = QPushButton()
        self.bb_play.setObjectName("PlayAccentBtn")
        self.bb_play.setFixedSize(36, 36)
        self.bb_play.setIconSize(QSize(18, 18))
        self.bb_play.setToolTip("Reproducir / Pausar [Espacio]")
        self.bb_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_play.clicked.connect(self._on_bb_play_pause)
        ctrl_row.addWidget(self.bb_play)

        self.bb_next = QPushButton()
        self.bb_next.setObjectName("IconBtn")
        self.bb_next.setFixedSize(30, 30)
        self.bb_next.setToolTip("Siguiente")
        self.bb_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_next.clicked.connect(self._on_bb_next)
        ctrl_row.addWidget(self.bb_next)

        ctrl_row.addStretch()
        center_col.addLayout(ctrl_row)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)
        self.bb_pos = QLabel("0:00")
        self.bb_pos.setObjectName("MutedLabel")
        self.bb_pos.setFixedWidth(42)
        seek_row.addWidget(self.bb_pos)

        self.bb_seek = ClickableSlider(Qt.Orientation.Horizontal)
        self.bb_seek.setRange(0, 1000)
        self.bb_seek.sliderPressed.connect(lambda: setattr(self, "_seeking_bottom", True))
        self.bb_seek.sliderReleased.connect(self._on_bb_seek_release)
        self.bb_seek.sliderMoved.connect(self._on_bb_seek_moved)
        seek_row.addWidget(self.bb_seek, 1)

        self.bb_dur = QLabel("0:00")
        self.bb_dur.setObjectName("MutedLabel")
        self.bb_dur.setFixedWidth(42)
        self.bb_dur.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self.bb_dur)
        center_col.addLayout(seek_row)
        lay.addLayout(center_col, 1)

        # Derecha: switch rápido si ambos decks cargados + favorito + descargar + volumen
        self.bb_switch_btn = QPushButton("Ir a Video")
        self.bb_switch_btn.setObjectName("PillBtn")
        self.bb_switch_btn.setVisible(False)
        self.bb_switch_btn.setToolTip("Cambiar al otro reproductor activo")
        self.bb_switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_switch_btn.clicked.connect(self._switch_active_deck_from_bb)
        lay.addWidget(self.bb_switch_btn)

        self.bb_fav = QPushButton()
        self.bb_fav.setObjectName("IconBtn")
        self.bb_fav.setFixedSize(32, 32)
        self.bb_fav.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_fav.clicked.connect(self._toggle_bb_saved)
        lay.addWidget(self.bb_fav)

        self.bb_dl = QPushButton()
        self.bb_dl.setObjectName("IconBtn")
        self.bb_dl.setFixedSize(32, 32)
        self.bb_dl.setToolTip("Descargar actual")
        self.bb_dl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_dl.clicked.connect(self._download_bb_current)
        lay.addWidget(self.bb_dl)

        self.bb_vol_icon = QPushButton()
        self.bb_vol_icon.setObjectName("IconBtn")
        self.bb_vol_icon.setFixedSize(30, 30)
        self.bb_vol_icon.setToolTip("Silenciar / Activar sonido [M]")
        self.bb_vol_icon.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_vol_icon.clicked.connect(self._toggle_bb_mute)
        lay.addWidget(self.bb_vol_icon)

        self.bb_vol = ClickableSlider(Qt.Orientation.Horizontal, volume_wheel=True)
        self.bb_vol.setRange(0, 100)
        self.bb_vol.setValue(self.cfg.music_volume)
        self.bb_vol.setFixedWidth(84)
        self.bb_vol.setToolTip("Volumen (rueda: ±5%, Shift: ±1%)")
        self.bb_vol.valueChanged.connect(self._on_bb_volume_changed)
        lay.addWidget(self.bb_vol)

        self.thumb_cache.ready.connect(self._on_bb_thumb_ready)
        return bar

    # ------------------------------------------------------------------
    # Conexión de señales de ambos reproductores y overlays
    # ------------------------------------------------------------------

    def _connect_player_signals(self) -> None:
        # Video deck
        vp = self.video_player
        vp.position_changed.connect(self._on_video_pos_changed)
        vp.duration_changed.connect(self._on_video_dur_changed)
        vp.paused_changed.connect(self._on_video_paused_changed)
        vp.buffering_changed.connect(self._on_video_buffering_changed)
        vp.track_ended.connect(self._on_video_ended)
        vp.error_occurred.connect(lambda m: self.show_toast(m))
        vp.vo_error_detected.connect(self._on_video_vo_error)
        vp.wheel_scrolled.connect(self._on_video_wheel_volume)
        vp.mouse_moved.connect(self._on_video_mouse_moved)
        vp.clicked.connect(self._on_video_surface_clicked)
        vp.double_clicked.connect(self._on_video_surface_double_clicked)

        # Music deck
        mp = self.music_player
        mp.position_changed.connect(self._on_music_pos_changed)
        mp.duration_changed.connect(self._on_music_dur_changed)
        mp.paused_changed.connect(self._on_music_paused_changed)
        mp.track_ended.connect(self._on_music_ended)
        mp.error_occurred.connect(lambda m: self.show_toast(m))

    def _connect_video_panel_signals(self) -> None:
        p = self.video_panel
        p.back_requested.connect(self._on_video_panel_back)
        p.play_pause_requested.connect(self._on_video_play_btn_clicked)
        p.seek_requested.connect(self.video_player.seek)
        p.volume_changed.connect(self._on_video_volume_changed)
        p.speed_changed.connect(self._on_video_speed_changed)
        p.quality_changed.connect(self._on_video_quality_changed)
        p.audio_only_toggled.connect(self._on_audio_only_toggled)
        p.watch_later_toggled.connect(self._toggle_current_video_saved)
        p.reload_live_requested.connect(self._reload_current_live_stream)
        p.download_requested.connect(self._download_current_video)
        p.fullscreen_toggled.connect(self.toggle_fullscreen)
        p.video_clicked.connect(self._on_video_surface_clicked)
        p.video_double_clicked.connect(self._on_video_surface_double_clicked)
        p.theater_toggled.connect(self.toggle_sidebar)
        p.channel_clicked.connect(self._open_current_video_channel)
        p.wheel_scrolled.connect(self._on_video_wheel_volume)
        p.video_surface.mouse_moved.connect(self._on_video_mouse_moved)
        p.video_surface.surface_shown.connect(
            lambda: self._schedule_video_surface_refresh(55)
        )

    def _connect_fs_overlay_signals(self) -> None:
        ov = self.fs_overlay
        ov.play_pause_requested.connect(lambda: self.toggle_deck_play_pause("video"))
        ov.seek_requested.connect(self.video_player.seek)
        ov.volume_changed.connect(self._on_video_volume_changed)
        ov.speed_changed.connect(self._on_video_speed_changed)
        ov.quality_changed.connect(self._on_video_quality_changed)
        ov.audio_only_toggled.connect(self._on_audio_only_toggled)
        ov.watch_later_toggled.connect(self._toggle_current_video_saved)
        ov.reload_live_requested.connect(self._reload_current_live_stream)
        ov.download_requested.connect(self._download_current_video)
        ov.exit_fullscreen_requested.connect(self.toggle_fullscreen)
        ov.theater_requested.connect(self.toggle_sidebar)
        ov.wheel_scrolled.connect(self._on_video_wheel_volume)

    def _connect_minibar_signals(self) -> None:
        mb = self.mini_bar
        mb.expand_requested.connect(self._restore_from_mini_bar)
        mb.play_pause_requested.connect(self.toggle_play_pause)
        mb.prev_requested.connect(self.play_previous)
        mb.next_requested.connect(self.play_next)
        mb.seek_requested.connect(self._seek_active_deck)
        mb.volume_changed.connect(self._on_minibar_volume_changed)
        mb.pin_toggled.connect(self._on_minibar_pin_toggled)

    # ------------------------------------------------------------------
    # Bandeja, Atajos, Tab Switch y Teclas Multimedia
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self)
        if ICON_ICO_PATH.exists():
            self.tray_icon.setIcon(QIcon(str(ICON_ICO_PATH)))
        self.tray_icon.setToolTip(APP_NAME)

        menu = QMenu()
        act_show = QAction("Abrir SwiftPlay", self)
        act_show.triggered.connect(self.restore_window)
        menu.addAction(act_show)

        act_mini = QAction("Mini-barra flotante", self)
        act_mini.triggered.connect(self.toggle_mini_bar)
        menu.addAction(act_mini)

        menu.addSeparator()

        act_pp = QAction("Reproducir / Pausar", self)
        act_pp.triggered.connect(self.toggle_play_pause)
        menu.addAction(act_pp)

        act_prev = QAction("Anterior (Música)", self)
        act_prev.triggered.connect(self.play_previous)
        menu.addAction(act_prev)

        act_next = QAction("Siguiente (Música)", self)
        act_next.triggered.connect(self.play_next)
        menu.addAction(act_next)

        menu.addSeparator()

        act_exit = QAction("Salir", self)
        act_exit.triggered.connect(self.quit_app)
        menu.addAction(act_exit)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_window()

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Space"), self, activated=self._shortcut_space)
        QShortcut(QKeySequence("F"), self, activated=self._shortcut_fullscreen)
        QShortcut(QKeySequence("T"), self, activated=self._shortcut_theater)
        QShortcut(QKeySequence("Escape"), self, activated=self._shortcut_escape)
        QShortcut(QKeySequence("M"), self, activated=self._shortcut_mute)
        QShortcut(QKeySequence("Left"), self, activated=lambda: self._shortcut_seek(-5.0))
        QShortcut(QKeySequence("Right"), self, activated=lambda: self._shortcut_seek(5.0))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self and event.type() == QEvent.Type.WindowActivate:
            if hasattr(self, "discord_panel"):
                self.discord_panel.on_window_activated()
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if self.isActiveWindow() and not self._typing_in_input():
                if (
                    event.key() == Qt.Key.Key_Tab
                    and event.modifiers() == Qt.KeyboardModifier.NoModifier
                ):
                    self.switch_music_video_tab()
                    return True
                if event.key() == Qt.Key.Key_Question or event.text() == "?":
                    self.open_shortcuts_dialog()
                    return True
        return super().eventFilter(obj, event)

    def _typing_in_input(self) -> bool:
        fw = QApplication.focusWidget()
        return isinstance(fw, (QLineEdit, QPlainTextEdit))

    def _focus_search(self) -> None:
        if self._is_fullscreen:
            self.toggle_fullscreen()
        if self.stack.currentIndex() == self.PAGE_DISCORD:
            if self.discord_panel.chat_input.isEnabled():
                self.discord_panel.chat_input.setFocus()
                self.discord_panel.chat_input.selectAll()
            return
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _shortcut_space(self) -> None:
        if not self._typing_in_input():
            if (
                self._get_effective_deck() == "video"
                and self._video_item is not None
                and self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
            ):
                self._show_action_hud("play" if self._video_paused else "pause")
            self.toggle_play_pause()

    def _shortcut_fullscreen(self) -> None:
        if not self._typing_in_input() and self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER:
            self.toggle_fullscreen()

    def _shortcut_theater(self) -> None:
        if not self._typing_in_input():
            self.toggle_sidebar()

    def _shortcut_escape(self) -> None:
        if hasattr(self, "discord_panel") and self.discord_panel.close_image_lightbox():
            return
        if self._is_fullscreen:
            self.toggle_fullscreen()

    def _shortcut_mute(self) -> None:
        if self._typing_in_input():
            return
        deck = self._get_effective_deck()
        if deck == "video":
            self.video_panel._toggle_mute()
            self._show_volume_hud(self.cfg.video_volume)
        else:
            self._toggle_music_mute()
            if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER:
                self._show_music_volume_hud(self.cfg.music_volume)

    def _shortcut_seek(self, delta: float) -> None:
        if not self._typing_in_input():
            deck = self._get_effective_deck()
            if deck == "video" and self._video_item is not None:
                if self._video_item.kind == "twitch":
                    return
                self.video_player.seek_relative(delta)
                if self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER:
                    self._show_action_hud("play", text=f"{int(delta):+d}s")
            elif deck == "music" and self._music_item is not None:
                self.music_player.seek_relative(delta)

    def _setup_media_keys(self) -> None:
        self._discord_hotkey_statuses: dict[str, str] = {}
        self._media_filter = GlobalMediaKeyFilter(self)
        self._media_filter.play_pause_pressed.connect(self.toggle_play_pause)
        self._media_filter.next_pressed.connect(self.play_next)
        self._media_filter.prev_pressed.connect(self.play_previous)
        self._media_filter.stop_pressed.connect(self._stop_all)
        self._media_filter.discord_mute_pressed.connect(self._on_global_discord_mute)
        self._media_filter.discord_deafen_pressed.connect(self._on_global_discord_deafen)
        self._media_filter.discord_hangup_pressed.connect(self._on_global_discord_hangup)
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._media_filter)
        self._media_filter.register(int(self.winId()))
        self._apply_discord_hotkeys()

    def _apply_discord_hotkeys(self) -> dict[str, str]:
        self._discord_hotkey_statuses = self._media_filter.configure_discord_hotkeys(
            self.cfg.discord_hotkey_mute,
            self.cfg.discord_hotkey_deafen,
            self.cfg.discord_hotkey_hangup,
            mute_force_priority=self.cfg.discord_hotkey_mute_priority,
        )
        return dict(self._discord_hotkey_statuses)

    def play_discord_sound(self, event_id: str, *, force: bool = False) -> bool:
        if not force and not self.cfg.discord_sounds_enabled:
            return False
        if (
            not force
            and self._discord_deafened
            and event_id in ("user_join", "user_leave", "message")
        ):
            return False
        vol = int(self.cfg.discord_sound_volumes.get(event_id, 60))
        return self.sound_fx.play(
            event_id,
            pack=self.cfg.discord_sound_pack,
            volume_pct=vol,
            enabled=True,
            force=force,
        )

    def _on_global_discord_mute(self) -> None:
        muted = self.discord_panel.toggle_mute_from_hotkey()
        if self.isVisible() and self.isActiveWindow():
            self.show_toast(
                "Discord: Micrófono silenciado" if muted else "Discord: Micrófono activo"
            )

    def _on_global_discord_deafen(self) -> None:
        deaf = self.discord_panel.toggle_deafen_from_hotkey()
        if self.isVisible() and self.isActiveWindow():
            self.show_toast(
                "Discord: Audio ensordecido" if deaf else "Discord: Audio activo"
            )

    def _on_global_discord_hangup(self) -> None:
        if self.discord_panel.hangup_from_hotkey():
            if self.isVisible() and self.isActiveWindow():
                self.show_toast("Discord: Saliste del canal de voz")

    def _stop_all(self) -> None:
        self._save_current_video_progress(force=True)
        self.persist_session_state(force=True)
        self.video_player.unload()
        self.music_player.unload()
        trim_process_memory()

    def open_shortcuts_dialog(self) -> None:
        dlg = ShortcutsDialog(self.palette, config=self.cfg, parent=self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        try:
            dlg.exec()
        finally:
            try:
                dlg.deleteLater()
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Tema Unificado (Obsidian Graphite & Matte Silver)
    # ------------------------------------------------------------------

    def apply_theme(self, theme_key: str = "matte_silver") -> None:
        self.cfg.theme = "matte_silver"
        save_config(self.cfg)
        self.palette = get_theme(self.cfg.theme)
        p = self.palette

        qss = build_stylesheet(
            self.cfg.theme, notify_color=self.cfg.discord_notify_color
        )
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        else:
            self.setStyleSheet(qss)

        apply_windows_dark_titlebar(int(self.winId()), p.base)

        self.nav_yt.setIcon(render_icon("video", p.text, 17))
        self.nav_wl.setIcon(render_icon("bookmark", p.text, 17))
        self.nav_history.setIcon(render_icon("history", p.text, 17))
        self.nav_ytm.setIcon(render_icon("music", p.text, 17))
        self.nav_mymusic.setIcon(render_icon("heart", p.text, 17))
        self.nav_music_history.setIcon(render_icon("history", p.text, 17))
        self.nav_twitch.setIcon(render_icon("live", p.text, 17))
        self._refresh_discord_nav_btn()
        self.minibar_btn.setIcon(render_icon("mini_bar", p.text2, 16))
        self.shortcuts_btn.setIcon(render_icon("help", p.text2, 16))
        self.settings_btn.setIcon(render_icon("settings", p.text2, 16))
        self.search_btn.setIcon(render_icon("search", p.text, 16))
        self.back_to_video_btn.setIcon(render_icon("video", p.text, 15))
        self.close_media_btn.setIcon(render_icon("close", "#F87171", 13))
        self.continue_banner.update_theme(p)
        self.toast_lbl.update_theme(p)
        self._sync_discord_sidebar_card()

        self.bb_thumb.set_bg_color(p.base)
        self.bb_prev.setIcon(render_icon("prev", p.text, 17))
        self.bb_next.setIcon(render_icon("next", p.text, 17))
        self.bb_dl.setIcon(render_icon("download", p.text2, 17))

        self.video_panel.update_theme(p)
        self.music_panel.update_theme(p)
        self.fs_overlay.update_theme(p)
        self.vol_hud.update_theme(p)
        self.action_hud.update_theme(p)
        self.mini_bar.update_theme(p)
        self.discord_panel.update_theme(p)
        self.discord_overlay.update_theme(p)
        if hasattr(self, "discord_notif_toast"):
            self.discord_notif_toast.update_theme(p, self.cfg.discord_notify_color)
        for shimmer in self._page_shimmers:
            shimmer.set_accent(p.accent)
        self._refresh_sidebar_toggle_ui()
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setIcon(render_icon("refresh", p.text, 15))
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setIcon(render_icon("refresh", p.text, 15))
        if self.twitch_refresh_btn is not None:
            self.twitch_refresh_btn.setIcon(render_icon("refresh", p.text, 15))
        if hasattr(self, "channel_back_btn"):
            self.channel_back_btn.setIcon(render_icon("back", p.text, 15))
        if hasattr(self, "channel_refresh_btn") and self.channel_refresh_btn is not None:
            self.channel_refresh_btn.setIcon(render_icon("refresh", p.text, 15))
        if hasattr(self, "channel_avatar_lbl") and not self._channel_avatar_url:
            self.channel_avatar_lbl.setStyleSheet(
                f"background-color: {p.surface2}; color: {p.text2}; "
                f"border-radius: 20px; border: 1px solid {p.border}; "
                "font-size: 16px; font-weight: 700;"
            )
        self._refresh_account_badge()
        self._sync_bottom_bar()
        self._clear_hidden_browse_pages(clear_all=False)
        for card in self._video_cards:
            card.update_theme(p)
        for card in self._channel_cards:
            card.update_theme(p)
        for card in self.wl_grid._widgets:
            if hasattr(card, "update_theme"):
                card.update_theme(p)
        for card in self.history_grid._widgets:
            if hasattr(card, "update_theme"):
                card.update_theme(p)
        for card in self._twitch_cards:
            card.update_theme(p)
        for row in self._music_rows:
            row.update_theme(p)
        for shelf in self._music_shelves:
            shelf.update_theme(p)

    # ------------------------------------------------------------------
    # Persistencia de última sesión + liberación en reposo
    # ------------------------------------------------------------------

    def persist_session_state(self, force: bool = True) -> None:
        if not force and (time.monotonic() - self._last_saved_session_ts) < 5.0:
            return
        self._last_saved_session_ts = time.monotonic()
        is_twitch_cur = self._video_item is not None and self._video_item.kind == "twitch"
        if is_twitch_cur:
            prev = load_session_state(self._session_state_path)
            v_it = prev.video_item
            v_pos = prev.video_pos
            v_dur = prev.video_dur
        else:
            v_it = self._video_item
            v_pos = float(self._video_pos or 0.0)
            v_dur = float(self._video_dur or 0.0)
        state = SessionState(
            active_deck="music" if (is_twitch_cur and self._music_item is not None) else self._active_deck,
            video_item=v_it,
            video_pos=v_pos,
            video_dur=v_dur,
            music_item=self._music_item,
            music_pos=float(self._music_pos or 0.0),
            music_dur=float(self._music_dur or 0.0),
            music_queue=list(self._music_queue),
            queue_index=self._queue_index,
        )
        try:
            save_session_state(state, self._session_state_path)
        except Exception:
            pass

    def _restore_last_session(self) -> None:
        state = load_session_state(self._session_state_path)
        if state.video_item is not None:
            v_it = state.video_item
            self._video_item = v_it
            self._video_pos = state.video_pos
            self._video_dur = max(state.video_dur, float(v_it.duration_sec or 0))
            self._video_paused = True
            self._last_saved_video_pos = self._video_pos
            wl_saved = self.db.is_in_watch_later(v_it.video_id)
            self.video_panel.set_item(v_it, wl_saved)
            if self._video_dur > 0:
                self.video_panel.set_duration(self._video_dur)
                self.fs_overlay.set_duration(self._video_dur)
                self.video_panel.set_position(self._video_pos)
                self.fs_overlay.set_position(self._video_pos)
            self.video_panel.set_paused(True)
            self.video_panel.set_resume_preview(v_it, self._video_pos)
            self.fs_overlay.set_item(v_it, wl_saved)
            self.continue_banner.set_item(v_it, self._video_pos, self._video_dur)
        else:
            self.continue_banner.setVisible(False)

        if state.music_item is not None:
            m_it = state.music_item
            self._music_item = m_it
            self._music_pos = state.music_pos
            self._music_dur = max(state.music_dur, float(m_it.duration_sec or 0))
            self._music_paused = True
            self._music_queue = list(state.music_queue)
            self._queue_index = state.queue_index
            self.music_panel.set_current_item(m_it, self.db.is_in_my_music(m_it.video_id))

        if state.active_deck == "music" and self._music_item is not None:
            self._active_deck = "music"
        elif self._video_item is not None:
            self._active_deck = "video"
        elif self._music_item is not None:
            self._active_deck = "music"

    def _on_continue_banner_resume(self, item: MediaItem, pos: float) -> None:
        self.play_video(item, start_pos=pos)

    def _unload_idle_video_player(self) -> None:
        if not (self.video_player.is_loaded and self._video_paused):
            return
        self._save_current_video_progress(force=True)
        self.persist_session_state(force=True)
        self.video_player.unload()
        if (
            self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
            and self._video_item is not None
        ):
            if self._video_item.kind == "twitch":
                self.video_panel.set_status("Pausado (en espera)")
            else:
                self.video_panel.set_resume_preview(self._video_item, self._video_pos)
        trim_process_memory()

    def _unload_idle_music_player(self) -> None:
        if self.music_player.is_loaded and self._music_paused:
            self.persist_session_state(force=True)
            self.music_player.unload()
            trim_process_memory()

    def free_memory_now(self) -> None:
        self._save_current_video_progress(force=True)
        self.persist_session_state(force=True)
        if self.video_player.is_loaded and (
            self._video_paused or self.video_player.is_paused
        ):
            self._video_paused = True
            self.video_player.unload()
            if (
                self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
                and self._video_item is not None
                and self._video_item.kind != "twitch"
            ):
                self.video_panel.set_resume_preview(self._video_item, self._video_pos)
        if self.music_player.is_loaded and (
            self._music_paused or self.music_player.is_paused
        ):
            self._music_paused = True
            self.music_player.unload()
        self._clear_hidden_browse_pages(clear_all=False)
        self.thumb_cache.trim(8)
        QPixmapCache.clear()
        self.yt.close_worker()
        self.discord_service.stop_if_idle()
        trim_process_memory()
        self._log_ram_sample("free_memory")

    def _on_periodic_ram_tick(self) -> None:
        if self._cleaned_up:
            return
        self._ram_log_tick_count += 1
        self._log_ram_sample("periodic")
        if (self._ram_log_tick_count % 2) == 0:
            self.thumb_cache.trim(20)
            QPixmapCache.clear()
            active_vid = self.video_player.has_active_stream and not self._video_paused
            trim_process_memory(collect_gc=not active_vid)

    def _soft_trim_after_stream_start(self) -> None:
        if self._cleaned_up:
            return
        trim_process_memory(collect_gc=False)

    def _current_page_label(self) -> str:
        names = {
            self.PAGE_VIDEO_SEARCH: "video",
            self.PAGE_VIDEO_PLAYER: "video_player",
            self.PAGE_WATCH_LATER: "watch_later",
            self.PAGE_MUSIC_SEARCH: "music",
            self.PAGE_MY_MUSIC: "my_music",
            self.PAGE_MUSIC_PLAYER: "music_player",
            self.PAGE_HISTORY: "video_history",
            self.PAGE_MUSIC_HISTORY: "music_history",
            self.PAGE_TWITCH: "twitch",
            self.PAGE_DISCORD: "discord",
        }
        return names.get(self.stack.currentIndex(), str(self.stack.currentIndex()))

    def _log_ram_sample(self, event: str) -> None:
        try:
            ws_mb, priv_mb = get_process_memory_mb()
            v_on = self.video_player.is_loaded
            m_on = self.music_player.is_loaded
            if v_on and m_on:
                mpv_st = "video+music"
            elif v_on:
                mpv_st = "video"
            elif m_on:
                mpv_st = "music"
            else:
                mpv_st = "none"

            if not self.discord_service.is_worker_running:
                d_st = "off"
                w_ws = 0.0
                w_priv = 0.0
            else:
                w_ws = self._discord_ws_mb
                w_priv = self._discord_priv_mb
                if self.discord_service.is_in_voice:
                    d_st = f"voice({self.cfg.discord_noise_suppression})"
                elif self.discord_service.is_connected:
                    d_st = "connected"
                else:
                    d_st = "starting"

            self._ram_logger.log_sample(
                event,
                gui_ws_mb=ws_mb,
                gui_priv_mb=priv_mb,
                worker_ws_mb=w_ws,
                worker_priv_mb=w_priv,
                page=self._current_page_label(),
                mpv_state=mpv_st,
                discord_state=d_st,
            )
        except Exception:
            pass

    def _on_discord_worker_ram_updated(self, ws_mb: float, priv_mb: float) -> None:
        self._discord_ws_mb = max(0.0, float(ws_mb))
        self._discord_priv_mb = max(0.0, float(priv_mb))
        if hasattr(self, "res_ram_total_lbl") and self._last_gui_ws_mb > 0.0:
            self.res_ram_total_lbl.setText(
                f"{(self._last_gui_ws_mb + self._discord_ws_mb + self._screen_share_ws_mb):.0f} MB"
            )

    def _on_screen_share_ram_updated(self, ws_mb: float, priv_mb: float) -> None:
        self._screen_share_ws_mb = max(0.0, float(ws_mb))
        self._screen_share_priv_mb = max(0.0, float(priv_mb))
        if hasattr(self, "res_ram_total_lbl") and self._last_gui_ws_mb > 0.0:
            self.res_ram_total_lbl.setText(
                f"{(self._last_gui_ws_mb + self._discord_ws_mb + self._screen_share_ws_mb):.0f} MB"
            )

    def _on_discord_state_changed_for_log(self, state: str, data: dict) -> None:
        if state == "connected":
            self._discord_bot_name = str(
                data.get("bot_name") or self.discord_service.bot_name or "Discord"
            )
            self._discord_bot_avatar_url = str(
                data.get("bot_avatar_url") or self.discord_service.bot_avatar_url or ""
            )
            if not self._was_discord_connected:
                self._was_discord_connected = True
                self._log_ram_sample("discord_connected")
        elif state in ("disconnected", "error"):
            was_in_voice = self._was_discord_in_voice or self._discord_in_voice
            self._discord_ws_mb = 0.0
            self._discord_priv_mb = 0.0
            if hasattr(self, "res_ram_total_lbl") and self._last_gui_ws_mb > 0.0:
                self.res_ram_total_lbl.setText(
                    f"{(self._last_gui_ws_mb + self._screen_share_ws_mb):.0f} MB"
                )
            self._discord_bot_name = ""
            self._discord_bot_avatar_url = ""
            self._discord_voice_active = False
            self._discord_in_voice = False
            self._discord_participants = []
            self._discord_speaking = {}
            self._sync_discord_overlay()
            if was_in_voice:
                self.play_discord_sound("self_leave")
            if self._was_discord_connected:
                self._was_discord_connected = False
                self._was_discord_in_voice = False
                self._log_ram_sample("discord_disconnected")
        self._sync_discord_sidebar_card()

    def _refresh_discord_nav_btn(self) -> None:
        if not hasattr(self, "nav_discord"):
            return
        in_voice = bool(self.discord_service.is_in_voice)
        unread = max(0, int(self._discord_unread_count))
        base_lbl = "  Discord · Voz" if in_voice else "  Discord"
        if unread > 0:
            self.nav_discord.setText(f"{base_lbl} ({unread})")
        else:
            self.nav_discord.setText(base_lbl)
        self.nav_discord.setProperty("unread", "true" if unread > 0 else "false")
        nc_border = get_notification_color(self.cfg.discord_notify_color).border
        self.nav_discord.setIcon(
            render_icon(
                "discord",
                "#4ADE80" if in_voice else (nc_border if unread > 0 else self.palette.text),
                17,
            )
        )
        self.nav_discord.style().unpolish(self.nav_discord)
        self.nav_discord.style().polish(self.nav_discord)

    def _on_discord_unread_count_changed(self, count: int) -> None:
        self._discord_unread_count = max(0, int(count))
        self._refresh_discord_nav_btn()

    def _on_discord_voice_state_changed(self, state: dict) -> None:
        in_voice = bool(state.get("in_voice", False))
        ch = str(state.get("channel_name") or "").strip()
        self._discord_in_voice = in_voice
        self._discord_participants = list(state.get("participants") or []) if in_voice else []
        if not in_voice:
            self._discord_speaking = {}
        if in_voice:
            self.nav_discord.setToolTip(f"Conectado en canal de voz: {ch}")
            if not self._was_discord_in_voice:
                self._was_discord_in_voice = True
                self._log_ram_sample("discord_voice_join")
        else:
            self.nav_discord.setToolTip(
                "Canales de voz de Discord (Bot ultraliviano, 0 MB en reposo) [Tab]"
            )
            if self._was_discord_in_voice:
                self._was_discord_in_voice = False
                self._log_ram_sample("discord_voice_leave")
        self._refresh_discord_nav_btn()
        self._sync_discord_overlay()
        self._sync_discord_sidebar_card()

    def _on_discord_speaking_updated(self, speaking_map: dict) -> None:
        self._discord_speaking = dict(speaking_map)
        self.discord_overlay.update_speaking(self._discord_speaking)

    def _on_discord_token_changed(self) -> None:
        self.discord_panel.refresh_token_state()
        if not has_discord_token() and self.discord_service.is_worker_running:
            self.discord_service.disconnect_and_kill()

    def _on_discord_voice_status_changed(
        self, active: bool, muted: bool, deafened: bool
    ) -> None:
        self._discord_voice_active = bool(active)
        self._discord_muted = bool(muted)
        self._discord_deafened = bool(deafened)
        self._sync_discord_overlay()
        self._sync_discord_sidebar_card()

    def _on_sidebar_discord_audio_clicked(self) -> None:
        if hasattr(self, "discord_panel"):
            self.discord_panel.toggle_audio_popover(self.discord_mini_card)

    def _on_discord_mic_level_updated(self, rms: int, transmitting: bool) -> None:
        if not hasattr(self, "discord_mini_vu_lbl"):
            return
        if self._discord_muted or self._discord_deafened:
            val = 0
            tx = False
        else:
            val = max(0, min(100, int(rms)))
            tx = bool(transmitting)
        self.discord_mini_vu_lbl.setText(f"Mic {val:2d}%")
        self.discord_mini_vu_bar.setValue(val)
        if tx:
            self.discord_mini_vu_bar.setStyleSheet("QProgressBar::chunk { background-color: #4ADE80; }")
        else:
            self.discord_mini_vu_bar.setStyleSheet("")

    def _on_sidebar_discord_mute(self) -> None:
        if hasattr(self, "discord_panel"):
            self.discord_panel.toggle_mute_from_hotkey()

    def _on_sidebar_discord_deafen(self) -> None:
        if hasattr(self, "discord_panel"):
            self.discord_panel.toggle_deafen_from_hotkey()

    def _on_sidebar_discord_hangup(self) -> None:
        if hasattr(self, "discord_panel"):
            if not self.discord_panel.hangup_from_hotkey():
                self.discord_service.leave_channel()
        else:
            self.discord_service.leave_channel()

    def _sync_discord_sidebar_card(self) -> None:
        if not hasattr(self, "discord_mini_card"):
            return
        active = bool(
            self._was_discord_connected
            or self.discord_service.is_connected
            or self._discord_voice_active
        )
        self.discord_mini_card.setVisible(active)
        if not active:
            if hasattr(self, "discord_panel"):
                self.discord_panel.close_audio_popover()
            if hasattr(self, "discord_mini_hangup_btn"):
                self.discord_mini_hangup_btn.setVisible(False)
            self._on_discord_mic_level_updated(0, False)
            return

        bot_name = self._discord_bot_name or self.discord_service.bot_name or "Discord"
        self.discord_mini_name.setText(bot_name)
        in_voice = bool(self._discord_in_voice or self.discord_service.is_in_voice)
        if in_voice and self.discord_service.active_channel_name:
            self.discord_mini_sub.setText(f"En voz · {self.discord_service.active_channel_name}")
            self.discord_mini_sub.setStyleSheet("font-size: 9.5px; color: #4ADE80; font-weight: 600;")
        else:
            self.discord_mini_sub.setText("Conectado")
            self.discord_mini_sub.setStyleSheet("font-size: 9.5px;")

        av_set = False
        if self._discord_bot_avatar_url:
            pix = self.thumb_cache.get_or_fetch(
                self._discord_bot_avatar_url, QSize(22, 22), circular=True
            )
            if pix is not None:
                self.discord_mini_avatar.setText("")
                self.discord_mini_avatar.setPixmap(pix)
                self.discord_mini_avatar.setStyleSheet("background-color: transparent; border: none;")
                av_set = True
        if not av_set:
            self.discord_mini_avatar.setPixmap(QPixmap())
            self.discord_mini_avatar.setText((bot_name.strip()[:1] or "D").upper())
            self.discord_mini_avatar.setStyleSheet(
                f"background-color: #2D3647; color: {self.palette.text}; "
                f"border-radius: 11px; border: 1px solid {self.palette.border_bright}; "
                "font-size: 10px; font-weight: 700;"
            )

        p = self.palette
        if hasattr(self, "discord_mini_audio_btn"):
            self.discord_mini_audio_btn.setIcon(render_icon("settings", p.text, 12))
        self.discord_mini_mute_btn.setIcon(
            render_icon(
                "mic_off" if self._discord_muted else "mic",
                "#F87171" if self._discord_muted else p.text,
                12,
            )
        )
        self.discord_mini_mute_btn.setProperty("active", "true" if self._discord_muted else "false")
        self.discord_mini_deaf_btn.setIcon(
            render_icon(
                "headphones_off" if self._discord_deafened else "headphones",
                "#F87171" if self._discord_deafened else p.text,
                12,
            )
        )
        self.discord_mini_deaf_btn.setProperty("active", "true" if self._discord_deafened else "false")
        if hasattr(self, "discord_mini_hangup_btn"):
            self.discord_mini_hangup_btn.setVisible(in_voice)
            self.discord_mini_hangup_btn.setIcon(render_icon("phone_off", "#F87171", 12))
        for b in (self.discord_mini_mute_btn, self.discord_mini_deaf_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        if not in_voice or self._discord_muted or self._discord_deafened:
            self._on_discord_mic_level_updated(0, False)

    def _sync_discord_overlay(self) -> None:
        self.discord_overlay.set_custom_position(
            self.cfg.discord_overlay_x, self.cfg.discord_overlay_y
        )
        self.discord_overlay.update_state(
            overlay_mode=self.cfg.discord_overlay_mode,
            active=self._discord_voice_active,
            in_voice=self._discord_in_voice,
            muted=self._discord_muted,
            deafened=self._discord_deafened,
            opacity_pct=self.cfg.discord_overlay_opacity,
            participants=self._discord_participants,
            speaking=self._discord_speaking,
        )

    def _on_discord_overlay_position_changed(self, x: int, y: int) -> None:
        self.cfg.discord_overlay_x = int(x)
        self.cfg.discord_overlay_y = int(y)
        save_config(self.cfg)

    def _on_discord_overlay_reset_position(self) -> None:
        self.cfg.discord_overlay_x = None
        self.cfg.discord_overlay_y = None
        save_config(self.cfg)
        self.discord_overlay.set_custom_position(None, None)
        self.show_toast("Posición del overlay restablecida.")

    def _start_discord_overlay_position_edit(self) -> None:
        self.discord_overlay.set_custom_position(
            self.cfg.discord_overlay_x, self.cfg.discord_overlay_y
        )
        self.discord_overlay.set_edit_mode(True)
        self.show_toast("Arrastra el overlay y pulsa Guardar (o Esc) cuando termines.")

    def _on_discord_ui_settings_changed(self) -> None:
        self.discord_panel.refresh_ui_preferences()
        self.discord_panel.sync_notify_channels()
        self._sync_discord_overlay()

    def _on_discord_channel_alert(self, data: dict) -> None:
        if not self.cfg.discord_notify_chat_enabled:
            return
        cid = str(data.get("channel_id") or "")
        if not cid or cid not in self.cfg.discord_notify_channels:
            return
        if (
            self.isVisible()
            and not self.isMinimized()
            and self.isActiveWindow()
            and self.stack.currentIndex() == self.PAGE_DISCORD
            and self.discord_panel.is_viewing_channel(cid)
        ):
            return
        ch_name = str(data.get("channel_name") or "canal")
        gid = str(data.get("guild_id") or "")
        author = str(data.get("author_name") or "Discord")
        av_url = str(data.get("author_avatar_url") or "")
        preview = str(data.get("preview") or "").strip() or "Nuevo mensaje"
        self.play_discord_sound("message")
        if self.isVisible():
            flash_window_taskbar(int(self.winId()), count=3)
        self.discord_notif_toast.show_notification(
            author,
            preview,
            header_text=f"{APP_NAME} · #{ch_name}",
            avatar_url=av_url,
            payload={
                "kind": "channel",
                "guild_id": gid,
                "target_id": cid,
                "title": ch_name,
            },
        )

    def _on_discord_dm_alert(self, data: dict) -> None:
        if not self.cfg.discord_notify_chat_enabled:
            return
        uid = str(data.get("user_id") or "")
        if not uid:
            return
        if (
            self.isVisible()
            and not self.isMinimized()
            and self.isActiveWindow()
            and self.stack.currentIndex() == self.PAGE_DISCORD
            and self.discord_panel.is_viewing_dm(uid)
        ):
            return
        author = str(data.get("author_name") or "Discord")
        peer_name = str(data.get("peer_name") or author)
        av_url = str(
            data.get("author_avatar_url") or data.get("peer_avatar_url") or ""
        )
        preview = str(data.get("preview") or "").strip() or "Nuevo mensaje directo"
        if self.isVisible():
            flash_window_taskbar(int(self.winId()), count=3)
        self.discord_notif_toast.show_notification(
            author,
            preview,
            header_text=APP_NAME,
            avatar_url=av_url,
            payload={
                "kind": "dm",
                "target_id": uid,
                "title": peer_name,
                "avatar_url": av_url,
            },
        )

    def _on_discord_notification_clicked(self, payload: dict) -> None:
        self.restore_window()
        self._switch_page(self.PAGE_DISCORD)
        if payload:
            self.discord_panel.open_notification_target(payload)

    def _on_discord_notify_color_changed(self, color_key: str) -> None:
        spec = get_notification_color(color_key)
        self.cfg.discord_notify_color = spec.key
        self.apply_theme(self.cfg.theme)

    def _preview_discord_notification(self) -> None:
        self.play_discord_sound("message", force=True)
        self.discord_notif_toast.show_notification(
            "NuBa",
            "¡Hola! Así se ven tus notificaciones de SwiftPlay",
            header_text=APP_NAME,
            avatar_url="",
            payload=None,
            duration_ms=4200,
        )

    # ------------------------------------------------------------------
    # Navegación rápida (Tab Switch) y Vistas
    # ------------------------------------------------------------------

    def switch_music_video_tab(self) -> None:
        """Rota instantáneamente entre YouTube Video, YT Music, Twitch y Discord manteniendo estados."""
        cur = self.stack.currentIndex()
        playing_twitch = (
            self._video_item is not None and self._video_item.kind == "twitch"
        )
        in_twitch = cur == self.PAGE_TWITCH or (
            cur == self.PAGE_VIDEO_PLAYER and playing_twitch
        )
        in_music = cur in (
            self.PAGE_MUSIC_SEARCH,
            self.PAGE_MY_MUSIC,
            self.PAGE_MUSIC_PLAYER,
            self.PAGE_MUSIC_HISTORY,
        )

        if cur == self.PAGE_DISCORD:
            self._active_deck = "video"
            if (
                self._video_item is not None
                and not playing_twitch
                and self.video_player.has_active_stream
            ):
                target = self.PAGE_VIDEO_PLAYER
            else:
                target = self._last_video_page
                if target in (self.PAGE_TWITCH, self.PAGE_DISCORD) or (
                    target == self.PAGE_VIDEO_PLAYER and playing_twitch
                ):
                    target = self.PAGE_VIDEO_SEARCH
            self._switch_page(target)
        elif not in_music and not in_twitch:
            self._last_video_page = cur
            self._active_deck = "music"
            if (
                self._music_item is not None
                and self._last_music_page == self.PAGE_MUSIC_SEARCH
            ):
                target = self.PAGE_MUSIC_PLAYER
            else:
                target = self._last_music_page
            self._switch_page(target)
        elif in_music:
            self._last_music_page = cur
            if playing_twitch and self.video_player.has_active_stream:
                self._active_deck = "video"
                target = self.PAGE_VIDEO_PLAYER
            else:
                target = self.PAGE_TWITCH
            self._switch_page(target)
        else:
            self._switch_page(self.PAGE_DISCORD)

    def _switch_active_deck_from_bb(self) -> None:
        """Salta directamente entre el deck de Música y el deck de Video/Directo cargado."""
        cur = self.stack.currentIndex()
        playing_twitch = (
            self._video_item is not None and self._video_item.kind == "twitch"
        )
        if cur == self.PAGE_DISCORD:
            if self._active_deck == "music" and self._music_item is not None:
                self._switch_page(self.PAGE_MUSIC_PLAYER)
            elif self._video_item is not None and self.video_player.has_active_stream:
                self._switch_page(self.PAGE_VIDEO_PLAYER)
            else:
                self._switch_page(self._last_video_page)
            return
        in_video = cur in (
            self.PAGE_VIDEO_SEARCH,
            self.PAGE_VIDEO_PLAYER,
            self.PAGE_WATCH_LATER,
            self.PAGE_HISTORY,
            self.PAGE_TWITCH,
            self.PAGE_CHANNEL,
        )
        if in_video:
            if not (
                cur == self.PAGE_TWITCH
                or (cur == self.PAGE_VIDEO_PLAYER and playing_twitch)
            ):
                self._last_video_page = cur
            self._active_deck = "music"
            target = (
                self.PAGE_MUSIC_PLAYER
                if self._music_item is not None
                else self._last_music_page
            )
            self._switch_page(target)
        else:
            self._last_music_page = cur
            self._active_deck = "video"
            if self._video_item is not None and self.video_player.has_active_stream:
                target = self.PAGE_VIDEO_PLAYER
            elif playing_twitch:
                target = self.PAGE_TWITCH
            else:
                target = self._last_video_page
            self._switch_page(target)

    def _set_page_loading(self, page: QWidget | None, loading: bool) -> None:
        shimmer = getattr(page, "_shimmer_bar", None) if page is not None else None
        if shimmer is not None:
            shimmer.set_active(bool(loading))

    def _maybe_relayout_grid(self, grid: ResponsiveGridWidget, hint_width: int) -> None:
        if not grid._widgets:
            return
        last_hw = int(grid.property("_rendered_hint_w") or 0)
        if abs(last_hw - int(hint_width)) > 12 or grid._last_cols == 0:
            grid.relayout(force_regrid=False, hint_width=hint_width)
            grid.setProperty("_rendered_hint_w", int(hint_width))

    def _clear_hidden_browse_pages(self, *, clear_all: bool = False) -> None:
        cur = -1 if clear_all else self.stack.currentIndex()
        if cur != self.PAGE_VIDEO_SEARCH and self.video_grid._widgets:
            self.video_grid.clear_widgets()
            self._video_cards.clear()
            self._video_page_dirty = True
        if cur != self.PAGE_WATCH_LATER and self.wl_grid._widgets:
            self.wl_grid.clear_widgets()
            self._wl_page_dirty = True
        if cur != self.PAGE_HISTORY and self.history_grid._widgets:
            self.history_grid.clear_widgets()
            self._history_page_dirty = True
        if cur != self.PAGE_MUSIC_SEARCH and (
            self._music_rows or self._music_tiles or self._music_shelves
        ):
            self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
            self._music_rows.clear()
            self._music_tiles.clear()
            self._music_shelves.clear()
            self._music_search_page_dirty = True
        if cur != self.PAGE_MY_MUSIC and not self._my_music_page_dirty:
            self._clear_layout_widgets(self.my_music_lay, keep_widget=self.my_music_empty_lbl)
            self._my_music_page_dirty = True
        if cur != self.PAGE_MUSIC_HISTORY and not self._music_history_page_dirty:
            self._clear_layout_widgets(
                self.music_history_lay, keep_widget=self.music_history_empty_lbl
            )
            self._music_history_page_dirty = True
        if cur != self.PAGE_TWITCH and self.twitch_grid._widgets:
            self.twitch_grid.clear_widgets()
            self._twitch_cards.clear()
            self._twitch_page_dirty = True
        if cur != self.PAGE_CHANNEL and self.channel_grid._widgets:
            self.channel_grid.clear_widgets()
            self._channel_cards.clear()
            self._channel_page_dirty = True

    def _schedule_video_surface_refresh(self, delay_ms: int = 55) -> None:
        if self._cleaned_up:
            return
        self._video_refresh_timer.start(max(10, int(delay_ms)))

    def _on_video_vo_error(self, _detail: str) -> None:
        if (
            not self._cleaned_up
            and self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
            and not self.cfg.video_audio_only
        ):
            self._schedule_video_surface_refresh(30)

    def _post_reinit_video_redraw(self) -> None:
        if self._cleaned_up or self.stack.currentIndex() != self.PAGE_VIDEO_PLAYER:
            return
        self.video_panel.video_surface.force_native_redraw()

    def _refresh_video_surface_output(self) -> None:
        if self._cleaned_up or self.stack.currentIndex() != self.PAGE_VIDEO_PLAYER:
            return
        if self._video_item is None:
            return
        if (
            not self.cfg.video_audio_only
            and self.video_panel.media_stack.currentIndex() != 0
            and self.video_player.has_active_stream
        ):
            self.video_panel.media_stack.setCurrentIndex(0)
        self.video_panel.video_surface.force_native_redraw()
        if not self.cfg.video_audio_only and self.video_player.has_active_stream:
            if self.video_player.refresh_video_output():
                self._video_post_reinit_timer.start(150)

    def _on_page_fade_step(self, val: object) -> None:
        w = self._page_fade_target
        if w is None:
            return
        eff = w.graphicsEffect()
        if isinstance(eff, QGraphicsOpacityEffect):
            eff.setOpacity(max(0.0, min(1.0, float(val))))

    def _on_page_fade_finished(self) -> None:
        w = self._page_fade_target
        self._page_fade_target = None
        if w is not None:
            try:
                w.setGraphicsEffect(None)
            except RuntimeError:
                pass

    def _stop_page_fade(self) -> None:
        try:
            self._page_fade_anim.stop()
        except RuntimeError:
            pass
        if self._page_fade_target is not None:
            try:
                self._page_fade_target.setGraphicsEffect(None)
            except RuntimeError:
                pass
            self._page_fade_target = None

    def _animate_page_fade(self, widget: QWidget | None) -> None:
        if widget is None or not self.isVisible() or self._cleaned_up:
            return
        self._stop_page_fade()
        eff = QGraphicsOpacityEffect(widget)
        eff.setOpacity(0.86)
        widget.setGraphicsEffect(eff)
        self._page_fade_target = widget
        self._page_fade_anim.start()

    def _switch_page(self, page_idx: int) -> None:
        if self._is_fullscreen and page_idx != self.PAGE_VIDEO_PLAYER:
            self.toggle_fullscreen()

        self._stop_page_fade()
        prev_idx = self.stack.currentIndex()
        hint_w = max(480, self.stack.width() - 52)
        trigger_music_home = False
        trigger_twitch_load = False

        suppress_updates = page_idx != self.PAGE_VIDEO_PLAYER
        if suppress_updates:
            self.stack.setUpdatesEnabled(False)
        try:
            if page_idx == self.PAGE_VIDEO_PLAYER:
                if prev_idx != self.PAGE_VIDEO_PLAYER:
                    self._video_return_page = prev_idx
                self.thumb_cache.trim(12)
                QPixmapCache.clear()
                if self._video_paused and self.video_player.is_loaded:
                    self._video_idle_unload_timer.start(90_000)
                else:
                    self._video_idle_unload_timer.stop()
            elif self._video_paused and self.video_player.is_loaded:
                self._video_idle_unload_timer.start(20_000)

            self.top_bar.setVisible(not self._is_fullscreen and page_idx != self.PAGE_DISCORD)

            music_return_nav = {
                self.PAGE_MUSIC_SEARCH: self.nav_ytm,
                self.PAGE_MY_MUSIC: self.nav_mymusic,
                self.PAGE_MUSIC_HISTORY: self.nav_music_history,
            }.get(self._music_return_page, self.nav_ytm)

            video_player_nav = (
                self.nav_twitch
                if (self._video_item is not None and self._video_item.kind == "twitch")
                else self.nav_yt
            )
            nav_map = {
                self.PAGE_VIDEO_SEARCH: self.nav_yt,
                self.PAGE_VIDEO_PLAYER: video_player_nav,
                self.PAGE_WATCH_LATER: self.nav_wl,
                self.PAGE_HISTORY: self.nav_history,
                self.PAGE_MUSIC_SEARCH: self.nav_ytm,
                self.PAGE_MY_MUSIC: self.nav_mymusic,
                self.PAGE_MUSIC_PLAYER: music_return_nav,
                self.PAGE_MUSIC_HISTORY: self.nav_music_history,
                self.PAGE_TWITCH: self.nav_twitch,
                self.PAGE_DISCORD: self.nav_discord,
                self.PAGE_CHANNEL: self.nav_yt,
            }
            active_btn = nav_map.get(page_idx)
            for btn in (
                self.nav_yt,
                self.nav_wl,
                self.nav_history,
                self.nav_ytm,
                self.nav_mymusic,
                self.nav_music_history,
                self.nav_twitch,
                self.nav_discord,
            ):
                want = "true" if btn is active_btn else "false"
                if btn.property("active") != want:
                    btn.setProperty("active", want)
                    btn.style().unpolish(btn)
                    btn.style().polish(btn)

            is_twitch_player = (
                page_idx == self.PAGE_VIDEO_PLAYER
                and self._video_item is not None
                and self._video_item.kind == "twitch"
            )
            if page_idx == self.PAGE_DISCORD:
                self._search_mode = "video"
                self.search_input.setPlaceholderText("Buscar videos en YouTube... [Ctrl+L]")
                self.discord_panel.refresh_token_state()
            elif page_idx == self.PAGE_TWITCH or is_twitch_player:
                self._search_mode = "twitch"
                self.search_input.setPlaceholderText(
                    "Buscar canales en Twitch o pegar enlace... [Ctrl+L]"
                )
            elif page_idx in (
                self.PAGE_VIDEO_SEARCH,
                self.PAGE_VIDEO_PLAYER,
                self.PAGE_WATCH_LATER,
                self.PAGE_HISTORY,
                self.PAGE_CHANNEL,
            ):
                self._search_mode = "video"
                self._last_video_page = page_idx
                self.search_input.setPlaceholderText("Buscar videos en YouTube... [Ctrl+L]")
            else:
                self._search_mode = "music"
                self._last_music_page = page_idx
                if page_idx in (
                    self.PAGE_MUSIC_SEARCH,
                    self.PAGE_MY_MUSIC,
                    self.PAGE_MUSIC_HISTORY,
                ):
                    self._music_return_page = page_idx
                self.search_input.setPlaceholderText(
                    "Buscar canciones o artistas en YT Music... [Ctrl+L]"
                )

            self._update_music_filter_visibility()
            if self._search_mode != self._last_completer_mode:
                self._refresh_search_completer()

            # Pre-construir únicamente si la vista destino está sucia o sin renderizar
            if page_idx == self.PAGE_VIDEO_SEARCH:
                if self._video_page_dirty:
                    if self._video_showing_search:
                        if self._video_results:
                            self._render_video_cards(self._video_results, hint_width=hint_w)
                    elif self._video_feed_items:
                        self._render_video_cards(self._video_feed_items, hint_width=hint_w)
                else:
                    self._maybe_relayout_grid(self.video_grid, hint_w)
            elif page_idx == self.PAGE_WATCH_LATER:
                if self._wl_page_dirty:
                    self._render_watch_later(hint_width=hint_w)
                else:
                    self._maybe_relayout_grid(self.wl_grid, hint_w)
            elif page_idx == self.PAGE_HISTORY:
                if self._history_page_dirty:
                    self._render_history(hint_width=hint_w)
                else:
                    self._maybe_relayout_grid(self.history_grid, hint_w)
            elif page_idx == self.PAGE_MY_MUSIC:
                if self._my_music_page_dirty:
                    self._render_my_music()
            elif page_idx == self.PAGE_MUSIC_PLAYER:
                self._render_queue()
            elif page_idx == self.PAGE_MUSIC_HISTORY:
                if self._music_history_page_dirty:
                    self._render_music_history()
            elif page_idx == self.PAGE_MUSIC_SEARCH:
                if self._music_search_page_dirty:
                    if self._music_showing_search:
                        if self._music_results:
                            self._render_music_search_rows(self._music_results)
                    elif self._music_home_loaded and self._music_home_sections:
                        self._render_music_home_sections(self._music_home_sections)
                    elif not self._music_home_loaded:
                        trigger_music_home = True
            elif page_idx == self.PAGE_TWITCH:
                if not self._twitch_page_dirty:
                    self._maybe_relayout_grid(self.twitch_grid, hint_w)
                elif self._twitch_showing_search and self._twitch_results:
                    self._render_twitch_cards(self._twitch_results, hint_width=hint_w)
                else:
                    cached_tw = self._twitch_feed_cache.get(self._twitch_tab)
                    if cached_tw is not None and (time.monotonic() - cached_tw[0]) < 90.0:
                        self._render_twitch_cards(cached_tw[1], hint_width=hint_w)
                    else:
                        trigger_twitch_load = True
            elif page_idx == self.PAGE_CHANNEL:
                if self._channel_page_dirty and self._channel_items:
                    self._render_channel_cards(self._channel_items, hint_width=hint_w)
                else:
                    self._maybe_relayout_grid(self.channel_grid, hint_w)

            self.stack.setCurrentIndex(page_idx)
        finally:
            if suppress_updates:
                self.stack.setUpdatesEnabled(True)

        if trigger_music_home:
            self._load_music_home()
        elif trigger_twitch_load:
            self._load_twitch_view()

        if page_idx == self.PAGE_MUSIC_PLAYER and prev_idx != self.PAGE_MUSIC_PLAYER:
            self.music_panel.play_entrance_animation()
        elif page_idx == self.PAGE_VIDEO_PLAYER and self._video_item is not None:
            self._schedule_video_surface_refresh(55)
        elif page_idx != self.PAGE_VIDEO_PLAYER and page_idx != prev_idx:
            self._animate_page_fade(self.stack.currentWidget())
        if page_idx == self.PAGE_VIDEO_SEARCH:
            self._video_viewport_timer.start()
        elif page_idx == self.PAGE_CHANNEL:
            self._channel_viewport_timer.start()

        self.back_to_video_btn.setText(
            " Directo"
            if (self._video_item is not None and self._video_item.kind == "twitch")
            else " Video"
        )
        self.back_to_video_btn.setVisible(
            self._video_item is not None and page_idx != self.PAGE_VIDEO_PLAYER
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()

    def _update_music_filter_visibility(self) -> None:
        show = self._search_mode == "music"
        self.filter_songs_btn.setVisible(show)
        self.filter_videos_btn.setVisible(show)

    def _set_music_filter(self, flt: str) -> None:
        self._music_filter = flt
        self.filter_songs_btn.setProperty("active", "true" if flt == "songs" else "false")
        self.filter_videos_btn.setProperty("active", "true" if flt == "videos" else "false")
        for b in (self.filter_songs_btn, self.filter_videos_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        if self.search_input.text().strip():
            self._on_search_submitted()

    def _refresh_search_completer(self) -> None:
        self._last_completer_mode = self._search_mode
        self._search_history_model.setStringList(
            self.db.get_search_history(self._search_mode)
        )

    def _on_search_submitted(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            return
        self.db.record_search(self._search_mode, query)
        self._refresh_search_completer()
        self._search_gen += 1
        gen = self._search_gen

        if self._search_mode == "video":
            self._video_showing_search = True
            self._video_results = []
            self._video_page_dirty = True
            self.video_view_title.setText(f"«{query}»")
            if self.video_back_feed_btn is not None:
                self.video_back_feed_btn.setVisible(True)
            self._switch_page(self.PAGE_VIDEO_SEARCH)
            self.video_empty_lbl.setText(f"Buscando '{query}'...")
            self.video_empty_lbl.setVisible(True)
            self.video_grid.clear_widgets()
            self._video_cards.clear()
            self._set_page_loading(self.video_search_page, True)
            self._run_async(
                lambda: self.yt.search_videos(query, limit=16),
                on_done=lambda res: self._on_video_search_done(gen, query, res),
                on_error=lambda err: self._on_search_error(gen, "video", err),
            )
        elif self._search_mode == "twitch":
            self._twitch_showing_search = True
            self._twitch_results = []
            self._twitch_page_dirty = True
            self.twitch_view_title.setText(f"«{query}»")
            if self.twitch_back_btn is not None:
                self.twitch_back_btn.setVisible(True)
            self._switch_page(self.PAGE_TWITCH)
            self.twitch_empty_lbl.setText(f"Buscando '{query}' en Twitch...")
            self.twitch_empty_lbl.setVisible(True)
            self.twitch_grid.clear_widgets()
            self._twitch_cards.clear()
            self._set_page_loading(self.twitch_page, True)
            self._run_async(
                lambda: self.twitch.search_channels(query, limit=16),
                on_done=lambda res: self._on_twitch_search_done(gen, query, res),
                on_error=lambda err: self._on_search_error(gen, "twitch", err),
            )
        else:
            self._music_showing_search = True
            self._music_results = []
            self._music_search_page_dirty = True
            self.music_view_title.setText(f"«{query}»")
            if self.music_back_home_btn is not None:
                self.music_back_home_btn.setVisible(True)
            self._switch_page(self.PAGE_MUSIC_SEARCH)
            self.music_empty_lbl.setText(f"Buscando '{query}'...")
            self.music_empty_lbl.setVisible(True)
            self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
            self._music_rows.clear()
            self._music_tiles.clear()
            self._music_shelves.clear()
            self._set_page_loading(self.music_search_page, True)
            flt = self._music_filter
            self._run_async(
                lambda: self.yt.search_music(query, search_filter=flt, limit=20),
                on_done=lambda res: self._on_music_search_done(gen, query, res),
                on_error=lambda err: self._on_search_error(gen, "music", err),
            )

    def _refresh_offscreen_grid_thumbs(
        self, scroll: QScrollArea, cards: list[Any]
    ) -> None:
        if not cards:
            return
        vp = scroll.viewport()
        if vp is None or vp.height() <= 0:
            return
        vp_h = vp.height()
        margin = 1500
        for card in cards:
            set_off = getattr(card, "set_offscreen_unloaded", None)
            if not callable(set_off):
                continue
            try:
                top_y = card.mapTo(vp, QPoint(0, 0)).y()
                bot_y = top_y + card.height()
                offscreen = (top_y > vp_h + margin) or (bot_y < -margin)
                set_off(offscreen)
            except RuntimeError:
                pass

    def _on_video_scroll_changed(self, value: int) -> None:
        self._video_viewport_timer.start()
        if (
            self._video_showing_search
            or self._video_feed_loading_more
            or self._video_feed_exhausted
            or self.stack.currentIndex() != self.PAGE_VIDEO_SEARCH
            or len(self._video_feed_items) >= self.MAX_FEED_ITEMS
        ):
            return
        sb = self.video_scroll.verticalScrollBar()
        if sb is None or sb.maximum() <= 0:
            return
        if value >= int(sb.maximum() * 0.80):
            self._load_more_video_feed()

    def _render_video_cards(
        self, items: list[MediaItem], empty_text: str = "", hint_width: int = 0
    ) -> None:
        self.video_grid.clear_widgets()
        self._video_cards.clear()
        self._video_page_dirty = False
        if not items:
            if empty_text:
                self.video_empty_lbl.setText(empty_text)
            self.video_empty_lbl.setVisible(True)
            return
        self.video_empty_lbl.setVisible(False)
        wl_ids = self.db.list_watch_later_ids()
        cards: list[QWidget] = []
        for item in items:
            card = self._create_video_card(item, wl_ids=wl_ids)
            self._video_cards.append(card)
            cards.append(card)
        hint_w = hint_width if hint_width > 0 else max(480, self.stack.width() - 52)
        self.video_grid.set_items_widgets(cards, hint_width=hint_w)
        self.video_grid.setProperty("_rendered_hint_w", int(hint_w))
        self._video_viewport_timer.start()

    def _load_more_video_feed(self) -> None:
        if (
            self._cleaned_up
            or self._video_feed_loading_more
            or self._video_feed_exhausted
            or self._video_showing_search
        ):
            return
        remaining = self.MAX_FEED_ITEMS - len(self._video_feed_items)
        if remaining <= 0:
            return
        self._video_feed_loading_more = True
        self._set_page_loading(self.video_search_page, True)
        gen = self._video_feed_gen
        existing_ids = {it.video_id for it in self._video_feed_items if it.video_id}
        watched_ids = self.db.list_watched_video_ids()
        recent_ids = set(self._video_feed_shown_ids) | existing_ids
        fallback_q = self.db.get_search_history("video")
        batch_limit = min(24, remaining)
        self._run_async(
            lambda: self.yt.get_video_home_feed(
                limit=batch_limit,
                fallback_queries=fallback_q,
                watched_ids=watched_ids,
                recent_shown_ids=recent_ids,
                refresh=False,
                append=True,
            ),
            on_done=lambda items: self._on_video_feed_more_done(gen, items),
            on_error=lambda _err: self._on_video_feed_more_error(gen),
        )

    def _on_video_feed_more_done(self, gen: int, items: list[MediaItem]) -> None:
        if gen != self._video_feed_gen:
            return
        self._video_feed_loading_more = False
        self._set_page_loading(self.video_search_page, False)
        seen = {it.video_id for it in self._video_feed_items if it.video_id}
        cap_left = max(0, self.MAX_FEED_ITEMS - len(self._video_feed_items))
        new_items = [it for it in items if it.video_id and it.video_id not in seen][:cap_left]
        if not new_items:
            self._video_feed_exhausted = True
            return
        self._video_feed_items.extend(new_items)
        if len(self._video_feed_items) >= self.MAX_FEED_ITEMS:
            self._video_feed_exhausted = True
        if self._video_showing_search or self.stack.currentIndex() != self.PAGE_VIDEO_SEARCH:
            self._video_page_dirty = True
            return
        wl_ids = self.db.list_watch_later_ids()
        new_cards: list[QWidget] = []
        for it in new_items:
            card = self._create_video_card(it, wl_ids=wl_ids)
            self._video_cards.append(card)
            new_cards.append(card)
        hint_w = max(480, self.stack.width() - 52)
        self.video_grid.append_widgets(new_cards, hint_width=hint_w)
        self._video_viewport_timer.start()

    def _on_video_feed_more_error(self, gen: int) -> None:
        if gen != self._video_feed_gen:
            return
        self._video_feed_loading_more = False
        self._set_page_loading(self.video_search_page, False)

    def _render_music_home_sections(
        self, sections: list[FeedSection], empty_text: str = ""
    ) -> None:
        self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
        self._music_rows.clear()
        self._music_tiles.clear()
        self._music_shelves.clear()
        self._music_search_page_dirty = False
        if not sections:
            if empty_text:
                self.music_empty_lbl.setText(empty_text)
            self.music_empty_lbl.setVisible(True)
            return
        self.music_empty_lbl.setVisible(False)
        mm_ids = self.db.list_my_music_ids()
        for sec in sections:
            tiles: list[MusicTileWidget] = []
            for item in sec.items:
                tile = self._create_music_tile(item, mm_ids=mm_ids)
                self._music_tiles.append(tile)
                tiles.append(tile)
            shelf = MusicShelfWidget(sec.title, tiles, self.palette)
            self._music_shelves.append(shelf)
            self.music_results_lay.insertWidget(self.music_results_lay.count() - 1, shelf)

    def _render_music_search_rows(
        self, items: list[MediaItem], empty_text: str = ""
    ) -> None:
        self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
        self._music_rows.clear()
        self._music_tiles.clear()
        self._music_shelves.clear()
        self._music_search_page_dirty = False
        if not items:
            if empty_text:
                self.music_empty_lbl.setText(empty_text)
            self.music_empty_lbl.setVisible(True)
            return
        self.music_empty_lbl.setVisible(False)
        mm_ids = self.db.list_my_music_ids()
        for item in items:
            row = self._create_music_row(item, show_enqueue=True, show_remove=False, mm_ids=mm_ids)
            self._music_rows.append(row)
            self.music_results_lay.insertWidget(self.music_results_lay.count() - 1, row)

    def _show_video_feed_view(self) -> None:
        self._video_showing_search = False
        self.video_view_title.setText("Inicio")
        if self.video_back_feed_btn is not None:
            self.video_back_feed_btn.setVisible(False)
        if self._video_feed_items:
            self._render_video_cards(self._video_feed_items)
        else:
            self._load_video_feed()

    def _load_video_feed(self, refresh: bool = False) -> None:
        if self._cleaned_up:
            return
        prev_items = list(self._video_feed_items)
        if refresh and prev_items:
            for prev_it in prev_items:
                if prev_it.video_id:
                    self._video_feed_shown_ids.append(prev_it.video_id)
        self._video_showing_search = False
        self._video_feed_items = []
        self._video_feed_loading_more = False
        self._video_feed_exhausted = False
        self._video_page_dirty = True
        self.video_view_title.setText("Inicio")
        if self.video_back_feed_btn is not None:
            self.video_back_feed_btn.setVisible(False)
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setEnabled(False)
        self.video_empty_lbl.setText("Cargando videos...")
        self.video_empty_lbl.setVisible(True)
        self.video_grid.clear_widgets()
        self._video_cards.clear()
        if refresh:
            self.thumb_cache.clear()
            QPixmapCache.clear()
            trim_process_memory(collect_gc=True)
        self._set_page_loading(self.video_search_page, True)

        self._video_feed_gen += 1
        gen = self._video_feed_gen
        fallback_q = self.db.get_search_history("video")
        watched_ids = self.db.list_watched_video_ids()
        recent_ids = set(self._video_feed_shown_ids) if refresh else None
        self._run_async(
            lambda: self.yt.get_video_home_feed(
                limit=24,
                fallback_queries=fallback_q,
                watched_ids=watched_ids,
                recent_shown_ids=recent_ids,
                refresh=refresh,
            ),
            on_done=lambda items: self._on_video_feed_done(gen, items),
            on_error=lambda err: self._on_video_feed_error(gen, err),
        )

    def _on_video_feed_done(self, gen: int, items: list[MediaItem]) -> None:
        if gen != self._video_feed_gen:
            return
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setEnabled(True)
        self._set_page_loading(self.video_search_page, False)
        self._video_feed_loading_more = False
        self._video_feed_items = list(items[: self.MAX_FEED_ITEMS])
        self._video_page_dirty = True
        if self._video_showing_search or self.stack.currentIndex() != self.PAGE_VIDEO_SEARCH:
            return
        self._render_video_cards(
            self._video_feed_items,
            empty_text="No se pudo cargar el feed ahora. Prueba buscar arriba o inicia sesión en Ajustes.",
        )
        QTimer.singleShot(800, trim_process_memory)

    def _on_video_feed_error(self, gen: int, err: str) -> None:
        if gen != self._video_feed_gen:
            return
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setEnabled(True)
        self._video_feed_loading_more = False
        self._set_page_loading(self.video_search_page, False)
        if not self._video_showing_search:
            self.video_empty_lbl.setText(f"No se pudo cargar el feed: {err}")
            self.video_empty_lbl.setVisible(True)

    def _show_music_home_view(self) -> None:
        self._music_showing_search = False
        self.music_view_title.setText("Música")
        if self.music_back_home_btn is not None:
            self.music_back_home_btn.setVisible(False)
        if self._music_home_loaded and self._music_home_sections:
            self._render_music_home_sections(self._music_home_sections)
        else:
            self._load_music_home()

    def _load_music_home(self, refresh: bool = False) -> None:
        self._music_showing_search = False
        self._music_home_sections = []
        self._music_home_loading_more = False
        self._music_home_exhausted = False
        self._music_search_page_dirty = True
        self.music_view_title.setText("Música")
        if self.music_back_home_btn is not None:
            self.music_back_home_btn.setVisible(False)
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setEnabled(False)
        self.music_empty_lbl.setText("Cargando música...")
        self.music_empty_lbl.setVisible(True)
        self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
        self._music_rows.clear()
        self._music_tiles.clear()
        self._music_shelves.clear()
        if refresh:
            self.thumb_cache.clear()
            QPixmapCache.clear()
            trim_process_memory(collect_gc=True)
        self._set_page_loading(self.music_search_page, True)

        self._music_home_gen += 1
        gen = self._music_home_gen
        local_recent = self.db.list_listen_again(kind="music", limit=12)
        self._run_async(
            lambda: self.yt.get_music_home_sections(local_listen_again=local_recent),
            on_done=lambda secs: self._on_music_home_done(gen, secs),
            on_error=lambda err: self._on_music_home_error(gen, err),
        )

    def _on_music_home_done(self, gen: int, sections: list[FeedSection]) -> None:
        if gen != self._music_home_gen:
            return
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setEnabled(True)
        self._set_page_loading(self.music_search_page, False)
        self._music_home_loading_more = False
        self._music_home_loaded = True
        self._music_home_sections = list(sections)
        self._music_search_page_dirty = True
        if self._music_showing_search or self.stack.currentIndex() != self.PAGE_MUSIC_SEARCH:
            return
        self._render_music_home_sections(
            self._music_home_sections,
            empty_text="Escucha una canción o inicia sesión con Google para ver tus recomendaciones aquí.",
        )
        QTimer.singleShot(800, trim_process_memory)

    def _on_music_home_error(self, gen: int, err: str) -> None:
        if gen != self._music_home_gen:
            return
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setEnabled(True)
        self._music_home_loading_more = False
        self._set_page_loading(self.music_search_page, False)
        if not self._music_showing_search:
            self.music_empty_lbl.setText(f"No se pudo cargar inicio musical: {err}")
            self.music_empty_lbl.setVisible(True)

    def _on_music_scroll_changed(self, value: int) -> None:
        if (
            self._music_showing_search
            or self._music_home_loading_more
            or self._music_home_exhausted
            or not self._music_home_loaded
            or self.stack.currentIndex() != self.PAGE_MUSIC_SEARCH
            or len(self._music_tiles) >= self.MAX_FEED_ITEMS
        ):
            return
        sb = self.music_scroll.verticalScrollBar()
        if sb is None or sb.maximum() <= 0:
            return
        if value >= int(sb.maximum() * 0.80):
            self._load_more_music_home()

    def _load_more_music_home(self) -> None:
        if (
            self._cleaned_up
            or self._music_home_loading_more
            or self._music_home_exhausted
            or self._music_showing_search
            or len(self._music_tiles) >= self.MAX_FEED_ITEMS
        ):
            return
        self._music_home_loading_more = True
        self._set_page_loading(self.music_search_page, True)
        gen = self._music_home_gen
        ex_titles = {s.title for s in self._music_home_sections if s.title}
        ex_ids = {it.video_id for s in self._music_home_sections for it in s.items if it.video_id}
        local_recent = self.db.list_listen_again(kind="music", limit=12)
        self._run_async(
            lambda: self.yt.get_music_home_more_sections(
                local_recent, exclude_titles=ex_titles, exclude_ids=ex_ids, batch_size=3
            ),
            on_done=lambda secs: self._on_music_home_more_done(gen, secs),
            on_error=lambda _err: self._on_music_home_more_error(gen),
        )

    def _on_music_home_more_done(self, gen: int, sections: list[FeedSection]) -> None:
        if gen != self._music_home_gen:
            return
        self._music_home_loading_more = False
        self._set_page_loading(self.music_search_page, False)
        if not sections:
            self._music_home_exhausted = True
            return
        cur_count = sum(len(s.items) for s in self._music_home_sections)
        added_secs: list[FeedSection] = []
        for sec in sections:
            if cur_count >= self.MAX_FEED_ITEMS:
                break
            take = sec.items[: max(0, self.MAX_FEED_ITEMS - cur_count)]
            if take:
                trimmed_sec = FeedSection(title=sec.title, items=take)
                self._music_home_sections.append(trimmed_sec)
                added_secs.append(trimmed_sec)
                cur_count += len(take)
        if not added_secs or cur_count >= self.MAX_FEED_ITEMS:
            if cur_count >= self.MAX_FEED_ITEMS:
                self._music_home_exhausted = True
        if self._music_showing_search or self.stack.currentIndex() != self.PAGE_MUSIC_SEARCH:
            self._music_search_page_dirty = True
            return
        mm_ids = self.db.list_my_music_ids()
        for sec in added_secs:
            tiles: list[MusicTileWidget] = []
            for item in sec.items:
                tile = self._create_music_tile(item, mm_ids=mm_ids)
                self._music_tiles.append(tile)
                tiles.append(tile)
            shelf = MusicShelfWidget(sec.title, tiles, self.palette)
            self._music_shelves.append(shelf)
            self.music_results_lay.insertWidget(self.music_results_lay.count() - 1, shelf)

    def _on_music_home_more_error(self, gen: int) -> None:
        if gen != self._music_home_gen:
            return
        self._music_home_loading_more = False
        self._set_page_loading(self.music_search_page, False)

    # ------------------------------------------------------------------
    # Vista de Canal de YouTube (Videos y Directos sin Shorts)
    # ------------------------------------------------------------------

    def _refresh_channel_tab_pills(self) -> None:
        if not hasattr(self, "channel_videos_btn"):
            return
        for btn, active in (
            (self.channel_videos_btn, self._channel_tab == "videos"),
            (self.channel_streams_btn, self._channel_tab == "streams"),
        ):
            btn.setProperty("active", "true" if active else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_channel_avatar_ready(self, url: str, pix: QPixmap) -> None:
        if url and url == self._channel_avatar_url and pix.size() == QSize(40, 40):
            self.channel_avatar_lbl.setText("")
            self.channel_avatar_lbl.setPixmap(pix)
            self.channel_avatar_lbl.setStyleSheet("background-color: transparent; border: none;")

    def _apply_channel_header(self, name: str, avatar_url: str, sub_info: str = "") -> None:
        self.channel_title_lbl.setText(name or "Canal de YouTube")
        if sub_info:
            self.channel_meta_lbl.setText(sub_info)
            self.channel_meta_lbl.setVisible(True)
        else:
            self.channel_meta_lbl.setVisible(False)
        self._channel_avatar_url = avatar_url or ""
        if self._channel_avatar_url:
            pix = self.thumb_cache.get_or_fetch(self._channel_avatar_url, QSize(40, 40), circular=True)
            if pix is not None and not pix.isNull():
                self.channel_avatar_lbl.setText("")
                self.channel_avatar_lbl.setPixmap(pix)
                self.channel_avatar_lbl.setStyleSheet("background-color: transparent; border: none;")
                return
        self.channel_avatar_lbl.clear()
        self.channel_avatar_lbl.setText((name or "Y").strip()[:1].upper())
        self.channel_avatar_lbl.setStyleSheet(
            f"background-color: {self.palette.surface2}; color: {self.palette.text2}; "
            f"border-radius: 20px; border: 1px solid {self.palette.border}; "
            "font-size: 16px; font-weight: 700;"
        )

    def _open_current_video_channel(self) -> None:
        if self._video_item is not None and self._video_item.kind == "video":
            self.open_youtube_channel(self._video_item)

    def open_youtube_channel(self, item: MediaItem) -> None:
        if self._cleaned_up or item.kind != "video":
            return
        cur = self.stack.currentIndex()
        if cur != self.PAGE_CHANNEL:
            self._channel_return_page = cur
        self._channel_tab = "videos"
        self._channel_info = None
        self._channel_id = item.channel_id.strip()
        self._channel_items = []
        self._channel_continuation = None
        self._channel_loading_more = False
        self._refresh_channel_tab_pills()
        self._apply_channel_header(item.author or "Canal de YouTube", item.channel_avatar_url)
        self._switch_page(self.PAGE_CHANNEL)
        self._fetch_channel_tab(refresh=False, seed_video_id=item.video_id)

    def _set_channel_tab(self, tab: Literal["videos", "streams"]) -> None:
        if self._channel_tab == tab:
            return
        self._channel_tab = tab
        self._refresh_channel_tab_pills()
        self._fetch_channel_tab(refresh=False)

    def _reload_channel_page(self) -> None:
        self._fetch_channel_tab(refresh=True)

    def _fetch_channel_tab(self, *, refresh: bool, seed_video_id: str = "") -> None:
        self._channel_gen += 1
        gen = self._channel_gen
        self._channel_items = []
        self._channel_continuation = None
        self._channel_loading_more = False
        self._channel_page_dirty = True
        self.channel_grid.clear_widgets()
        self._channel_cards.clear()
        if refresh:
            self.thumb_cache.clear()
            QPixmapCache.clear()
            trim_process_memory(collect_gc=True)
        self.channel_empty_lbl.setText(
            "Cargando directos del canal..." if self._channel_tab == "streams" else "Cargando videos del canal..."
        )
        self.channel_empty_lbl.setVisible(True)
        self._set_page_loading(self.channel_page, True)

        cid = self._channel_id
        tab = self._channel_tab
        tab_params = None
        if self._channel_info is not None:
            tab_params = (
                self._channel_info.streams_params
                if tab == "streams"
                else self._channel_info.videos_params
            )
        fb_name = self.channel_title_lbl.text()
        fb_av = self._channel_avatar_url

        def _worker() -> tuple[ChannelInfo, list[MediaItem], str | None]:
            resolved_cid = cid
            if not resolved_cid and seed_video_id:
                resolved_cid = self.yt.resolve_channel_id_for_video(seed_video_id)
            if not resolved_cid:
                raise RuntimeError("No se pudo identificar el canal de este video.")
            return self.yt.get_channel_page(
                resolved_cid,
                tab=tab,
                limit=24,
                params=tab_params,
                fallback_name=fb_name,
                fallback_avatar=fb_av,
            )

        self._run_async(
            _worker,
            on_done=lambda res: self._on_channel_loaded(gen, res, append=False),
            on_error=lambda err: self._on_channel_error(gen, err, append=False),
        )

    def _on_channel_loaded(
        self,
        gen: int,
        res: tuple[ChannelInfo, list[MediaItem], str | None],
        *,
        append: bool,
    ) -> None:
        if gen != self._channel_gen:
            return
        self._set_page_loading(self.channel_page, False)
        self._channel_loading_more = False
        info, items, next_tok = res
        if not append:
            self._channel_id = info.channel_id
            self._channel_info = info
            meta_parts = [x for x in (info.handle, info.subscribers) if x]
            self._apply_channel_header(info.name, info.avatar_url, " · ".join(meta_parts))
            self._channel_items = list(items[: self.MAX_FEED_ITEMS])
            self._channel_continuation = next_tok if len(self._channel_items) < self.MAX_FEED_ITEMS else None
            self._channel_page_dirty = True
            if self.stack.currentIndex() == self.PAGE_CHANNEL:
                empty_msg = (
                    "Este canal no tiene directos recientes."
                    if self._channel_tab == "streams"
                    else "No se encontraron videos en este canal."
                )
                self._render_channel_cards(self._channel_items, empty_text=empty_msg)
            return

        seen = {it.video_id for it in self._channel_items if it.video_id}
        cap_left = max(0, self.MAX_FEED_ITEMS - len(self._channel_items))
        new_items = [it for it in items if it.video_id and it.video_id not in seen][:cap_left]
        self._channel_continuation = next_tok if (new_items and len(self._channel_items) + len(new_items) < self.MAX_FEED_ITEMS) else None
        if not new_items:
            return
        self._channel_items.extend(new_items)
        if self.stack.currentIndex() != self.PAGE_CHANNEL:
            self._channel_page_dirty = True
            return
        wl_ids = self.db.list_watch_later_ids()
        new_cards: list[QWidget] = []
        for it in new_items:
            card = self._create_video_card(it, wl_ids=wl_ids)
            self._channel_cards.append(card)
            new_cards.append(card)
        hint_w = max(480, self.stack.width() - 52)
        self.channel_grid.append_widgets(new_cards, hint_width=hint_w)
        self._channel_viewport_timer.start()

    def _on_channel_error(self, gen: int, err: str, *, append: bool) -> None:
        if gen != self._channel_gen:
            return
        self._set_page_loading(self.channel_page, False)
        self._channel_loading_more = False
        if not append:
            self.channel_empty_lbl.setText(f"No se pudo cargar el canal: {err}")
            self.channel_empty_lbl.setVisible(True)

    def _render_channel_cards(
        self, items: list[MediaItem], empty_text: str = "", hint_width: int = 0
    ) -> None:
        self.channel_grid.clear_widgets()
        self._channel_cards.clear()
        self._channel_page_dirty = False
        if not items:
            if empty_text:
                self.channel_empty_lbl.setText(empty_text)
            self.channel_empty_lbl.setVisible(True)
            return
        self.channel_empty_lbl.setVisible(False)
        wl_ids = self.db.list_watch_later_ids()
        cards: list[QWidget] = []
        for it in items:
            card = self._create_video_card(it, wl_ids=wl_ids)
            self._channel_cards.append(card)
            cards.append(card)
        hint_w = hint_width if hint_width > 0 else max(480, self.stack.width() - 52)
        self.channel_grid.set_items_widgets(cards, hint_width=hint_w)
        self.channel_grid.setProperty("_rendered_hint_w", int(hint_w))
        self._channel_viewport_timer.start()

    def _on_channel_scroll_changed(self, value: int) -> None:
        self._channel_viewport_timer.start()
        if (
            self._channel_loading_more
            or not self._channel_continuation
            or not self._channel_id
            or self.stack.currentIndex() != self.PAGE_CHANNEL
            or len(self._channel_items) >= self.MAX_FEED_ITEMS
        ):
            return
        sb = self.channel_scroll.verticalScrollBar()
        if sb is None or sb.maximum() <= 0:
            return
        if value >= int(sb.maximum() * 0.80):
            self._channel_loading_more = True
            self._set_page_loading(self.channel_page, True)
            gen = self._channel_gen
            cid = self._channel_id
            tab = self._channel_tab
            tok = self._channel_continuation
            fb_name = self.channel_title_lbl.text()
            fb_av = self._channel_avatar_url
            rem = min(24, self.MAX_FEED_ITEMS - len(self._channel_items))
            self._run_async(
                lambda: self.yt.get_channel_page(
                    cid,
                    tab=tab,
                    continuation=tok,
                    limit=rem,
                    fallback_name=fb_name,
                    fallback_avatar=fb_av,
                ),
                on_done=lambda res: self._on_channel_loaded(gen, res, append=True),
                on_error=lambda err: self._on_channel_error(gen, err, append=True),
            )

    def _on_channel_back(self) -> None:
        target = getattr(self, "_channel_return_page", self.PAGE_VIDEO_SEARCH)
        if not isinstance(target, int) or target < 0 or target >= self.stack.count() or target == self.PAGE_CHANNEL:
            target = self.PAGE_VIDEO_SEARCH
        self._switch_page(target)

    # ------------------------------------------------------------------
    # Cuenta e ícono superior de perfil
    # ------------------------------------------------------------------

    def _refresh_account_badge(self) -> None:
        if has_saved_session():
            prof = load_account_profile()
            if prof and prof.name:
                first_name = prof.name.split()[0]
                self.account_btn.setText(f" {first_name}")
                tip = f"Cuenta: {prof.name}" + (f" ({prof.handle})" if prof.handle else "")
                self.account_btn.setToolTip(f"{tip} — Clic para gestionar")
            else:
                self.account_btn.setText(" Cuenta")
                self.account_btn.setToolTip("Sesión activa — Clic para gestionar")
            self.account_btn.setProperty("active", "true")

            self._account_avatar_url = prof.avatar_url if prof else ""
            if self._account_avatar_url:
                pix = self.thumb_cache.get_or_fetch(
                    self._account_avatar_url, QSize(22, 22), circular=True
                )
                if pix is not None:
                    self.account_btn.setIcon(QIcon(pix))
                else:
                    self.account_btn.setIcon(render_icon("user", self.palette.text, 16))
            else:
                self.account_btn.setIcon(render_icon("user", self.palette.text, 16))
        else:
            self._account_avatar_url = ""
            self.account_btn.setText(" Acceder")
            self.account_btn.setToolTip("Iniciar sesión con Google (DPAPI)")
            self.account_btn.setProperty("active", "false")
            self.account_btn.setIcon(render_icon("user", self.palette.text, 16))

        self.account_btn.style().unpolish(self.account_btn)
        self.account_btn.style().polish(self.account_btn)

    def _on_account_avatar_ready(self, url: str, pix: QPixmap) -> None:
        if self._account_avatar_url and url == self._account_avatar_url and pix.size() == QSize(22, 22):
            self.account_btn.setIcon(QIcon(pix))
        if (
            self._discord_bot_avatar_url
            and url == self._discord_bot_avatar_url
            and pix.size() == QSize(22, 22)
        ):
            self.discord_mini_avatar.setText("")
            self.discord_mini_avatar.setPixmap(pix)
            self.discord_mini_avatar.setStyleSheet("background-color: transparent; border: none;")

    def _refresh_account_async_if_needed(self) -> None:
        if has_saved_session() and load_account_profile() is None:
            self._run_async(
                self.yt.fetch_account_profile,
                on_done=lambda _prof: self._refresh_account_badge(),
                on_error=lambda _err: None,
            )

    def _on_account_changed(self) -> None:
        self._refresh_account_badge()
        self._music_home_loaded = False
        if not self._video_showing_search:
            self._load_video_feed()
        if self.stack.currentIndex() == self.PAGE_MUSIC_SEARCH and not self._music_showing_search:
            self._load_music_home()

    def _on_video_search_done(self, gen: int, query: str, items: list[MediaItem]) -> None:
        if gen != self._search_gen:
            return
        self._set_page_loading(self.video_search_page, False)
        self._video_results = list(items)
        self._video_page_dirty = True
        if not self._video_showing_search or self.stack.currentIndex() != self.PAGE_VIDEO_SEARCH:
            return
        self._render_video_cards(
            self._video_results,
            empty_text=f"No se encontraron videos para '{query}'.",
        )

    def _on_music_search_done(self, gen: int, query: str, items: list[MediaItem]) -> None:
        if gen != self._search_gen:
            return
        self._set_page_loading(self.music_search_page, False)
        self._music_results = list(items)
        self._music_search_page_dirty = True
        if not self._music_showing_search or self.stack.currentIndex() != self.PAGE_MUSIC_SEARCH:
            return
        self._render_music_search_rows(
            self._music_results,
            empty_text=f"No se encontraron canciones para '{query}'.",
        )

    def _on_search_error(self, gen: int, mode: str, err: str) -> None:
        if gen != self._search_gen:
            return
        if mode == "video":
            self._set_page_loading(self.video_search_page, False)
            self.video_empty_lbl.setText(f"Error al buscar: {err}")
            self.video_empty_lbl.setVisible(True)
        elif mode == "twitch":
            self._set_page_loading(self.twitch_page, False)
            self.twitch_empty_lbl.setText(f"Error al buscar en Twitch: {err}")
            self.twitch_empty_lbl.setVisible(True)
        else:
            self._set_page_loading(self.music_search_page, False)
            self.music_empty_lbl.setText(f"Error al buscar: {err}")
            self.music_empty_lbl.setVisible(True)

    # ------------------------------------------------------------------
    # Twitch (Solo invitados, directos sin chat)
    # ------------------------------------------------------------------

    def _refresh_twitch_tab_pills(self) -> None:
        fav_n = self.db.count_twitch_favorites()
        self.twitch_fav_btn.setText(f"Favoritos ({fav_n})" if fav_n else "Favoritos")
        mapping = {
            "ES": self.twitch_es_btn,
            "ALL": self.twitch_all_btn,
            "FAV": self.twitch_fav_btn,
        }
        active_pill = mapping.get(self._twitch_tab, self.twitch_es_btn)
        for btn in (self.twitch_es_btn, self.twitch_all_btn, self.twitch_fav_btn):
            btn.setProperty("active", "true" if btn is active_pill else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _set_twitch_tab(self, tab: Literal["ES", "ALL", "FAV"]) -> None:
        self._twitch_tab = tab
        self._twitch_showing_search = False
        self._twitch_results = []
        self._twitch_page_dirty = True
        self.twitch_view_title.setText("Twitch")
        if self.twitch_back_btn is not None:
            self.twitch_back_btn.setVisible(False)
        self._refresh_twitch_tab_pills()
        cached = self._twitch_feed_cache.get(tab)
        if cached is not None and (time.monotonic() - cached[0]) < 90.0:
            self._render_twitch_cards(cached[1])
            return
        self._load_twitch_view()

    def _show_twitch_feed_view(self) -> None:
        self._twitch_showing_search = False
        self._twitch_results = []
        self._twitch_page_dirty = True
        self.twitch_view_title.setText("Twitch")
        if self.twitch_back_btn is not None:
            self.twitch_back_btn.setVisible(False)
        cached = self._twitch_feed_cache.get(self._twitch_tab)
        if cached is not None and (time.monotonic() - cached[0]) < 90.0:
            self._render_twitch_cards(cached[1])
            return
        self._load_twitch_view()

    def _reload_twitch_current_view(self) -> None:
        if self._twitch_showing_search and self.search_input.text().strip():
            self._search_mode = "twitch"
            self._on_search_submitted()
        else:
            self._twitch_feed_cache.pop(self._twitch_tab, None)
            self._twitch_page_dirty = True
            self._load_twitch_view()

    def _load_twitch_view(self) -> None:
        self._twitch_showing_search = False
        self._twitch_page_dirty = True
        self.twitch_view_title.setText("Twitch")
        if self.twitch_back_btn is not None:
            self.twitch_back_btn.setVisible(False)
        if self.twitch_refresh_btn is not None:
            self.twitch_refresh_btn.setEnabled(False)
        self.twitch_grid.clear_widgets()
        self._twitch_cards.clear()

        tab = self._twitch_tab
        self._twitch_gen += 1
        gen = self._twitch_gen

        if tab == "FAV":
            favs = self.db.list_twitch_favorites()
            if not favs:
                if self.twitch_refresh_btn is not None:
                    self.twitch_refresh_btn.setEnabled(True)
                self._set_page_loading(self.twitch_page, False)
                self.twitch_empty_lbl.setText(
                    "Aún no tienes canales favoritos. Usa el menú ⋮ o el corazón en un directo para guardarlo."
                )
                self.twitch_empty_lbl.setVisible(True)
                self._twitch_feed_cache["FAV"] = (time.monotonic(), [])
                self._twitch_page_dirty = False
                return
            self._set_page_loading(self.twitch_page, True)
            self.twitch_empty_lbl.setText("Consultando canales favoritos en vivo...")
            self.twitch_empty_lbl.setVisible(True)
            logins = [it.video_id for it in favs]
            self._run_async(
                lambda: self.twitch.get_channels_status(logins),
                on_done=lambda items, t=tab: self._on_twitch_items_done(gen, items, t),
                on_error=lambda err: self._on_twitch_error(gen, err),
            )
            return

        self._set_page_loading(self.twitch_page, True)
        self.twitch_empty_lbl.setText("Cargando directos de Twitch...")
        self.twitch_empty_lbl.setVisible(True)
        self._run_async(
            lambda: self.twitch.get_top_streams(limit=18, language=tab),
            on_done=lambda items, t=tab: self._on_twitch_items_done(gen, items, t),
            on_error=lambda err: self._on_twitch_error(gen, err),
        )

    def _render_twitch_cards(
        self, items: list[MediaItem], hint_width: int = 0
    ) -> None:
        self.twitch_grid.clear_widgets()
        self._twitch_cards.clear()
        self._twitch_page_dirty = False
        if not items:
            self.twitch_empty_lbl.setText("No se encontraron canales en Twitch.")
            self.twitch_empty_lbl.setVisible(True)
            return
        self.twitch_empty_lbl.setVisible(False)
        fav_ids = self.db.list_twitch_favorite_ids()
        cards: list[QWidget] = []
        for item in items:
            card = self._create_twitch_card(item, fav_ids=fav_ids)
            self._twitch_cards.append(card)
            cards.append(card)
        hint_w = hint_width if hint_width > 0 else max(480, self.stack.width() - 52)
        self.twitch_grid.set_items_widgets(cards, hint_width=hint_w)
        self.twitch_grid.setProperty("_rendered_hint_w", int(hint_w))

    def _on_twitch_items_done(
        self, gen: int, items: list[MediaItem], tab: str | None = None
    ) -> None:
        if gen != self._twitch_gen:
            return
        if self.twitch_refresh_btn is not None:
            self.twitch_refresh_btn.setEnabled(True)
        self._set_page_loading(self.twitch_page, False)
        self._twitch_loaded = True
        cache_tab = tab or self._twitch_tab
        self._twitch_feed_cache[cache_tab] = (time.monotonic(), list(items))
        self._twitch_page_dirty = True
        if self._twitch_showing_search or self.stack.currentIndex() != self.PAGE_TWITCH:
            return
        self._render_twitch_cards(items)
        QTimer.singleShot(800, trim_process_memory)

    def _on_twitch_error(self, gen: int, err: str) -> None:
        if gen != self._twitch_gen:
            return
        if self.twitch_refresh_btn is not None:
            self.twitch_refresh_btn.setEnabled(True)
        self._set_page_loading(self.twitch_page, False)
        if not self._twitch_showing_search:
            self.twitch_empty_lbl.setText(f"No se pudo cargar Twitch: {err}")
            self.twitch_empty_lbl.setVisible(True)

    def _on_twitch_search_done(self, gen: int, query: str, items: list[MediaItem]) -> None:
        if gen != self._search_gen:
            return
        self._set_page_loading(self.twitch_page, False)
        self._twitch_results = items
        self._twitch_page_dirty = True
        if self.stack.currentIndex() != self.PAGE_TWITCH:
            return
        if not items:
            self.twitch_grid.clear_widgets()
            self._twitch_cards.clear()
            self.twitch_empty_lbl.setText(f"No se encontraron canales para '{query}'.")
            self.twitch_empty_lbl.setVisible(True)
            self._twitch_page_dirty = False
            return
        self._render_twitch_cards(items)

    def _create_twitch_card(
        self, item: MediaItem, fav_ids: set[str] | None = None
    ) -> VideoGridCard:
        login_key = (item.video_id or "").strip().lower()
        is_fav = (
            login_key in fav_ids
            if fav_ids is not None
            else self.db.is_twitch_favorite(item.video_id)
        )
        card = VideoGridCard(
            item=item,
            in_watch_later=is_fav,
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            show_remove=False,
        )
        card.play_requested.connect(self.play_twitch_stream)
        card.watch_later_toggled.connect(self._toggle_twitch_favorite)
        return card

    def _toggle_twitch_favorite(self, item: MediaItem) -> None:
        login = (item.video_id or "").strip().lower()
        if not login:
            return
        if self.db.is_twitch_favorite(login):
            self.db.remove_twitch_favorite(login)
            saved = False
            self.show_toast("Quitado de Favoritos de Twitch")
        else:
            self.db.add_twitch_favorite(item)
            saved = True
            self.show_toast("Guardado en Favoritos de Twitch")

        self._twitch_feed_cache.pop("FAV", None)
        for card in self._twitch_cards:
            if card.item.video_id == login:
                card.set_watch_later(saved)
        if self._video_item and self._video_item.kind == "twitch" and self._video_item.video_id == login:
            self.video_panel.set_watch_later(saved)
            self.fs_overlay.set_watch_later(saved)
            self._refresh_bb_fav_icon()
        if self._twitch_tab == "FAV" and not self._twitch_showing_search:
            if self.stack.currentIndex() == self.PAGE_TWITCH:
                self._load_twitch_view()
            else:
                self._twitch_page_dirty = True
        self._refresh_badges()

    # ------------------------------------------------------------------
    # Tarjetas y Listas Locales
    # ------------------------------------------------------------------

    def _create_video_card(
        self,
        item: MediaItem,
        show_remove: bool = False,
        wl_ids: set[str] | None = None,
        force_wl: bool | None = None,
    ) -> VideoGridCard:
        if force_wl is not None:
            in_wl = force_wl
        elif wl_ids is not None:
            in_wl = item.video_id in wl_ids
        else:
            in_wl = self.db.is_in_watch_later(item.video_id)
        card = VideoGridCard(
            item=item,
            in_watch_later=in_wl,
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            show_remove=show_remove,
        )
        card.play_requested.connect(self.play_video)
        card.channel_clicked.connect(self.open_youtube_channel)
        card.watch_later_toggled.connect(self._toggle_watch_later_item)
        card.download_requested.connect(lambda it: self._download_item(it, "video"))
        if show_remove:
            card.remove_requested.connect(self._remove_history_item)
        return card

    def _finish_prefetch(self) -> None:
        self._prefetch_inflight = max(0, self._prefetch_inflight - 1)

    def _create_music_tile(
        self, item: MediaItem, mm_ids: set[str] | None = None
    ) -> MusicTileWidget:
        in_mm = (
            item.video_id in mm_ids
            if mm_ids is not None
            else self.db.is_in_my_music(item.video_id)
        )
        tile = MusicTileWidget(
            item=item,
            in_my_music=in_mm,
            thumb_cache=self.thumb_cache,
            palette=self.palette,
        )
        tile.play_requested.connect(self._play_music_from_search)
        tile.my_music_toggled.connect(self._toggle_my_music_item)
        tile.enqueue_requested.connect(self._enqueue_music_item)
        tile.download_requested.connect(lambda it: self._download_item(it, "music"))
        return tile

    def _create_music_row(
        self,
        item: MediaItem,
        show_enqueue: bool = True,
        show_remove: bool = False,
        on_play: Callable[[MediaItem], None] | None = None,
        on_remove: Callable[[MediaItem], None] | None = None,
        mm_ids: set[str] | None = None,
        force_mm: bool | None = None,
    ) -> MusicRowWidget:
        is_cur = self._music_item is not None and self._music_item.video_id == item.video_id
        if force_mm is not None:
            in_mm = force_mm
        elif mm_ids is not None:
            in_mm = item.video_id in mm_ids
        else:
            in_mm = self.db.is_in_my_music(item.video_id)
        row = MusicRowWidget(
            item=item,
            in_my_music=in_mm,
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            show_enqueue=show_enqueue,
            show_remove=show_remove,
            is_current=is_cur,
        )
        row.play_requested.connect(on_play or self._play_music_from_search)
        row.my_music_toggled.connect(self._toggle_my_music_item)
        row.enqueue_requested.connect(self._enqueue_music_item)
        row.download_requested.connect(lambda it: self._download_item(it, "music"))
        if show_remove:
            row.remove_requested.connect(on_remove or self._remove_from_queue)
        return row

    def _clear_layout_widgets(self, lay: QVBoxLayout, keep_widget: QWidget) -> None:
        for i in reversed(range(lay.count())):
            item = lay.itemAt(i)
            w = item.widget() if item else None
            if w is not None and w is not keep_widget:
                rel = getattr(w, "release_resources", None)
                if callable(rel):
                    rel()
                w.hide()
                lay.removeWidget(w)
                w.deleteLater()

    def _render_watch_later(self, hint_width: int = 0) -> None:
        self.wl_grid.clear_widgets()
        items = self.db.list_watch_later()
        self.wl_empty_lbl.setVisible(len(items) == 0)
        self._wl_page_dirty = False
        if items:
            hint_w = hint_width if hint_width > 0 else max(480, self.stack.width() - 52)
            self.wl_grid.set_items_widgets(
                [self._create_video_card(it, force_wl=True) for it in items],
                hint_width=hint_w,
            )
            self.wl_grid.setProperty("_rendered_hint_w", int(hint_w))

    def _render_history(self, hint_width: int = 0) -> None:
        self.history_grid.clear_widgets()
        items = self.db.list_video_history(limit=50)
        self.history_empty_lbl.setVisible(len(items) == 0)
        self._history_page_dirty = False
        if items:
            wl_ids = self.db.list_watch_later_ids()
            hint_w = hint_width if hint_width > 0 else max(480, self.stack.width() - 52)
            self.history_grid.set_items_widgets(
                [self._create_video_card(it, show_remove=True, wl_ids=wl_ids) for it in items],
                hint_width=hint_w,
            )
            self.history_grid.setProperty("_rendered_hint_w", int(hint_w))

    def _remove_history_item(self, item: MediaItem) -> None:
        self.db.remove_from_history(item.video_id)
        if self._video_item and self._video_item.video_id == item.video_id:
            self._last_saved_video_pos = 0.0
        self.show_toast("Quitado del historial")
        self._history_page_dirty = True
        if self.stack.currentIndex() == self.PAGE_HISTORY:
            self._render_history()

    def _clear_video_history(self) -> None:
        self.db.clear_video_history()
        self._last_saved_video_pos = 0.0
        self.show_toast("Historial local limpiado")
        self._history_page_dirty = True
        if self.stack.currentIndex() == self.PAGE_HISTORY:
            self._render_history()

    def _render_music_history(self) -> None:
        self._clear_layout_widgets(
            self.music_history_lay, keep_widget=self.music_history_empty_lbl
        )
        items = self.db.list_music_history(limit=80)
        self.music_history_empty_lbl.setVisible(len(items) == 0)
        self._music_history_page_dirty = False
        if not items:
            return
        mm_ids = self.db.list_my_music_ids()
        for idx, item in enumerate(items):
            row = self._create_music_row(
                item,
                show_enqueue=True,
                show_remove=True,
                on_play=lambda _it, i=idx, lst=items: self._play_list_from(lst, i),
                on_remove=self._remove_music_history_item,
                mm_ids=mm_ids,
            )
            self.music_history_lay.insertWidget(self.music_history_lay.count() - 1, row)

    def _remove_music_history_item(self, item: MediaItem) -> None:
        self.db.remove_from_history(item.video_id)
        self.show_toast("Quitado del historial")
        self._music_history_page_dirty = True
        if self.stack.currentIndex() == self.PAGE_MUSIC_HISTORY:
            self._render_music_history()

    def _clear_music_history(self) -> None:
        self.db.clear_music_history()
        self.show_toast("Historial de música limpiado")
        self._music_history_page_dirty = True
        if self.stack.currentIndex() == self.PAGE_MUSIC_HISTORY:
            self._render_music_history()

    def _play_all_music_history(self) -> None:
        items = self.db.list_music_history(limit=80)
        if not items:
            self.show_toast("Tu historial de música está vacío")
            return
        self._play_list_from(items, 0)

    def _render_my_music(self) -> None:
        self._clear_layout_widgets(self.my_music_lay, keep_widget=self.my_music_empty_lbl)
        items = self.db.list_my_music()
        self.my_music_empty_lbl.setVisible(len(items) == 0)
        self._my_music_page_dirty = False
        for idx, item in enumerate(items):
            row = self._create_music_row(
                item,
                show_enqueue=True,
                show_remove=False,
                on_play=lambda _it, i=idx, lst=items: self._play_list_from(lst, i),
                force_mm=True,
            )
            self.my_music_lay.insertWidget(self.my_music_lay.count() - 1, row)

    def _render_queue(self) -> None:
        self.music_panel.render_queue(
            self._music_queue,
            self._queue_index,
            self.db.is_in_my_music,
        )

    def _refresh_badges(self) -> None:
        wl_n = self.db.count_watch_later()
        mm_n = self.db.count_my_music()
        self.nav_wl.setText(f"  Ver más tarde ({wl_n})" if wl_n else "  Ver más tarde")
        self.nav_mymusic.setText(f"  Mi música ({mm_n})" if mm_n else "  Mi música")
        if hasattr(self, "twitch_fav_btn"):
            self._refresh_twitch_tab_pills()

    def _toggle_watch_later_item(self, item: MediaItem) -> None:
        if item.kind == "twitch":
            self._toggle_twitch_favorite(item)
            if self._video_item is not None and self._video_item.video_id == item.video_id:
                fav = self.db.is_twitch_favorite(item.video_id)
                self.video_panel.set_watch_later(fav)
                self.fs_overlay.set_watch_later(fav)
            self._refresh_bb_fav_icon()
            return
        if self.db.is_in_watch_later(item.video_id):
            self.db.remove_watch_later(item.video_id)
            saved = False
            self.show_toast("Quitado de Ver más tarde")
        else:
            self.db.add_watch_later(item)
            saved = True
            self.show_toast("Guardado en Ver más tarde")

        for card in self._video_cards:
            if card.item.video_id == item.video_id:
                card.set_watch_later(saved)
        for card in self.history_grid._widgets:
            if getattr(card, "item", None) is not None and card.item.video_id == item.video_id:
                card.set_watch_later(saved)
        if self._video_item and self._video_item.video_id == item.video_id:
            self.video_panel.set_watch_later(saved)
            self.fs_overlay.set_watch_later(saved)
            self._refresh_bb_fav_icon()
        self._wl_page_dirty = True
        if self.stack.currentIndex() == self.PAGE_WATCH_LATER:
            self._render_watch_later()
        elif self.stack.currentIndex() == self.PAGE_HISTORY:
            self._render_history()
        self._refresh_badges()

    def _toggle_my_music_item(self, item: MediaItem) -> None:
        if self.db.is_in_my_music(item.video_id):
            self.db.remove_my_music(item.video_id)
            saved = False
            self.show_toast("Quitado de Mi música")
        else:
            self.db.add_my_music(item)
            saved = True
            self.show_toast("Guardado en Mi música")

        for row in self._music_rows:
            if row.item.video_id == item.video_id:
                row.set_my_music(saved)
        for tile in self._music_tiles:
            if tile.item.video_id == item.video_id:
                tile.set_my_music(saved)
        self.music_panel.refresh_saved_state(item.video_id, saved)
        if self._music_item and self._music_item.video_id == item.video_id:
            self._refresh_bb_fav_icon()
        self._my_music_page_dirty = True
        self._music_history_page_dirty = True
        if self.stack.currentIndex() == self.PAGE_MY_MUSIC:
            self._render_my_music()
        elif self.stack.currentIndex() == self.PAGE_MUSIC_HISTORY:
            self._render_music_history()
        self._refresh_badges()

    def _toggle_current_video_saved(self) -> None:
        if self._video_item is not None:
            self._toggle_watch_later_item(self._video_item)

    # ------------------------------------------------------------------
    # Reproductor Dual Independiente (Video + Música)
    # ------------------------------------------------------------------

    def _pause_other_if_auto(self, starting_deck: Literal["video", "music"]) -> None:
        if not self.cfg.auto_pause_other_player:
            return
        if starting_deck == "video" and self._music_item is not None and not self._music_paused:
            self.music_player.set_pause(True)
        elif starting_deck == "music" and self._video_item is not None and not self._video_paused:
            self.video_player.set_pause(True)

    def _save_current_video_progress(self, force: bool = False) -> None:
        if (
            self._video_item is None
            or self._video_item.kind == "twitch"
            or self._video_item.is_live
        ):
            return
        if not force:
            if (
                abs(self._video_pos - self._last_saved_video_pos) < 5.0
                or (time.monotonic() - self._last_saved_video_ts) < 4.0
            ):
                return
        self.db.update_play_progress(
            self._video_item.video_id, self._video_pos, self._video_dur
        )
        self._last_saved_video_pos = self._video_pos
        self._last_saved_video_ts = time.monotonic()

    def play_video(
        self,
        item: MediaItem,
        start_pos: float | None = None,
        *,
        force_fresh: bool = False,
    ) -> None:
        self._video_click_timer.stop()
        self._video_idle_unload_timer.stop()
        self.continue_banner.setVisible(False)
        self._save_current_video_progress(force=True)
        self._pause_other_if_auto("video")

        # Cierre instantáneo del video anterior al cambiar a uno nuevo
        if self._video_item is not None and self.video_player.has_active_stream:
            self.video_player.stop()

        is_yt_live = bool(item.is_live)
        if is_yt_live:
            effective_start = 0.0
        elif start_pos is None:
            saved_pos = float(getattr(item, "resume_pos", 0.0) or 0.0)
            if saved_pos <= 0.0:
                saved_pos = self.db.get_play_position(item.video_id)
            effective_start = saved_pos if saved_pos >= 3.0 else 0.0
        else:
            effective_start = max(0.0, float(start_pos))

        same_item = self._video_item is item
        self._active_deck = "video"
        self._video_item = item
        self._video_live_start_ts = 0.0
        self._video_pos = effective_start
        self._video_dur = (
            0.0
            if is_yt_live
            else (
                max(self._video_dur, float(item.duration_sec or 0))
                if same_item
                else float(item.duration_sec or 0)
            )
        )
        self._last_saved_video_pos = effective_start
        self._last_saved_video_ts = time.monotonic()

        self.db.record_play(item)
        self._history_page_dirty = True
        self.persist_session_state(force=True)
        cur_page = self.stack.currentIndex()
        if cur_page in (self.PAGE_VIDEO_SEARCH, self.PAGE_WATCH_LATER, self.PAGE_HISTORY):
            self._video_return_page = cur_page
        self.video_player.set_speed(self.cfg.video_speed)
        self._switch_page(self.PAGE_VIDEO_PLAYER)

        wl_saved = self.db.is_in_watch_later(item.video_id)
        self.video_panel.set_live_display_offset(0.0)
        self.fs_overlay.set_live_display_offset(0.0)
        self.video_panel.set_live_mode(is_yt_live, is_twitch=False)
        self.video_panel.set_item(item, wl_saved)
        if is_yt_live:
            self.video_panel.set_duration(0.0)
            self.fs_overlay.set_duration(0.0)
            self.video_panel.seek_slider.blockSignals(True)
            self.video_panel.seek_slider.setValue(0)
            self.video_panel.seek_slider.blockSignals(False)
            self.video_panel.pos_lbl.setText("0:00")
        elif self._video_dur > 0:
            self.video_panel.set_duration(self._video_dur)
            self.fs_overlay.set_duration(self._video_dur)
            self.video_panel.set_position(effective_start)
            self.fs_overlay.set_position(effective_start)

        audio_only = self.cfg.video_audio_only
        quality = self.cfg.video_quality
        is_cached = (
            False
            if (force_fresh or is_yt_live)
            else self.yt.has_cached_stream(
                item.video_id, audio_only=audio_only, quality=quality
            )
        )
        self.video_panel.set_audio_only_visual(audio_only)
        self.video_panel.set_loading_state(not is_cached)
        self.video_panel.set_status(
            "Conectando al directo..." if is_yt_live else ("" if is_cached else "Cargando video...")
        )
        self.fs_overlay.set_live_mode(is_yt_live, is_twitch=False)
        self.fs_overlay.set_item(item, wl_saved)
        self.fs_overlay.set_audio_only_silent(audio_only)
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()
        self._sync_mini_bar()

        if not is_yt_live and start_pos is None and effective_start >= 3.0:
            res_lbl = format_duration(effective_start)
            if res_lbl:
                self.show_toast(f"Continuando desde {res_lbl}")

        self._video_play_gen += 1
        gen = self._video_play_gen
        self._run_async(
            lambda: self.yt.resolve_stream(
                item.video_id,
                audio_only=audio_only,
                quality=quality,
                use_cache=not (force_fresh or is_yt_live),
            ),
            on_done=lambda stream: self._on_video_stream_resolved(gen, stream, effective_start),
            on_error=lambda err: self._on_video_play_error(gen, err),
        )

    def _compute_live_display_offset(self, dur: float) -> float:
        if (
            self._video_item is None
            or not self._video_item.is_live
            or self._video_item.kind == "twitch"
            or self._video_live_start_ts <= 0.0
            or dur <= 0.0
        ):
            return 0.0
        real_elapsed = max(0.0, time.time() - self._video_live_start_ts)
        return max(0.0, real_elapsed - dur)

    def _refresh_item_metadata_in_lists(self, item: MediaItem) -> None:
        vid = item.video_id
        if not vid:
            return
        for pool in (self._video_feed_items, self._video_results):
            for other in pool:
                if other.video_id == vid:
                    other.title = item.title
                    other.author = item.author
                    other.thumb_url = item.thumb_url
                    other.is_live = item.is_live
                    other.duration = item.duration
                    other.duration_sec = item.duration_sec
        for card in self._video_cards:
            c_it = getattr(card, "item", None)
            if c_it is not None and c_it.video_id == vid:
                c_it.title = item.title
                c_it.author = item.author
                c_it.thumb_url = item.thumb_url
                c_it.is_live = item.is_live
                c_it.duration = item.duration
                c_it.duration_sec = item.duration_sec
                tlbl = getattr(card, "title_lbl", None)
                if tlbl is not None:
                    tlbl.setText(item.title)
                albl = getattr(card, "author_lbl", None)
                if albl is not None:
                    albl.setText(item.author or "YouTube")

    def _on_video_stream_resolved(
        self, gen: int, stream: ResolvedStream, start_pos: float
    ) -> None:
        if gen != self._video_play_gen:
            return
        self.video_panel.set_loading_state(False)
        self.video_panel.set_status("")
        is_yt_live = bool(
            stream.is_live or (self._video_item is not None and self._video_item.is_live)
        )
        self._video_live_start_ts = (
            float(getattr(stream, "live_start_ts", 0.0) or 0.0) if is_yt_live else 0.0
        )
        if self._video_item is not None and (
            not stream.video_id or stream.video_id == self._video_item.video_id
        ):
            changed = False
            if stream.title.strip() and stream.title.strip() != self._video_item.title:
                self._video_item.title = stream.title.strip()
                changed = True
            if stream.author.strip() and stream.author.strip() != self._video_item.author:
                self._video_item.author = stream.author.strip()
                changed = True
            if stream.thumb_url.strip() and stream.thumb_url.strip() != self._video_item.thumb_url:
                self._video_item.thumb_url = stream.thumb_url.strip()
                changed = True
            if is_yt_live:
                if (
                    not self._video_item.is_live
                    or self._video_item.duration != "EN VIVO"
                    or self._video_item.duration_sec != 0
                ):
                    changed = True
                self._video_item.is_live = True
                self._video_item.duration = "EN VIVO"
                self._video_item.duration_sec = 0
            if changed:
                self._refresh_item_metadata_in_lists(self._video_item)
                self.db.update_play_metadata(self._video_item)
                self._history_page_dirty = True
                wl_saved = self.db.is_in_watch_later(self._video_item.video_id)
                self.video_panel.set_item(self._video_item, wl_saved)
                self.fs_overlay.set_item(self._video_item, wl_saved)

        if is_yt_live:
            self._video_dur = 0.0
            self.video_panel.set_live_mode(True, is_twitch=False)
            self.fs_overlay.set_live_mode(True, is_twitch=False)
            off = self._compute_live_display_offset(stream.duration)
            self.video_panel.set_live_display_offset(off)
            self.fs_overlay.set_live_display_offset(off)
        else:
            self.video_panel.set_live_display_offset(0.0)
            self.fs_overlay.set_live_display_offset(0.0)
        if stream.duration > 0:
            self._on_video_dur_changed(stream.duration)
        self.video_player.play_stream(
            primary_url=stream.primary_url,
            audio_url=stream.audio_url,
            video_enabled=stream.video_enabled,
            http_headers=stream.http_headers,
            start_pos=0.0 if is_yt_live else start_pos,
            is_live=is_yt_live,
            allow_seek=is_yt_live,
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()
        QTimer.singleShot(4000, self._soft_trim_after_stream_start)

    def _on_video_play_error(self, gen: int, err: str) -> None:
        if gen != self._video_play_gen:
            return
        self.video_panel.set_loading_state(False)
        self.video_panel.set_status("Error")
        prefix = (
            "No se pudo abrir el directo"
            if self._video_item is not None and self._video_item.kind == "twitch"
            else "No se pudo reproducir el video"
        )
        self.show_toast(f"{prefix}: {err}")

    def play_twitch_stream(self, item: MediaItem) -> None:
        self._video_click_timer.stop()
        self._video_idle_unload_timer.stop()
        self.continue_banner.setVisible(False)
        self._save_current_video_progress(force=True)
        self._pause_other_if_auto("video")
        self._video_live_start_ts = 0.0

        if self._video_item is not None and self.video_player.has_active_stream:
            self.video_player.stop()

        item.kind = "twitch"
        item.is_live = True
        self._video_return_page = self.PAGE_TWITCH
        self._active_deck = "video"
        self._video_item = item
        self._video_pos = 0.0
        self._video_dur = 0.0
        self._last_saved_video_pos = 0.0
        self._last_saved_video_ts = time.monotonic()

        self.video_player.set_speed(1.0)
        self._switch_page(self.PAGE_VIDEO_PLAYER)

        fav = self.db.is_twitch_favorite(item.video_id)
        viewers_txt = format_viewers(item.viewer_count) if item.viewer_count > 0 else ""
        self.video_panel.set_item(item, fav)
        self.video_panel.set_live_mode(True, viewers_text=viewers_txt, is_twitch=True)
        self.fs_overlay.set_item(item, fav)
        self.fs_overlay.set_live_mode(True, viewers_text=viewers_txt, is_twitch=True)

        audio_only = self.cfg.video_audio_only
        quality = self.cfg.video_quality
        self.video_panel.set_audio_only_visual(audio_only)
        self.fs_overlay.set_audio_only_silent(audio_only)
        self.video_panel.set_loading_state(True)
        self.video_panel.set_status("Conectando al directo...")
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()
        self._sync_mini_bar()

        self._video_play_gen += 1
        gen = self._video_play_gen
        login = item.video_id
        self._run_async(
            lambda: self.twitch.resolve_live_stream(
                login, audio_only=audio_only, quality=quality
            ),
            on_done=lambda stream: self._on_twitch_stream_resolved(gen, stream),
            on_error=lambda err: self._on_video_play_error(gen, err),
        )

    def _on_twitch_stream_resolved(self, gen: int, stream: ResolvedStream) -> None:
        if gen != self._video_play_gen:
            return
        self.video_panel.set_loading_state(False)
        self.video_panel.set_status("")
        self.video_player.play_stream(
            primary_url=stream.primary_url,
            audio_url=None,
            video_enabled=stream.video_enabled,
            http_headers=stream.http_headers,
            start_pos=0.0,
            is_live=True,
            allow_seek=False,
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()
        QTimer.singleShot(4000, self._soft_trim_after_stream_start)

    def _reload_current_live_stream(self) -> None:
        if self._video_item is None:
            return
        if self._video_item.kind == "twitch":
            self.show_toast("Sincronizando directo con el momento real...")
            self.play_twitch_stream(self._video_item)
        elif self._video_item.is_live:
            self.yt.invalidate_stream_cache(self._video_item.video_id)
            self.show_toast("Sincronizando directo de YouTube al momento real...")
            self.play_video(self._video_item, start_pos=0.0, force_fresh=True)

    def _on_video_panel_back(self) -> None:
        target = getattr(self, "_video_return_page", self.PAGE_VIDEO_SEARCH)
        if (
            not isinstance(target, int)
            or target < 0
            or target >= self.stack.count()
            or target == self.PAGE_VIDEO_PLAYER
        ):
            target = (
                self.PAGE_TWITCH
                if (self._video_item is not None and self._video_item.kind == "twitch")
                else self.PAGE_VIDEO_SEARCH
            )
        self._switch_page(target)

    def _remember_music_return_page(self) -> None:
        cur = self.stack.currentIndex()
        if cur in (self.PAGE_MUSIC_SEARCH, self.PAGE_MY_MUSIC, self.PAGE_MUSIC_HISTORY):
            self._music_return_page = cur

    def _on_music_panel_back(self) -> None:
        target = (
            self._music_return_page
            if self._music_return_page
            in (self.PAGE_MUSIC_SEARCH, self.PAGE_MY_MUSIC, self.PAGE_MUSIC_HISTORY)
            else self.PAGE_MUSIC_SEARCH
        )
        self._switch_page(target)

    def _reset_radio_session(self) -> None:
        self._radio_gen += 1
        self._radio_fetching = False
        self._pending_advance_after_radio = False
        self._radio_seen_ids.clear()
        for it in self._music_queue:
            if it.video_id:
                self._radio_seen_ids.append(it.video_id)
        self.music_panel.set_radio_loading(False)

    def _maybe_extend_radio(self, force: bool = False) -> None:
        if not self.cfg.autoplay_music or not self._music_queue:
            return
        if self._radio_fetching:
            return
        idx = max(0, self._queue_index)
        remaining = len(self._music_queue) - 1 - idx
        if not force and remaining > 5:
            return

        seed = self._music_queue[-1]
        if not seed.video_id:
            return
        fallback_seeds = [
            it.video_id
            for it in reversed(self._music_queue[:-1])
            if it.video_id
        ][:2]
        exclude = set(self._radio_seen_ids)
        for it in self._music_queue:
            if it.video_id:
                exclude.add(it.video_id)

        self._radio_fetching = True
        self.music_panel.set_radio_loading(True)
        gen = self._radio_gen
        seed_vid = seed.video_id
        artist_hint = seed.author or ""
        self._run_async(
            lambda: self.yt.get_music_radio_queue(
                seed_vid,
                limit=22,
                exclude_ids=exclude,
                fallback_seeds=fallback_seeds,
                artist_hint=artist_hint,
            ),
            on_done=lambda tracks: self._on_radio_tracks_loaded(gen, tracks),
            on_error=lambda _err: self._on_radio_tracks_error(gen),
        )

    def _on_radio_tracks_loaded(self, gen: int, tracks: list[MediaItem]) -> None:
        if gen != self._radio_gen:
            return
        self._radio_fetching = False
        self.music_panel.set_radio_loading(False)

        existing = {it.video_id for it in self._music_queue}
        seen_set = set(self._radio_seen_ids)
        added = 0
        for t in tracks:
            if t.video_id and t.video_id not in existing and t.video_id not in seen_set:
                existing.add(t.video_id)
                seen_set.add(t.video_id)
                self._radio_seen_ids.append(t.video_id)
                self._music_queue.append(t)
                added += 1

        if self._queue_index > 30:
            trim = self._queue_index - 25
            if trim > 0:
                del self._music_queue[:trim]
                self._queue_index -= trim

        self._refresh_badges()
        if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER or added > 0:
            self._render_queue()

        if self._pending_advance_after_radio:
            self._pending_advance_after_radio = False
            if self._queue_index + 1 < len(self._music_queue):
                self._play_queue_index(self._queue_index + 1)

    def _on_radio_tracks_error(self, gen: int) -> None:
        if gen != self._radio_gen:
            return
        self._radio_fetching = False
        self._pending_advance_after_radio = False
        self.music_panel.set_radio_loading(False)

    def _play_music_from_search(self, item: MediaItem) -> None:
        self._remember_music_return_page()
        self._music_queue = [item]
        self._queue_index = 0
        self._reset_radio_session()
        self._refresh_badges()
        self._switch_page(self.PAGE_MUSIC_PLAYER)
        self._play_music_item(item)
        self._maybe_extend_radio(force=True)

    def _play_list_from(self, items: list[MediaItem], index: int) -> None:
        if not items:
            return
        self._remember_music_return_page()
        self._music_queue = list(items)
        self._queue_index = max(0, min(index, len(items) - 1))
        self._reset_radio_session()
        self._refresh_badges()
        self._switch_page(self.PAGE_MUSIC_PLAYER)
        self._play_music_item(self._music_queue[self._queue_index])
        self._maybe_extend_radio()

    def _play_all_my_music(self) -> None:
        items = self.db.list_my_music()
        if not items:
            self.show_toast("Tu lista Mi música está vacía")
            return
        self._play_list_from(items, 0)

    def _shuffle_my_music(self) -> None:
        items = self.db.list_my_music()
        if not items:
            self.show_toast("Tu lista Mi música está vacía")
            return
        random.shuffle(items)
        self._play_list_from(items, 0)

    def _play_queue_index(self, idx: int) -> None:
        if 0 <= idx < len(self._music_queue):
            self._queue_index = idx
            self._play_music_item(self._music_queue[idx])
            if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER:
                self._render_queue()

    def _enqueue_music_item(self, item: MediaItem) -> None:
        self._music_queue.append(item)
        if item.video_id:
            self._radio_seen_ids.append(item.video_id)
        self._refresh_badges()
        self.persist_session_state(force=True)
        self.show_toast(f"Agregado a la cola: {item.title}")
        if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER:
            self._render_queue()

    def _remove_from_queue(self, item: MediaItem) -> None:
        for i, q_it in enumerate(self._music_queue):
            if q_it.video_id == item.video_id:
                self._music_queue.pop(i)
                if i < self._queue_index:
                    self._queue_index -= 1
                elif i == self._queue_index:
                    self._queue_index = min(self._queue_index, len(self._music_queue) - 1)
                break
        self._refresh_badges()
        self.persist_session_state(force=True)
        self._render_queue()

    def _clear_queue(self) -> None:
        self._music_queue.clear()
        self._queue_index = -1
        self._reset_radio_session()
        self._refresh_badges()
        self.persist_session_state(force=True)
        self._render_queue()

    def _toggle_autoplay(self) -> None:
        self.cfg.autoplay_music = not self.cfg.autoplay_music
        save_config(self.cfg)
        self.music_panel.set_autoplay_state(self.cfg.autoplay_music)
        if self.cfg.autoplay_music:
            self._maybe_extend_radio(force=False)
        self._render_queue()

    def _play_music_item(self, item: MediaItem, start_pos: float = 0.0) -> None:
        self._music_idle_unload_timer.stop()
        self._pause_other_if_auto("music")
        self._active_deck = "music"
        eff_start = max(0.0, float(start_pos))
        same_item = self._music_item is item
        self._music_item = item
        self._music_pos = eff_start
        self._music_dur = max(
            self._music_dur if same_item else 0.0,
            float(item.duration_sec or 0),
        )
        self.db.record_play(item)
        self._music_history_page_dirty = True
        self.persist_session_state(force=True)
        self.music_player.set_speed(1.0)
        self.music_panel.set_current_item(item, self.db.is_in_my_music(item.video_id))
        self._sync_bottom_bar()
        self._sync_mini_bar()
        self._update_bottom_bar_visibility()

        for row in self._music_rows:
            row.set_current(row.item.video_id == item.video_id)
        if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER:
            self._render_queue()

        self._maybe_extend_radio()

        self._music_play_gen += 1
        gen = self._music_play_gen
        self._run_async(
            lambda: self.yt.resolve_stream(item.video_id, audio_only=True),
            on_done=lambda stream: self._on_music_stream_resolved(gen, stream, eff_start),
            on_error=lambda err: self._on_music_play_error(gen, err),
        )

    def _on_music_stream_resolved(
        self, gen: int, stream: ResolvedStream, start_pos: float = 0.0
    ) -> None:
        if gen != self._music_play_gen:
            return
        if stream.duration > 0:
            self._on_music_dur_changed(stream.duration)
        self.music_player.play_stream(
            primary_url=stream.primary_url,
            audio_url=None,
            video_enabled=False,
            http_headers=stream.http_headers,
            start_pos=start_pos,
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()

        # Pre-resolver la siguiente canción en segundo plano para transición instantánea
        nxt_idx = self._queue_index + 1
        if 0 <= nxt_idx < len(self._music_queue) and self._prefetch_inflight < 2:
            nxt = self._music_queue[nxt_idx]
            if nxt.video_id and not self.yt.has_cached_stream(nxt.video_id, audio_only=True):
                self._prefetch_inflight += 1
                self._run_async(
                    lambda vid=nxt.video_id: self.yt.prefetch_stream(vid, audio_only=True),
                    on_done=lambda _res: self._finish_prefetch(),
                    on_error=lambda _err: self._finish_prefetch(),
                )

    def _on_music_play_error(self, gen: int, err: str) -> None:
        if gen != self._music_play_gen:
            return
        self.show_toast(f"No se pudo reproducir la canción: {err}")

    def _get_effective_deck(self) -> Literal["video", "music"]:
        cur = self.stack.currentIndex()
        if cur == self.PAGE_VIDEO_PLAYER and self._video_item is not None:
            return "video"
        if cur in (
            self.PAGE_MUSIC_SEARCH,
            self.PAGE_MY_MUSIC,
            self.PAGE_MUSIC_PLAYER,
            self.PAGE_MUSIC_HISTORY,
        ):
            if self._music_item is not None or self._video_item is None:
                return "music"
        if self._active_deck == "music" and self._music_item is not None:
            return "music"
        if self._video_item is not None:
            return "video"
        if self._music_item is not None:
            return "music"
        return "video" if self._search_mode == "video" else "music"

    def toggle_deck_play_pause(self, deck: Literal["video", "music"]) -> None:
        if deck == "video":
            if self._video_item is None:
                return
            if not self.video_player.has_active_stream:
                if self._video_item.kind == "twitch":
                    self.play_twitch_stream(self._video_item)
                else:
                    self.play_video(self._video_item, start_pos=self._video_pos)
                return
            if self._video_paused:
                self._pause_other_if_auto("video")
                self._active_deck = "video"
            self.video_player.toggle_pause()
        else:
            if self._music_item is None:
                return
            if not self.music_player.has_active_stream:
                self._play_music_item(self._music_item, start_pos=self._music_pos)
                return
            if self._music_paused:
                self._pause_other_if_auto("music")
                self._active_deck = "music"
            self.music_player.toggle_pause()

    def toggle_play_pause(self) -> None:
        self.toggle_deck_play_pause(self._get_effective_deck())

    def play_next(self) -> None:
        if self._queue_index + 1 < len(self._music_queue):
            self._play_queue_index(self._queue_index + 1)
        elif self.cfg.autoplay_music and self._music_queue:
            self._pending_advance_after_radio = True
            self._maybe_extend_radio(force=True)

    def play_previous(self) -> None:
        if self._music_pos > 4.0 and self._music_item is not None:
            self.music_player.seek(0.0)
            return
        if self._queue_index - 1 >= 0:
            self._play_queue_index(self._queue_index - 1)

    def _seek_active_deck(self, pos: float) -> None:
        deck = self._get_effective_deck()
        if deck == "video" and self._video_item is not None:
            self.video_player.seek(pos)
        elif deck == "music" and self._music_item is not None:
            self.music_player.seek(pos)

    def _on_video_ended(self) -> None:
        self._video_paused = True
        is_twitch = self._video_item is not None and self._video_item.kind == "twitch"
        if self._video_item is not None and not is_twitch:
            self.db.update_play_progress(
                self._video_item.video_id, 0.0, self._video_dur
            )
            self._last_saved_video_pos = 0.0
            self._video_pos = 0.0
        if not is_twitch:
            self.persist_session_state(force=True)
        else:
            self.video_panel.set_status("Directo finalizado o desconectado")
        if self.video_player.is_loaded:
            self._video_idle_unload_timer.start(20_000)
        self.video_panel.set_paused(True)
        self.fs_overlay.set_paused(True)
        self._sync_bottom_bar()
        self._sync_mini_bar()

    def _on_music_ended(self) -> None:
        if self._queue_index + 1 < len(self._music_queue):
            self._play_queue_index(self._queue_index + 1)
        elif self.cfg.autoplay_music and self._music_queue:
            self._pending_advance_after_radio = True
            self._maybe_extend_radio(force=True)
        else:
            self._music_paused = True
            self._sync_bottom_bar()
            self._sync_mini_bar()

    # ------------------------------------------------------------------
    # Sincronización de ambos decks con UI, Barra Inferior, Overlay y Mini-barra
    # ------------------------------------------------------------------

    def _on_video_pos_changed(self, pos: float) -> None:
        self._video_pos = pos
        if self._video_item is not None and self._video_item.kind == "twitch":
            return
        if self._video_item is not None and self._video_item.is_live and pos > self._video_dur:
            self._on_video_dur_changed(pos)
        if self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER and not self._is_fullscreen:
            self.video_panel.set_position(pos)
        if self._is_fullscreen and self.fs_overlay.isVisible():
            self.fs_overlay.set_position(pos)
        if self.bottom_bar.isVisible() and self._get_bottom_bar_deck() == "video":
            self._update_bb_position(pos, self._video_dur)
        if self.mini_bar.isVisible() and self._get_minibar_deck() == "video":
            off_mb = self._compute_live_display_offset(self._video_dur)
            self.mini_bar.set_position(pos + off_mb)
        self._save_current_video_progress(force=False)
        self.persist_session_state(force=False)

    def _on_video_dur_changed(self, dur: float) -> None:
        if self._video_item is not None and self._video_item.kind == "twitch":
            return
        self._video_dur = max(0.0, dur)
        off = self._compute_live_display_offset(self._video_dur)
        self.video_panel.set_live_display_offset(off)
        self.fs_overlay.set_live_display_offset(off)
        self.video_panel.set_duration(self._video_dur)
        self.fs_overlay.set_duration(self._video_dur)
        if self._get_bottom_bar_deck() == "video":
            self.bb_dur.setText(format_duration(self._video_dur + off) or "0:00")
        if self._get_minibar_deck() == "video":
            self.mini_bar.set_duration(self._video_dur + off)
        if (
            self._video_item is not None
            and not self._video_item.is_live
            and self._video_dur > 0
            and self._video_item.duration_sec <= 0
        ):
            self._video_item.duration_sec = int(self._video_dur)
            self._video_item.duration = (
                format_duration(self._video_dur) or self._video_item.duration
            )
            self.db.update_play_progress(
                self._video_item.video_id, self._video_pos, self._video_dur
            )

    def _on_video_paused_changed(self, paused: bool) -> None:
        self._video_paused = paused
        self.video_panel.set_paused(paused)
        self.fs_overlay.set_paused(paused)
        if not paused:
            self._video_idle_unload_timer.stop()
            self.continue_banner.setVisible(False)
            self._active_deck = "video"
        else:
            if self._video_item is not None and self._video_item.kind != "twitch":
                self._save_current_video_progress(force=True)
                self.persist_session_state(force=True)
            if self.video_player.is_loaded:
                self._video_idle_unload_timer.start(
                    90_000
                    if self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
                    else 20_000
                )
        self._sync_bottom_bar()
        self._sync_mini_bar()

    def _on_video_buffering_changed(self, buffering: bool) -> None:
        self.video_panel.set_status("Buffering..." if buffering else "")

    def _on_music_pos_changed(self, pos: float) -> None:
        self._music_pos = pos
        if self.bottom_bar.isVisible() and self._get_bottom_bar_deck() == "music":
            self._update_bb_position(pos, self._music_dur)
        if self.mini_bar.isVisible() and self._get_minibar_deck() == "music":
            self.mini_bar.set_position(pos)
        self.persist_session_state(force=False)

    def _on_music_dur_changed(self, dur: float) -> None:
        self._music_dur = max(0.0, dur)
        if self._get_bottom_bar_deck() == "music":
            self.bb_dur.setText(format_duration(self._music_dur) or "0:00")
        if self._get_minibar_deck() == "music":
            self.mini_bar.set_duration(self._music_dur)

    def _on_music_paused_changed(self, paused: bool) -> None:
        self._music_paused = paused
        self.music_panel.set_playing_state(not paused)
        if not paused:
            self._music_idle_unload_timer.stop()
            self._active_deck = "music"
        else:
            self.persist_session_state(force=True)
            if self.music_player.is_loaded:
                self._music_idle_unload_timer.start()
        self._sync_bottom_bar()
        self._sync_mini_bar()

    def _on_video_volume_changed(self, vol: int) -> None:
        vol = max(0, min(100, int(vol)))
        self.cfg.video_volume = vol
        if vol > 0:
            self._prev_video_vol = vol
        save_config(self.cfg)
        self.video_player.set_volume(vol)
        self.video_panel.set_volume_silent(vol)
        self.fs_overlay.set_volume_silent(vol)
        if self._get_bottom_bar_deck() == "video":
            self.bb_vol.blockSignals(True)
            self.bb_vol.setValue(vol)
            self.bb_vol.blockSignals(False)
            self._refresh_bb_vol_icon()
        if self._get_minibar_deck() == "video":
            self.mini_bar.set_volume_silent(vol)

    def _on_music_volume_changed(self, vol: int) -> None:
        vol = max(0, min(100, int(vol)))
        self.cfg.music_volume = vol
        if vol > 0:
            self._prev_music_vol = vol
        save_config(self.cfg)
        self.music_player.set_volume(vol)
        if self._get_bottom_bar_deck() == "music":
            self.bb_vol.blockSignals(True)
            self.bb_vol.setValue(vol)
            self.bb_vol.blockSignals(False)
            self._refresh_bb_vol_icon()
        if self._get_minibar_deck() == "music":
            self.mini_bar.set_volume_silent(vol)

    def _toggle_music_mute(self) -> None:
        if self.cfg.music_volume > 0:
            self._prev_music_vol = self.cfg.music_volume
            self._on_music_volume_changed(0)
        else:
            self._on_music_volume_changed(max(10, self._prev_music_vol))

    def _on_video_wheel_volume(self, delta: int) -> None:
        new_vol = max(0, min(100, self.cfg.video_volume + int(delta)))
        self._on_video_volume_changed(new_vol)
        self._show_volume_hud(new_vol)

    def _show_music_volume_hud(self, percent: int) -> None:
        anchor_w = self.music_panel.stage_area
        top_left = anchor_w.mapToGlobal(QPoint(0, 0))
        self.vol_hud.show_volume(percent, QRect(top_left, anchor_w.size()))

    def _on_music_wheel_volume(self, delta: int) -> None:
        new_vol = max(0, min(100, self.cfg.music_volume + int(delta)))
        self._on_music_volume_changed(new_vol)
        self._show_music_volume_hud(new_vol)

    def _on_music_stage_clicked(self) -> None:
        if self._music_item is None:
            return
        anchor_w = self.music_panel.stage_area
        top_left = anchor_w.mapToGlobal(QPoint(0, 0))
        self.action_hud.show_action(
            "play" if self._music_paused else "pause",
            QRect(top_left, anchor_w.size()),
        )
        self.toggle_deck_play_pause("music")

    def _show_volume_hud(self, percent: int) -> None:
        if self._is_fullscreen:
            scr = self.screen() or QApplication.primaryScreen()
            anchor = scr.geometry() if scr else self.geometry()
        else:
            ms = self.video_panel.media_stack
            top_left = ms.mapToGlobal(QPoint(0, 0))
            anchor = QRect(top_left, ms.size())
        self.vol_hud.show_volume(percent, anchor)

    def _show_action_hud(self, icon_name: str, text: str = "") -> None:
        if self._is_fullscreen:
            scr = self.screen() or QApplication.primaryScreen()
            anchor = scr.geometry() if scr else self.geometry()
        else:
            ms = self.video_panel.media_stack
            top_left = ms.mapToGlobal(QPoint(0, 0))
            anchor = QRect(top_left, ms.size())
        self.action_hud.show_action(icon_name, anchor, text=text)

    def _on_video_play_btn_clicked(self) -> None:
        if self._video_item is not None:
            self._show_action_hud("play" if self._video_paused else "pause")
        self.toggle_deck_play_pause("video")

    def _on_video_surface_clicked(self) -> None:
        if self._video_item is None or self.stack.currentIndex() != self.PAGE_VIDEO_PLAYER:
            return
        if (time.monotonic() - self._last_video_dbl_ts) < 0.30:
            return
        self._video_click_timer.start()

    def _on_video_single_click_commit(self) -> None:
        if self._video_item is None or self.stack.currentIndex() != self.PAGE_VIDEO_PLAYER:
            return
        if (time.monotonic() - self._last_video_dbl_ts) < 0.30:
            return
        self._show_action_hud("play" if self._video_paused else "pause")
        self.toggle_deck_play_pause("video")

    def _on_video_surface_double_clicked(self) -> None:
        self._video_click_timer.stop()
        now = time.monotonic()
        if (now - self._last_video_dbl_ts) < 0.30:
            return
        self._last_video_dbl_ts = now
        if self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER:
            self.toggle_fullscreen()

    def _on_video_mouse_moved(self) -> None:
        if self._is_fullscreen:
            if self._video_item is not None and self._video_item.kind != "twitch":
                self.fs_overlay.set_position(self._video_pos)
            scr = self.screen() or QApplication.primaryScreen()
            rect = scr.geometry() if scr else self.geometry()
            self.fs_overlay.reveal(rect)

    def _on_video_speed_changed(self, speed: float) -> None:
        if self._video_item is not None and self._video_item.kind == "twitch":
            return
        self.cfg.video_speed = speed
        save_config(self.cfg)
        self.video_player.set_speed(speed)
        self.video_panel.set_speed_silent(speed)
        self.fs_overlay.set_speed_silent(speed)

    def _on_video_quality_changed(self, quality: str) -> None:
        self.cfg.video_quality = quality
        save_config(self.cfg)
        self.video_panel.set_quality_silent(quality)
        self.fs_overlay.set_quality_silent(quality)
        if self._video_item and not self.cfg.video_audio_only:
            if self._video_item.kind == "twitch":
                self.play_twitch_stream(self._video_item)
            else:
                self.play_video(self._video_item, start_pos=self._video_pos)

    def _on_audio_only_toggled(self, audio_only: bool) -> None:
        self.cfg.video_audio_only = audio_only
        save_config(self.cfg)
        self.video_panel.set_audio_only_visual(audio_only)
        self.fs_overlay.set_audio_only_silent(audio_only)
        if self._video_item:
            if self._video_item.kind == "twitch":
                self.play_twitch_stream(self._video_item)
            else:
                self.play_video(self._video_item, start_pos=self._video_pos)

    # ------------------------------------------------------------------
    # Barra Inferior Inteligente (click -> vista completa del deck)
    # ------------------------------------------------------------------

    def _get_bottom_bar_deck(self) -> Literal["video", "music"] | None:
        cur = self.stack.currentIndex()
        if cur in (
            self.PAGE_MUSIC_SEARCH,
            self.PAGE_MY_MUSIC,
            self.PAGE_MUSIC_PLAYER,
            self.PAGE_MUSIC_HISTORY,
        ):
            if self._music_item is not None:
                return "music"
            if self._video_item is not None:
                return "video"
            return None

        if cur in (
            self.PAGE_VIDEO_SEARCH,
            self.PAGE_VIDEO_PLAYER,
            self.PAGE_WATCH_LATER,
            self.PAGE_HISTORY,
            self.PAGE_TWITCH,
        ):
            if self._video_item is not None:
                return "video"
            if self._music_item is not None:
                return "music"
            return None

        # En Discord u otras vistas globales, priorizar el deck reproduciéndose o el activo
        if self._music_item is not None and not self._music_paused:
            return "music"
        if self._video_item is not None and not self._video_paused:
            return "video"
        if self._active_deck == "music" and self._music_item is not None:
            return "music"
        if self._video_item is not None:
            return "video"
        if self._music_item is not None:
            return "music"
        return None

    def _get_minibar_deck(self) -> Literal["video", "music"]:
        if not self._music_paused and self._music_item is not None:
            return "music"
        if not self._video_paused and self._video_item is not None:
            return "video"
        if self._active_deck == "music" and self._music_item is not None:
            return "music"
        if self._video_item is not None:
            return "video"
        return "music"

    def _on_bottom_bar_clicked(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck == "video" and self._video_item is not None:
            self._active_deck = "video"
            self._switch_page(self.PAGE_VIDEO_PLAYER)
            if self.video_player.has_active_stream:
                self._schedule_video_surface_refresh(55)
            else:
                if self._video_item.kind == "twitch":
                    fav = self.db.is_twitch_favorite(self._video_item.video_id)
                    vtxt = (
                        format_viewers(self._video_item.viewer_count)
                        if self._video_item.viewer_count > 0
                        else ""
                    )
                    self.video_panel.set_item(self._video_item, fav)
                    self.video_panel.set_live_mode(True, viewers_text=vtxt, is_twitch=True)
                else:
                    self.video_panel.set_resume_preview(self._video_item, self._video_pos)
        elif deck == "music" and self._music_item is not None:
            self._active_deck = "music"
            if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER:
                self._on_music_panel_back()
            else:
                self._remember_music_return_page()
                self._switch_page(self.PAGE_MUSIC_PLAYER)

    def _on_bb_play_pause(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck is not None:
            self.toggle_deck_play_pause(deck)

    def _on_bb_prev(self) -> None:
        if self._get_bottom_bar_deck() == "music":
            self.play_previous()
        elif self._video_item is not None and self._video_item.kind != "twitch":
            self.video_player.seek_relative(-10.0)

    def _on_bb_next(self) -> None:
        if self._get_bottom_bar_deck() == "music":
            self.play_next()
        elif self._video_item is not None and self._video_item.kind != "twitch":
            self.video_player.seek_relative(10.0)

    def _on_bb_seek_moved(self, val: int) -> None:
        deck = self._get_bottom_bar_deck()
        dur = self._video_dur if deck == "video" else self._music_dur
        if not self._seeking_bottom and dur > 0:
            pos = (val / 1000.0) * dur
            if deck == "video":
                self.video_player.seek(pos)
            else:
                self.music_player.seek(pos)

    def _on_bb_seek_release(self) -> None:
        self._seeking_bottom = False
        deck = self._get_bottom_bar_deck()
        dur = self._video_dur if deck == "video" else self._music_dur
        if dur > 0:
            pos = (self.bb_seek.value() / 1000.0) * dur
            if deck == "video":
                self.video_player.seek(pos)
            else:
                self.music_player.seek(pos)

    def _on_bb_volume_changed(self, vol: int) -> None:
        if self._get_bottom_bar_deck() == "video":
            self._on_video_volume_changed(vol)
        else:
            self._on_music_volume_changed(vol)

    def _on_minibar_volume_changed(self, vol: int) -> None:
        if self._get_minibar_deck() == "video":
            self._on_video_volume_changed(vol)
        else:
            self._on_music_volume_changed(vol)

    def _toggle_bb_mute(self) -> None:
        if self._get_bottom_bar_deck() == "video":
            self.video_panel._toggle_mute()
        else:
            self._toggle_music_mute()

    def _toggle_bb_saved(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck == "video" and self._video_item is not None:
            self._toggle_watch_later_item(self._video_item)
        elif deck == "music" and self._music_item is not None:
            self._toggle_my_music_item(self._music_item)

    def _download_bb_current(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck == "video" and self._video_item is not None:
            if self._video_item.kind == "twitch" or self._video_item.is_live:
                return
            self._download_item(self._video_item, "video")
        elif deck == "music" and self._music_item is not None:
            self._download_item(self._music_item, "music")

    def _update_bb_position(self, pos: float, dur: float) -> None:
        if not self._seeking_bottom and dur > 0:
            ratio = max(0.0, min(1.0, pos / dur))
            self.bb_seek.blockSignals(True)
            self.bb_seek.setValue(int(ratio * 1000))
            self.bb_seek.blockSignals(False)
            off = (
                self._compute_live_display_offset(dur)
                if self._get_bottom_bar_deck() == "video"
                else 0.0
            )
            self.bb_pos.setText(format_duration(pos + off) or "0:00")

    def _get_close_media_target(self) -> Literal["video", "music"] | None:
        cur = self.stack.currentIndex()
        if cur == self.PAGE_VIDEO_PLAYER and self._video_item is not None:
            return "video"
        if cur == self.PAGE_MUSIC_PLAYER and self._music_item is not None:
            return "music"
        return self._get_bottom_bar_deck()

    def _refresh_close_media_btn(self) -> None:
        if not hasattr(self, "close_media_btn"):
            return
        deck = self._get_close_media_target()
        if deck == "video" and self._video_item is not None:
            is_live = self._video_item.kind == "twitch" or self._video_item.is_live
            self.close_media_btn.setText(" Cerrar directo" if is_live else " Cerrar video")
            self.close_media_btn.setToolTip(
                "Cerrar el directo actual y liberar memoria"
                if is_live
                else "Cerrar el video actual y liberar memoria"
            )
            self.close_media_btn.setVisible(True)
        elif deck == "music" and self._music_item is not None:
            self.close_media_btn.setText(" Cerrar música")
            self.close_media_btn.setToolTip("Cerrar la reproducción de música actual")
            self.close_media_btn.setVisible(True)
        else:
            self.close_media_btn.setVisible(False)

    def _close_active_media(self, target: Literal["video", "music"] | None = None) -> None:
        deck = target or self._get_close_media_target()
        if deck == "video":
            if self._is_fullscreen:
                self.toggle_fullscreen()
            self._video_click_timer.stop()
            self._video_idle_unload_timer.stop()
            self._save_current_video_progress(force=True)
            was_twitch = self._video_item is not None and self._video_item.kind == "twitch"
            self._video_play_gen += 1
            self.video_player.unload()
            self._video_item = None
            self._video_pos = 0.0
            self._video_dur = 0.0
            self._video_live_start_ts = 0.0
            self._video_paused = True
            self._last_saved_video_pos = 0.0
            self.video_panel.set_live_display_offset(0.0)
            self.fs_overlay.set_live_display_offset(0.0)
            self.video_panel.set_loading_state(False)
            self.video_panel.set_status("")
            self.back_to_video_btn.setVisible(False)
            if self._music_item is not None:
                self._active_deck = "music"
            self.persist_session_state(force=True)
            if self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER:
                dest = (
                    self.PAGE_TWITCH
                    if was_twitch
                    else getattr(self, "_video_return_page", self.PAGE_VIDEO_SEARCH)
                )
                self._switch_page(dest)
        elif deck == "music":
            self._music_idle_unload_timer.stop()
            self._music_play_gen += 1
            self.music_player.unload()
            self._music_item = None
            self._music_pos = 0.0
            self._music_dur = 0.0
            self._music_paused = True
            self._music_queue.clear()
            self._queue_index = -1
            self._reset_radio_session()
            self.music_panel.set_current_item(None, False)
            self._render_queue()
            if self._video_item is not None:
                self._active_deck = "video"
            self.persist_session_state(force=True)
            if self.stack.currentIndex() == self.PAGE_MUSIC_PLAYER:
                self._switch_page(
                    getattr(self, "_music_return_page", self.PAGE_MUSIC_SEARCH)
                    or self.PAGE_MUSIC_SEARCH
                )
            self._refresh_badges()
        self._sync_bottom_bar()
        self._sync_mini_bar()
        self._update_bottom_bar_visibility()
        self._refresh_close_media_btn()
        trim_process_memory()

    def _sync_bottom_bar(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck is None:
            self._bb_thumb_url = ""
            self.bb_title.setText("Sin reproducción")
            self.bb_author.setText("")
            self.bb_thumb.clear()
            self.bb_switch_btn.setVisible(False)
            self._refresh_close_media_btn()
            return

        it = self._video_item if deck == "video" else self._music_item
        paused = self._video_paused if deck == "video" else self._music_paused
        pos = self._video_pos if deck == "video" else self._music_pos
        dur = self._video_dur if deck == "video" else self._music_dur
        vol = self.cfg.video_volume if deck == "video" else self.cfg.music_volume
        is_twitch = deck == "video" and it is not None and it.kind == "twitch"
        is_live = (
            deck == "video" and it is not None and (it.kind == "twitch" or it.is_live)
        )

        self.bb_deck_tag.setText(
            "EN VIVO" if is_live else ("VIDEO" if deck == "video" else "MÚSICA")
        )
        self.bb_seek.setVisible(not is_twitch)
        self.bb_prev.setVisible(not is_live)
        self.bb_next.setVisible(not is_live)
        self.bb_dl.setVisible(not is_live)
        if it is not None:
            self.bb_title.setText(it.title)
            if is_twitch:
                sub = (
                    f"{it.author}  ·  {it.game_name}"
                    if it.game_name
                    else (it.author or "Twitch")
                )
            elif is_live:
                sub = f"{it.author or 'YouTube'}  ·  En vivo"
            elif (
                deck == "video"
                and pos >= 3.0
                and not self.video_player.has_active_stream
            ):
                sub = f"{it.author or 'YouTube'}  ·  Continuar en {format_duration(pos)}"
            elif (
                deck == "music"
                and pos >= 3.0
                and not self.music_player.has_active_stream
            ):
                sub = f"{it.author or 'YouTube Music'}  ·  Continuar en {format_duration(pos)}"
            else:
                sub = it.author or ""
            self.bb_author.setText(sub)
            self._bb_thumb_url = it.normalized_thumb()
            pix = self.thumb_cache.get_or_fetch(self._bb_thumb_url, QSize(48, 48))
            if pix is not None:
                self.bb_thumb.set_pixmap(pix)
            else:
                self.bb_thumb.clear()

        self.bb_play.setIcon(
            render_icon("play" if paused else "pause", self.palette.accent_text, 18)
        )
        if is_twitch:
            self.bb_pos.setText("EN VIVO")
            self.bb_dur.setText(
                format_viewers(it.viewer_count) if it and it.viewer_count > 0 else ""
            )
        else:
            off = self._compute_live_display_offset(dur) if deck == "video" else 0.0
            self.bb_dur.setText(format_duration(dur + off) or "0:00")
            if dur > 0:
                self._update_bb_position(pos, dur)
            else:
                self.bb_pos.setText(format_duration(off) or "0:00")
                if not self._seeking_bottom:
                    self.bb_seek.blockSignals(True)
                    self.bb_seek.setValue(0)
                    self.bb_seek.blockSignals(False)
        self.bb_vol.blockSignals(True)
        self.bb_vol.setValue(vol)
        self.bb_vol.blockSignals(False)
        self._refresh_bb_vol_icon()
        self._refresh_bb_fav_icon()

        both_loaded = self._video_item is not None and self._music_item is not None
        self.bb_switch_btn.setVisible(both_loaded)
        if both_loaded:
            vid_label = (
                "Directo"
                if (self._video_item is not None and self._video_item.kind == "twitch")
                else "Video"
            )
            self.bb_switch_btn.setText("Música" if deck == "video" else vid_label)
        self._refresh_close_media_btn()

    def _sync_mini_bar(self) -> None:
        deck = self._get_minibar_deck()
        it = self._video_item if deck == "video" else self._music_item
        paused = self._video_paused if deck == "video" else self._music_paused
        if deck == "video" and it is not None and it.kind == "twitch":
            dur = 0.0
            pos = 0.0
        else:
            dur = self._video_dur if deck == "video" else self._music_dur
            pos = self._video_pos if deck == "video" else self._music_pos
            if deck == "video":
                off_mb = self._compute_live_display_offset(dur)
                dur += off_mb
                if pos > 0:
                    pos += off_mb
        vol = self.cfg.video_volume if deck == "video" else self.cfg.music_volume

        self.mini_bar.set_item(it)
        self.mini_bar.set_paused(paused)
        self.mini_bar.set_duration(dur)
        self.mini_bar.set_position(pos)
        self.mini_bar.set_volume_silent(vol)

    def _on_bb_thumb_ready(self, url: str, pix: QPixmap) -> None:
        if (
            self._bb_thumb_url
            and url == self._bb_thumb_url
            and pix.size() == QSize(48, 48)
        ):
            self.bb_thumb.set_pixmap(pix)

    def _refresh_bb_vol_icon(self) -> None:
        vol = self.bb_vol.value()
        name = "mute" if vol == 0 else "volume"
        self.bb_vol_icon.setIcon(render_icon(name, self.palette.text, 16))

    def _refresh_bb_fav_icon(self) -> None:
        deck = self._get_bottom_bar_deck()
        it = self._video_item if deck == "video" else self._music_item
        if it is None:
            self.bb_fav.setIcon(render_icon("heart", self.palette.text2, 16))
            return
        if deck == "video":
            if it.kind == "twitch":
                saved = self.db.is_twitch_favorite(it.video_id)
                icon_name = "heart_filled" if saved else "heart"
            else:
                saved = self.db.is_in_watch_later(it.video_id)
                icon_name = "bookmark_filled" if saved else "bookmark"
        else:
            saved = self.db.is_in_my_music(it.video_id)
            icon_name = "heart_filled" if saved else "heart"
        color = self.palette.accent if saved else self.palette.text2
        self.bb_fav.setIcon(render_icon(icon_name, color, 16))

    def _update_bottom_bar_visibility(self) -> None:
        # Fija siempre mientras haya reproducción/contenido, excepto en video fullscreen o dentro del reproductor de video
        if self._is_fullscreen:
            self._bottom_bar_anim.stop()
            self._bottom_bar_hiding = False
            self.bottom_bar_wrap.set_reveal_height(74)
            self.bottom_bar_wrap.setVisible(False)
            return
        on_video_player = self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
        has_any = self._video_item is not None or self._music_item is not None
        should_show = has_any and not on_video_player
        was_visible = self.bottom_bar_wrap.isVisible()
        if should_show and (not was_visible or self._bottom_bar_hiding):
            self._bottom_bar_anim.stop()
            self._bottom_bar_hiding = False
            cur_h = self.bottom_bar_wrap.height() if was_visible else 0
            self.bottom_bar_wrap.set_reveal_height(cur_h)
            self.bottom_bar_wrap.setVisible(True)
            self._bottom_bar_anim.setDuration(max(70, int(185 * (1.0 - cur_h / 74.0))))
            self._bottom_bar_anim.setStartValue(cur_h)
            self._bottom_bar_anim.setEndValue(74)
            self._bottom_bar_anim.start()
        elif not should_show and was_visible and not self._bottom_bar_hiding:
            self._bottom_bar_anim.stop()
            self._bottom_bar_hiding = True
            cur_h = max(0, min(74, self.bottom_bar_wrap.height()))
            self._bottom_bar_anim.setDuration(max(60, int(165 * (cur_h / 74.0))))
            self._bottom_bar_anim.setStartValue(cur_h)
            self._bottom_bar_anim.setEndValue(0)
            self._bottom_bar_anim.start()

    def _on_bottom_bar_anim_step(self, val: object) -> None:
        if isinstance(val, (int, float)):
            self.bottom_bar_wrap.set_reveal_height(int(round(float(val))))

    def _on_bottom_bar_anim_finished(self) -> None:
        if self._bottom_bar_hiding:
            self._bottom_bar_hiding = False
            self.bottom_bar_wrap.setVisible(False)
            self.bottom_bar_wrap.set_reveal_height(74)
        elif self.bottom_bar_wrap.isVisible():
            self.bottom_bar_wrap.set_reveal_height(74)

    # ------------------------------------------------------------------
    # Modo Teatro (Sidebar), Fullscreen 100%, Mini-barra y Ajustes
    # ------------------------------------------------------------------

    def toggle_sidebar(self) -> None:
        """Oculta o muestra la barra lateral izquierda (Modo teatro) [T]."""
        if self._is_fullscreen:
            self._sidebar_hidden = True
            self.toggle_fullscreen()
            self._refresh_sidebar_toggle_ui()
            return
        self._sidebar_hidden = not self._sidebar_hidden
        self.sidebar.setVisible(not self._sidebar_hidden)
        self._refresh_sidebar_toggle_ui()

    def _refresh_sidebar_toggle_ui(self) -> None:
        p = self.palette
        if hasattr(self, "sidebar_toggle_btn"):
            self.sidebar_toggle_btn.setProperty(
                "active", "true" if self._sidebar_hidden else "false"
            )
            self.sidebar_toggle_btn.style().unpolish(self.sidebar_toggle_btn)
            self.sidebar_toggle_btn.style().polish(self.sidebar_toggle_btn)
            icon_color = p.accent if self._sidebar_hidden else p.text
            self.sidebar_toggle_btn.setIcon(render_icon("sidebar", icon_color, 17))
            self.sidebar_toggle_btn.setToolTip(
                "Mostrar barra lateral [T]"
                if self._sidebar_hidden
                else "Ocultar barra lateral (Modo teatro) [T]"
            )
        if hasattr(self, "video_panel"):
            self.video_panel.set_theater_state(self._sidebar_hidden)

    def toggle_fullscreen(self) -> None:
        # NUNCA reparentar VideoSurface; ocultar todo el cromo para 100% borde a borde
        if not self._is_fullscreen:
            self._pre_fs_was_maximized = (
                bool(self.windowState() & Qt.WindowState.WindowMaximized)
                or self.isMaximized()
            )
            self._pre_fs_geometry = QRect(
                self.normalGeometry() if self._pre_fs_was_maximized else self.geometry()
            )
            self._is_fullscreen = True
            self.sidebar.setVisible(False)
            self.top_bar.setVisible(False)
            self.video_panel.set_fullscreen_state(True)
            self._update_bottom_bar_visibility()
            self.showFullScreen()
            self.video_player.set_mouse_tracking(True)
            self.video_player.set_cursor_autohide(500)
            self._show_action_hud("fullscreen")
            QTimer.singleShot(50, self._on_video_mouse_moved)
        else:
            self._is_fullscreen = False
            self.fs_overlay.hide_immediately()
            self.video_player.set_mouse_tracking(False)
            self.video_player.set_cursor_autohide("no")
            if self._pre_fs_was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
                if self._pre_fs_geometry is not None and self._pre_fs_geometry.isValid():
                    self.setGeometry(self._pre_fs_geometry)
            self.sidebar.setVisible(not self._sidebar_hidden)
            self.top_bar.setVisible(self.stack.currentIndex() != self.PAGE_DISCORD)
            self.video_panel.set_fullscreen_state(False)
            self._update_bottom_bar_visibility()
            self._refresh_sidebar_toggle_ui()
            if self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER and self._video_item is not None:
                self._show_action_hud("exit_fullscreen")

    def toggle_mini_bar(self) -> None:
        if self.mini_bar.isVisible():
            self._restore_from_mini_bar()
        else:
            if self._is_fullscreen:
                self.toggle_fullscreen()
            self.hide()
            if self.video_player.is_loaded and (
                self._video_paused or self.video_player.is_paused
            ):
                self._video_paused = True
                self.video_player.unload()
            if self.music_player.is_loaded and (
                self._music_paused or self.music_player.is_paused
            ):
                self._music_paused = True
                self.music_player.unload()
            self._clear_hidden_browse_pages(clear_all=False)
            self.thumb_cache.trim(12)
            QPixmapCache.clear()
            self.yt.close_worker()
            self.discord_service.stop_if_idle()
            trim_process_memory()
            self._sync_mini_bar()
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                target = QPoint(
                    geo.right() - self.mini_bar.width() - 24,
                    geo.bottom() - self.mini_bar.height() - 24,
                )
            else:
                target = QPoint(40, 40)
            self.mini_bar.show_animated(target)

    def _restore_from_mini_bar(self) -> None:
        self.mini_bar.hide()
        self.restore_window()

    def _on_minibar_pin_toggled(self, pinned: bool) -> None:
        self.cfg.mini_bar_always_on_top = pinned
        save_config(self.cfg)

    def _on_normalization_changed(self, enabled: bool) -> None:
        self.video_player.set_normalization(enabled)
        self.music_player.set_normalization(enabled)

    def open_settings(
        self,
        account_tab: bool = False,
        discord_tab: bool = False,
        discord_shortcuts_tab: bool = False,
    ) -> None:
        dlg = SettingsDialog(
            self.cfg,
            self.yt,
            thumb_cache=self.thumb_cache,
            discord_channels=self.discord_panel.get_available_channels_for_settings(),
            parent=self,
        )
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        if account_tab:
            dlg.select_account_tab()
        elif discord_shortcuts_tab:
            dlg.select_discord_shortcuts_tab()
        elif discord_tab:
            dlg.select_discord_tab()
        dlg.normalization_changed.connect(self._on_normalization_changed)
        dlg.account_changed.connect(self._on_account_changed)
        dlg.discord_token_changed.connect(self._on_discord_token_changed)
        dlg.hotkey_recording_changed.connect(self._media_filter.set_suspended)
        dlg.discord_hotkeys_changed.connect(
            lambda d=dlg: d.update_discord_hotkey_statuses(self._apply_discord_hotkeys())
        )
        dlg.discord_ui_settings_changed.connect(self._on_discord_ui_settings_changed)
        dlg.discord_notify_color_changed.connect(self._on_discord_notify_color_changed)
        dlg.discord_notify_preview_requested.connect(self._preview_discord_notification)
        dlg.discord_overlay_settings_changed.connect(self._sync_discord_overlay)
        dlg.discord_overlay_preview_requested.connect(
            lambda op: self.discord_overlay.preview(op, overlay_mode=self.cfg.discord_overlay_mode)
        )
        dlg.discord_overlay_edit_requested.connect(
            lambda: QTimer.singleShot(50, self._start_discord_overlay_position_edit)
        )
        dlg.discord_sound_preview_requested.connect(
            lambda ev: self.play_discord_sound(ev, force=True)
        )
        dlg.update_discord_hotkey_statuses(self._apply_discord_hotkeys())
        dlg.trim_memory_requested.connect(self.free_memory_now)
        try:
            dlg.exec()
        finally:
            self._media_filter.set_suspended(False)
            try:
                dlg.deleteLater()
            except RuntimeError:
                pass
        trim_process_memory()

    # ------------------------------------------------------------------
    # Descargas
    # ------------------------------------------------------------------

    def _download_current_video(self) -> None:
        if self._video_item is not None:
            self._download_item(self._video_item, "video")

    def _download_item(self, item: MediaItem, mode: Literal["music", "video"]) -> None:
        out_dir = self.cfg.music_download_dir if mode == "music" else self.cfg.video_download_dir
        quality = self.cfg.video_quality
        self.show_toast(
            f"Descargando ({'Música' if mode == 'music' else 'Video'}): {item.title}..."
        )
        self._run_async(
            lambda: self.yt.download_media(
                item.video_id,
                mode=mode,
                output_dir=out_dir,
                quality=quality,
            ),
            on_done=lambda path: self.show_toast(f"Descarga lista: {Path(path).name}"),
            on_error=lambda err: self.show_toast(f"Error al descargar: {err}"),
        )

    # ------------------------------------------------------------------
    # Utilidades async y ciclo de vida
    # ------------------------------------------------------------------

    def _run_async(
        self,
        fn: Callable,
        on_done: Callable,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._task_seq += 1
        tid = self._task_seq
        task = _AsyncTask(tid, fn)
        sig = task.signals
        self._inflight[tid] = sig

        def _cleanup_done(task_id: int, result: object, s: _TaskSignals = sig) -> None:
            self._inflight.pop(task_id, None)
            try:
                on_done(result)
            finally:
                try:
                    s.deleteLater()
                except RuntimeError:
                    pass

        def _cleanup_err(task_id: int, msg: str, s: _TaskSignals = sig) -> None:
            self._inflight.pop(task_id, None)
            try:
                if on_error:
                    on_error(msg)
            finally:
                try:
                    s.deleteLater()
                except RuntimeError:
                    pass

        sig.done.connect(_cleanup_done)
        sig.error.connect(_cleanup_err)
        self.pool.start(task)

    def show_toast(self, message: str, duration_ms: int = 3500) -> None:
        y_offset = self.bottom_bar.height() + 18 if self.bottom_bar.isVisible() else 18
        self.toast_lbl.show_message(
            message, self.size(), y_offset, duration_ms=duration_ms
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toast_lbl.isVisible():
            y_offset = self.bottom_bar.height() + 18 if self.bottom_bar.isVisible() else 18
            self.toast_lbl.reposition(self.size(), y_offset)

    def restore_window(self) -> None:
        self.mini_bar.hide()
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        bring_window_to_front(int(self.winId()))

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._force_quit and self.cfg.minimize_to_tray and self.tray_icon.isVisible():
            event.ignore()
            self._save_current_video_progress(force=True)
            self.persist_session_state(force=True)
            if self._is_fullscreen:
                self.toggle_fullscreen()
            self.hide()
            if self.video_player.is_loaded and (
                self._video_paused or self.video_player.is_paused
            ):
                self._video_paused = True
                self.video_player.unload()
            if self.music_player.is_loaded and (
                self._music_paused or self.music_player.is_paused
            ):
                self._music_paused = True
                self.music_player.unload()
            self._clear_hidden_browse_pages(clear_all=False)
            self.thumb_cache.trim(12)
            QPixmapCache.clear()
            self.yt.close_worker()
            self.discord_service.stop_if_idle()
            trim_process_memory()
            self._log_ram_sample("minimize_to_tray")
            if not self._tray_notified:
                self._tray_notified = True
                self.tray_icon.showMessage(
                    APP_NAME,
                    "SwiftPlay sigue activo en la bandeja del sistema.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
            return
        self._cleanup_before_exit()
        event.accept()

    def quit_app(self) -> None:
        self._force_quit = True
        self._cleanup_before_exit()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def nativeEvent(self, eventType, message):
        if is_win32_disabled_parent_click(message):
            modal = QApplication.activeModalWidget()
            if (
                isinstance(modal, (SettingsDialog, ShortcutsDialog))
                and QApplication.activePopupWidget() is None
                and not modal.frameGeometry().contains(QCursor.pos())
            ):
                QTimer.singleShot(0, modal.reject)
                return True, 1
        return super().nativeEvent(eventType, message)

    def _refresh_resource_monitor(self) -> None:
        if self._cleaned_up:
            return
        if self._res_sampled_once and not self.isVisible():
            return
        self._res_sampled_once = True
        snap = self._resource_monitor.sample()
        gui_ws = snap.ws_mb if snap.ws_mb > 0 else snap.priv_mb
        self._last_gui_ws_mb = gui_ws
        ds_ws = max(0.0, float(self._discord_ws_mb))
        ds_priv = max(0.0, float(self._discord_priv_mb))
        ss_ws = max(0.0, float(self._screen_share_ws_mb))
        ss_priv = max(0.0, float(self._screen_share_priv_mb))
        total_ws = gui_ws + ds_ws + ss_ws
        self.res_ram_lbl.setText(f"{gui_ws:.0f} MB")
        self.res_ram_total_lbl.setText(f"{total_ws:.0f} MB")
        self.res_cpu_lbl.setText(f"{snap.app_cpu_pct:.1f}%")
        self.res_gpu_lbl.setText(f"{snap.app_gpu_pct:.1f}%")
        ss_line = (
            f"RAM física Pantalla (WS): {ss_ws:.1f} MB  ·  Confirmada (Priv): {ss_priv:.1f} MB\n"
            if ss_ws > 0.0
            else ""
        )
        self.resource_card.setToolTip(
            f"RAM física App (WS): {gui_ws:.1f} MB  ·  Confirmada (Priv): {snap.priv_mb:.1f} MB\n"
            f"RAM física Discord (WS): {ds_ws:.1f} MB  ·  Confirmada (Priv): {ds_priv:.1f} MB\n"
            f"{ss_line}"
            f"Total físico (App + Workers): {total_ws:.1f} MB\n"
            f"CPU app: {snap.app_cpu_pct:.1f}%  ·  CPU sistema: {snap.sys_cpu_pct:.1f}%\n"
            f"GPU app: {snap.app_gpu_pct:.1f}%  ·  GPU sistema: {snap.sys_gpu_pct:.1f}%"
        )

    def _cleanup_before_exit(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            self._bottom_bar_anim.stop()
            self._page_fade_anim.stop()
            self._video_viewport_timer.stop()
            self._channel_viewport_timer.stop()
            for shimmer in self._page_shimmers:
                shimmer.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "discord_panel"):
                self.discord_panel.close_audio_popover()
                self.discord_panel.close_image_lightbox()
                self.discord_panel._clear_pending_attachments(delete_temp=True)
                self.discord_panel.shutdown_screen_share()
            self.discord_overlay.set_edit_mode(False)
        except Exception:
            pass
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:
            pass
        try:
            self.sound_fx.stop()
        except Exception:
            pass
        try:
            self.discord_overlay.close()
        except Exception:
            pass
        try:
            self._ram_log_timer.stop()
        except Exception:
            pass
        try:
            self._video_refresh_timer.stop()
            self._video_post_reinit_timer.stop()
        except Exception:
            pass
        try:
            self._resource_timer.stop()
            self._resource_monitor.close()
        except Exception:
            pass
        try:
            self._save_current_video_progress(force=True)
        except Exception:
            pass
        try:
            self.persist_session_state(force=True)
        except Exception:
            pass
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:
            pass
        try:
            self._media_filter.unregister()
        except Exception:
            pass
        try:
            self.fs_overlay.close()
        except Exception:
            pass
        try:
            self.vol_hud.close()
        except Exception:
            pass
        try:
            self.action_hud.close()
        except Exception:
            pass
        try:
            self.discord_overlay.close()
        except Exception:
            pass
        try:
            self.discord_notif_toast.close_immediately()
        except Exception:
            pass
        try:
            self.mini_bar.close()
        except Exception:
            pass
        try:
            self.tray_icon.hide()
        except Exception:
            pass
        try:
            clear_window_taskbar_properties(int(self.winId()))
        except Exception:
            pass
        try:
            self.pool.clear()
        except Exception:
            pass
        try:
            self.thumb_cache.shutdown()
        except Exception:
            pass
        try:
            self.yt.close()
        except Exception:
            pass
        try:
            self.twitch.close()
        except Exception:
            pass
        try:
            self.discord_service.close()
        except Exception:
            pass
        try:
            QPixmapCache.clear()
        except Exception:
            pass
        try:
            self.video_player.shutdown()
        except Exception:
            pass
        try:
            self.music_player.shutdown()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self._log_ram_sample("shutdown")
