"""Overlay flotante click-through arriba a la izquierda para estado y participantes de Discord."""

from __future__ import annotations

import ctypes
import math
import sys
from typing import Any

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from swiftplay.paths import IS_ISOLATED_PROFILE
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


class _OverlayEditBar(QFrame):
    """Barra flotante compacta para posicionar libremente el overlay."""

    def __init__(
        self,
        palette: ThemePalette,
        *,
        on_save,
        on_reset,
    ) -> None:
        super().__init__(None)
        self._palette = palette
        self._on_save = on_save
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 12, 8)
        lay.setSpacing(10)

        msg = QLabel("Arrastra el overlay donde quieras  ·  Esc / Enter para guardar")
        msg.setStyleSheet(
            f"background: transparent; color: {palette.text}; font-size: 12px; font-weight: 600;"
        )
        lay.addWidget(msg)

        reset_btn = QPushButton("Restablecer")
        reset_btn.setObjectName("PillBtn")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(on_reset)
        lay.addWidget(reset_btn)

        save_btn = QPushButton("Guardar")
        save_btn.setObjectName("AccentBtn")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(on_save)
        lay.addWidget(save_btn)

        self._apply_theme(palette)
        self.adjustSize()

    def _apply_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"QFrame {{ background-color: {palette.surface1}; border: 1.5px solid {palette.accent}; border-radius: 12px; }}"
            f"QPushButton#PillBtn {{ background-color: {palette.surface2}; color: {palette.text}; border: 1px solid {palette.border_bright}; border-radius: 8px; padding: 5px 11px; font-weight: 600; }}"
            f"QPushButton#PillBtn:hover {{ background-color: {palette.border_bright}; }}"
            f"QPushButton#AccentBtn {{ background-color: {palette.accent}; color: {palette.accent_text}; border: none; border-radius: 8px; padding: 5px 13px; font-weight: 700; }}"
            f"QPushButton#AccentBtn:hover {{ background-color: {palette.accent_hover}; }}"
        )

    def position_top_center(self) -> None:
        self.adjustSize()
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr is not None else QRect(0, 0, 1280, 720)
        x = geo.left() + max(12, (geo.width() - self.width()) // 2)
        y = geo.top() + 18
        self.move(x, y)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (
            Qt.Key.Key_Escape,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self._on_save()
            event.accept()
            return
        super().keyPressEvent(event)


class DiscordStatusOverlay(QWidget):
    """Overlay minimalista global movible (click-through en uso normal)."""

    position_changed = Signal(int, int)
    reset_position_requested = Signal()
    edit_mode_changed = Signal(bool)

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
        self._streaming: bool = False
        self._live_pulse: float = 0.0
        self._participants: list[dict[str, Any]] = []
        self._speaking: dict[str, bool] = {}
        self._wp_spectators: list[dict[str, Any]] = []
        self._wp_count: int = 0
        self._opacity_pct: int = 25
        self._previewing: bool = False
        self._custom_pos: tuple[int, int] | None = None
        self._edit_mode: bool = False
        self._dragging: bool = False
        self._drag_offset = QPoint()
        self._edit_bar: _OverlayEditBar | None = None
        self._last_render_key: str = ""

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

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(55)
        self._live_timer.timeout.connect(self._on_live_pulse_tick)

        self.set_opacity_percent(self._opacity_pct)
        self._apply_position()

    def _on_live_pulse_tick(self) -> None:
        self._live_pulse = (self._live_pulse + 0.16) % (2.0 * math.pi)
        if self.isVisible():
            self.update()

    def _sync_live_timer(self) -> None:
        want = self.isVisible() and (
            self._streaming
            or any(
                bool(p.get("screen_share") or p.get("is_streaming"))
                for p in self._participants
            )
        )
        if want:
            if not self._live_timer.isActive():
                self._live_timer.start()
        elif self._live_timer.isActive():
            self._live_timer.stop()

    def _on_avatar_ready(self, _url: str, _pix: QPixmap) -> None:
        if self.isVisible() and self._effective_overlay_mode() == "full":
            self.update()

    def set_custom_position(self, x: int | None, y: int | None) -> None:
        if x is not None and y is not None:
            self._custom_pos = (int(x), int(y))
        else:
            self._custom_pos = None
        self._apply_position()

    def _clamp_pos_to_screens(self, x: int, y: int) -> tuple[int, int]:
        screens = QApplication.screens()
        if not screens:
            return max(0, int(x)), max(0, int(y))
        cx = int(x) + self.width() // 2
        cy = int(y) + self.height() // 2
        target_geo = None
        for scr in screens:
            g = scr.geometry()
            if g.contains(cx, cy):
                target_geo = g
                break
        if target_geo is None:
            best_dist = None
            for scr in screens:
                g = scr.geometry()
                dx = max(g.left() - cx, 0, cx - g.right())
                dy = max(g.top() - cy, 0, cy - g.bottom())
                dist = dx * dx + dy * dy
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    target_geo = g
        if target_geo is None:
            return int(x), int(y)
        max_x = max(target_geo.left() + 4, target_geo.right() - self.width() - 4)
        max_y = max(target_geo.top() + 4, target_geo.bottom() - self.height() - 4)
        clamped_x = max(target_geo.left() + 4, min(max_x, int(x)))
        clamped_y = max(target_geo.top() + 4, min(max_y, int(y)))
        return clamped_x, clamped_y

    def _apply_position(self) -> None:
        if self._custom_pos is None:
            scr = QApplication.primaryScreen()
            geo = scr.geometry() if scr is not None else None
            offset_y = 68 if IS_ISOLATED_PROFILE else 14
            tx, ty = (geo.left() + 14, geo.top() + offset_y) if geo is not None else (14, offset_y)
        else:
            tx, ty = self._clamp_pos_to_screens(*self._custom_pos)
        if self.pos() != QPoint(tx, ty):
            self.move(tx, ty)

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
            if self._edit_mode:
                target_ex = (ex & ~(_WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE)) | _WS_EX_LAYERED | _WS_EX_TOOLWINDOW
            else:
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
        self._apply_position()
        if not self.isVisible():
            self.show()
        self._apply_win32_flags(force_topmost=True)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_position()
        self._apply_win32_flags(force_topmost=True)
        self._sync_live_timer()

    def hideEvent(self, event: Any) -> None:
        if self._live_timer.isActive():
            self._live_timer.stop()
        super().hideEvent(event)

    def set_edit_mode(self, enabled: bool) -> None:
        want = bool(enabled)
        self._preview_timer.stop()
        if want:
            self._previewing = False
            self._preview_mode_override = None
            if not self._edit_mode:
                self._edit_mode = True
                self.setWindowFlags(
                    Qt.WindowType.Tool
                    | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowStaysOnTopHint
                )
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            if self._edit_bar is None:
                self._edit_bar = _OverlayEditBar(
                    self._palette,
                    on_save=lambda: self.set_edit_mode(False),
                    on_reset=self.reset_position_requested.emit,
                )
            else:
                self._edit_bar._apply_theme(self._palette)
            self._edit_bar.position_top_center()
            self._edit_bar.show()
            self._edit_bar.raise_()
            self._sync_visibility()
            self.raise_()
            self.activateWindow()
            self.edit_mode_changed.emit(True)
            return

        was_edit = self._edit_mode
        self._edit_mode = False
        self._dragging = False
        if self._edit_bar is not None:
            self._edit_bar.hide()
            self._edit_bar.deleteLater()
            self._edit_bar = None
        if was_edit:
            self.setWindowFlags(
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowTransparentForInput
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.edit_mode_changed.emit(False)
        self._sync_visibility()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._edit_mode and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._edit_mode and self._dragging:
            raw_pt = event.globalPosition().toPoint() - self._drag_offset
            nx, ny = self._clamp_pos_to_screens(raw_pt.x(), raw_pt.y())
            self.move(nx, ny)
            self._custom_pos = (nx, ny)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._edit_mode and self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            pos = self.pos()
            self._custom_pos = (pos.x(), pos.y())
            self.position_changed.emit(pos.x(), pos.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._edit_mode and event.key() in (
            Qt.Key.Key_Escape,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self.set_edit_mode(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def update_theme(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._last_render_key = ""
        if self._edit_bar is not None:
            self._edit_bar._apply_theme(palette)
        if self.isVisible():
            self.update()

    def set_opacity_percent(self, opacity_pct: int) -> None:
        val = max(10, min(100, int(opacity_pct)))
        if val == self._opacity_pct and abs(self.windowOpacity() - (val / 100.0)) < 0.01:
            return
        self._opacity_pct = val
        self.setWindowOpacity(self._opacity_pct / 100.0)

    def _resolve_mode(self, muted: bool, deafened: bool, streaming: bool = False) -> str:
        base = ""
        if muted and deafened:
            base = "both"
        elif deafened:
            base = "deafened"
        elif muted:
            base = "muted"
        if streaming:
            return f"{base}_streaming" if base else "streaming"
        return base

    def _effective_overlay_mode(self) -> str:
        if self._previewing and self._preview_mode_override in ("status", "full"):
            return self._preview_mode_override
        if self._edit_mode and self._overlay_mode == "off":
            return "status"
        return self._overlay_mode

    def _visible_participants(self) -> tuple[list[dict[str, Any]], int]:
        if self._effective_overlay_mode() != "full":
            return [], 0
        merged: list[dict[str, Any]] = []
        wp_by_norm: dict[str, list[dict[str, Any]]] = {}
        for sp in self._wp_spectators:
            if not isinstance(sp, dict):
                continue
            sp_name = str(sp.get("name") or sp.get("code") or "").strip().lower()
            wp_by_norm.setdefault(sp_name, []).append(sp)

        used_wp_codes: set[str] = set()
        if self._in_voice and self._participants:
            for part in self._participants:
                row = dict(part)
                p_name = str(row.get("name") or "").strip().lower()
                matched = next(
                    (
                        sp
                        for sp in wp_by_norm.get(p_name, ())
                        if str(sp.get("code") or "").upper() not in used_wp_codes
                    ),
                    None,
                )
                if matched is not None:
                    used_wp_codes.add(str(matched.get("code") or "").upper())
                    row["wp_kinds"] = list(matched.get("kinds") or [])
                merged.append(row)

        for sp in self._wp_spectators:
            if not isinstance(sp, dict):
                continue
            sc = str(sp.get("code") or "").upper()
            if sc in used_wp_codes:
                continue
            merged.append(
                {
                    "id": f"wp:{sc or sp.get('name') or 'guest'}",
                    "name": str(sp.get("name") or sc or "Socio"),
                    "avatar_url": str(sp.get("avatar_url") or ""),
                    "is_self": False,
                    "muted": False,
                    "deafened": False,
                    "wp_kinds": list(sp.get("kinds") or []),
                    "is_wp_only": True,
                }
            )

        if merged:
            visible = merged[:_MAX_VISIBLE_PARTICIPANTS]
            extra = max(0, len(merged) - _MAX_VISIBLE_PARTICIPANTS)
            return visible, extra

        if self._previewing or self._edit_mode:
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

    def _is_part_streaming(self, part: dict[str, Any]) -> bool:
        if str(part.get("id") or "").startswith("__prev_") or bool(part.get("is_wp_only")):
            return False if bool(part.get("is_wp_only")) else bool(part.get("_preview_streaming", False))
        if bool(part.get("is_self", False)):
            return self._streaming
        return bool(part.get("screen_share") or part.get("is_streaming"))

    def _compute_full_width(self, participants: list[dict[str, Any]]) -> int:
        font = QFont(self.font())
        font.setPointSizeF(9.5)
        font.setBold(True)
        fm = QFontMetrics(font)
        max_row_w = 108
        any_streaming = False
        any_wp_icons = False
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
            p_streaming = self._is_part_streaming(part)
            if p_streaming:
                any_streaming = True
            wp_kinds = list(part.get("wp_kinds") or [])
            if wp_kinds:
                any_wp_icons = True
            disp = format_overlay_name(str(part.get("name") or "Usuario"), _MAX_NAME_CHARS)
            text_w = min(96, fm.horizontalAdvance(disp))
            icons_w = (15 if p_muted else 0) + (15 if p_deaf else 0) + (4 if (p_muted or p_deaf) else 0)
            stream_w = 40 if p_streaming else 0
            wp_w = (len(wp_kinds) * 15) + (4 if wp_kinds and not (p_muted or p_deaf or p_streaming) else 0)
            row_w = 31 + 7 + text_w + icons_w + stream_w + wp_w + 9
            if row_w > max_row_w:
                max_row_w = row_w
        cap_w = _OVERLAY_MAX_W + (40 if any_streaming else 0) + (36 if any_wp_icons else 0)
        return max(108, min(cap_w, max_row_w))

    def _wp_status_segment_width(self, *, combined: bool) -> int:
        if self._wp_count <= 0:
            return 0
        if combined:
            return 58 if self._wp_count < 10 else 66
        return 64 if self._wp_count < 10 else 72

    def _apply_size_for_state(
        self,
        status_mode: str,
        participants: list[dict[str, Any]],
        extra_count: int,
    ) -> bool:
        if not participants:
            if not status_mode:
                base_w = 0
            elif status_mode == "streaming":
                base_w = 104
            elif status_mode in ("muted_streaming", "deafened_streaming"):
                base_w = 142
            elif status_mode == "both_streaming":
                base_w = 182
            elif status_mode == "both":
                base_w = 86
            else:
                base_w = 46
            wp_seg_w = self._wp_status_segment_width(combined=bool(base_w))
            target_w = max(46, base_w + wp_seg_w)
            target_h = 46
        else:
            target_w = self._compute_full_width(participants)
            header_h = 24 if self._wp_count > 0 else 0
            rows_h = len(participants) * _ROW_H + max(0, len(participants) - 1) * (_ROW_STEP - _ROW_H)
            extra_h = 18 if extra_count > 0 else 0
            target_h = max(36, header_h + rows_h + extra_h + 4)
        if self.width() != target_w or self.height() != target_h:
            self.setFixedSize(target_w, target_h)
            return True
        return False

    def _sync_visibility(self) -> None:
        eff_ov = self._effective_overlay_mode()
        parts, extra = self._visible_participants()
        # En "Overlay total" el estado se muestra inline en cada fila; solo mostrar badge si no hay filas activas y estás transmitiendo.
        if eff_ov == "full" and parts:
            raw_status = ""
        else:
            raw_status = self._resolve_mode(self._muted, self._deafened, self._streaming)

        has_wp = self._wp_count > 0
        if self._previewing or self._edit_mode:
            status_mode = raw_status
            if eff_ov != "full" and not status_mode and not has_wp:
                status_mode = "both_streaming" if self._streaming else "both"
            self._mode = status_mode
            self._apply_size_for_state(status_mode, parts, extra)
            self.set_opacity_percent(max(85, self._opacity_pct) if self._edit_mode else self._opacity_pct)
            self._last_render_key = ""
            if not self.isVisible():
                self._show_no_activate()
            else:
                self._apply_position()
                self._apply_win32_flags(force_topmost=True)
                self.update()
            self._sync_live_timer()
            return

        has_discord_status = bool(raw_status) and (self._active or self._streaming)
        status_mode = raw_status if has_discord_status else ""
        has_content = bool(parts) or has_discord_status or has_wp
        if eff_ov != "off" and self._enabled and has_content:
            render_key = repr((
                eff_ov,
                status_mode,
                self._opacity_pct,
                self._wp_count,
                self._custom_pos,
                self._muted,
                self._deafened,
                self._streaming,
                parts,
                extra,
                self._speaking if eff_ov == "full" else None,
            ))
            if self.isVisible() and render_key == self._last_render_key:
                self._sync_live_timer()
                return
            self._last_render_key = render_key
            self._mode = status_mode
            resized = self._apply_size_for_state(status_mode, parts, extra)
            self.set_opacity_percent(self._opacity_pct)
            if not self.isVisible():
                self._show_no_activate()
            else:
                self._apply_position()
                if resized:
                    self._apply_win32_flags(force_topmost=False)
                self.update()
            self._sync_live_timer()
        else:
            self._mode = ""
            self._last_render_key = ""
            if self.isVisible():
                self.hide()
            self._sync_live_timer()

    def update_watchparty_state(
        self,
        *,
        spectators: list[dict[str, Any]] | None = None,
    ) -> None:
        self._wp_spectators = list(spectators or [])
        self._wp_count = len(self._wp_spectators)
        self._sync_visibility()

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
        streaming: bool | None = None,
        wp_spectators: list[dict[str, Any]] | None = None,
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
        if streaming is not None:
            self._streaming = bool(streaming)
        if participants is not None:
            self._participants = list(participants)
        if speaking is not None:
            self._speaking = dict(speaking)
        if wp_spectators is not None:
            self._wp_spectators = list(wp_spectators)
            self._wp_count = len(self._wp_spectators)
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
        self._live_timer.stop()
        self._previewing = False
        self._preview_mode_override = None
        self._edit_mode = False
        self._dragging = False
        if self._edit_bar is not None:
            self._edit_bar.hide()
            self._edit_bar.deleteLater()
            self._edit_bar = None
        if self._thumb_cache is not None and self._thumb_connected:
            self._thumb_connected = False
            try:
                self._thumb_cache.ready.disconnect(self._on_avatar_ready)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)

    def _paint_live_pill(self, painter: QPainter, x: float, y: float, w: float, h: float, text: str = "EN VIVO") -> None:
        pulse = 0.5 + 0.5 * math.sin(self._live_pulse)
        pill_bg = QColor(220, 38, 38, 225)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pill_bg)
        painter.drawRoundedRect(QRectF(x, y, w, h), h / 2.0, h / 2.0)

        dot_r = 3.0 if h >= 20.0 else 2.3
        cx = x + (8.5 if h >= 20.0 else 6.5)
        cy = y + h / 2.0
        glow = QColor(255, 255, 255, int(90 + 130 * pulse))
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(cx - dot_r - 1.3, cy - dot_r - 1.3, (dot_r + 1.3) * 2.0, (dot_r + 1.3) * 2.0))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(cx - dot_r, cy - dot_r, dot_r * 2.0, dot_r * 2.0))

        txt_font = QFont(painter.font())
        txt_font.setPointSizeF(8.5 if h >= 20.0 else 7.2)
        txt_font.setBold(True)
        painter.setFont(txt_font)
        painter.setPen(QColor("#FFFFFF"))
        txt_x = int(round(cx + dot_r + 3.0))
        painter.drawText(
            QRect(txt_x, int(round(y)), max(10, int(round(x + w - txt_x - 3))), int(round(h))),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )

    def _paint_eye_counter(
        self,
        painter: QPainter,
        x: int,
        y: int,
        w: int,
        h: int,
        *,
        ico_sz: int,
        pt_size: float,
        icon_color: str | None = None,
    ) -> None:
        cnt_txt = str(max(0, self._wp_count))
        f_cnt = QFont(painter.font())
        f_cnt.setPointSizeF(pt_size)
        f_cnt.setBold(True)
        fm = QFontMetrics(f_cnt)
        gap = 5
        txt_w = fm.horizontalAdvance(cnt_txt)
        total_w = ico_sz + gap + txt_w
        start_x = x + max(0, (w - total_w) // 2)

        eye_pm = render_icon("eye", icon_color or self._palette.text, ico_sz).pixmap(
            QSize(ico_sz, ico_sz)
        )
        eye_y = y + (h - ico_sz) // 2
        painter.drawPixmap(start_x, eye_y, eye_pm)

        painter.setFont(f_cnt)
        painter.setPen(QColor(self._palette.text))
        tb = fm.tightBoundingRect(cnt_txt)
        baseline_y = int(round(y + (h / 2.0) - ((tb.top() + tb.bottom()) / 2.0)))
        painter.drawText(start_x + ico_sz + gap, baseline_y, cnt_txt)

    def _paint_status_badge(self, painter: QPainter, badge_w: int, badge_h: int, mode: str) -> None:
        has_stream = "streaming" in mode
        base_mode = mode.replace("_streaming", "") if has_stream else mode
        if base_mode == "streaming":
            base_mode = ""

        rect = QRectF(1.5, 1.5, float(badge_w) - 3.0, float(badge_h) - 3.0)
        bg = QColor(self._palette.surface2)
        border = QColor("#EF4444" if has_stream else self._palette.accent)

        painter.setBrush(bg)
        painter.setPen(QPen(border, 1.8))
        painter.drawRoundedRect(rect, 10.0, 10.0)

        wp_seg_w = self._wp_status_segment_width(combined=bool(mode))
        disc_w = badge_w - wp_seg_w

        if wp_seg_w > 0 and not mode:
            self._paint_eye_counter(
                painter,
                0,
                0,
                badge_w,
                badge_h,
                ico_sz=19,
                pt_size=11.0,
                icon_color=self._palette.text,
            )
            return

        if has_stream and not base_mode:
            self._paint_live_pill(
                painter, 10.0, (badge_h - 24.0) / 2.0, float(disc_w - 20), 24.0, "EN VIVO"
            )
        elif has_stream and base_mode:
            status_w = 84 if base_mode == "both" else 44
            if base_mode == "both":
                pm_mic = render_icon("mic_off", self._palette.text, 20).pixmap(QSize(20, 20))
                pm_deaf = render_icon("headphones_off", self._palette.text, 20).pixmap(QSize(20, 20))
                py = (badge_h - pm_mic.height()) // 2
                painter.drawPixmap(12, py, pm_mic)
                painter.setPen(QPen(QColor(self._palette.border_bright), 1.0))
                painter.drawLine(42, 12, 42, badge_h - 12)
                painter.drawPixmap(50, py, pm_deaf)
            else:
                ico_name = "headphones_off" if base_mode == "deafened" else "mic_off"
                pm = render_icon(ico_name, self._palette.text, 21).pixmap(QSize(21, 21))
                painter.drawPixmap((status_w - pm.width()) // 2, (badge_h - pm.height()) // 2, pm)

            painter.setPen(QPen(QColor(self._palette.border_bright), 1.0))
            painter.drawLine(status_w, 11, status_w, badge_h - 11)
            self._paint_live_pill(
                painter,
                float(status_w + 8),
                (badge_h - 24.0) / 2.0,
                float(disc_w - status_w - 16),
                24.0,
                "EN VIVO",
            )
        elif base_mode == "both":
            pm_mic = render_icon("mic_off", self._palette.text, 21).pixmap(QSize(21, 21))
            pm_deaf = render_icon("headphones_off", self._palette.text, 21).pixmap(
                QSize(21, 21)
            )
            py = (badge_h - pm_mic.height()) // 2
            painter.drawPixmap(14, py, pm_mic)
            painter.setPen(QPen(QColor(self._palette.border_bright), 1.0))
            painter.drawLine(disc_w // 2, 11, disc_w // 2, badge_h - 11)
            painter.drawPixmap(disc_w - 14 - pm_deaf.width(), py, pm_deaf)
        else:
            ico_name = "headphones_off" if base_mode == "deafened" else "mic_off"
            pm = render_icon(ico_name, self._palette.text, 22).pixmap(QSize(22, 22))
            px = (disc_w - pm.width()) // 2
            py = (badge_h - pm.height()) // 2
            painter.drawPixmap(px, py, pm)

        if wp_seg_w > 0:
            painter.setPen(QPen(QColor(self._palette.border_bright), 1.0))
            painter.drawLine(disc_w, 11, disc_w, badge_h - 11)
            self._paint_eye_counter(
                painter,
                disc_w,
                0,
                wp_seg_w,
                badge_h,
                ico_sz=18,
                pt_size=10.5,
                icon_color=self._palette.accent,
            )

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        parts, extra = self._visible_participants()
        status_mode = self._mode

        if not parts:
            self._paint_status_badge(painter, self.width(), self.height(), status_mode)
            if self._edit_mode:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(self._palette.accent), 2.0, Qt.PenStyle.DashLine))
                painter.drawRoundedRect(
                    QRectF(1.0, 1.0, float(self.width()) - 2.0, float(self.height()) - 2.0),
                    10.0,
                    10.0,
                )
            painter.end()
            return

        base_font = painter.font()
        name_font = QFont(base_font)
        name_font.setPointSizeF(9.5)

        rows_top = 2
        if self._wp_count > 0:
            pill_hdr_w = 46 if self._wp_count < 10 else 52
            hdr_rect = QRectF(2.0, 2.0, float(pill_hdr_w), 20.0)
            hdr_bg = QColor(self._palette.surface2)
            hdr_bg.setAlpha(215)
            painter.setBrush(hdr_bg)
            painter.setPen(QPen(QColor(self._palette.accent), 1.1))
            painter.drawRoundedRect(hdr_rect, 9.0, 9.0)
            self._paint_eye_counter(
                painter,
                2,
                2,
                pill_hdr_w,
                20,
                ico_sz=13,
                pt_size=9.0,
                icon_color=self._palette.accent,
            )
            rows_top = 26

        for idx, part in enumerate(parts):
            row_y = rows_top + idx * _ROW_STEP
            uid = str(part.get("id") or "")
            is_prev = uid.startswith("__prev_")
            is_wp_only = bool(part.get("is_wp_only"))
            is_self = bool(part.get("is_self", False))
            key = "local" if is_self else uid
            speaking = (
                False
                if is_wp_only
                else (bool(part.get("_preview_speaking", False)) or bool(self._speaking.get(key, False)))
            )
            p_muted = (
                False
                if is_wp_only
                else (
                    bool(part.get("muted", False))
                    if is_prev
                    else (self._muted if is_self else bool(part.get("muted", False)))
                )
            )
            p_deaf = (
                False
                if is_wp_only
                else (
                    bool(part.get("deafened", False))
                    if is_prev
                    else (self._deafened if is_self else bool(part.get("deafened", False)))
                )
            )
            p_streaming = self._is_part_streaming(part)
            wp_kinds = list(part.get("wp_kinds") or [])
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
                painter.setBrush(QColor(self._palette.active_bg if is_self else self._palette.surface2))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(av_x, av_y, _AVATAR_SZ, _AVATAR_SZ)
                init_ch = (disp_name.strip()[:1] or "?").upper()
                init_font = QFont(base_font)
                init_font.setPointSizeF(9.0)
                init_font.setBold(True)
                painter.setFont(init_font)
                painter.setPen(QColor(self._palette.text))
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
            stream_w = 40 if p_streaming else 0
            wp_w = (len(wp_kinds) * 15) + (4 if wp_kinds and not (p_muted or p_deaf or p_streaming) else 0)

            pill_x = 31
            pill_y = row_y + 3
            pill_h = 22
            pill_w = min(self.width() - pill_x - 2, 7 + text_w + icons_w + stream_w + wp_w + 7)
            pill_rect = QRectF(float(pill_x), float(pill_y), float(pill_w), float(pill_h))
            pill_bg = QColor(self._palette.surface1)
            pill_bg.setAlpha(155)
            painter.setBrush(QColor(16, 30, 24, 130) if speaking else pill_bg)
            if p_streaming and not speaking:
                painter.setPen(QPen(QColor(239, 68, 68, 135), 1.0))
            elif speaking:
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
            painter.setPen(QColor("#FFFFFF" if speaking else self._palette.text))
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
                icon_x += 15
            if p_streaming:
                badge_x = float(icon_x + (2 if (p_muted or p_deaf) else 0))
                self._paint_live_pill(painter, badge_x, float(pill_y + 3), 36.0, 16.0, "LIVE")
                icon_x = int(badge_x + 39)
            for wk in wp_kinds:
                if wk == "youtube":
                    wp_pm = render_icon("video", "#F87171", 13).pixmap(QSize(13, 13))
                elif wk == "twitch":
                    wp_pm = render_icon("live", "#C084FC", 13).pixmap(QSize(13, 13))
                else:
                    wp_pm = render_icon("music", self._palette.accent, 13).pixmap(QSize(13, 13))
                painter.drawPixmap(icon_x, icon_y, wp_pm)
                icon_x += 15

        if extra > 0:
            extra_y = rows_top + len(parts) * _ROW_STEP
            cap_font = QFont(base_font)
            cap_font.setPointSizeF(8.5)
            painter.setFont(cap_font)
            painter.setPen(QColor(self._palette.text2))
            painter.drawText(
                QRect(4, extra_y, self.width() - 8, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"+{extra} más",
            )

        if self._edit_mode:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(self._palette.accent), 2.0, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(
                QRectF(1.0, 1.0, float(self.width()) - 2.0, float(self.height()) - 2.0),
                8.0,
                8.0,
            )

        painter.end()
