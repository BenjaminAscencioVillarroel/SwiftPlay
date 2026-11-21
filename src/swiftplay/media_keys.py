"""Captura global de teclas multimedia y atajos globales prioritarios en Windows."""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal

WM_HOTKEY = 0x0312
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WH_KEYBOARD_LL = 13

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

_HOTKEY_PLAY_PAUSE = 1001
_HOTKEY_NEXT = 1002
_HOTKEY_PREV = 1003
_HOTKEY_STOP = 1004

_HOTKEY_DISCORD_MUTE = 2001
_HOTKEY_DISCORD_DEAFEN = 2002
_HOTKEY_DISCORD_HANGUP = 2003

_MOD_ALIASES: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "mayus": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
}

_SPECIAL_VKS: dict[str, tuple[int, bool]] = {
    "space": (0x20, False),
    "espacio": (0x20, False),
    "pause": (0x13, True),
    "break": (0x13, True),
    "insert": (0x2D, True),
    "ins": (0x2D, True),
    "delete": (0x2E, True),
    "del": (0x2E, True),
    "supr": (0x2E, True),
    "home": (0x24, True),
    "inicio": (0x24, True),
    "end": (0x23, True),
    "fin": (0x23, True),
    "pageup": (0x21, True),
    "pgup": (0x21, True),
    "pagedown": (0x22, True),
    "pgdown": (0x22, True),
    "scrolllock": (0x91, True),
    "scrlk": (0x91, True),
    "printscreen": (0x2C, True),
    "prtsc": (0x2C, True),
    "up": (0x26, False),
    "down": (0x28, False),
    "left": (0x25, False),
    "right": (0x27, False),
    "numpad*": (0x6A, True),
    "numpad+": (0x6B, True),
    "numpad-": (0x6D, True),
    "numpad.": (0x6E, True),
    "numpad/": (0x6F, True),
    "-": (0xBD, False),
    "=": (0xBB, False),
    "[": (0xDB, False),
    "]": (0xDD, False),
    ";": (0xBA, False),
    "'": (0xDE, False),
    ",": (0xBC, False),
    ".": (0xBE, False),
    "/": (0xBF, False),
    "`": (0xC0, False),
    "\\": (0xDC, False),
}

for _fn in range(1, 25):
    _SPECIAL_VKS[f"f{_fn}"] = (0x6F + _fn, True)
for _np in range(10):
    _SPECIAL_VKS[f"numpad{_np}"] = (0x60 + _np, False)


