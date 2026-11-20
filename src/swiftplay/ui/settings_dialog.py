"""Diálogo de Ajustes de SwiftPlay (Reproductor Dual, Descargas, Cuenta DPAPI, Sistema)."""

from __future__ import annotations

import threading
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from swiftplay.browser_auth import login_with_browser
from swiftplay.config import AppConfig, save_config
from swiftplay.security import (
    clear_credentials,
    has_saved_session,
    load_account_profile,
    save_cookies_encrypted,
)
from swiftplay.themes import apply_windows_dark_titlebar, get_theme
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon
from swiftplay.windows_os import (
    get_process_ram_mb,
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


class SettingsDialog(QDialog):
    """Ventana de configuración de SwiftPlay."""

    normalization_changed = Signal(bool)
    config_updated = Signal()
    account_changed = Signal()

    def __init__(
        self,
        config: AppConfig,
        yt_service: YouTubeService,
        thumb_cache: ThumbnailCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = config
        self.yt = yt_service
        self._thumb_cache = thumb_cache
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

    def select_account_tab(self) -> None:
        self.tabs.setCurrentIndex(1)

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
        self.ram_lbl = QLabel("Uso de RAM actual: -- MB")
        self.ram_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        ram_lay.addWidget(self.ram_lbl)
        ram_lay.addStretch()
        lay.addWidget(ram_box)

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

    def _refresh_ram_label(self) -> None:
        mb = get_process_ram_mb()
        self.ram_lbl.setText(f"Uso de RAM actual: {mb:.1f} MB")

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
        self._ram_timer.stop()
        if self._login_cancel is not None:
            self._login_cancel.set()
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
