"""Overlay flotante click-through arriba a la izquierda para estado y participantes de Discord."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QShowEvent
from PySide6.QtWidgets import QApplication, QWidget

from swiftplay.themes import ThemePalette
from swiftplay.thumb_cache import ThumbnailCache
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

_MAX_VISIBLE_PARTICIPANTS = 8
_MAX_NAME_CHARS = 10
_AVATAR_SZ = 24
_ROW_H = 28
_ROW_STEP = 32
_OVERLAY_MAX_W = 184


def format_overlay_name(name: str, max_chars: int = _MAX_NAME_CHARS) -> str:
    """Acorta un nombre para el overlay a un máximo de caracteres (10 por defecto)."""
    clean = str(name or "").strip() or "Usuario"
    if len(clean) <= max_chars:
        return clean
    return clean[: max(1, max_chars - 1)].rstrip() + "…"


class DiscordStatusOverlay(QWidget):
    """Overlay minimalista global en la esquina superior izquierda (no roba foco ni clics)."""

    def __init__(
        self,
        palette: ThemePalette,
        thumb_cache: ThumbnailCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._thumb_cache = thumb_cache
        self._thumb_connected = False
        self._mode: str = ""  # "muted" | "deafened" | "both" | ""
        self._overlay_mode: str = "status"  # "off" | "status" | "full"
        self._preview_mode_override: str | None = None
        self._enabled: bool = True
        self._active: bool = False
        self._in_voice: bool = False
        self._muted: bool = False
        self._deafened: bool = False
        self._participants: list[dict[str, Any]] = []
        self._speaking: dict[str, bool] = {}
        self._opacity_pct: int = 25
        self._previewing: bool = False

        if self._thumb_cache is not None:
            self._thumb_cache.ready.connect(self._on_avatar_ready)
            self._thumb_connected = True

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

    def _on_avatar_ready(self, _url: str, _pix: QPixmap) -> None:
        if self.isVisible() and self._effective_overlay_mode() == "full":
            self.update()

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

    def _effective_overlay_mode(self) -> str:
        if self._previewing and self._preview_mode_override in ("status", "full"):
            return self._preview_mode_override
        return self._overlay_mode

    def _visible_participants(self) -> tuple[list[dict[str, Any]], int]:
        if self._effective_overlay_mode() != "full":
            return [], 0
        if self._in_voice and self._participants:
            visible = self._participants[:_MAX_VISIBLE_PARTICIPANTS]
            extra = max(0, len(self._participants) - _MAX_VISIBLE_PARTICIPANTS)
            return visible, extra
        if self._previewing:
            return [
                {
                    "id": "__prev_1",
                    "name": "Voz 1",
                    "avatar_url": "",
                    "is_self": False,
                    "muted": False,
                    "deafened": False,
                    "_preview_speaking": True,
                },
                {
                    "id": "__prev_2",
                    "name": "Voz 2",
                    "avatar_url": "",
                    "is_self": True,
                    "muted": True if not (self._muted or self._deafened) else self._muted,
                    "deafened": self._deafened,
                    "_preview_speaking": False,
                },
            ], 0
        return [], 0

    def _compute_full_width(self, participants: list[dict[str, Any]]) -> int:
        font = QFont(self.font())
        font.setPointSizeF(9.5)
        font.setBold(True)
        fm = QFontMetrics(font)
        max_row_w = 108
        for part in participants:
            is_self = bool(part.get("is_self", False))
            is_prev = str(part.get("id") or "").startswith("__prev_")
            p_muted = (
                bool(part.get("muted", False))
                if is_prev
                else (self._muted if is_self else bool(part.get("muted", False)))
            )
            p_deaf = (
                bool(part.get("deafened", False))
                if is_prev
                else (self._deafened if is_self else bool(part.get("deafened", False)))
            )
            disp = format_overlay_name(str(part.get("name") or "Usuario"), _MAX_NAME_CHARS)
            text_w = min(96, fm.horizontalAdvance(disp))
            icons_w = (15 if p_muted else 0) + (15 if p_deaf else 0) + (4 if (p_muted or p_deaf) else 0)
            row_w = 31 + 7 + text_w + icons_w + 9
            if row_w > max_row_w:
                max_row_w = row_w
        return max(108, min(_OVERLAY_MAX_W, max_row_w))

    def _apply_size_for_state(
        self,
        status_mode: str,
        participants: list[dict[str, Any]],
        extra_count: int,
    ) -> None:
        if not participants:
            target_w = 86 if status_mode == "both" else 46
            target_h = 46
        else:
            target_w = self._compute_full_width(participants)
            rows_h = len(participants) * _ROW_H + max(0, len(participants) - 1) * (_ROW_STEP - _ROW_H)
            extra_h = 18 if extra_count > 0 else 0
            target_h = max(36, rows_h + extra_h + 4)
        if self.width() != target_w or self.height() != target_h:
            self.setFixedSize(target_w, target_h)

    def _sync_visibility(self) -> None:
        eff_ov = self._effective_overlay_mode()
        parts, extra = self._visible_participants()
        # En "Overlay total" el estado se muestra inline en cada fila; no duplicar el cuadro aparte.
        status_mode = "" if eff_ov == "full" else self._resolve_mode(self._muted, self._deafened)

        if self._previewing:
            if eff_ov != "full" and not status_mode:
                status_mode = "both"
            self._mode = status_mode
            self._apply_size_for_state(status_mode, parts, extra)
            self.set_opacity_percent(self._opacity_pct)
            if not self.isVisible():
                self._show_no_activate()
            else:
                self._position_top_left()
                self.update()
            return

        has_content = bool(parts) if eff_ov == "full" else bool(status_mode)
        if eff_ov != "off" and self._enabled and self._active and has_content:
            self._mode = status_mode
            self._apply_size_for_state(status_mode, parts, extra)
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
        enabled: bool | None = None,
        overlay_mode: str | None = None,
        active: bool,
        in_voice: bool | None = None,
        muted: bool,
        deafened: bool,
        opacity_pct: int,
        participants: list[dict[str, Any]] | None = None,
        speaking: dict[str, bool] | None = None,
    ) -> None:
        if overlay_mode is not None:
            ov = str(overlay_mode).strip().lower()
            if ov not in ("off", "status", "full"):
                ov = "status"
            self._overlay_mode = ov
            self._enabled = ov != "off"
        elif enabled is not None:
            self._enabled = bool(enabled)
            self._overlay_mode = "status" if self._enabled else "off"

        self._active = bool(active)
        if in_voice is not None:
            self._in_voice = bool(in_voice)
        self._muted = bool(muted)
        self._deafened = bool(deafened)
        if participants is not None:
            self._participants = list(participants)
        if speaking is not None:
            self._speaking = dict(speaking)
        self._opacity_pct = max(10, min(100, int(opacity_pct)))
        if (not self._enabled or self._overlay_mode == "off") and self._previewing:
            self._previewing = False
            self._preview_mode_override = None
            self._preview_timer.stop()
        self._sync_visibility()

    def update_speaking(self, speaking_map: dict[str, bool]) -> None:
        self._speaking = dict(speaking_map)
        if self.isVisible() and self._effective_overlay_mode() == "full":
            self.update()

    def preview(
        self,
        opacity_pct: int,
        duration_ms: int = 1800,
        overlay_mode: str | None = None,
    ) -> None:
        self._opacity_pct = max(10, min(100, int(opacity_pct)))
        ov = str(overlay_mode or self._overlay_mode or "status").strip().lower()
        self._preview_mode_override = ov if ov in ("status", "full") else "status"
        self._previewing = True
        self._sync_visibility()
        self._preview_timer.start(max(300, int(duration_ms)))

    def _end_preview(self) -> None:
        self._previewing = False
        self._preview_mode_override = None
        self._sync_visibility()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._preview_timer.stop()
        self._previewing = False
        self._preview_mode_override = None
        if self._thumb_cache is not None and self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_avatar_ready)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)

    def _paint_status_badge(self, painter: QPainter, badge_w: int, badge_h: int, mode: str) -> None:
        rect = QRectF(1.5, 1.5, float(badge_w) - 3.0, float(badge_h) - 3.0)
        bg = QColor(self._palette.surface2)
        border = QColor(self._palette.accent)

        painter.setBrush(bg)
        painter.setPen(QPen(border, 1.8))
        painter.drawRoundedRect(rect, 10.0, 10.0)

        if mode == "both":
            pm_mic = render_icon("mic_off", self._palette.text, 21).pixmap(QSize(21, 21))
            pm_deaf = render_icon("headphones_off", self._palette.text, 21).pixmap(
                QSize(21, 21)
            )
            py = (badge_h - pm_mic.height()) // 2
            painter.drawPixmap(14, py, pm_mic)
            painter.setPen(QPen(QColor(self._palette.border_bright), 1.0))
            painter.drawLine(badge_w // 2, 11, badge_w // 2, badge_h - 11)
            painter.drawPixmap(badge_w - 14 - pm_deaf.width(), py, pm_deaf)
        else:
            ico_name = "headphones_off" if mode == "deafened" else "mic_off"
            pm = render_icon(ico_name, self._palette.text, 22).pixmap(QSize(22, 22))
            px = (badge_w - pm.width()) // 2
            py = (badge_h - pm.height()) // 2
            painter.drawPixmap(px, py, pm)

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        parts, extra = self._visible_participants()
        status_mode = self._mode

        if not parts:
            self._paint_status_badge(painter, self.width(), self.height(), status_mode)
            painter.end()
            return

        base_font = painter.font()
        name_font = QFont(base_font)
        name_font.setPointSizeF(9.5)

        for idx, part in enumerate(parts):
            row_y = 2 + idx * _ROW_STEP
            uid = str(part.get("id") or "")
            is_prev = uid.startswith("__prev_")
            is_self = bool(part.get("is_self", False))
            key = "local" if is_self else uid
            speaking = bool(part.get("_preview_speaking", False)) or bool(self._speaking.get(key, False))
            p_muted = (
                bool(part.get("muted", False))
                if is_prev
                else (self._muted if is_self else bool(part.get("muted", False)))
            )
            p_deaf = (
                bool(part.get("deafened", False))
                if is_prev
                else (self._deafened if is_self else bool(part.get("deafened", False)))
            )
            disp_name = format_overlay_name(str(part.get("name") or "Usuario"), _MAX_NAME_CHARS)
            av_url = str(part.get("avatar_url") or "")

            av_x = 2
            av_y = row_y + 2
            av_pix: QPixmap | None = None
            if av_url and self._thumb_cache is not None:
                av_pix = self._thumb_cache.get_or_fetch(
                    av_url, QSize(_AVATAR_SZ, _AVATAR_SZ), circular=True
                )
            if av_pix is not None and not av_pix.isNull():
                painter.drawPixmap(av_x, av_y, av_pix)
            else:
                painter.setBrush(QColor("#28354A" if is_self else "#333C4D"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(av_x, av_y, _AVATAR_SZ, _AVATAR_SZ)
                init_ch = (disp_name.strip()[:1] or "?").upper()
                init_font = QFont(base_font)
                init_font.setPointSizeF(9.0)
                init_font.setBold(True)
                painter.setFont(init_font)
                painter.setPen(QColor("#F1F3F6"))
                painter.drawText(
                    QRect(av_x, av_y, _AVATAR_SZ, _AVATAR_SZ),
                    Qt.AlignmentFlag.AlignCenter,
                    init_ch,
                )

            if speaking:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#4ADE80"), 2.0))
                painter.drawEllipse(
                    QRectF(
                        float(av_x) - 0.5,
                        float(av_y) - 0.5,
                        float(_AVATAR_SZ) + 1.0,
                        float(_AVATAR_SZ) + 1.0,
                    )
                )

            name_font.setBold(speaking)
            painter.setFont(name_font)
            fm = QFontMetrics(name_font)
            text_w = min(96, fm.horizontalAdvance(disp_name))
            icons_w = (15 if p_muted else 0) + (15 if p_deaf else 0) + (4 if (p_muted or p_deaf) else 0)

            pill_x = 31
            pill_y = row_y + 3
            pill_h = 22
            pill_w = min(self.width() - pill_x - 2, 7 + text_w + icons_w + 7)
            pill_rect = QRectF(float(pill_x), float(pill_y), float(pill_w), float(pill_h))
            painter.setBrush(QColor(16, 30, 24, 115) if speaking else QColor(12, 15, 22, 95))
            if speaking:
                painter.setPen(QPen(QColor(74, 222, 128, 120), 1.0))
            else:
                painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(pill_rect, 6.0, 6.0)

            text_rect = QRect(pill_x + 7, pill_y, text_w + 2, pill_h)
            painter.setPen(QColor(0, 0, 0, 155))
            painter.drawText(
                text_rect.translated(1, 1),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                disp_name,
            )
            painter.setPen(QColor("#FFFFFF" if speaking else "#E8EDF5"))
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                disp_name,
            )

            icon_x = pill_x + 7 + text_w + 5
            icon_y = pill_y + (pill_h - 13) // 2
            if p_muted:
                pm_m = render_icon("mic_off", "#F87171", 13).pixmap(QSize(13, 13))
                painter.drawPixmap(icon_x, icon_y, pm_m)
                icon_x += 15
            if p_deaf:
                pm_d = render_icon("headphones_off", "#F87171", 13).pixmap(QSize(13, 13))
                painter.drawPixmap(icon_x, icon_y, pm_d)

        if extra > 0:
            extra_y = 2 + len(parts) * _ROW_STEP
            cap_font = QFont(base_font)
            cap_font.setPointSizeF(8.5)
            painter.setFont(cap_font)
            painter.setPen(QColor(self._palette.text2))
            painter.drawText(
                QRect(4, extra_y, self.width() - 8, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"+{extra} más",
            )

        painter.end()
