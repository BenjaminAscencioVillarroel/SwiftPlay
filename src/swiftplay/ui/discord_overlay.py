"""Overlay flotante click-through arriba a la izquierda para silencio/ensordecido de Discord."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QShowEvent
from PySide6.QtWidgets import QApplication, QWidget

from swiftplay.themes import ThemePalette
from swiftplay.ui.icons import render_icon

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010


class DiscordStatusOverlay(QWidget):
    """Overlay minimalista global en la esquina superior izquierda (no roba foco ni clics)."""

    def __init__(self, palette: ThemePalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._mode: str = ""  # "muted" | "deafened" | "both" | ""
        self._enabled: bool = True
        self._active: bool = False
        self._muted: bool = False
        self._deafened: bool = False
        self._opacity_pct: int = 25
        self._previewing: bool = False

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(46, 46)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._end_preview)

        self.set_opacity_percent(self._opacity_pct)
        self._position_top_left()

    def _position_top_left(self) -> None:
        scr = QApplication.primaryScreen()
        geo = scr.geometry() if scr is not None else None
        if geo is not None:
            self.move(geo.left() + 14, geo.top() + 14)
        else:
            self.move(14, 14)

    def _apply_win32_flags(self, *, force_topmost: bool = False) -> None:
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
            target_ex = (
                ex
                | _WS_EX_LAYERED
                | _WS_EX_TRANSPARENT
                | _WS_EX_NOACTIVATE
                | _WS_EX_TOOLWINDOW
            )
            if target_ex != ex:
                set_long(ctypes.c_void_p(hwnd), _GWL_EXSTYLE, target_ex)

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

    def _show_no_activate(self) -> None:
        self._apply_win32_flags(force_topmost=False)
        self._position_top_left()
        if not self.isVisible():
            self.show()
        self._apply_win32_flags(force_topmost=True)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._position_top_left()
        self._apply_win32_flags(force_topmost=True)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        if self.isVisible():
            self.update()

    def set_opacity_percent(self, opacity_pct: int) -> None:
        self._opacity_pct = max(10, min(100, int(opacity_pct)))
        self.setWindowOpacity(self._opacity_pct / 100.0)

    def _resolve_mode(self, muted: bool, deafened: bool) -> str:
        if muted and deafened:
            return "both"
        if deafened:
            return "deafened"
        if muted:
            return "muted"
        return ""

    def _apply_size_for_mode(self, mode: str) -> None:
        target_w = 86 if mode == "both" else 46
        if self.width() != target_w or self.height() != 46:
            self.setFixedSize(target_w, 46)

    def _sync_visibility(self) -> None:
        if self._previewing:
            mode = self._resolve_mode(self._muted, self._deafened) or "both"
            self._mode = mode
            self._apply_size_for_mode(mode)
            self.set_opacity_percent(self._opacity_pct)
            if not self.isVisible():
                self._show_no_activate()
            else:
                self._position_top_left()
                self.update()
            return

        if self._enabled and self._active and (self._deafened or self._muted):
            mode = self._resolve_mode(self._muted, self._deafened)
            self._mode = mode
            self._apply_size_for_mode(mode)
            self.set_opacity_percent(self._opacity_pct)
            if not self.isVisible():
                self._show_no_activate()
            else:
                self._position_top_left()
                self.update()
        else:
            self._mode = ""
            if self.isVisible():
                self.hide()

    def update_state(
        self,
        *,
        enabled: bool,
        active: bool,
        muted: bool,
        deafened: bool,
        opacity_pct: int,
    ) -> None:
        self._enabled = bool(enabled)
        self._active = bool(active)
        self._muted = bool(muted)
        self._deafened = bool(deafened)
        self._opacity_pct = max(10, min(100, int(opacity_pct)))
        if not self._enabled and self._previewing:
            self._previewing = False
            self._preview_timer.stop()
        self._sync_visibility()

    def preview(self, opacity_pct: int, duration_ms: int = 1800) -> None:
        self._opacity_pct = max(10, min(100, int(opacity_pct)))
        self._previewing = True
        self._sync_visibility()
        self._preview_timer.start(max(300, int(duration_ms)))

    def _end_preview(self) -> None:
        self._previewing = False
        self._sync_visibility()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._preview_timer.stop()
        self._previewing = False
        super().closeEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(1.5, 1.5, float(self.width()) - 3.0, float(self.height()) - 3.0)
        bg = QColor(self._palette.surface2)
        border = QColor(self._palette.accent)

        painter.setBrush(bg)
        painter.setPen(QPen(border, 1.8))
        painter.drawRoundedRect(rect, 10.0, 10.0)

        if self._mode == "both":
            pm_mic = render_icon("mic_off", self._palette.text, 21).pixmap(QSize(21, 21))
            pm_deaf = render_icon("headphones_off", self._palette.text, 21).pixmap(QSize(21, 21))
            py = (self.height() - pm_mic.height()) // 2
            painter.drawPixmap(14, py, pm_mic)
            painter.setPen(QPen(QColor(self._palette.border_bright), 1.0))
            painter.drawLine(self.width() // 2, 11, self.width() // 2, self.height() - 11)
            painter.drawPixmap(self.width() - 14 - pm_deaf.width(), py, pm_deaf)
        else:
            ico_name = "headphones_off" if self._mode == "deafened" else "mic_off"
            pm = render_icon(ico_name, self._palette.text, 22).pixmap(QSize(22, 22))
            px = (self.width() - pm.width()) // 2
            py = (self.height() - pm.height()) // 2
            painter.drawPixmap(px, py, pm)
        painter.end()
