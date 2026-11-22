"""Diálogo de Ajustes de SwiftPlay (Reproductor Dual, Descargas, Cuenta DPAPI, Sistema)."""

from __future__ import annotations

import os
import sys
import threading
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFocusEvent, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from swiftplay.browser_auth import login_with_browser
from swiftplay.config import AppConfig, save_config
from swiftplay.media_keys import parse_hotkey_string
from swiftplay.paths import RAM_LOG_PATH
from swiftplay.ram_logger import RamLogger
from swiftplay.security import (
    clear_credentials,
    clear_discord_token,
    has_discord_token,
    has_saved_session,
    load_account_profile,
    save_cookies_encrypted,
    save_discord_token_encrypted,
)
from swiftplay.themes import apply_windows_dark_titlebar, get_theme
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.ui.shortcuts_dialog import OutsideClickDismissFilter
from swiftplay.windows_os import (
    get_process_memory_mb,
    is_startup_enabled,
    open_folder_in_explorer,
    set_startup_enabled,
)
from swiftplay.youtube_service import YouTubeService


class _WorkerSignals(QObject):
    finished = Signal(bool, str)
    progress = Signal(str)


class _UpdateMotorTask(QRunnable):
    def __init__(self, yt: YouTubeService) -> None:
        super().__init__()
        self.yt = yt
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            ok, msg = self.yt.update_yt_dlp()
            self.signals.finished.emit(ok, msg)
        except Exception as exc:
            self.signals.finished.emit(False, str(exc))


class _BrowserLoginTask(QRunnable):
    def __init__(self, yt: YouTubeService, cancel_event: threading.Event) -> None:
        super().__init__()
        self.yt = yt
        self.cancel_event = cancel_event
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            netscape = login_with_browser(
                status_cb=self.signals.progress.emit,
                cancel_event=self.cancel_event,
            )
            count = save_cookies_encrypted(netscape)
            self.yt.reload_auth()
            prof = self.yt.fetch_account_profile()
            who = f" ({prof.name})" if prof and prof.name else ""
            self.signals.finished.emit(
                True,
                f"Sesión vinculada{who}: {count} cookies de YouTube cifradas con Windows DPAPI.",
            )
        except Exception as exc:
            self.signals.finished.emit(False, str(exc))


_QT_SPECIAL_KEY_MAP: dict[int, str] = {
    int(Qt.Key.Key_Space): "Space",
    int(Qt.Key.Key_Pause): "Pause",
    int(Qt.Key.Key_Insert): "Insert",
    int(Qt.Key.Key_Delete): "Delete",
    int(Qt.Key.Key_Home): "Home",
    int(Qt.Key.Key_End): "End",
    int(Qt.Key.Key_PageUp): "PageUp",
    int(Qt.Key.Key_PageDown): "PageDown",
    int(Qt.Key.Key_ScrollLock): "ScrollLock",
    int(Qt.Key.Key_Print): "PrintScreen",
    int(Qt.Key.Key_Up): "Up",
    int(Qt.Key.Key_Down): "Down",
    int(Qt.Key.Key_Left): "Left",
    int(Qt.Key.Key_Right): "Right",
    int(Qt.Key.Key_Minus): "-",
    int(Qt.Key.Key_Equal): "=",
    int(Qt.Key.Key_BracketLeft): "[",
    int(Qt.Key.Key_BracketRight): "]",
    int(Qt.Key.Key_Semicolon): ";",
    int(Qt.Key.Key_Apostrophe): "'",
    int(Qt.Key.Key_Comma): ",",
    int(Qt.Key.Key_Period): ".",
    int(Qt.Key.Key_Slash): "/",
    int(Qt.Key.Key_QuoteLeft): "`",
    int(Qt.Key.Key_Backslash): "\\",
    int(Qt.Key.Key_Asterisk): "Numpad*",
    int(Qt.Key.Key_Plus): "Numpad+",
}


def _qt_key_to_token(key: int, modifiers: Qt.KeyboardModifier) -> str:
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        return chr(key)
    if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        if modifiers & Qt.KeyboardModifier.KeypadModifier:
            return f"Numpad{chr(key)}"
        return chr(key)
    if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        return f"F{key - int(Qt.Key.Key_F1) + 1}"
    return _QT_SPECIAL_KEY_MAP.get(key, "")