def parse_hotkey_string(spec: str) -> tuple[int, int] | None:
    """Convierte una cadena tipo 'Ctrl+Shift+M' o 'F9' a (win32_mods, vk).

    Devuelve None si está vacía, es inválida o usa una tecla alfanumérica sin modificador.
    """
    raw = str(spec or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    if not parts:
        return None

    mods = 0
    key_tok = ""
    for part in parts:
        low = part.lower()
        if low in _MOD_ALIASES:
            mods |= _MOD_ALIASES[low]
        elif not key_tok:
            key_tok = part
        else:
            return None

    if not key_tok:
        return None

    low_key = key_tok.lower()
    vk = 0
    allow_unmodified = False

    if low_key in _SPECIAL_VKS:
        vk, allow_unmodified = _SPECIAL_VKS[low_key]
    elif len(key_tok) == 1:
        ch = key_tok.upper()
        code = ord(ch)
        if ord("A") <= code <= ord("Z") or ord("0") <= code <= ord("9"):
            vk = code
            allow_unmodified = False
        else:
            return None
    else:
        return None

    if mods == 0 and not allow_unmodified:
        return None
    return mods, vk


_LRESULT = ctypes.c_ssize_t
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, _WPARAM, _LPARAM)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class GlobalMediaKeyFilter(QAbstractNativeEventFilter, QObject):
    play_pause_pressed = Signal()
    next_pressed = Signal()
    prev_pressed = Signal()
    stop_pressed = Signal()

    discord_mute_pressed = Signal()
    discord_deafen_pressed = Signal()
    discord_hangup_pressed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        QAbstractNativeEventFilter.__init__(self)
        QObject.__init__(self, parent)
        self._hwnd: int = 0
        self._registered_ids: list[int] = []
        self._discord_reg_ids: list[int] = []

        self._suspended = False
        self._last_fire_ts: dict[str, float] = {}

        # Estado del hook global WH_KEYBOARD_LL (prioridad absoluta por sobre otras apps/pestañas)
        self._ll_hook: Any = None
        self._ll_proc = _HOOKPROC(self._ll_keyboard_cb)
        self._ll_bindings: dict[tuple[int, int], tuple[str, Signal]] = {}
        self._ll_vks: set[int] = set()
        self._ll_down_keys: set[int] = set()

        self._user32: Any = None
        self._kernel32: Any = None
        if sys.platform == "win32":
            self._init_win32_apis()

    def _init_win32_apis(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

        user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            _HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            _WPARAM,
            _LPARAM,
        ]
        user32.CallNextHookEx.restype = _LRESULT
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

        self._user32 = user32
        self._kernel32 = kernel32

    def register(self, hwnd: int) -> None:
        if sys.platform != "win32" or not hwnd or self._hwnd:
            return
        self._hwnd = int(hwnd)
        user32 = self._user32 or ctypes.windll.user32
        mapping = (
            (_HOTKEY_PLAY_PAUSE, VK_MEDIA_PLAY_PAUSE),
            (_HOTKEY_NEXT, VK_MEDIA_NEXT_TRACK),
            (_HOTKEY_PREV, VK_MEDIA_PREV_TRACK),
            (_HOTKEY_STOP, VK_MEDIA_STOP),
        )
        for hk_id, vk in mapping:
            ok = user32.RegisterHotKey(wintypes.HWND(self._hwnd), hk_id, MOD_NOREPEAT, vk)
            if ok:
                self._registered_ids.append(hk_id)

    def set_suspended(self, suspended: bool) -> None:
        """Pausa temporalmente los atajos mientras se graba una nueva tecla en Ajustes."""
        self._suspended = bool(suspended)
        if self._suspended:
            self._ll_down_keys.clear()

    def _install_ll_hook(self) -> bool:
        if sys.platform != "win32" or self._user32 is None or self._kernel32 is None:
            return False
        if self._ll_hook:
            return True
        try:
            hmod = self._kernel32.GetModuleHandleW(None)
            hook = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._ll_proc, hmod, 0)
            if hook:
                self._ll_hook = hook
                return True
        except Exception:
            pass
        return False

    def _uninstall_ll_hook(self) -> None:
        self._ll_down_keys.clear()
        if sys.platform != "win32" or not self._ll_hook or self._user32 is None:
            self._ll_hook = None
            return
        try:
            self._user32.UnhookWindowsHookEx(self._ll_hook)
        except Exception:
            pass
        self._ll_hook = None

    def _read_async_mods(self) -> int:
        user32 = self._user32
        if user32 is None:
            return 0
        mods = 0
        if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
            mods |= MOD_CONTROL
        if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
            mods |= MOD_ALT
        if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
            mods |= MOD_SHIFT
        if (user32.GetAsyncKeyState(VK_LWIN) & 0x8000) or (
            user32.GetAsyncKeyState(VK_RWIN) & 0x8000
        ):
            mods |= MOD_WIN
        return mods

    def _emit_debounced(self, action_key: str, sig: Any) -> None:
        now = time.monotonic()
        if (now - self._last_fire_ts.get(action_key, 0.0)) < 0.18:
            return
        self._last_fire_ts[action_key] = now
        QTimer.singleShot(0, sig.emit)

    def _ll_keyboard_cb(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code == 0 and l_param:
                info = _KBDLLHOOKSTRUCT.from_address(int(l_param))
                vk = int(info.vkCode)
                if w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self._ll_down_keys.discard(vk)
                elif (
                    not self._suspended
                    and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    and vk in self._ll_vks
                ):
                    mods = self._read_async_mods()
                    binding = self._ll_bindings.get((mods, vk))
                    if binding is not None:
                        if vk not in self._ll_down_keys:
                            self._ll_down_keys.add(vk)
                            action_key, sig = binding
                            self._emit_debounced(action_key, sig)
                        return 1
        except Exception:
            pass
        user32 = self._user32
        if user32 is not None:
            return int(user32.CallNextHookEx(None, n_code, w_param, l_param))
        return 0

    def _clear_discord_bindings(self) -> None:
        if sys.platform == "win32" and self._hwnd and self._user32 is not None:
            for hk_id in self._discord_reg_ids:
                try:
                    self._user32.UnregisterHotKey(wintypes.HWND(self._hwnd), hk_id)
                except Exception:
                    pass
        self._discord_reg_ids.clear()
        self._ll_bindings.clear()
        self._ll_vks.clear()
        self._ll_down_keys.clear()

    def configure_discord_hotkeys(
        self,
        mute: str,
        deafen: str,
        hangup: str,
        *,
        mute_force_priority: bool = True,
    ) -> dict[str, str]:
        """Configura los 3 atajos globales de Discord; devuelve estado por acción."""
        self._clear_discord_bindings()
        statuses: dict[str, str] = {}
        entries = (
            ("mute", _HOTKEY_DISCORD_MUTE, mute, self.discord_mute_pressed, bool(mute_force_priority)),
            ("deafen", _HOTKEY_DISCORD_DEAFEN, deafen, self.discord_deafen_pressed, False),
            ("hangup", _HOTKEY_DISCORD_HANGUP, hangup, self.discord_hangup_pressed, False),
        )

        if sys.platform != "win32":
            for name, _hk_id, spec, _sig, _prio in entries:
                statuses[name] = "disabled" if not str(spec or "").strip() else "invalid"
            self._uninstall_ll_hook()
            return statuses

        for name, hk_id, spec, sig, prefer_hook in entries:
            raw = str(spec or "").strip()
            if not raw:
                statuses[name] = "disabled"
                continue
            parsed = parse_hotkey_string(raw)
            if parsed is None:
                statuses[name] = "invalid"
                continue
            mods, vk = parsed

            if prefer_hook and (mods, vk) not in self._ll_bindings:
                if self._install_ll_hook():
                    self._ll_bindings[(mods, vk)] = (name, sig)
                    self._ll_vks.add(vk)
                    statuses[name] = "global_hook"
                    continue

            reg_ok = False
            if self._hwnd and self._user32 is not None:
                reg_ok = bool(
                    self._user32.RegisterHotKey(
                        wintypes.HWND(self._hwnd),
                        hk_id,
                        mods | MOD_NOREPEAT,
                        vk,
                    )
                )
            if reg_ok:
                self._discord_reg_ids.append(hk_id)
                statuses[name] = "global_os"
                continue

            if (mods, vk) not in self._ll_bindings and self._install_ll_hook():
                self._ll_bindings[(mods, vk)] = (name, sig)
                self._ll_vks.add(vk)
                statuses[name] = "global_hook"
            else:
                statuses[name] = "conflict"

        if not self._ll_bindings:
            self._uninstall_ll_hook()
        return statuses

    def unregister(self) -> None:
        self._clear_discord_bindings()
        self._uninstall_ll_hook()
        if sys.platform != "win32" or not self._hwnd:
            return
        user32 = self._user32 or ctypes.windll.user32
        for hk_id in self._registered_ids:
            try:
                user32.UnregisterHotKey(wintypes.HWND(self._hwnd), hk_id)
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
                if not self._suspended:
                    if hk_id == _HOTKEY_DISCORD_MUTE:
                        self._emit_debounced("mute", self.discord_mute_pressed)
                        return True, 0
                    if hk_id == _HOTKEY_DISCORD_DEAFEN:
                        self._emit_debounced("deafen", self.discord_deafen_pressed)
                        return True, 0
                    if hk_id == _HOTKEY_DISCORD_HANGUP:
                        self._emit_debounced("hangup", self.discord_hangup_pressed)
                        return True, 0
        return False, 0
