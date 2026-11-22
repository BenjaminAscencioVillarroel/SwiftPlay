"""Notificación flotante estilo Windows 11 Discord con tema SwiftPlay."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from swiftplay.paths import APP_NAME, ICON_PNG_PATH
from swiftplay.themes import NotifyColorSpec, ThemePalette, get_notification_color
from swiftplay.thumb_cache import ThumbnailCache
from swiftplay.ui.icons import render_icon

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010

_TOAST_W = 350
_TOAST_H = 108

_BADGE_FALLBACK_TINTS: tuple[str, ...] = (
    "#5A362D",
    "#334155",
    "#3B4454",
    "#2F4840",
    "#4B3B58",
    "#4F4430",
)


def _initial_tint(name: str) -> str:
    acc = sum(ord(ch) for ch in (name or "D"))
    return _BADGE_FALLBACK_TINTS[acc % len(_BADGE_FALLBACK_TINTS)]


class DiscordMessageToast(QWidget):
    """Notificación de escritorio estilo Discord inferior-derecha que no roba foco."""

    notification_clicked = Signal(dict)

    def __init__(
        self,
        palette: ThemePalette,
        notify_color: str = "purple",
        thumb_cache: ThumbnailCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._notify_spec: NotifyColorSpec = get_notification_color(notify_color)
        self._thumb_cache = thumb_cache
        self._thumb_connected = False
        self._avatar_url: str = ""
        self._author_name: str = ""
        self._payload: dict[str, Any] = {}
        self._fading_out: bool = False
        self._target_pos = QPoint(0, 0)

        if self._thumb_cache is not None:
            self._thumb_cache.ready.connect(self._on_avatar_ready)
            self._thumb_connected = True

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_TOAST_W, _TOAST_H)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 12, 12)
        root.setSpacing(8)

        # Cabecera: ícono Discord con acento + "Discord · #canal" + botón cerrar
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(7)

        self.app_icon_lbl = QLabel()
        self.app_icon_lbl.setFixedSize(18, 18)
        self.app_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr_row.addWidget(self.app_icon_lbl)

        self.header_lbl = QLabel(APP_NAME)
        self.header_lbl.setStyleSheet(
            "background-color: transparent; color: #E5E9F0; font-size: 12px; font-weight: 600;"
        )
        hdr_row.addWidget(self.header_lbl, 1)

        self.close_btn = QPushButton()
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Cerrar notificación")
        self.close_btn.clicked.connect(self.dismiss)
        hdr_row.addWidget(self.close_btn)
        root.addLayout(hdr_row)

        # Cuerpo: avatar circular 48x48 + Autor en negrita + Mensaje
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(12)

        self.avatar_lbl = QLabel()
        self.avatar_lbl.setFixedSize(48, 48)
        self.avatar_lbl.setScaledContents(True)
        self.avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_row.addWidget(self.avatar_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 1, 0, 1)
        text_col.setSpacing(2)
        text_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.author_lbl = QLabel("")
        self.author_lbl.setStyleSheet(
            "background-color: transparent; color: #F1F3F6; font-size: 14px; font-weight: 700;"
        )
        text_col.addWidget(self.author_lbl)

        self.preview_lbl = QLabel("")
        self.preview_lbl.setWordWrap(True)
        self.preview_lbl.setStyleSheet(
            "background-color: transparent; color: #B8C0CE; font-size: 13px; font-weight: 400;"
        )
        text_col.addWidget(self.preview_lbl)

        body_row.addLayout(text_col, 1)
        root.addLayout(body_row, 1)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.dismiss)

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.finished.connect(self._on_anim_finished)
        self._pos_anim = QPropertyAnimation(self, b"pos", self)
        self.setWindowOpacity(0.0)
        self._apply_colors()

    def _apply_colors(self) -> None:
        nc = self._notify_spec
        if ICON_PNG_PATH.exists():
            pm = QPixmap(str(ICON_PNG_PATH)).scaled(
                18,
                18,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.app_icon_lbl.setPixmap(pm)
            self.app_icon_lbl.setStyleSheet("background-color: transparent; border: none;")
        else:
            self.app_icon_lbl.setPixmap(render_icon("video", nc.badge_text, 13).pixmap(QSize(13, 13)))
            self.app_icon_lbl.setStyleSheet(
                f"background-color: {nc.badge_bg}; border: none; border-radius: 5px;"
            )
        self.close_btn.setIcon(render_icon("close", "#9299A8", 12))
        self.close_btn.setStyleSheet(
            "QPushButton { background-color: transparent; border: none; border-radius: 11px; padding: 0px; }"
            "QPushButton:hover { background-color: rgba(255, 255, 255, 22); }"
        )
        if self.isVisible():
            self.update()

    def update_theme(self, palette: ThemePalette, notify_color: str = "purple") -> None:
        self._palette = palette
        self._notify_spec = get_notification_color(notify_color)
        self._apply_colors()

    def _apply_win32_no_activate(self, *, force_topmost: bool = False) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t

            ex = int(get_long(ctypes.c_void_p(hwnd), _GWL_EXSTYLE))
            target = ex | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
            if target != ex:
                set_long(ctypes.c_void_p(hwnd), _GWL_EXSTYLE, target)
            if force_topmost:
                user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_void_p(_HWND_TOPMOST),
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
                )
        except Exception:
            pass

    def _compute_bottom_right_pos(self) -> QPoint:
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr is not None else None
        if geo is not None:
            return QPoint(geo.right() - self.width() - 14, geo.bottom() - self.height() - 14)
        return QPoint(40, 40)

    def _refresh_avatar_pixmap(self) -> None:
        av = self._avatar_url
        if av and self._thumb_cache is not None:
            pix = self._thumb_cache.get_or_fetch(av, QSize(48, 48), circular=True)
            if pix is not None and not pix.isNull():
                self.avatar_lbl.setText("")
                self.avatar_lbl.setPixmap(pix)
                self.avatar_lbl.setStyleSheet("background-color: transparent; border: none;")
                return
        self.avatar_lbl.setPixmap(QPixmap())
        initial = (self._author_name.strip()[:1] or "D").upper()
        tint = _initial_tint(self._author_name)
        self.avatar_lbl.setText(initial)
        self.avatar_lbl.setStyleSheet(
            f"background-color: {tint}; color: #F1F3F6; "
            f"border: 1px solid rgba(255, 255, 255, 36); border-radius: 24px; "
            "font-size: 18px; font-weight: 700;"
        )

    def _on_avatar_ready(self, url: str, pix: QPixmap) -> None:
        if (
            self._avatar_url
            and url == self._avatar_url
            and pix.size() == QSize(48, 48)
            and self.isVisible()
        ):
            self.avatar_lbl.setText("")
            self.avatar_lbl.setPixmap(pix)
            self.avatar_lbl.setStyleSheet("background-color: transparent; border: none;")

    def show_notification(
        self,
        author_name: str,
        preview: str,
        *,
        header_text: str = APP_NAME,
        avatar_url: str = "",
        payload: dict[str, Any] | None = None,
        duration_ms: int = 5000,
    ) -> None:
        clean_author = str(author_name or APP_NAME).strip() or APP_NAME
        clean_preview = str(preview or "").replace("\r", " ").replace("\n", " ").strip() or "Nuevo mensaje"
        clean_hdr = str(header_text or APP_NAME).strip() or APP_NAME

        self._author_name = clean_author
        self._avatar_url = str(avatar_url or "").strip()
        self._payload = dict(payload) if isinstance(payload, dict) else {}

        fm_hdr = QFontMetrics(self.header_lbl.font())
        self.header_lbl.setText(fm_hdr.elidedText(clean_hdr, Qt.TextElideMode.ElideRight, 260))

        fm_auth = QFontMetrics(self.author_lbl.font())
        self.author_lbl.setText(fm_auth.elidedText(clean_author, Qt.TextElideMode.ElideRight, 252))

        if len(clean_preview) > 92:
            clean_preview = clean_preview[:89].rstrip() + "…"
        self.preview_lbl.setText(clean_preview)
        self._refresh_avatar_pixmap()

        self._target_pos = self._compute_bottom_right_pos()
        self._opacity_anim.stop()
        self._pos_anim.stop()
        self._fading_out = False
        self._apply_win32_no_activate(force_topmost=False)

        if not self.isVisible():
            start_pos = QPoint(self._target_pos.x(), self._target_pos.y() + 12)
            self.setWindowOpacity(0.0)
            self.move(start_pos)
            self.show()
            self._apply_win32_no_activate(force_topmost=True)
            self._opacity_anim.setDuration(160)
            self._opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._opacity_anim.setStartValue(0.0)
            self._opacity_anim.setEndValue(1.0)
            self._opacity_anim.start()

            self._pos_anim.setDuration(160)
            self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._pos_anim.setStartValue(start_pos)
            self._pos_anim.setEndValue(self._target_pos)
            self._pos_anim.start()
        else:
            self.move(self._target_pos)
            self.setWindowOpacity(1.0)
            self._apply_win32_no_activate(force_topmost=True)
            self.update()

        self._hide_timer.start(max(1200, int(duration_ms)))

    def dismiss(self) -> None:
        self._hide_timer.stop()
        if not self.isVisible() or self._fading_out:
            return
        cur = float(self.windowOpacity())
        self._opacity_anim.stop()
        self._pos_anim.stop()
        self._fading_out = True
        dur = max(80, int(170 * max(0.2, cur)))
        self._opacity_anim.setDuration(dur)
        self._opacity_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._opacity_anim.setStartValue(cur)
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.start()

    def _on_anim_finished(self) -> None:
        if self._fading_out:
            self._fading_out = False
            self.hide()

    def close_immediately(self) -> None:
        self._hide_timer.stop()
        self._opacity_anim.stop()
        self._pos_anim.stop()
        self._fading_out = False
        if self._thumb_connected and self._thumb_cache is not None:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_avatar_ready)
            except Exception:
                pass
        self.setWindowOpacity(0.0)
        self.hide()
        self.close()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_win32_no_activate(force_topmost=True)

    def enterEvent(self, event) -> None:
        self._hide_timer.stop()
        if self._fading_out:
            self._opacity_anim.stop()
            self._fading_out = False
            self.setWindowOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self.isVisible() and not self._fading_out:
            self._hide_timer.start(1800)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if not self.close_btn.geometry().contains(event.position().toPoint()):
                payload = dict(self._payload)
                self._hide_timer.stop()
                self._opacity_anim.stop()
                self._pos_anim.stop()
                self._fading_out = False
                self.hide()
                self.notification_clicked.emit(payload)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(1.0, 1.0, float(w - 2), float(h - 2))

        bg = QColor("#141821")
        bg.setAlpha(246)
        painter.setBrush(bg)

        border = QColor("#2E3748")
        painter.setPen(QPen(border, 1.2))
        painter.drawRoundedRect(rect, 12.0, 12.0)

        # Sutil franja superior del color de notificación elegido
        accent = QColor(self._notify_spec.border)
        accent.setAlpha(150)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(QRectF(14.0, 1.5, 44.0, 2.2), 1.1, 1.1)
        painter.end()
