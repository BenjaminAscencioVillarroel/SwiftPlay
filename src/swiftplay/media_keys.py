"""Captura global de teclas multimedia del teclado en Windows."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

_HOTKEY_PLAY_PAUSE = 1001
_HOTKEY_NEXT = 1002
_HOTKEY_PREV = 1003
_HOTKEY_STOP = 1004


class GlobalMediaKeyFilter(QAbstractNativeEventFilter, QObject):
    play_pause_pressed = Signal()
    next_pressed = Signal()
    prev_pressed = Signal()
    stop_pressed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        QAbstractNativeEventFilter.__init__(self)
        QObject.__init__(self, parent)
        self._hwnd: int = 0
        self._registered_ids: list[int] = []

    def register(self, hwnd: int) -> None:
        if sys.platform != "win32" or not hwnd or self._hwnd:
            return
        self._hwnd = int(hwnd)
        user32 = ctypes.windll.user32
        mapping = (
            (_HOTKEY_PLAY_PAUSE, VK_MEDIA_PLAY_PAUSE),
            (_HOTKEY_NEXT, VK_MEDIA_NEXT_TRACK),
            (_HOTKEY_PREV, VK_MEDIA_PREV_TRACK),
            (_HOTKEY_STOP, VK_MEDIA_STOP),
        )
        for hk_id, vk in mapping:
            # Intentar primero sin MOD_NOREPEAT o con MOD_NOREPEAT para máxima compatibilidad
            ok = user32.RegisterHotKey(ctypes.c_void_p(self._hwnd), hk_id, MOD_NOREPEAT, vk)
            if ok:
                self._registered_ids.append(hk_id)

    def unregister(self) -> None:
        if sys.platform != "win32" or not self._hwnd:
            return
        user32 = ctypes.windll.user32
        for hk_id in self._registered_ids:
            try:
                user32.UnregisterHotKey(ctypes.c_void_p(self._hwnd), hk_id)
            except Exception:
                pass
        self._registered_ids.clear()
        self._hwnd = 0

    def nativeEventFilter(self, event_type, message):  # type: ignore[override]
        if sys.platform != "win32":
            return False, 0
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                hk_id = int(msg.wParam)
                if hk_id == _HOTKEY_PLAY_PAUSE:
                    self.play_pause_pressed.emit()
                    return True, 0
                if hk_id == _HOTKEY_NEXT:
                    self.next_pressed.emit()
                    return True, 0
                if hk_id == _HOTKEY_PREV:
                    self.prev_pressed.emit()
                    return True, 0
                if hk_id == _HOTKEY_STOP:
                    self.stop_pressed.emit()
                    return True, 0
        return False, 0
