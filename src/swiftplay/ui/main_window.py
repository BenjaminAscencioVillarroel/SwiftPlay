"""Ventana principal de SwiftPlay con Reproductor Dual, Barra Inteligente y Fullscreen 100%."""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Callable, Literal

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    QStringListModel,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig, load_config, save_config
from swiftplay.media_keys import GlobalMediaKeyFilter
from swiftplay.models import FeedSection, MediaItem, ResolvedStream, format_duration
from swiftplay.paths import APP_NAME, ICON_ICO_PATH, ICON_PNG_PATH
from swiftplay.player import MediaPlayer
from swiftplay.security import has_saved_session, load_account_profile
from swiftplay.storage import LocalLibrary
from swiftplay.themes import (
    ThemePalette,
    apply_windows_dark_titlebar,
    build_stylesheet,
    get_theme,
)
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.mini_bar import MiniBarWindow
from swiftplay.ui.settings_dialog import SettingsDialog
from swiftplay.ui.shortcuts_dialog import ShortcutsDialog
from swiftplay.ui.video_overlays import FullscreenOverlayBar, VolumeHudOverlay
from swiftplay.ui.video_surface import VideoPlayerPanel
from swiftplay.ui.widgets import (
    ClickableSlider,
    MusicRowWidget,
    SectionTitleWidget,
    VideoCardWidget,
)
from swiftplay.windows_os import (
    apply_window_taskbar_properties,
    bring_window_to_front,
    clear_window_taskbar_properties,
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
    PAGE_QUEUE = 5
    PAGE_HISTORY = 6

    def __init__(self) -> None:
        super().__init__()
        self.cfg: AppConfig = load_config()
        self.palette: ThemePalette = get_theme(self.cfg.theme)
        self.db = LocalLibrary()
        self.yt = YouTubeService()
        self.thumb_cache = ThumbnailCache(max_items=45, parent=self)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)

        self._inflight: dict[int, _TaskSignals] = {}
        self._task_seq = 0
        self._search_gen = 0
        self._video_feed_gen = 0
        self._music_home_gen = 0
        self._video_play_gen = 0
        self._music_play_gen = 0
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
        self._account_avatar_url = ""
        self._cleaned_up = False

        # Estado independiente del Reproductor Dual
        self._search_mode: Literal["video", "music"] = "video"
        self._active_deck: Literal["video", "music"] = "video"
        self._last_video_page: int = self.PAGE_VIDEO_SEARCH
        self._last_music_page: int = self.PAGE_MUSIC_SEARCH

        self._video_item: MediaItem | None = None
        self._video_pos: float = 0.0
        self._video_dur: float = 0.0
        self._video_paused: bool = True
        self._last_saved_video_pos: float = 0.0
        self._last_saved_video_ts: float = 0.0
        self._prev_video_vol: int = max(10, self.cfg.video_volume)

        self._music_item: MediaItem | None = None
        self._music_pos: float = 0.0
        self._music_dur: float = 0.0
        self._music_paused: bool = True
        self._prev_music_vol: int = max(10, self.cfg.music_volume)
        self._music_queue: list[MediaItem] = []
        self._queue_index: int = -1

        self._video_results: list[MediaItem] = []
        self._music_results: list[MediaItem] = []
        self._video_cards: list[VideoCardWidget] = []
        self._music_rows: list[MusicRowWidget] = []

        self.setWindowTitle(APP_NAME)
        self.resize(1160, 750)
        self.setMinimumSize(860, 560)
        if ICON_ICO_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_ICO_PATH)))

        self._build_ui()

        # Dos instancias ligeras de libmpv en paralelo (Video GPU + Música Solo-Audio)
        self.video_player = MediaPlayer(
            wid=int(self.video_panel.video_surface.winId()),
            initial_volume=self.cfg.video_volume,
            initial_speed=self.cfg.video_speed,
            normalization=self.cfg.audio_normalization,
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
        self._switch_page(self.PAGE_VIDEO_SEARCH)

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
        sb_lay.setContentsMargins(12, 14, 12, 12)
        sb_lay.setSpacing(4)

        brand_row = QHBoxLayout()
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

        vid_sec = QLabel("VIDEO")
        vid_sec.setObjectName("SectionHeader")
        sb_lay.addWidget(vid_sec)

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
        sb_lay.addWidget(self.nav_yt)
        sb_lay.addWidget(self.nav_wl)
        sb_lay.addWidget(self.nav_history)

        mus_sec = QLabel("MÚSICA")
        mus_sec.setObjectName("SectionHeader")
        sb_lay.addWidget(mus_sec)

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
        self.nav_queue = self._make_nav_btn(
            "Cola",
            lambda: self._switch_page(self.PAGE_QUEUE),
            tooltip="Cola de reproducción de música",
        )
        sb_lay.addWidget(self.nav_ytm)
        sb_lay.addWidget(self.nav_mymusic)
        sb_lay.addWidget(self.nav_queue)

        sb_lay.addStretch()

        self.minibar_btn = QPushButton(" Mini-barra")
        self.minibar_btn.setObjectName("NavBtn")
        self.minibar_btn.setToolTip("Abrir mini-barra flotante")
        self.minibar_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.minibar_btn.clicked.connect(self.toggle_mini_bar)
        sb_lay.addWidget(self.minibar_btn)

        self.shortcuts_btn = QPushButton(" Atajos [?]")
        self.shortcuts_btn.setObjectName("NavBtn")
        self.shortcuts_btn.setToolTip("Ver todos los atajos de teclado [?]")
        self.shortcuts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.shortcuts_btn.clicked.connect(self.open_shortcuts_dialog)
        sb_lay.addWidget(self.shortcuts_btn)

        self.settings_btn = QPushButton(" Ajustes")
        self.settings_btn.setObjectName("NavBtn")
        self.settings_btn.setToolTip("Configuración de reproductor dual, descargas y cuenta")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(lambda: self.open_settings(account_tab=False))
        sb_lay.addWidget(self.settings_btn)

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

        self.search_btn = QPushButton(" Buscar")
        self.search_btn.setObjectName("AccentBtn")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.clicked.connect(self._on_search_submitted)
        top_lay.addWidget(self.search_btn)

        self.back_to_video_btn = QPushButton("Volver al video")
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

        # Pág 0: YouTube Feed (sin Shorts) + Búsqueda
        self.video_back_feed_btn: QPushButton | None = None
        self.video_refresh_btn: QPushButton | None = None
        (
            self.video_search_page,
            self.video_results_lay,
            self.video_empty_lbl,
            self.video_view_title,
            video_hdr_btns,
        ) = self._make_scroll_page(
            "YouTube — Tu Feed (sin Shorts)",
            "Cargando recomendaciones sin Shorts...",
            header_actions=[
                ("Volver al feed", self._show_video_feed_view, False),
                ("Actualizar feed", self._load_video_feed, False),
            ],
            return_header=True,
        )
        self.video_back_feed_btn = video_hdr_btns[0]
        self.video_back_feed_btn.setVisible(False)
        self.video_refresh_btn = video_hdr_btns[1]
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

        # Pág 2: Ver más tarde (local)
        self.wl_page, self.wl_lay, self.wl_empty_lbl = self._make_scroll_page(
            "Ver más tarde (Local)",
            "Aún no tienes videos guardados. Pulsa el marcador en cualquier video para añadirlo.",
        )
        self.stack.addWidget(self.wl_page)

        # Pág 3: YT Music Inicio (2-3 secciones) + Búsqueda
        self.music_back_home_btn: QPushButton | None = None
        self.music_refresh_btn: QPushButton | None = None
        (
            self.music_search_page,
            self.music_results_lay,
            self.music_empty_lbl,
            self.music_view_title,
            music_hdr_btns,
        ) = self._make_scroll_page(
            "YouTube Music — Inicio",
            "Cargando secciones de música...",
            header_actions=[
                ("Volver al inicio", self._show_music_home_view, False),
                ("Actualizar", self._load_music_home, False),
            ],
            return_header=True,
        )
        self.music_back_home_btn = music_hdr_btns[0]
        self.music_back_home_btn.setVisible(False)
        self.music_refresh_btn = music_hdr_btns[1]
        self.stack.addWidget(self.music_search_page)

        # Pág 4: Mi música (local)
        self.my_music_page, self.my_music_lay, self.my_music_empty_lbl = self._make_scroll_page(
            "Mi música (Local)",
            "Aún no tienes canciones guardadas. Pulsa el corazón en cualquier canción para guardarla.",
            header_actions=[
                ("Reproducir todo", self._play_all_my_music, True),
                ("Aleatorio", self._shuffle_my_music, False),
            ],
        )
        self.stack.addWidget(self.my_music_page)

        # Pág 5: Cola / Autoplay
        self.autoplay_btn_ref: QPushButton | None = None
        self.queue_page, self.queue_lay, self.queue_empty_lbl = self._make_scroll_page(
            "Cola de reproducción",
            "La cola está vacía. Al reproducir una canción se genera automáticamente la radio.",
            header_actions=[
                (
                    "Autoplay: ON" if self.cfg.autoplay_music else "Autoplay: OFF",
                    self._toggle_autoplay,
                    False,
                ),
                ("Limpiar cola", self._clear_queue, False),
            ],
        )
        self.stack.addWidget(self.queue_page)

        # Pág 6: Historial local con recuperación de progreso exacto
        self.history_page, self.history_lay, self.history_empty_lbl = self._make_scroll_page(
            "Historial (Local)",
            "Aún no hay videos en tu historial. Los videos que veas aparecerán aquí con el minuto exacto donde los dejaste.",
            header_actions=[("Limpiar historial", self._clear_video_history, False)],
        )
        self.stack.addWidget(self.history_page)

        right_col.addWidget(self.stack, 1)
        body_row.addLayout(right_col, 1)
        outer.addLayout(body_row, 1)

        # --- Barra inferior inteligente (fija en ventana; click redirige al contenido) ---
        self.bottom_bar = self._build_bottom_bar()
        self.bottom_bar.setVisible(False)
        outer.addWidget(self.bottom_bar)

        # Toast flotante no intrusivo
        self.toast_lbl = QLabel("", self)
        self.toast_lbl.setObjectName("ToastLabel")
        self.toast_lbl.setVisible(False)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast_lbl.setVisible(False))

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

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
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
        if return_header:
            return page, items_lay, empty_lbl, title_lbl, created_btns
        return page, items_lay, empty_lbl

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

        self.bb_thumb = QLabel()
        self.bb_thumb.setFixedSize(48, 48)
        self.bb_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        click_lay.addWidget(self.bb_thumb)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        title_line = QHBoxLayout()
        title_line.setSpacing(6)
        self.bb_deck_tag = QLabel("MÚSICA")
        self.bb_deck_tag.setObjectName("DeckTag")
        title_line.addWidget(self.bb_deck_tag)

        self.bb_title = QLabel("Sin reproducción")
        self.bb_title.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.bb_title.setMaximumWidth(210)
        title_line.addWidget(self.bb_title, 1)
        info_col.addLayout(title_line)

        self.bb_author = QLabel("")
        self.bb_author.setObjectName("MutedLabel")
        self.bb_author.setMaximumWidth(250)
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
        self.bb_switch_btn.setToolTip("Alternar entre Música y Video [Tab]")
        self.bb_switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bb_switch_btn.clicked.connect(self.switch_music_video_tab)
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

        self.bb_vol = ClickableSlider(Qt.Orientation.Horizontal)
        self.bb_vol.setRange(0, 100)
        self.bb_vol.setValue(self.cfg.music_volume)
        self.bb_vol.setFixedWidth(84)
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
        vp.wheel_scrolled.connect(self._on_video_wheel_volume)
        vp.mouse_moved.connect(self._on_video_mouse_moved)
        vp.double_clicked.connect(self.toggle_fullscreen)

        # Music deck
        mp = self.music_player
        mp.position_changed.connect(self._on_music_pos_changed)
        mp.duration_changed.connect(self._on_music_dur_changed)
        mp.paused_changed.connect(self._on_music_paused_changed)
        mp.track_ended.connect(self._on_music_ended)
        mp.error_occurred.connect(lambda m: self.show_toast(m))

    def _connect_video_panel_signals(self) -> None:
        p = self.video_panel
        p.back_requested.connect(lambda: self._switch_page(self.PAGE_VIDEO_SEARCH))
        p.play_pause_requested.connect(lambda: self.toggle_deck_play_pause("video"))
        p.seek_requested.connect(self.video_player.seek)
        p.volume_changed.connect(self._on_video_volume_changed)
        p.speed_changed.connect(self._on_video_speed_changed)
        p.quality_changed.connect(self._on_video_quality_changed)
        p.audio_only_toggled.connect(self._on_audio_only_toggled)
        p.watch_later_toggled.connect(self._toggle_current_video_saved)
        p.download_requested.connect(self._download_current_video)
        p.fullscreen_toggled.connect(self.toggle_fullscreen)
        p.theater_toggled.connect(self.toggle_sidebar)
        p.wheel_scrolled.connect(self._on_video_wheel_volume)
        p.video_surface.mouse_moved.connect(self._on_video_mouse_moved)

    def _connect_fs_overlay_signals(self) -> None:
        ov = self.fs_overlay
        ov.play_pause_requested.connect(lambda: self.toggle_deck_play_pause("video"))
        ov.seek_requested.connect(self.video_player.seek)
        ov.volume_changed.connect(self._on_video_volume_changed)
        ov.speed_changed.connect(self._on_video_speed_changed)
        ov.quality_changed.connect(self._on_video_quality_changed)
        ov.audio_only_toggled.connect(self._on_audio_only_toggled)
        ov.watch_later_toggled.connect(self._toggle_current_video_saved)
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
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _shortcut_space(self) -> None:
        if not self._typing_in_input():
            self.toggle_play_pause()

    def _shortcut_fullscreen(self) -> None:
        if not self._typing_in_input() and self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER:
            self.toggle_fullscreen()

    def _shortcut_theater(self) -> None:
        if not self._typing_in_input():
            self.toggle_sidebar()

    def _shortcut_escape(self) -> None:
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

    def _shortcut_seek(self, delta: float) -> None:
        if not self._typing_in_input():
            deck = self._get_effective_deck()
            if deck == "video" and self._video_item is not None:
                self.video_player.seek_relative(delta)
            elif deck == "music" and self._music_item is not None:
                self.music_player.seek_relative(delta)

    def _setup_media_keys(self) -> None:
        self._media_filter = GlobalMediaKeyFilter(self)
        self._media_filter.play_pause_pressed.connect(self.toggle_play_pause)
        self._media_filter.next_pressed.connect(self.play_next)
        self._media_filter.prev_pressed.connect(self.play_previous)
        self._media_filter.stop_pressed.connect(self._stop_all)
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._media_filter)
        self._media_filter.register(int(self.winId()))

    def _stop_all(self) -> None:
        self._save_current_video_progress(force=True)
        self.video_player.stop()
        self.music_player.stop()
        trim_process_memory()

    def open_shortcuts_dialog(self) -> None:
        dlg = ShortcutsDialog(self.palette, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Tema Unificado (Obsidian Graphite & Matte Silver)
    # ------------------------------------------------------------------

    def apply_theme(self, theme_key: str = "matte_silver") -> None:
        self.cfg.theme = "matte_silver"
        save_config(self.cfg)
        self.palette = get_theme(self.cfg.theme)
        p = self.palette

        qss = build_stylesheet(self.cfg.theme)
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
        self.nav_queue.setIcon(render_icon("queue", p.text, 17))
        self.minibar_btn.setIcon(render_icon("mini_bar", p.text2, 16))
        self.shortcuts_btn.setIcon(render_icon("help", p.text2, 16))
        self.settings_btn.setIcon(render_icon("settings", p.text2, 16))
        self.search_btn.setIcon(render_icon("search", p.accent_text, 15))

        self.bb_thumb.setStyleSheet(
            f"background-color: {p.base}; border-radius: 6px; border: 1px solid {p.border};"
        )
        self.bb_prev.setIcon(render_icon("prev", p.text, 17))
        self.bb_next.setIcon(render_icon("next", p.text, 17))
        self.bb_dl.setIcon(render_icon("download", p.text2, 17))

        self.video_panel.update_theme(p)
        self.fs_overlay.update_theme(p)
        self.vol_hud.update_theme(p)
        self.mini_bar.update_theme(p)
        self._refresh_sidebar_toggle_ui()
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setIcon(render_icon("refresh", p.text, 15))
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setIcon(render_icon("refresh", p.text, 15))
        self._refresh_account_badge()
        self._sync_bottom_bar()
        for card in self._video_cards:
            card.update_theme(p)
        for row in self._music_rows:
            row.update_theme(p)

    # ------------------------------------------------------------------
    # Navegación rápida (Tab Switch) y Vistas
    # ------------------------------------------------------------------

    def switch_music_video_tab(self) -> None:
        """Alterna instantáneamente entre Video y Música manteniendo ambos estados."""
        cur = self.stack.currentIndex()
        in_video = cur in (
            self.PAGE_VIDEO_SEARCH,
            self.PAGE_VIDEO_PLAYER,
            self.PAGE_WATCH_LATER,
            self.PAGE_HISTORY,
        )
        if in_video:
            self._last_video_page = cur
            self._active_deck = "music"
            if self._music_item is not None and self._last_music_page == self.PAGE_MUSIC_SEARCH:
                target = self.PAGE_QUEUE if self._music_queue else self.PAGE_MUSIC_SEARCH
            else:
                target = self._last_music_page
            self._switch_page(target)
        else:
            self._last_music_page = cur
            self._active_deck = "video"
            if self._video_item is not None:
                target = self.PAGE_VIDEO_PLAYER
            else:
                target = self._last_video_page
            self._switch_page(target)

    def _switch_page(self, page_idx: int) -> None:
        if self._is_fullscreen and page_idx != self.PAGE_VIDEO_PLAYER:
            self.toggle_fullscreen()

        self.stack.setCurrentIndex(page_idx)

        nav_map = {
            self.PAGE_VIDEO_SEARCH: self.nav_yt,
            self.PAGE_VIDEO_PLAYER: self.nav_yt,
            self.PAGE_WATCH_LATER: self.nav_wl,
            self.PAGE_HISTORY: self.nav_history,
            self.PAGE_MUSIC_SEARCH: self.nav_ytm,
            self.PAGE_MY_MUSIC: self.nav_mymusic,
            self.PAGE_QUEUE: self.nav_queue,
        }
        active_btn = nav_map.get(page_idx)
        for btn in (
            self.nav_yt,
            self.nav_wl,
            self.nav_history,
            self.nav_ytm,
            self.nav_mymusic,
            self.nav_queue,
        ):
            btn.setProperty("active", "true" if btn is active_btn else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        if page_idx in (
            self.PAGE_VIDEO_SEARCH,
            self.PAGE_VIDEO_PLAYER,
            self.PAGE_WATCH_LATER,
            self.PAGE_HISTORY,
        ):
            self._search_mode = "video"
            self._last_video_page = page_idx
            self.search_input.setPlaceholderText("Buscar videos en YouTube... [Ctrl+L]")
        else:
            self._search_mode = "music"
            self._last_music_page = page_idx
            self.search_input.setPlaceholderText(
                "Buscar canciones o artistas en YT Music... [Ctrl+L]"
            )

        self._update_music_filter_visibility()
        self._refresh_search_completer()

        if page_idx == self.PAGE_WATCH_LATER:
            self._render_watch_later()
        elif page_idx == self.PAGE_HISTORY:
            self._render_history()
        elif page_idx == self.PAGE_MY_MUSIC:
            self._render_my_music()
        elif page_idx == self.PAGE_QUEUE:
            self._render_queue()
        elif (
            page_idx == self.PAGE_MUSIC_SEARCH
            and not self._music_home_loaded
            and not self._music_showing_search
        ):
            self._load_music_home()

        self.back_to_video_btn.setVisible(
            self._video_item is not None and page_idx != self.PAGE_VIDEO_PLAYER
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()

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
            self.video_view_title.setText(f"Resultados en YouTube: '{query}'")
            if self.video_back_feed_btn is not None:
                self.video_back_feed_btn.setVisible(True)
            self._switch_page(self.PAGE_VIDEO_SEARCH)
            self.video_empty_lbl.setText(f"Buscando '{query}' en YouTube...")
            self.video_empty_lbl.setVisible(True)
            self._clear_layout_widgets(self.video_results_lay, keep_widget=self.video_empty_lbl)
            self._video_cards.clear()
            self._run_async(
                lambda: self.yt.search_videos(query, limit=16),
                on_done=lambda res: self._on_video_search_done(gen, query, res),
                on_error=lambda err: self._on_search_error(gen, "video", err),
            )
        else:
            self._music_showing_search = True
            self.music_view_title.setText(f"Resultados en YT Music: '{query}'")
            if self.music_back_home_btn is not None:
                self.music_back_home_btn.setVisible(True)
            self._switch_page(self.PAGE_MUSIC_SEARCH)
            self.music_empty_lbl.setText(f"Buscando '{query}' en YouTube Music...")
            self.music_empty_lbl.setVisible(True)
            self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
            self._music_rows.clear()
            flt = self._music_filter
            self._run_async(
                lambda: self.yt.search_music(query, search_filter=flt, limit=20),
                on_done=lambda res: self._on_music_search_done(gen, query, res),
                on_error=lambda err: self._on_search_error(gen, "music", err),
            )

    def _show_video_feed_view(self) -> None:
        self._video_showing_search = False
        self.video_view_title.setText("YouTube — Tu Feed (sin Shorts)")
        if self.video_back_feed_btn is not None:
            self.video_back_feed_btn.setVisible(False)
        self._load_video_feed()

    def _load_video_feed(self) -> None:
        self._video_showing_search = False
        self.video_view_title.setText("YouTube — Tu Feed (sin Shorts)")
        if self.video_back_feed_btn is not None:
            self.video_back_feed_btn.setVisible(False)
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setEnabled(False)
        self.video_empty_lbl.setText("Cargando feed recomendado sin Shorts...")
        self.video_empty_lbl.setVisible(True)
        self._clear_layout_widgets(self.video_results_lay, keep_widget=self.video_empty_lbl)
        self._video_cards.clear()

        self._video_feed_gen += 1
        gen = self._video_feed_gen
        fallback_q = self.db.get_search_history("video")
        self._run_async(
            lambda: self.yt.get_video_home_feed(limit=18, fallback_queries=fallback_q),
            on_done=lambda items: self._on_video_feed_done(gen, items),
            on_error=lambda err: self._on_video_feed_error(gen, err),
        )

    def _on_video_feed_done(self, gen: int, items: list[MediaItem]) -> None:
        if gen != self._video_feed_gen:
            return
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setEnabled(True)
        if self._video_showing_search:
            return
        self._clear_layout_widgets(self.video_results_lay, keep_widget=self.video_empty_lbl)
        self._video_cards.clear()
        if not items:
            self.video_empty_lbl.setText(
                "No se pudo cargar el feed ahora. Prueba buscar arriba o inicia sesión en Ajustes."
            )
            self.video_empty_lbl.setVisible(True)
            return
        self.video_empty_lbl.setVisible(False)
        for item in items:
            card = self._create_video_card(item)
            self._video_cards.append(card)
            self.video_results_lay.insertWidget(self.video_results_lay.count() - 1, card)

    def _on_video_feed_error(self, gen: int, err: str) -> None:
        if gen != self._video_feed_gen:
            return
        if self.video_refresh_btn is not None:
            self.video_refresh_btn.setEnabled(True)
        if not self._video_showing_search:
            self.video_empty_lbl.setText(f"No se pudo cargar el feed: {err}")
            self.video_empty_lbl.setVisible(True)

    def _show_music_home_view(self) -> None:
        self._music_showing_search = False
        self.music_view_title.setText("YouTube Music — Inicio")
        if self.music_back_home_btn is not None:
            self.music_back_home_btn.setVisible(False)
        self._load_music_home()

    def _load_music_home(self) -> None:
        self._music_showing_search = False
        self.music_view_title.setText("YouTube Music — Inicio")
        if self.music_back_home_btn is not None:
            self.music_back_home_btn.setVisible(False)
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setEnabled(False)
        self.music_empty_lbl.setText("Cargando 'Volver a escuchar' y recomendaciones...")
        self.music_empty_lbl.setVisible(True)
        self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
        self._music_rows.clear()

        self._music_home_gen += 1
        gen = self._music_home_gen
        local_recent = self.db.list_listen_again(kind="music", limit=8)
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
        self._music_home_loaded = True
        if self._music_showing_search:
            return
        self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
        self._music_rows.clear()
        if not sections:
            self.music_empty_lbl.setText(
                "Escucha una canción o inicia sesión con Google para ver tus recomendaciones aquí."
            )
            self.music_empty_lbl.setVisible(True)
            return
        self.music_empty_lbl.setVisible(False)
        for sec in sections:
            hdr = SectionTitleWidget(sec.title)
            self.music_results_lay.insertWidget(self.music_results_lay.count() - 1, hdr)
            for item in sec.items:
                row = self._create_music_row(item, show_enqueue=True, show_remove=False)
                self._music_rows.append(row)
                self.music_results_lay.insertWidget(self.music_results_lay.count() - 1, row)

    def _on_music_home_error(self, gen: int, err: str) -> None:
        if gen != self._music_home_gen:
            return
        if self.music_refresh_btn is not None:
            self.music_refresh_btn.setEnabled(True)
        if not self._music_showing_search:
            self.music_empty_lbl.setText(f"No se pudo cargar inicio musical: {err}")
            self.music_empty_lbl.setVisible(True)

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
                    self.account_btn.setIcon(render_icon("user", self.palette.accent_text, 16))
            else:
                self.account_btn.setIcon(render_icon("user", self.palette.accent_text, 16))
        else:
            self._account_avatar_url = ""
            self.account_btn.setText(" Acceder")
            self.account_btn.setToolTip("Iniciar sesión con Google (DPAPI)")
            self.account_btn.setProperty("active", "false")
            self.account_btn.setIcon(render_icon("user", self.palette.text, 16))

        self.account_btn.style().unpolish(self.account_btn)
        self.account_btn.style().polish(self.account_btn)

    def _on_account_avatar_ready(self, url: str, pix: QPixmap) -> None:
        if self._account_avatar_url and url == self._account_avatar_url:
            self.account_btn.setIcon(QIcon(pix))

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
        self._video_results = items
        self._clear_layout_widgets(self.video_results_lay, keep_widget=self.video_empty_lbl)
        self._video_cards.clear()
        if not items:
            self.video_empty_lbl.setText(f"No se encontraron videos para '{query}'.")
            self.video_empty_lbl.setVisible(True)
            return
        self.video_empty_lbl.setVisible(False)
        for item in items:
            card = self._create_video_card(item)
            self._video_cards.append(card)
            self.video_results_lay.insertWidget(self.video_results_lay.count() - 1, card)

    def _on_music_search_done(self, gen: int, query: str, items: list[MediaItem]) -> None:
        if gen != self._search_gen:
            return
        self._music_results = items
        self._clear_layout_widgets(self.music_results_lay, keep_widget=self.music_empty_lbl)
        self._music_rows.clear()
        if not items:
            self.music_empty_lbl.setText(f"No se encontraron canciones para '{query}'.")
            self.music_empty_lbl.setVisible(True)
            return
        self.music_empty_lbl.setVisible(False)
        for item in items:
            row = self._create_music_row(item, show_enqueue=True, show_remove=False)
            self._music_rows.append(row)
            self.music_results_lay.insertWidget(self.music_results_lay.count() - 1, row)

    def _on_search_error(self, gen: int, mode: str, err: str) -> None:
        if gen != self._search_gen:
            return
        if mode == "video":
            self.video_empty_lbl.setText(f"Error al buscar: {err}")
            self.video_empty_lbl.setVisible(True)
        else:
            self.music_empty_lbl.setText(f"Error al buscar: {err}")
            self.music_empty_lbl.setVisible(True)

    # ------------------------------------------------------------------
    # Tarjetas y Listas Locales
    # ------------------------------------------------------------------

    def _create_video_card(
        self, item: MediaItem, show_remove: bool = False
    ) -> VideoCardWidget:
        card = VideoCardWidget(
            item=item,
            in_watch_later=self.db.is_in_watch_later(item.video_id),
            thumb_cache=self.thumb_cache,
            palette=self.palette,
            show_remove=show_remove,
        )
        card.play_requested.connect(self.play_video)
        card.watch_later_toggled.connect(self._toggle_watch_later_item)
        card.download_requested.connect(lambda it: self._download_item(it, "video"))
        if show_remove:
            card.remove_requested.connect(self._remove_history_item)
        return card

    def _create_music_row(
        self,
        item: MediaItem,
        show_enqueue: bool = True,
        show_remove: bool = False,
        on_play: Callable[[MediaItem], None] | None = None,
    ) -> MusicRowWidget:
        is_cur = self._music_item is not None and self._music_item.video_id == item.video_id
        row = MusicRowWidget(
            item=item,
            in_my_music=self.db.is_in_my_music(item.video_id),
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
            row.remove_requested.connect(self._remove_from_queue)
        return row

    def _clear_layout_widgets(self, lay: QVBoxLayout, keep_widget: QWidget) -> None:
        for i in reversed(range(lay.count())):
            item = lay.itemAt(i)
            w = item.widget() if item else None
            if w is not None and w is not keep_widget:
                lay.removeWidget(w)
                w.deleteLater()

    def _render_watch_later(self) -> None:
        self._clear_layout_widgets(self.wl_lay, keep_widget=self.wl_empty_lbl)
        items = self.db.list_watch_later()
        self.wl_empty_lbl.setVisible(len(items) == 0)
        for item in items:
            card = self._create_video_card(item)
            self.wl_lay.insertWidget(self.wl_lay.count() - 1, card)

    def _render_history(self) -> None:
        self._clear_layout_widgets(self.history_lay, keep_widget=self.history_empty_lbl)
        items = self.db.list_video_history(limit=50)
        self.history_empty_lbl.setVisible(len(items) == 0)
        for item in items:
            card = self._create_video_card(item, show_remove=True)
            self.history_lay.insertWidget(self.history_lay.count() - 1, card)

    def _remove_history_item(self, item: MediaItem) -> None:
        self.db.remove_from_history(item.video_id)
        if self._video_item and self._video_item.video_id == item.video_id:
            self._last_saved_video_pos = 0.0
        self.show_toast("Quitado del historial")
        if self.stack.currentIndex() == self.PAGE_HISTORY:
            self._render_history()

    def _clear_video_history(self) -> None:
        self.db.clear_video_history()
        self._last_saved_video_pos = 0.0
        self.show_toast("Historial local limpiado")
        if self.stack.currentIndex() == self.PAGE_HISTORY:
            self._render_history()

    def _render_my_music(self) -> None:
        self._clear_layout_widgets(self.my_music_lay, keep_widget=self.my_music_empty_lbl)
        items = self.db.list_my_music()
        self.my_music_empty_lbl.setVisible(len(items) == 0)
        for idx, item in enumerate(items):
            row = self._create_music_row(
                item,
                show_enqueue=True,
                show_remove=False,
                on_play=lambda _it, i=idx, lst=items: self._play_list_from(lst, i),
            )
            self.my_music_lay.insertWidget(self.my_music_lay.count() - 1, row)

    def _render_queue(self) -> None:
        self._clear_layout_widgets(self.queue_lay, keep_widget=self.queue_empty_lbl)
        self.queue_empty_lbl.setVisible(len(self._music_queue) == 0)
        for idx, item in enumerate(self._music_queue):
            row = self._create_music_row(
                item,
                show_enqueue=False,
                show_remove=True,
                on_play=lambda _it, i=idx: self._play_queue_index(i),
            )
            row.set_current(idx == self._queue_index)
            self.queue_lay.insertWidget(self.queue_lay.count() - 1, row)

    def _refresh_badges(self) -> None:
        wl_n = self.db.count_watch_later()
        mm_n = self.db.count_my_music()
        q_n = len(self._music_queue)
        self.nav_wl.setText(f"  Ver más tarde ({wl_n})" if wl_n else "  Ver más tarde")
        self.nav_mymusic.setText(f"  Mi música ({mm_n})" if mm_n else "  Mi música")
        self.nav_queue.setText(f"  Cola ({q_n})" if q_n else "  Cola")

    def _toggle_watch_later_item(self, item: MediaItem) -> None:
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
        if self._video_item and self._video_item.video_id == item.video_id:
            self.video_panel.set_watch_later(saved)
            self.fs_overlay.set_watch_later(saved)
            self._refresh_bb_fav_icon()
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
        if self._music_item and self._music_item.video_id == item.video_id:
            self._refresh_bb_fav_icon()
        if self.stack.currentIndex() == self.PAGE_MY_MUSIC:
            self._render_my_music()
        if self.stack.currentIndex() == self.PAGE_QUEUE:
            self._render_queue()
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
        if self._video_item is None:
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

    def play_video(self, item: MediaItem, start_pos: float | None = None) -> None:
        self._save_current_video_progress(force=True)
        self._pause_other_if_auto("video")

        if start_pos is None:
            saved_pos = float(getattr(item, "resume_pos", 0.0) or 0.0)
            if saved_pos <= 0.0:
                saved_pos = self.db.get_play_position(item.video_id)
            effective_start = saved_pos if saved_pos >= 3.0 else 0.0
        else:
            effective_start = max(0.0, float(start_pos))

        same_item = self._video_item is item
        self._active_deck = "video"
        self._video_item = item
        self._video_pos = effective_start
        self._video_dur = (
            max(self._video_dur, float(item.duration_sec or 0))
            if same_item
            else float(item.duration_sec or 0)
        )
        self._last_saved_video_pos = effective_start
        self._last_saved_video_ts = time.monotonic()

        self.db.record_play(item)
        self.video_player.set_speed(self.cfg.video_speed)
        self._switch_page(self.PAGE_VIDEO_PLAYER)

        wl_saved = self.db.is_in_watch_later(item.video_id)
        self.video_panel.set_item(item, wl_saved)
        if self._video_dur > 0:
            self.video_panel.set_duration(self._video_dur)
            self.fs_overlay.set_duration(self._video_dur)
            self.video_panel.set_position(effective_start)
            self.fs_overlay.set_position(effective_start)
        self.video_panel.set_status("Cargando stream...")
        self.video_panel.set_audio_only_visual(self.cfg.video_audio_only)
        self.fs_overlay.set_item(item, wl_saved)
        self.fs_overlay.set_audio_only_silent(self.cfg.video_audio_only)
        self._sync_mini_bar()

        if start_pos is None and effective_start >= 3.0:
            res_lbl = format_duration(effective_start)
            if res_lbl:
                self.show_toast(f"Continuando desde {res_lbl}")

        self._video_play_gen += 1
        gen = self._video_play_gen
        audio_only = self.cfg.video_audio_only
        quality = self.cfg.video_quality
        self._run_async(
            lambda: self.yt.resolve_stream(item.video_id, audio_only=audio_only, quality=quality),
            on_done=lambda stream: self._on_video_stream_resolved(gen, stream, effective_start),
            on_error=lambda err: self._on_video_play_error(gen, err),
        )

    def _on_video_stream_resolved(
        self, gen: int, stream: ResolvedStream, start_pos: float
    ) -> None:
        if gen != self._video_play_gen:
            return
        self.video_panel.set_status("")
        if stream.duration > 0:
            self._on_video_dur_changed(stream.duration)
        self.video_player.play_stream(
            primary_url=stream.primary_url,
            audio_url=stream.audio_url,
            video_enabled=stream.video_enabled,
            http_headers=stream.http_headers,
            start_pos=start_pos,
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()

    def _on_video_play_error(self, gen: int, err: str) -> None:
        if gen != self._video_play_gen:
            return
        self.video_panel.set_status("Error")
        self.show_toast(f"No se pudo reproducir el video: {err}")

    def _play_music_from_search(self, item: MediaItem) -> None:
        self._music_queue = [item]
        self._queue_index = 0
        self._refresh_badges()
        self._play_music_item(item)

        if self.cfg.autoplay_music:
            vid = item.video_id
            self._run_async(
                lambda: self.yt.get_music_radio_queue(vid, limit=18),
                on_done=lambda tracks: self._append_radio_tracks(vid, tracks),
                on_error=lambda _err: None,
            )

    def _append_radio_tracks(self, seed_vid: str, tracks: list[MediaItem]) -> None:
        if not self._music_queue or self._music_queue[0].video_id != seed_vid:
            return
        existing = {it.video_id for it in self._music_queue}
        added = 0
        for t in tracks:
            if t.video_id not in existing:
                existing.add(t.video_id)
                self._music_queue.append(t)
                added += 1
        if added:
            self._refresh_badges()
            if self.stack.currentIndex() == self.PAGE_QUEUE:
                self._render_queue()

    def _play_list_from(self, items: list[MediaItem], index: int) -> None:
        if not items:
            return
        self._music_queue = list(items)
        self._queue_index = max(0, min(index, len(items) - 1))
        self._refresh_badges()
        self._play_music_item(self._music_queue[self._queue_index])

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
            if self.stack.currentIndex() == self.PAGE_QUEUE:
                self._render_queue()

    def _enqueue_music_item(self, item: MediaItem) -> None:
        self._music_queue.append(item)
        self._refresh_badges()
        self.show_toast(f"Agregado a la cola: {item.title}")
        if self.stack.currentIndex() == self.PAGE_QUEUE:
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
        self._render_queue()

    def _clear_queue(self) -> None:
        self._music_queue.clear()
        self._queue_index = -1
        self._refresh_badges()
        self._render_queue()

    def _toggle_autoplay(self) -> None:
        self.cfg.autoplay_music = not self.cfg.autoplay_music
        save_config(self.cfg)
        if self.autoplay_btn_ref is not None:
            self.autoplay_btn_ref.setText(
                "Autoplay: ON" if self.cfg.autoplay_music else "Autoplay: OFF"
            )

    def _play_music_item(self, item: MediaItem) -> None:
        self._pause_other_if_auto("music")
        self._active_deck = "music"
        self._music_item = item
        self._music_pos = 0.0
        self.db.record_play(item)
        self.music_player.set_speed(1.0)
        self._sync_bottom_bar()
        self._sync_mini_bar()
        self._update_bottom_bar_visibility()

        for row in self._music_rows:
            row.set_current(row.item.video_id == item.video_id)
        if self.stack.currentIndex() == self.PAGE_QUEUE:
            self._render_queue()

        self._music_play_gen += 1
        gen = self._music_play_gen
        self._run_async(
            lambda: self.yt.resolve_stream(item.video_id, audio_only=True),
            on_done=lambda stream: self._on_music_stream_resolved(gen, stream),
            on_error=lambda err: self._on_music_play_error(gen, err),
        )

    def _on_music_stream_resolved(self, gen: int, stream: ResolvedStream) -> None:
        if gen != self._music_play_gen:
            return
        if stream.duration > 0:
            self._on_music_dur_changed(stream.duration)
        self.music_player.play_stream(
            primary_url=stream.primary_url,
            audio_url=None,
            video_enabled=False,
            http_headers=stream.http_headers,
            start_pos=0.0,
        )
        self._sync_bottom_bar()
        self._update_bottom_bar_visibility()

    def _on_music_play_error(self, gen: int, err: str) -> None:
        if gen != self._music_play_gen:
            return
        self.show_toast(f"No se pudo reproducir la canción: {err}")

    def _get_effective_deck(self) -> Literal["video", "music"]:
        cur = self.stack.currentIndex()
        if cur == self.PAGE_VIDEO_PLAYER and self._video_item is not None:
            return "video"
        if cur in (self.PAGE_MUSIC_SEARCH, self.PAGE_MY_MUSIC, self.PAGE_QUEUE):
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
            if self._video_paused:
                self._pause_other_if_auto("video")
                self._active_deck = "video"
            self.video_player.toggle_pause()
        else:
            if self._music_item is None:
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
        if self._video_item is not None:
            self.db.update_play_progress(
                self._video_item.video_id, 0.0, self._video_dur
            )
            self._last_saved_video_pos = 0.0
            self._video_pos = 0.0
        self.video_panel.set_paused(True)
        self.fs_overlay.set_paused(True)
        self._sync_bottom_bar()
        self._sync_mini_bar()

    def _on_music_ended(self) -> None:
        if self._queue_index + 1 < len(self._music_queue):
            self._play_queue_index(self._queue_index + 1)
        else:
            self._music_paused = True
            self._sync_bottom_bar()
            self._sync_mini_bar()

    # ------------------------------------------------------------------
    # Sincronización de ambos decks con UI, Barra Inferior, Overlay y Mini-barra
    # ------------------------------------------------------------------

    def _on_video_pos_changed(self, pos: float) -> None:
        self._video_pos = pos
        self.video_panel.set_position(pos)
        self.fs_overlay.set_position(pos)
        if self._get_bottom_bar_deck() == "video":
            self._update_bb_position(pos, self._video_dur)
        if self._get_minibar_deck() == "video":
            self.mini_bar.set_position(pos)
        self._save_current_video_progress(force=False)

    def _on_video_dur_changed(self, dur: float) -> None:
        self._video_dur = max(0.0, dur)
        self.video_panel.set_duration(self._video_dur)
        self.fs_overlay.set_duration(self._video_dur)
        if self._get_bottom_bar_deck() == "video":
            self.bb_dur.setText(format_duration(self._video_dur) or "0:00")
        if self._get_minibar_deck() == "video":
            self.mini_bar.set_duration(self._video_dur)
        if (
            self._video_item is not None
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
            self._active_deck = "video"
        else:
            self._save_current_video_progress(force=True)
        self._sync_bottom_bar()
        self._sync_mini_bar()

    def _on_video_buffering_changed(self, buffering: bool) -> None:
        self.video_panel.set_status("Buffering..." if buffering else "")

    def _on_music_pos_changed(self, pos: float) -> None:
        self._music_pos = pos
        if self._get_bottom_bar_deck() == "music":
            self._update_bb_position(pos, self._music_dur)
        if self._get_minibar_deck() == "music":
            self.mini_bar.set_position(pos)

    def _on_music_dur_changed(self, dur: float) -> None:
        self._music_dur = max(0.0, dur)
        if self._get_bottom_bar_deck() == "music":
            self.bb_dur.setText(format_duration(self._music_dur) or "0:00")
        if self._get_minibar_deck() == "music":
            self.mini_bar.set_duration(self._music_dur)

    def _on_music_paused_changed(self, paused: bool) -> None:
        self._music_paused = paused
        if not paused:
            self._active_deck = "music"
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

    def _on_video_wheel_volume(self, step: int) -> None:
        new_vol = max(0, min(100, self.cfg.video_volume + int(step) * 5))
        self._on_video_volume_changed(new_vol)
        self._show_volume_hud(new_vol)

    def _show_volume_hud(self, percent: int) -> None:
        if self._is_fullscreen:
            scr = self.screen() or QApplication.primaryScreen()
            anchor = scr.geometry() if scr else self.geometry()
        else:
            ms = self.video_panel.media_stack
            top_left = ms.mapToGlobal(QPoint(0, 0))
            anchor = QRect(top_left, ms.size())
        self.vol_hud.show_volume(percent, anchor)

    def _on_video_mouse_moved(self) -> None:
        if self._is_fullscreen:
            scr = self.screen() or QApplication.primaryScreen()
            rect = scr.geometry() if scr else self.geometry()
            self.fs_overlay.reveal(rect)

    def _on_video_speed_changed(self, speed: float) -> None:
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
            self.play_video(self._video_item, start_pos=self._video_pos)

    def _on_audio_only_toggled(self, audio_only: bool) -> None:
        self.cfg.video_audio_only = audio_only
        save_config(self.cfg)
        self.video_panel.set_audio_only_visual(audio_only)
        self.fs_overlay.set_audio_only_silent(audio_only)
        if self._video_item:
            self.play_video(self._video_item, start_pos=self._video_pos)

    # ------------------------------------------------------------------
    # Barra Inferior Inteligente (click -> vista completa del deck)
    # ------------------------------------------------------------------

    def _get_bottom_bar_deck(self) -> Literal["video", "music"] | None:
        cur = self.stack.currentIndex()
        if cur in (self.PAGE_MUSIC_SEARCH, self.PAGE_MY_MUSIC, self.PAGE_QUEUE):
            if self._music_item is not None:
                return "music"
            if self._video_item is not None:
                return "video"
            return None

        # En páginas de Video (Feed / Ver más tarde): priorizar el deck activo sonando
        if self._music_item is not None and not self._music_paused:
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
        elif deck == "music" and self._music_item is not None:
            self._active_deck = "music"
            target = self.PAGE_QUEUE if self._music_queue else self.PAGE_MUSIC_SEARCH
            self._switch_page(target)

    def _on_bb_play_pause(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck is not None:
            self.toggle_deck_play_pause(deck)

    def _on_bb_prev(self) -> None:
        if self._get_bottom_bar_deck() == "music":
            self.play_previous()
        elif self._video_item is not None:
            self.video_player.seek_relative(-10.0)

    def _on_bb_next(self) -> None:
        if self._get_bottom_bar_deck() == "music":
            self.play_next()
        elif self._video_item is not None:
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
            self._download_item(self._video_item, "video")
        elif deck == "music" and self._music_item is not None:
            self._download_item(self._music_item, "music")

    def _update_bb_position(self, pos: float, dur: float) -> None:
        if not self._seeking_bottom and dur > 0:
            ratio = max(0.0, min(1.0, pos / dur))
            self.bb_seek.blockSignals(True)
            self.bb_seek.setValue(int(ratio * 1000))
            self.bb_seek.blockSignals(False)
            self.bb_pos.setText(format_duration(pos) or "0:00")

    def _sync_bottom_bar(self) -> None:
        deck = self._get_bottom_bar_deck()
        if deck is None:
            self.bb_title.setText("Sin reproducción")
            self.bb_author.setText("")
            self.bb_thumb.clear()
            self.bb_switch_btn.setVisible(False)
            return

        it = self._video_item if deck == "video" else self._music_item
        paused = self._video_paused if deck == "video" else self._music_paused
        pos = self._video_pos if deck == "video" else self._music_pos
        dur = self._video_dur if deck == "video" else self._music_dur
        vol = self.cfg.video_volume if deck == "video" else self.cfg.music_volume

        self.bb_deck_tag.setText("VIDEO" if deck == "video" else "MÚSICA")
        if it is not None:
            fm_t = self.bb_title.fontMetrics()
            self.bb_title.setText(fm_t.elidedText(it.title, Qt.TextElideMode.ElideRight, 205))
            fm_a = self.bb_author.fontMetrics()
            self.bb_author.setText(fm_a.elidedText(it.author or "", Qt.TextElideMode.ElideRight, 240))
            pix = self.thumb_cache.get_or_fetch(it.normalized_thumb(), QSize(48, 48))
            if pix is not None:
                self.bb_thumb.setPixmap(pix)

        self.bb_play.setIcon(
            render_icon("play" if paused else "pause", self.palette.accent_text, 18)
        )
        self.bb_dur.setText(format_duration(dur) or "0:00")
        self._update_bb_position(pos, dur)
        self.bb_vol.blockSignals(True)
        self.bb_vol.setValue(vol)
        self.bb_vol.blockSignals(False)
        self._refresh_bb_vol_icon()
        self._refresh_bb_fav_icon()

        both_loaded = self._video_item is not None and self._music_item is not None
        self.bb_switch_btn.setVisible(both_loaded)
        if both_loaded:
            self.bb_switch_btn.setText("Ir a Música" if deck == "video" else "Ir a Video")

    def _sync_mini_bar(self) -> None:
        deck = self._get_minibar_deck()
        it = self._video_item if deck == "video" else self._music_item
        paused = self._video_paused if deck == "video" else self._music_paused
        dur = self._video_dur if deck == "video" else self._music_dur
        pos = self._video_pos if deck == "video" else self._music_pos
        vol = self.cfg.video_volume if deck == "video" else self.cfg.music_volume

        self.mini_bar.set_item(it)
        self.mini_bar.set_paused(paused)
        self.mini_bar.set_duration(dur)
        self.mini_bar.set_position(pos)
        self.mini_bar.set_volume_silent(vol)

    def _on_bb_thumb_ready(self, url: str, pix: QPixmap) -> None:
        deck = self._get_bottom_bar_deck()
        it = self._video_item if deck == "video" else self._music_item
        if it is not None and url == it.normalized_thumb():
            self.bb_thumb.setPixmap(pix)

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
            self.bottom_bar.setVisible(False)
            return
        on_video_player = self.stack.currentIndex() == self.PAGE_VIDEO_PLAYER
        has_any = self._video_item is not None or self._music_item is not None
        self.bottom_bar.setVisible(has_any and not on_video_player)

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
            self.video_player.set_cursor_autohide(500)
            QTimer.singleShot(50, self._on_video_mouse_moved)
        else:
            self._is_fullscreen = False
            self.fs_overlay.hide_immediately()
            self.video_player.set_cursor_autohide("no")
            if self._pre_fs_was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
                if self._pre_fs_geometry is not None and self._pre_fs_geometry.isValid():
                    self.setGeometry(self._pre_fs_geometry)
            self.sidebar.setVisible(not self._sidebar_hidden)
            self.top_bar.setVisible(True)
            self.video_panel.set_fullscreen_state(False)
            self._update_bottom_bar_visibility()
            self._refresh_sidebar_toggle_ui()

    def toggle_mini_bar(self) -> None:
        if self.mini_bar.isVisible():
            self._restore_from_mini_bar()
        else:
            if self._is_fullscreen:
                self.toggle_fullscreen()
            self.hide()
            trim_process_memory()
            self._sync_mini_bar()
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                self.mini_bar.move(
                    geo.right() - self.mini_bar.width() - 24,
                    geo.bottom() - self.mini_bar.height() - 24,
                )
            self.mini_bar.show()
            self.mini_bar.raise_()

    def _restore_from_mini_bar(self) -> None:
        self.mini_bar.hide()
        self.restore_window()

    def _on_minibar_pin_toggled(self, pinned: bool) -> None:
        self.cfg.mini_bar_always_on_top = pinned
        save_config(self.cfg)

    def _on_normalization_changed(self, enabled: bool) -> None:
        self.video_player.set_normalization(enabled)
        self.music_player.set_normalization(enabled)

    def open_settings(self, account_tab: bool = False) -> None:
        dlg = SettingsDialog(self.cfg, self.yt, thumb_cache=self.thumb_cache, parent=self)
        if account_tab:
            dlg.select_account_tab()
        dlg.normalization_changed.connect(self._on_normalization_changed)
        dlg.account_changed.connect(self._on_account_changed)
        dlg.exec()
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
        self._inflight[tid] = task.signals

        def _cleanup_done(task_id: int, result: object) -> None:
            self._inflight.pop(task_id, None)
            on_done(result)

        def _cleanup_err(task_id: int, msg: str) -> None:
            self._inflight.pop(task_id, None)
            if on_error:
                on_error(msg)

        task.signals.done.connect(_cleanup_done)
        task.signals.error.connect(_cleanup_err)
        self.pool.start(task)

    def show_toast(self, message: str, duration_ms: int = 3500) -> None:
        self.toast_lbl.setText(message)
        self.toast_lbl.adjustSize()
        margin = 18
        y_offset = self.bottom_bar.height() + margin if self.bottom_bar.isVisible() else margin
        x = max(margin, self.width() - self.toast_lbl.width() - margin)
        y = max(margin, self.height() - self.toast_lbl.height() - y_offset)
        self.toast_lbl.move(x, y)
        self.toast_lbl.setVisible(True)
        self.toast_lbl.raise_()
        self._toast_timer.start(duration_ms)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toast_lbl.isVisible():
            margin = 18
            y_offset = self.bottom_bar.height() + margin if self.bottom_bar.isVisible() else margin
            self.toast_lbl.move(
                max(margin, self.width() - self.toast_lbl.width() - margin),
                max(margin, self.height() - self.toast_lbl.height() - y_offset),
            )

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
            if self._is_fullscreen:
                self.toggle_fullscreen()
            self.hide()
            trim_process_memory()
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

    def _cleanup_before_exit(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            self._save_current_video_progress(force=True)
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