class _HotkeyCaptureEdit(QLineEdit):
    """Campo de captura interactiva para grabar atajos globales sin interferencia."""

    hotkey_changed = Signal(str)
    recording_changed = Signal(bool)
    invalid_hotkey = Signal(str)

    def __init__(self, initial: str = "", parent: QWidget | None = None) -> None:
        super().__init__(initial, parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setPlaceholderText("Sin asignar (haz clic y pulsa teclas)")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def focusInEvent(self, event: QFocusEvent) -> None:
        self.recording_changed.emit(True)
        self.setPlaceholderText("Pulsa la combinación ahora... (Esc cancela)")
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.recording_changed.emit(False)
        self.setPlaceholderText("Sin asignar (haz clic y pulsa teclas)")
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = int(event.key())
        if key in (
            int(Qt.Key.Key_Control),
            int(Qt.Key.Key_Shift),
            int(Qt.Key.Key_Alt),
            int(Qt.Key.Key_AltGr),
            int(Qt.Key.Key_Meta),
            int(Qt.Key.Key_Super_L),
            int(Qt.Key.Key_Super_R),
            int(Qt.Key.Key_unknown),
        ):
            event.accept()
            return

        mods = event.modifiers()
        clean_mods = mods & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        if key == int(Qt.Key.Key_Escape) and clean_mods == Qt.KeyboardModifier.NoModifier:
            self.clearFocus()
            event.accept()
            return

        token = _qt_key_to_token(key, mods)
        if not token:
            self.invalid_hotkey.emit("Tecla no soportada para atajo global.")
            event.accept()
            return

        parts: list[str] = []
        if clean_mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if clean_mods & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if clean_mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if clean_mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("Win")
        parts.append(token)
        combo = "+".join(parts)

        if parse_hotkey_string(combo) is None:
            self.invalid_hotkey.emit(
                "Usa Ctrl/Alt/Shift + tecla, o una tecla especial (F1–F24, Pause, etc.)."
            )
            event.accept()
            return

        self.setText(combo)
        self.hotkey_changed.emit(combo)
        self.clearFocus()
        event.accept()


class SettingsDialog(QDialog):
    """Ventana de configuración de SwiftPlay."""

    normalization_changed = Signal(bool)
    config_updated = Signal()
    account_changed = Signal()
    discord_token_changed = Signal()
    discord_hotkeys_changed = Signal()
    discord_ui_settings_changed = Signal()
    discord_overlay_settings_changed = Signal()
    discord_overlay_preview_requested = Signal(int)
    hotkey_recording_changed = Signal(bool)
    trim_memory_requested = Signal()

    def __init__(
        self,
        config: AppConfig,
        yt_service: YouTubeService,
        thumb_cache: ThumbnailCache | None = None,
        discord_channels: list[tuple[str, str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = config
        self.yt = yt_service
        self._thumb_cache = thumb_cache
        self._discord_channels: list[tuple[str, str]] = list(discord_channels or [])
        self._pool = QThreadPool(self)
        self._active_tasks: set[_WorkerSignals] = set()
        self._login_cancel: threading.Event | None = None
        self._profile_avatar_url = ""

        self.setWindowTitle("Ajustes — SwiftPlay")
        self.resize(640, 520)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_playback_downloads_tab(), "Reproducción y Descargas")
        self.tabs.addTab(self._build_account_tab(), "Cuenta (DPAPI)")
        self.tabs.addTab(self._build_system_tab(), "Sistema")
        self.tabs.addTab(self._build_discord_tab(), "Discord (DPAPI)")
        self.tabs.addTab(self._build_discord_shortcuts_tab(), "Atajos Discord")
        root.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        close_btn = QPushButton("Cerrar")
        close_btn.setObjectName("AccentBtn")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        if self._thumb_cache is not None:
            self._thumb_cache.ready.connect(self._on_avatar_ready)

        self._ram_timer = QTimer(self)
        self._ram_timer.setInterval(2000)
        self._ram_timer.timeout.connect(self._refresh_ram_label)
        self._ram_timer.start()
        self._refresh_ram_label()
        self._refresh_session_status()
        apply_windows_dark_titlebar(int(self.winId()), get_theme(self.cfg.theme).base)
        self._outside_filter = OutsideClickDismissFilter(
            self,
            should_ignore_deactivate=lambda: (
                self._login_cancel is not None and not self._login_cancel.is_set()
            ),
        )

    def select_account_tab(self) -> None:
        self.tabs.setCurrentIndex(1)

    def select_discord_tab(self) -> None:
        self.tabs.setCurrentIndex(3)

    def select_discord_shortcuts_tab(self) -> None:
        self.tabs.setCurrentIndex(4)

    # --- Tab 1: Reproducción y Descargas ---

    def _build_playback_downloads_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        dual_title = QLabel("Reproductor Dual (Música y Video en paralelo)")
        dual_title.setStyleSheet("font-size: 14px; font-weight: 700;")
        lay.addWidget(dual_title)

        self.auto_pause_check = QCheckBox(
            "Pausar automáticamente el otro reproductor (Auto-pausa inteligente)"
        )
        self.auto_pause_check.setChecked(self.cfg.auto_pause_other_player)
        self.auto_pause_check.toggled.connect(self._on_auto_pause_toggled)
        lay.addWidget(self.auto_pause_check)

        dual_hint = QLabel(
            "Activado (por defecto): al darle play a un video, la música se pausa en ese segundo exacto "
            "sin perder tu playlist (y viceversa).\n"
            "Desactivado: Música y Video pueden sonar simultáneamente."
        )
        dual_hint.setObjectName("MutedLabel")
        dual_hint.setWordWrap(True)
        lay.addWidget(dual_hint)

        audio_title = QLabel("Audio")
        audio_title.setStyleSheet("font-size: 14px; font-weight: 700; margin-top: 6px;")
        lay.addWidget(audio_title)

        self.norm_check = QCheckBox("Normalizar volumen (filtro dynaudnorm de mpv)")
        self.norm_check.setChecked(self.cfg.audio_normalization)
        self.norm_check.toggled.connect(self._on_norm_toggled)
        lay.addWidget(self.norm_check)

        norm_hint = QLabel(
            "Desactivado por defecto para mantener el audio 100% original y cero CPU extra. "
            "Al activarlo, iguala el volumen entre pistas en ambos reproductores."
        )
        norm_hint.setObjectName("MutedLabel")
        norm_hint.setWordWrap(True)
        lay.addWidget(norm_hint)

        dl_title = QLabel("Carpetas de descarga (persistentes y separadas)")
        dl_title.setStyleSheet("font-size: 14px; font-weight: 700; margin-top: 6px;")
        lay.addWidget(dl_title)

        lay.addWidget(QLabel("Carpeta para descargas de Música (.m4a):"))
        music_row = QHBoxLayout()
        self.music_dir_edit = QLineEdit(self.cfg.music_download_dir)
        self.music_dir_edit.setReadOnly(True)
        music_row.addWidget(self.music_dir_edit, 1)
        m_pick = QPushButton("Elegir...")
        m_pick.clicked.connect(self._pick_music_dir)
        music_row.addWidget(m_pick)
        m_open = QPushButton("Abrir")
        m_open.clicked.connect(lambda: open_folder_in_explorer(self.cfg.music_download_dir))
        music_row.addWidget(m_open)
        lay.addLayout(music_row)

        lay.addWidget(QLabel("Carpeta para descargas de Video (.mp4):"))
        video_row = QHBoxLayout()
        self.video_dir_edit = QLineEdit(self.cfg.video_download_dir)
        self.video_dir_edit.setReadOnly(True)
        video_row.addWidget(self.video_dir_edit, 1)
        v_pick = QPushButton("Elegir...")
        v_pick.clicked.connect(self._pick_video_dir)
        video_row.addWidget(v_pick)
        v_open = QPushButton("Abrir")
        v_open.clicked.connect(lambda: open_folder_in_explorer(self.cfg.video_download_dir))
        video_row.addWidget(v_open)
        lay.addLayout(video_row)

        lay.addStretch()
        return w

    def _on_auto_pause_toggled(self, checked: bool) -> None:
        self.cfg.auto_pause_other_player = checked
        save_config(self.cfg)
        self.config_updated.emit()

    def _on_norm_toggled(self, checked: bool) -> None:
        self.cfg.audio_normalization = checked
        save_config(self.cfg)
        self.normalization_changed.emit(checked)

    def _pick_music_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Elegir carpeta para Música", self.cfg.music_download_dir
        )
        if chosen:
            self.cfg.music_download_dir = str(Path(chosen))
            self.music_dir_edit.setText(self.cfg.music_download_dir)
            save_config(self.cfg)
            self.config_updated.emit()

    def _pick_video_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Elegir carpeta para Video", self.cfg.video_download_dir
        )
        if chosen:
            self.cfg.video_download_dir = str(Path(chosen))
            self.video_dir_edit.setText(self.cfg.video_download_dir)
            save_config(self.cfg)
            self.config_updated.emit()

    # --- Tab 2: Cuenta (DPAPI) ---

    def _build_account_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        acc_card = QFrame()
        acc_card.setObjectName("MediaCard")
        acc_lay = QHBoxLayout(acc_card)
        acc_lay.setContentsMargins(14, 12, 14, 12)
        acc_lay.setSpacing(12)

        self.acc_avatar_lbl = QLabel()
        self.acc_avatar_lbl.setFixedSize(44, 44)
        self.acc_avatar_lbl.setScaledContents(True)
        self.acc_avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        acc_lay.addWidget(self.acc_avatar_lbl)

        acc_text = QVBoxLayout()
        acc_text.setSpacing(2)
        self.acc_name_lbl = QLabel("Sin sesión vinculada")
        self.acc_name_lbl.setStyleSheet("font-size: 14px; font-weight: 700;")
        acc_text.addWidget(self.acc_name_lbl)

        self.session_status_lbl = QLabel("")
        self.session_status_lbl.setObjectName("MutedLabel")
        acc_text.addWidget(self.session_status_lbl)
        acc_lay.addLayout(acc_text, 1)
        lay.addWidget(acc_card)

        login_row = QHBoxLayout()
        login_row.setSpacing(8)
        self.browser_login_btn = QPushButton("Iniciar sesión con Google (Navegador)")
        self.browser_login_btn.setObjectName("AccentBtn")
        self.browser_login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browser_login_btn.clicked.connect(self._on_browser_login)
        login_row.addWidget(self.browser_login_btn)

        self.cancel_login_btn = QPushButton("Cancelar")
        self.cancel_login_btn.setVisible(False)
        self.cancel_login_btn.clicked.connect(self._on_cancel_browser_login)
        login_row.addWidget(self.cancel_login_btn)
        login_row.addStretch()
        lay.addLayout(login_row)

        sec_note = QLabel(
            "Seguridad 100% local:\n"
            "• Se abre una ventana limpia de Chrome/Edge directo en Google (nunca guarda tu contraseña).\n"
            "• Al entrar a YouTube, captura únicamente las cookies de YouTube/YT Music y cierra la ventana temporal.\n"
            "• Se guarda cifrado con Windows DPAPI ligado únicamente a tu usuario de Windows."
        )
        sec_note.setObjectName("MutedLabel")
        sec_note.setWordWrap(True)
        lay.addWidget(sec_note)

        adv_row = QHBoxLayout()
        import_file_btn = QPushButton("Importar cookies.txt")
        import_file_btn.clicked.connect(self._import_cookies_file)
        adv_row.addWidget(import_file_btn)

        paste_btn = QPushButton("Pegar cookies")
        paste_btn.clicked.connect(self._paste_cookies_dialog)
        adv_row.addWidget(paste_btn)
        adv_row.addStretch()
        lay.addLayout(adv_row)

        self.session_feedback_lbl = QLabel("")
        self.session_feedback_lbl.setObjectName("MutedLabel")
        self.session_feedback_lbl.setWordWrap(True)
        lay.addWidget(self.session_feedback_lbl)

        lay.addStretch()

        self.clear_btn = QPushButton("Cerrar sesión y borrar credenciales")
        self.clear_btn.setObjectName("DangerBtn")
        self.clear_btn.clicked.connect(self._on_clear_credentials)
        lay.addWidget(self.clear_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return w

    def _refresh_session_status(self) -> None:
        pal = get_theme(self.cfg.theme)
        has_sess = has_saved_session()
        prof = load_account_profile() if has_sess else None

        self._profile_avatar_url = prof.avatar_url if prof else ""
        if has_sess:
            if prof and prof.name:
                title = f"{prof.name} ({prof.handle})" if prof.handle else prof.name
                self.acc_name_lbl.setText(title)
            else:
                self.acc_name_lbl.setText("Cuenta de YouTube conectada")
            self.session_status_lbl.setText(
                "Sesión activa cifrada con Windows DPAPI — Feeds personalizados activos"
            )
            self.clear_btn.setEnabled(True)
        else:
            self.acc_name_lbl.setText("Modo local sin cuenta")
            self.session_status_lbl.setText(
                "Inicia sesión para ver tu feed de YouTube, 'Volver a escuchar' y tu foto"
            )
            self.clear_btn.setEnabled(False)

        if self._profile_avatar_url and self._thumb_cache is not None:
            pix = self._thumb_cache.get_or_fetch(
                self._profile_avatar_url, QSize(44, 44), circular=True
            )
            if pix is not None:
                self.acc_avatar_lbl.setPixmap(pix)
                self.acc_avatar_lbl.setStyleSheet("background-color: transparent; border: none;")
                return

        self.acc_avatar_lbl.setPixmap(
            render_icon("user", pal.text2, 24).pixmap(QSize(24, 24))
        )
        self.acc_avatar_lbl.setStyleSheet(
            f"background-color: {pal.surface2}; border-radius: 22px; border: 1px solid {pal.border};"
        )

    def _on_avatar_ready(self, url: str, pix: QPixmap) -> None:
        if self._profile_avatar_url and url == self._profile_avatar_url:
            self.acc_avatar_lbl.setPixmap(pix)
            self.acc_avatar_lbl.setStyleSheet("background-color: transparent; border: none;")

    def _on_browser_login(self) -> None:
        self._login_cancel = threading.Event()
        self.browser_login_btn.setEnabled(False)
        self.cancel_login_btn.setVisible(True)
        self.session_feedback_lbl.setText("Iniciando ventana segura de Google...")

        task = _BrowserLoginTask(self.yt, self._login_cancel)
        self._active_tasks.add(task.signals)
        task.signals.progress.connect(self.session_feedback_lbl.setText)
        task.signals.finished.connect(
            lambda ok, msg, sig=task.signals: self._on_browser_login_done(sig, ok, msg)
        )
        self._pool.start(task)

    def _on_cancel_browser_login(self) -> None:
        if self._login_cancel is not None:
            self._login_cancel.set()
            self.session_feedback_lbl.setText("Cancelando inicio de sesión...")

    def _on_browser_login_done(self, sig: _WorkerSignals, ok: bool, msg: str) -> None:
        self._active_tasks.discard(sig)
        self._login_cancel = None
        self.browser_login_btn.setEnabled(True)
        self.cancel_login_btn.setVisible(False)
        self.session_feedback_lbl.setText(msg)
        self._refresh_session_status()
        if ok:
            self.account_changed.emit()

    def _import_cookies_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleccionar cookies.txt exportado de Chrome",
            str(Path.home()),
            "Cookies (*.txt);;Todos (*.*)",
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="ignore")
            count = save_cookies_encrypted(raw)
            self.yt.reload_auth()
            self.yt.fetch_account_profile()
            self._refresh_session_status()
            self.session_feedback_lbl.setText(
                f"Listo: {count} cookies de YouTube guardadas y cifradas con Windows DPAPI."
            )
            self.account_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Error al importar cookies", str(exc))

    def _paste_cookies_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Pegar cookies de YouTube")
        dlg.resize(520, 320)
        lay = QVBoxLayout(dlg)
        lay.addWidget(
            QLabel(
                "Pega aquí el contenido de cookies.txt (Netscape) o el encabezado Cookie: de YouTube.\n"
                "Solo se conservarán cookies de .youtube.com y music.youtube.com."
            )
        )
        editor = QPlainTextEdit()
        editor.setPlaceholderText("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t...")
        lay.addWidget(editor, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        save_btn = QPushButton("Cifrar y guardar")
        save_btn.setObjectName("AccentBtn")
        save_btn.clicked.connect(dlg.accept)
        row.addWidget(save_btn)
        lay.addLayout(row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                count = save_cookies_encrypted(editor.toPlainText())
                self.yt.reload_auth()
                self.yt.fetch_account_profile()
                self._refresh_session_status()
                self.session_feedback_lbl.setText(
                    f"Listo: {count} cookies de YouTube cifradas con Windows DPAPI."
                )
                self.account_changed.emit()
            except Exception as exc:
                QMessageBox.warning(self, "Cookies inválidas", str(exc))

    def _on_clear_credentials(self) -> None:
        clear_credentials()
        self.yt.reload_auth()
        self._refresh_session_status()
        self.session_feedback_lbl.setText(
            "Credenciales sobrescritas y eliminadas por completo de tu PC."
        )
        self.account_changed.emit()

    # --- Tab 3: Sistema ---

    def _build_system_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        self.startup_check = QCheckBox("Abrir SwiftPlay al iniciar Windows (en YouTube)")
        self.startup_check.setChecked(is_startup_enabled() or self.cfg.start_with_windows)
        self.startup_check.toggled.connect(self._on_startup_toggled)
        lay.addWidget(self.startup_check)

        self.tray_check = QCheckBox(
            "Minimizar a la bandeja del reloj al cerrar (X) para seguir escuchando"
        )
        self.tray_check.setChecked(self.cfg.minimize_to_tray)
        self.tray_check.toggled.connect(self._on_tray_toggled)
        lay.addWidget(self.tray_check)

        ram_box = QFrame()
        ram_box.setObjectName("MediaCard")
        ram_lay = QHBoxLayout(ram_box)
        ram_lay.setContentsMargins(12, 10, 12, 10)
        self.ram_lbl = QLabel("RAM activa: -- MB  ·  Privada: -- MB")
        self.ram_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        ram_lay.addWidget(self.ram_lbl)
        ram_lay.addStretch()
        self.open_ram_log_btn = QPushButton("Abrir log de RAM")
        self.open_ram_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_ram_log_btn.setToolTip(f"Abrir {RAM_LOG_PATH}")
        self.open_ram_log_btn.clicked.connect(self._on_open_ram_log_clicked)
        ram_lay.addWidget(self.open_ram_log_btn)
        self.trim_ram_btn = QPushButton("Liberar memoria")
        self.trim_ram_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.trim_ram_btn.clicked.connect(self._on_trim_memory_clicked)
        ram_lay.addWidget(self.trim_ram_btn)
        lay.addWidget(ram_box)

        ram_path_lbl = QLabel(f"Registro histórico de RAM: {RAM_LOG_PATH}")
        ram_path_lbl.setObjectName("MutedLabel")
        ram_path_lbl.setWordWrap(True)
        lay.addWidget(ram_path_lbl)

        motor_row = QHBoxLayout()
        try:
            ver = pkg_version("yt-dlp")
        except PackageNotFoundError:
            ver = "desconocida"
        self.motor_ver_lbl = QLabel(f"Motor yt-dlp actual: {ver}")
        self.motor_ver_lbl.setObjectName("MutedLabel")
        motor_row.addWidget(self.motor_ver_lbl, 1)

        self.update_motor_btn = QPushButton("Actualizar motor (yt-dlp)")
        self.update_motor_btn.setObjectName("AccentBtn")
        self.update_motor_btn.clicked.connect(self._on_update_motor)
        motor_row.addWidget(self.update_motor_btn)
        lay.addLayout(motor_row)

        self.motor_log_lbl = QLabel("")
        self.motor_log_lbl.setObjectName("MutedLabel")
        self.motor_log_lbl.setWordWrap(True)
        lay.addWidget(self.motor_log_lbl)

        lay.addStretch()
        return w

    # --- Tab 4: Discord (DPAPI) ---

    def _build_discord_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)

        card = QFrame()
        card.setObjectName("MediaCard")
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(14, 10, 14, 10)
        c_lay.setSpacing(4)
        title = QLabel("Bot Token oficial de Discord (Cifrado Windows DPAPI)")
        title.setStyleSheet("font-size: 14px; font-weight: 700;")
        c_lay.addWidget(title)
        self.discord_status_lbl = QLabel("")
        self.discord_status_lbl.setObjectName("MutedLabel")
        self.discord_status_lbl.setWordWrap(True)
        c_lay.addWidget(self.discord_status_lbl)
        lay.addWidget(card)

        tok_row = QHBoxLayout()
        tok_row.setSpacing(8)
        self.discord_token_edit = QLineEdit()
        self.discord_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.discord_token_edit.setPlaceholderText("Pega aquí tu Discord Bot Token...")
        tok_row.addWidget(self.discord_token_edit, 1)

        self.discord_show_btn = QPushButton("Mostrar")
        self.discord_show_btn.setCheckable(True)
        self.discord_show_btn.toggled.connect(
            lambda checked: self.discord_token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        tok_row.addWidget(self.discord_show_btn)
        lay.addLayout(tok_row)

        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        save_btn = QPushButton("Cifrar y guardar token")
        save_btn.setObjectName("AccentBtn")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._on_save_discord_token)
        act_row.addWidget(save_btn)

        self.discord_clear_btn = QPushButton("Borrar token")
        self.discord_clear_btn.setObjectName("DangerBtn")
        self.discord_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_clear_btn.clicked.connect(self._on_clear_discord_token)
        act_row.addWidget(self.discord_clear_btn)
        act_row.addStretch()
        lay.addLayout(act_row)

        # Preferencias visuales y Overlay de silencio/ensordecido
        ui_card = QFrame()
        ui_card.setObjectName("MediaCard")
        ui_lay = QVBoxLayout(ui_card)
        ui_lay.setContentsMargins(14, 10, 14, 10)
        ui_lay.setSpacing(8)

        ui_title = QLabel("Interfaz y Overlay de Discord")
        ui_title.setStyleSheet("font-size: 13px; font-weight: 700;")
        ui_lay.addWidget(ui_title)

        self.discord_show_locked_check = QCheckBox("Mostrar canales de voz sin permiso")
        self.discord_show_locked_check.setChecked(self.cfg.discord_show_locked_channels)
        self.discord_show_locked_check.toggled.connect(self._on_show_locked_channels_toggled)
        ui_lay.addWidget(self.discord_show_locked_check)

        self.discord_overlay_check = QCheckBox(
            "Mostrar overlay flotante de silencio/ensordecido (arriba a la izquierda)"
        )
        self.discord_overlay_check.setChecked(self.cfg.discord_overlay_enabled)
        self.discord_overlay_check.toggled.connect(self._on_discord_overlay_toggled)
        ui_lay.addWidget(self.discord_overlay_check)

        op_row = QHBoxLayout()
        op_row.setSpacing(10)
        op_lbl = QLabel("Opacidad del overlay:")
        op_lbl.setObjectName("MutedLabel")
        op_row.addWidget(op_lbl)

        self.discord_overlay_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.discord_overlay_opacity_slider.setRange(10, 100)
        self.discord_overlay_opacity_slider.setSingleStep(5)
        self.discord_overlay_opacity_slider.setPageStep(10)
        self.discord_overlay_opacity_slider.setValue(self.cfg.discord_overlay_opacity)
        self.discord_overlay_opacity_slider.setEnabled(self.cfg.discord_overlay_enabled)
        self.discord_overlay_opacity_slider.valueChanged.connect(
            self._on_discord_overlay_opacity_changed
        )
        op_row.addWidget(self.discord_overlay_opacity_slider, 1)

        self.discord_overlay_opacity_lbl = QLabel(f"{self.cfg.discord_overlay_opacity}%")
        self.discord_overlay_opacity_lbl.setFixedWidth(40)
        self.discord_overlay_opacity_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        op_row.addWidget(self.discord_overlay_opacity_lbl)
        ui_lay.addLayout(op_row)

        self.discord_notify_chat_check = QCheckBox(
            "Avisar en la barra de tareas solo para los canales marcados con campana"
        )
        self.discord_notify_chat_check.setChecked(self.cfg.discord_notify_chat_enabled)
        self.discord_notify_chat_check.toggled.connect(self._on_notify_chat_toggled)
        ui_lay.addWidget(self.discord_notify_chat_check)

        notify_row = QHBoxLayout()
        notify_row.setSpacing(8)
        self.discord_notify_summary_lbl = QLabel()
        self.discord_notify_summary_lbl.setObjectName("MutedLabel")
        self._refresh_notify_summary()
        notify_row.addWidget(self.discord_notify_summary_lbl, 1)

        self.discord_mute_all_ch_btn = QPushButton("Silenciar todos")
        self.discord_mute_all_ch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_mute_all_ch_btn.setEnabled(bool(self.cfg.discord_notify_channels))
        self.discord_mute_all_ch_btn.clicked.connect(self._on_clear_notify_channels)
        notify_row.addWidget(self.discord_mute_all_ch_btn)
        ui_lay.addLayout(notify_row)
        lay.addWidget(ui_card)

        # Canales ocultos permanentemente (voz y texto)
        hidden_card = QFrame()
        hidden_card.setObjectName("MediaCard")
        hc_lay = QVBoxLayout(hidden_card)
        hc_lay.setContentsMargins(14, 10, 14, 10)
        hc_lay.setSpacing(8)

        hc_hdr = QHBoxLayout()
        hc_hdr.setSpacing(8)
        hc_title = QLabel("Canales ocultos de Discord (voz y texto)")
        hc_title.setStyleSheet("font-size: 13px; font-weight: 700;")
        hc_hdr.addWidget(hc_title, 1)

        self.discord_unhide_all_btn = QPushButton("Mostrar todos")
        self.discord_unhide_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_unhide_all_btn.clicked.connect(self._on_unhide_all_channels)
        hc_hdr.addWidget(self.discord_unhide_all_btn)
        hc_lay.addLayout(hc_hdr)

        hc_desc = QLabel(
            "Los canales ocultos no se mostrarán hasta que los reactives aquí. "
            "También puedes hacer clic derecho en cualquier canal de Discord → 'Ocultar este canal siempre'."
        )
        hc_desc.setObjectName("MutedLabel")
        hc_desc.setWordWrap(True)
        hc_lay.addWidget(hc_desc)

        add_hide_row = QHBoxLayout()
        add_hide_row.setSpacing(8)
        self.discord_hide_combo = QComboBox()
        self.discord_hide_combo.setToolTip("Elige un canal de voz o texto para ocultarlo siempre")
        add_hide_row.addWidget(self.discord_hide_combo, 1)

        self.discord_hide_btn = QPushButton("Ocultar canal")
        self.discord_hide_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discord_hide_btn.clicked.connect(self._on_hide_channel_from_combo)
        add_hide_row.addWidget(self.discord_hide_btn)
        hc_lay.addLayout(add_hide_row)

        hidden_box = QWidget()
        self.discord_hidden_list_lay = QVBoxLayout(hidden_box)
        self.discord_hidden_list_lay.setContentsMargins(0, 2, 0, 0)
        self.discord_hidden_list_lay.setSpacing(5)
        hc_lay.addWidget(hidden_box)

        lay.addWidget(hidden_card)
        self._refresh_hidden_channels_ui()

        guide = QLabel(
            "Guía rápida:\n"
            "• Ve a https://discord.com/developers/applications → Bot → Reset Token.\n"
            "• Activa Presence, Server Members y Message Content Intents si quieres ver miembros y leer todo el chat.\n"
            "• En OAuth2 → URL Generator marca 'bot' + View Channels, Connect, Speak, Read Message History y Send Messages."
        )
        guide.setObjectName("MutedLabel")
        guide.setWordWrap(True)
        lay.addWidget(guide)

        self.discord_feedback_lbl = QLabel("")
        self.discord_feedback_lbl.setObjectName("MutedLabel")
        self.discord_feedback_lbl.setWordWrap(True)
        lay.addWidget(self.discord_feedback_lbl)

        lay.addStretch()
        self._refresh_discord_status()
        scroll.setWidget(w)
        return scroll

    # --- Tab 5: Atajos Discord ---

    def _build_discord_shortcuts_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        card = QFrame()
        card.setObjectName("MediaCard")
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(14, 12, 14, 12)
        c_lay.setSpacing(6)
        title = QLabel("Atajos globales de Discord (funcionan en cualquier ventana o juego)")
        title.setStyleSheet("font-size: 14px; font-weight: 700;")
        c_lay.addWidget(title)
        desc = QLabel(
            "Haz clic en un campo y pulsa la combinación deseada (ej. Ctrl+Shift+M, Alt+M o F9). "
            "La opción prioritaria intercepta el atajo a nivel de teclado en Windows para no fallar al cambiar de pestaña o ventana."
        )
        desc.setObjectName("MutedLabel")
        desc.setWordWrap(True)
        c_lay.addWidget(desc)

        self.hotkey_mute_prio_check = QCheckBox(
            "Prioridad máxima global para silenciar micrófono (siempre activo sobre otras apps y pestañas)"
        )
        self.hotkey_mute_prio_check.setChecked(self.cfg.discord_hotkey_mute_priority)
        self.hotkey_mute_prio_check.toggled.connect(self._on_mute_priority_toggled)
        c_lay.addWidget(self.hotkey_mute_prio_check)
        lay.addWidget(card)

        self._hk_edits: dict[str, _HotkeyCaptureEdit] = {}
        self._hk_status_labels: dict[str, QLabel] = {}

        rows_spec = (
            ("mute", "Silenciar / Activar micrófono (Prioritario)", "discord_hotkey_mute"),
            ("deafen", "Ensordecer / Activar audio Discord", "discord_hotkey_deafen"),
            ("hangup", "Colgar / Salir del canal de voz", "discord_hotkey_hangup"),
        )
        form_card = QFrame()
        form_card.setObjectName("MediaCard")
        form_lay = QVBoxLayout(form_card)
        form_lay.setContentsMargins(14, 12, 14, 12)
        form_lay.setSpacing(10)

        for key, label_txt, attr in rows_spec:
            row = QHBoxLayout()
            row.setSpacing(10)
            lbl = QLabel(label_txt)
            lbl.setMinimumWidth(235)
            row.addWidget(lbl)

            edit = _HotkeyCaptureEdit(getattr(self.cfg, attr, ""))
            edit.setFixedWidth(165)
            edit.recording_changed.connect(self.hotkey_recording_changed.emit)
            edit.hotkey_changed.connect(lambda val, k=key, a=attr: self._on_hotkey_edited(k, a, val))
            edit.invalid_hotkey.connect(self._on_hotkey_invalid)
            row.addWidget(edit)

            clr_btn = QPushButton("Limpiar")
            clr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            clr_btn.clicked.connect(lambda _c=False, k=key, a=attr, e=edit: self._clear_hotkey(k, a, e))
            row.addWidget(clr_btn)

            st_lbl = QLabel("")
            st_lbl.setObjectName("MutedLabel")
            st_lbl.setMinimumWidth(120)
            row.addWidget(st_lbl, 1)

            self._hk_edits[key] = edit
            self._hk_status_labels[key] = st_lbl
            form_lay.addLayout(row)

        lay.addWidget(form_card)

        bottom_row = QHBoxLayout()
        reset_btn = QPushButton("Restablecer predeterminados")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self._on_reset_hotkeys)
        bottom_row.addWidget(reset_btn)
        bottom_row.addStretch()
        lay.addLayout(bottom_row)

        self.hotkey_feedback_lbl = QLabel("")
        self.hotkey_feedback_lbl.setObjectName("MutedLabel")
        self.hotkey_feedback_lbl.setWordWrap(True)
        lay.addWidget(self.hotkey_feedback_lbl)
        lay.addStretch()
        return w

    def _on_mute_priority_toggled(self, checked: bool) -> None:
        self.cfg.discord_hotkey_mute_priority = bool(checked)
        save_config(self.cfg)
        self.discord_hotkeys_changed.emit()

    def _on_hotkey_edited(self, _key: str, attr: str, value: str) -> None:
        setattr(self.cfg, attr, str(value or "").strip())
        save_config(self.cfg)
        self.hotkey_feedback_lbl.setText(f"Atajo actualizado: {value}")
        self.hotkey_feedback_lbl.setStyleSheet("")
        self.discord_hotkeys_changed.emit()

    def _clear_hotkey(self, _key: str, attr: str, edit: _HotkeyCaptureEdit) -> None:
        edit.setText("")
        setattr(self.cfg, attr, "")
        save_config(self.cfg)
        self.hotkey_feedback_lbl.setText("Atajo desactivado.")
        self.hotkey_feedback_lbl.setStyleSheet("")
        self.discord_hotkeys_changed.emit()

    def _on_hotkey_invalid(self, msg: str) -> None:
        self.hotkey_feedback_lbl.setText(msg)
        self.hotkey_feedback_lbl.setStyleSheet("color: #FBBF24;")

    def _on_reset_hotkeys(self) -> None:
        defaults = {
            "mute": ("discord_hotkey_mute", "Ctrl+Shift+M"),
            "deafen": ("discord_hotkey_deafen", "Ctrl+Shift+D"),
            "hangup": ("discord_hotkey_hangup", "Ctrl+Shift+H"),
        }
        for key, (attr, val) in defaults.items():
            setattr(self.cfg, attr, val)
            if key in self._hk_edits:
                self._hk_edits[key].setText(val)
        self.cfg.discord_hotkey_mute_priority = True
        self.hotkey_mute_prio_check.blockSignals(True)
        self.hotkey_mute_prio_check.setChecked(True)
        self.hotkey_mute_prio_check.blockSignals(False)
        save_config(self.cfg)
        self.hotkey_feedback_lbl.setText("Atajos restablecidos a los valores predeterminados.")
        self.hotkey_feedback_lbl.setStyleSheet("")
        self.discord_hotkeys_changed.emit()

    def update_discord_hotkey_statuses(self, statuses: dict[str, str]) -> None:
        labels = {
            "global_hook": ("Activo · Prioridad máxima", "color: #4ADE80; font-weight: 600;"),
            "global_os": ("Activo · Global", "color: #93C5FD; font-weight: 600;"),
            "disabled": ("Desactivado", ""),
            "conflict": ("Conflicto con otra app", "color: #F87171;"),
            "invalid": ("Tecla inválida", "color: #FBBF24;"),
        }
        for key, st_lbl in self._hk_status_labels.items():
            raw_st = str(statuses.get(key) or "disabled")
            txt, css = labels.get(raw_st, (raw_st, ""))
            st_lbl.setText(txt)
            st_lbl.setStyleSheet(css)

    def _refresh_discord_status(self) -> None:
        has_tok = has_discord_token()
        self.discord_clear_btn.setEnabled(has_tok)
        if has_tok:
            self.discord_status_lbl.setText(
                "Token guardado y cifrado localmente con Windows DPAPI (discord_token.dpapi)."
            )
        else:
            self.discord_status_lbl.setText(
                "No hay Bot Token configurado. Pega uno abajo para habilitar Discord Voz."
            )

    def _on_save_discord_token(self) -> None:
        raw = self.discord_token_edit.text()
        try:
            save_discord_token_encrypted(raw)
        except Exception as exc:
            QMessageBox.warning(self, "Token inválido", str(exc))
            return
        self.discord_token_edit.clear()
        self._refresh_discord_status()
        self.discord_feedback_lbl.setText(
            "Bot Token cifrado y guardado con Windows DPAPI correctamente."
        )
        self.discord_token_changed.emit()

    def _on_clear_discord_token(self) -> None:
        clear_discord_token()
        self.discord_token_edit.clear()
        self._refresh_discord_status()
        self.discord_feedback_lbl.setText(
            "Bot Token sobrescrito y eliminado por completo de tu PC."
        )
        self.discord_token_changed.emit()

    def _on_show_locked_channels_toggled(self, checked: bool) -> None:
        self.cfg.discord_show_locked_channels = bool(checked)
        save_config(self.cfg)
        self.discord_ui_settings_changed.emit()

    def _on_discord_overlay_toggled(self, checked: bool) -> None:
        self.cfg.discord_overlay_enabled = bool(checked)
        self.discord_overlay_opacity_slider.setEnabled(bool(checked))
        save_config(self.cfg)
        self.discord_overlay_settings_changed.emit()

    def _on_discord_overlay_opacity_changed(self, value: int) -> None:
        val = max(10, min(100, int(value)))
        self.discord_overlay_opacity_lbl.setText(f"{val}%")
        self.cfg.discord_overlay_opacity = val
        save_config(self.cfg)
        self.discord_overlay_settings_changed.emit()
        self.discord_overlay_preview_requested.emit(val)

    def _refresh_notify_summary(self) -> None:
        cnt = len(self.cfg.discord_notify_channels)
        self.discord_notify_summary_lbl.setText(f"Canales con aviso activo: {cnt}")
        if hasattr(self, "discord_mute_all_ch_btn"):
            self.discord_mute_all_ch_btn.setEnabled(cnt > 0)

    def _on_notify_chat_toggled(self, checked: bool) -> None:
        self.cfg.discord_notify_chat_enabled = bool(checked)
        save_config(self.cfg)
        self.discord_ui_settings_changed.emit()

    def _on_clear_notify_channels(self) -> None:
        self.cfg.discord_notify_channels = []
        save_config(self.cfg)
        self._refresh_notify_summary()
        self.discord_ui_settings_changed.emit()

    def _refresh_hidden_channels_ui(self) -> None:
        if not hasattr(self, "discord_hidden_list_lay"):
            return
        while self.discord_hidden_list_lay.count() > 0:
            item = self.discord_hidden_list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        hidden_ids = list(self.cfg.discord_hidden_channels)
        hidden_set = set(hidden_ids)
        label_by_id: dict[str, str] = dict(self.cfg.discord_hidden_channel_names)
        for cid, lbl in self._discord_channels:
            if cid and lbl:
                label_by_id.setdefault(cid, lbl)

        self.discord_hide_combo.clear()
        for cid, lbl in self._discord_channels:
            if cid and cid not in hidden_set:
                self.discord_hide_combo.addItem(lbl, cid)
        if self.discord_hide_combo.count() == 0:
            self.discord_hide_combo.addItem("Conecta Discord para listar más canales", "")
            self.discord_hide_btn.setEnabled(False)
        else:
            self.discord_hide_btn.setEnabled(True)

        self.discord_unhide_all_btn.setEnabled(len(hidden_ids) > 0)
        if not hidden_ids:
            empty_lbl = QLabel("No tienes canales ocultos actualmente.")
            empty_lbl.setObjectName("MutedLabel")
            self.discord_hidden_list_lay.addWidget(empty_lbl)
            return

        for cid in hidden_ids:
            row = QFrame()
            row.setObjectName("DiscordChannelRow")
            r_lay = QHBoxLayout(row)
            r_lay.setContentsMargins(10, 5, 8, 5)
            r_lay.setSpacing(8)
            disp = label_by_id.get(cid) or f"Canal {cid}"
            lbl = QLabel(disp)
            lbl.setWordWrap(False)
            r_lay.addWidget(lbl, 1)

            show_btn = QPushButton("Mostrar")
            show_btn.setObjectName("PillBtn")
            show_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            show_btn.clicked.connect(lambda _c=False, ch_id=cid: self._on_unhide_channel(ch_id))
            r_lay.addWidget(show_btn)
            self.discord_hidden_list_lay.addWidget(row)

    def _on_hide_channel_from_combo(self) -> None:
        cid = str(self.discord_hide_combo.currentData() or "").strip()
        if not cid:
            return
        label = str(self.discord_hide_combo.currentText() or "").strip()
        if cid not in self.cfg.discord_hidden_channels:
            self.cfg.discord_hidden_channels = list(self.cfg.discord_hidden_channels) + [cid]
        if label:
            names = dict(self.cfg.discord_hidden_channel_names)
            names[cid] = label
            self.cfg.discord_hidden_channel_names = names
        save_config(self.cfg)
        self._refresh_hidden_channels_ui()
        self.discord_ui_settings_changed.emit()

    def _on_unhide_channel(self, channel_id: str) -> None:
        cid = str(channel_id or "").strip()
        if not cid:
            return
        self.cfg.discord_hidden_channels = [
            x for x in self.cfg.discord_hidden_channels if x != cid
        ]
        names = dict(self.cfg.discord_hidden_channel_names)
        names.pop(cid, None)
        self.cfg.discord_hidden_channel_names = names
        save_config(self.cfg)
        self._refresh_hidden_channels_ui()
        self.discord_ui_settings_changed.emit()

    def _on_unhide_all_channels(self) -> None:
        if not self.cfg.discord_hidden_channels and not self.cfg.discord_hidden_channel_names:
            return
        self.cfg.discord_hidden_channels = []
        self.cfg.discord_hidden_channel_names = {}
        save_config(self.cfg)
        self._refresh_hidden_channels_ui()
        self.discord_ui_settings_changed.emit()

    def _on_startup_toggled(self, checked: bool) -> None:
        try:
            set_startup_enabled(checked)
            self.cfg.start_with_windows = checked
            save_config(self.cfg)
        except Exception as exc:
            QMessageBox.warning(self, "Inicio con Windows", str(exc))

    def _on_tray_toggled(self, checked: bool) -> None:
        self.cfg.minimize_to_tray = checked
        save_config(self.cfg)

    def _on_trim_memory_clicked(self) -> None:
        self.trim_memory_requested.emit()
        QTimer.singleShot(150, self._refresh_ram_label)

    def _on_open_ram_log_clicked(self) -> None:
        try:
            if not RAM_LOG_PATH.exists():
                ws, priv = get_process_memory_mb()
                RamLogger(RAM_LOG_PATH).log_sample(
                    "manual_open",
                    gui_ws_mb=ws,
                    gui_priv_mb=priv,
                )
            if sys.platform == "win32":
                os.startfile(str(RAM_LOG_PATH))  # noqa: S606
        except Exception as exc:
            QMessageBox.warning(self, "Log de RAM", str(exc))

    def _refresh_ram_label(self) -> None:
        ws, priv = get_process_memory_mb()
        self.ram_lbl.setText(f"RAM activa: {ws:.1f} MB  ·  Privada: {priv:.1f} MB")

    def _on_update_motor(self) -> None:
        self.update_motor_btn.setEnabled(False)
        self.motor_log_lbl.setText("Actualizando yt-dlp con uv...")
        task = _UpdateMotorTask(self.yt)
        self._active_tasks.add(task.signals)
        task.signals.finished.connect(
            lambda ok, msg, sig=task.signals: self._on_update_motor_done(sig, ok, msg)
        )
        self._pool.start(task)

    def _on_update_motor_done(self, sig: _WorkerSignals, ok: bool, msg: str) -> None:
        self._active_tasks.discard(sig)
        self.update_motor_btn.setEnabled(True)
        self.motor_log_lbl.setText(msg)
        if ok:
            self.motor_ver_lbl.setText(
                "Motor yt-dlp actualizado (reinicia la app para aplicar en memoria)."
            )

    def _cleanup_dialog(self) -> None:
        if hasattr(self, "_outside_filter"):
            self._outside_filter.uninstall()
        self.hotkey_recording_changed.emit(False)
        self._ram_timer.stop()
        if self._login_cancel is not None:
            self._login_cancel.set()
        self._pool.clear()
        if self._thumb_cache is not None:
            try:
                self._thumb_cache.ready.disconnect(self._on_avatar_ready)
            except Exception:
                pass

    def done(self, result: int) -> None:
        self._cleanup_dialog()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cleanup_dialog()
        super().closeEvent(event)
