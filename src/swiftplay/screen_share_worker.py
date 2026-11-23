"""Worker aislado de compartida de pantalla para SwiftPlay.

Ejecuta FFmpeg (GPU/CPU) + captura de audio WASAPI loopback + mini-relay aiohttp
HTTP/WebSocket MSE + túnel rápido cloudflared fuera del proceso GUI (0 MB en reposo).
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

_QUALITY_PRESETS: dict[str, tuple[int, int, str, str, str]] = {
    "480p": (854, 480, "1500k", "2200k", "1100k"),
    "720p": (1280, 720, "3000k", "4200k", "2100k"),
    "1080p": (1920, 1080, "4500k", "6500k", "3200k"),
}


def _compute_even_scaled_size(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Calcula resolución objetivo par <= max_w x max_h preservando relación de aspecto."""
    cap_w = max(2, int(max_w) & ~1)
    cap_h = max(2, int(max_h) & ~1)
    if src_w <= 0 or src_h <= 0:
        return cap_w, cap_h
    scale = min(1.0, cap_w / float(src_w), cap_h / float(src_h))
    out_w = min(cap_w, max(2, int(round(src_w * scale)) & ~1))
    out_h = min(cap_h, max(2, int(round(src_h * scale)) & ~1))
    return max(2, out_w & ~1), max(2, out_h & ~1)
_ENCODER_CACHE: str | None = None
_CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def _no_window_creationflags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _assign_child_job(proc: subprocess.Popen[Any]) -> None:
    try:
        from swiftplay.windows_os import assign_process_to_child_job

        assign_process_to_child_job(proc)
    except Exception:
        pass


def _find_binary(name: str) -> Path | None:
    from swiftplay.paths import BIN_DIR, USER_DATA_DIR

    for candidate in (
        BIN_DIR / name,
        USER_DATA_DIR / "bin" / name,
    ):
        if candidate.is_file():
            return candidate
    if name == "cloudflared.exe":
        pf_candidates = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "cloudflared" / name,
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "cloudflared" / name,
        )
        for cand in pf_candidates:
            if cand.is_file():
                return cand
    found = shutil.which(name) or shutil.which(name.removesuffix(".exe"))
    return Path(found) if found else None


class Mp4FragmentSplitter:
    """Segmentador incremental ISO BMFF (fMP4): separa ftyp+moov e pares moof+mdat."""

    def __init__(self, on_fragment: Callable[[bytes], None] | None = None) -> None:
        self._buf = bytearray()
        self._ftyp_box: bytes | None = None
        self._moov_box: bytes | None = None
        self._init_segment: bytes | None = None
        self._pending_moof: bytes | None = None
        self._latest_fragment: bytes | None = None
        self._codec_mime: str = 'video/mp4; codecs="avc1.4D4028"'
        self._has_audio: bool = False
        self._on_fragment = on_fragment

    @property
    def init_segment(self) -> bytes | None:
        return self._init_segment

    @property
    def latest_fragment(self) -> bytes | None:
        return self._latest_fragment

    @property
    def codec_mime(self) -> str:
        return self._codec_mime

    @property
    def has_audio(self) -> bool:
        return self._has_audio

    def _parse_moov_codecs(self, moov: bytes) -> None:
        avc_codec = "avc1.4D4028"
        idx = moov.find(b"avcC")
        if idx != -1 and (idx + 8) <= len(moov):
            profile = moov[idx + 5]
            compat = moov[idx + 6]
            level = moov[idx + 7]
            if profile:
                avc_codec = f"avc1.{profile:02X}{compat:02X}{level:02X}"
        self._has_audio = b"mp4a" in moov
        if self._has_audio:
            self._codec_mime = f'video/mp4; codecs="{avc_codec}, mp4a.40.2"'
        else:
            self._codec_mime = f'video/mp4; codecs="{avc_codec}"'

    def feed(self, data: bytes) -> list[bytes]:
        if not data:
            return []
        self._buf.extend(data)
        emitted: list[bytes] = []
        pos = 0
        total = len(self._buf)

        while pos + 8 <= total:
            size = int.from_bytes(self._buf[pos : pos + 4], "big")
            box_type = bytes(self._buf[pos + 4 : pos + 8])
            if size == 1:
                if pos + 16 > total:
                    break
                size = int.from_bytes(self._buf[pos + 8 : pos + 16], "big")
            if size < 8:
                break
            if pos + size > total:
                break

            box = bytes(self._buf[pos : pos + size])
            pos += size

            if box_type == b"ftyp" and self._ftyp_box is None:
                self._ftyp_box = box
            elif box_type == b"moov" and self._moov_box is None:
                self._moov_box = box
                self._parse_moov_codecs(box)
                prefix = self._ftyp_box or b""
                self._init_segment = prefix + box
            elif box_type == b"moof":
                self._pending_moof = box
            elif box_type == b"mdat":
                if self._pending_moof is not None:
                    frag = self._pending_moof + box
                    self._pending_moof = None
                    self._latest_fragment = frag
                    emitted.append(frag)
                    if self._on_fragment is not None:
                        self._on_fragment(frag)

        if pos > 0:
            del self._buf[:pos]
        return emitted


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def list_capture_sources() -> list[dict[str, Any]]:
    """Lista monitores activos y ventanas visibles del escritorio de Windows."""
    if sys.platform != "win32":
        return [{"id": "monitor:0", "kind": "monitor", "index": 0, "label": "Monitor principal"}]

    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    monitors: list[dict[str, Any]] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(_RECT),
        wintypes.LPARAM,
    )

    def _on_monitor(hmon: Any, _hdc: Any, _lprect: Any, _lparam: Any) -> bool:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            w = max(1, int(r.right - r.left))
            h = max(1, int(r.bottom - r.top))
            is_primary = bool(info.dwFlags & 0x1)
            idx = len(monitors)
            monitors.append(
                {
                    "id": f"monitor:{idx}",
                    "kind": "monitor",
                    "index": idx,
                    "left": int(r.left),
                    "top": int(r.top),
                    "width": w,
                    "height": h,
                    "is_primary": is_primary,
                    "label": (
                        f"Monitor {idx + 1} (Principal) · {w}×{h}"
                        if is_primary
                        else f"Monitor {idx + 1} · {w}×{h}"
                    ),
                }
            )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_on_monitor), 0)
    monitors.sort(key=lambda m: (0 if m.get("is_primary") else 1, int(m.get("index") or 0)))
    for new_idx, mon in enumerate(monitors):
        mon["index"] = new_idx
        mon["id"] = f"monitor:{new_idx}"

    windows: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _on_window(hwnd: Any, _lparam: Any) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0 or length > 260:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title in {"Program Manager", "Settings", "Windows Input Experience"}:
            return True
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w < 120 or h < 80:
            return True
        low = title.lower()
        if low in seen_titles:
            return True
        seen_titles.add(low)
        short = title if len(title) <= 58 else title[:55] + "…"
        windows.append(
            {
                "id": f"window:{title}",
                "kind": "window",
                "title": title,
                "width": w,
                "height": h,
                "label": f"Ventana · {short}",
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_on_window), 0)
    windows.sort(key=lambda item: str(item.get("title") or "").lower())
    return monitors + windows[:32]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, text: str) -> _GUID:
        g = cls()
        ctypes.windll.ole32.CLSIDFromString(text, ctypes.byref(g))
        return g


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", _WAVEFORMATEX),
        ("Samples", wintypes.WORD),
        ("dwChannelMask", wintypes.DWORD),
        ("SubFormat", _GUID),
    ]


class _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", wintypes.DWORD),
    ]


class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", wintypes.DWORD),
        ("ProcessLoopbackParams", _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class _BLOB(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("pBlobData", ctypes.c_void_p),
    ]


class _PROPVARIANT_BLOB(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("blob", _BLOB),
    ]


class WasapiLoopbackCapture:
    """Captura loopback de salida predeterminada Windows por COM/ctypes puros.

    Cuando `exclude_pid > 0` intenta Windows Process Loopback en modo exclusión
    para capturar todo el audio del PC (juegos, videos, etc.) excluyendo el proceso
    de voz de Discord y evitar eco en la llamada. Mantiene relleno de silencio cada
    20 ms para que FFmpeg nunca se quede bloqueado cuando no hay audio activo.
    """

    def __init__(self, exclude_pid: int = 0) -> None:
        self._exclude_pid = max(0, int(exclude_pid or 0))
        self.process_excluded: bool = False
        self._ole32: Any = None
        self._enum_ptr = ctypes.c_void_p()
        self._dev_ptr = ctypes.c_void_p()
        self._ac_ptr = ctypes.c_void_p()
        self._cap_ptr = ctypes.c_void_p()
        self._wf_ptr = ctypes.c_void_p()
        self._async_op = ctypes.c_void_p()
        self._handler_refs: Any = None
        self._co_init = False
        self._running = False
        self._thread: threading.Thread | None = None

        self.ffmpeg_fmt: str = "f32le"
        self.sample_rate: int = 48000
        self.channels: int = 2
        self.bytes_per_frame: int = 8

    @staticmethod
    def _vtable(ptr: ctypes.c_void_p) -> ctypes.POINTER(ctypes.c_void_p):
        return ctypes.cast(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )

    @classmethod
    def _release_com(cls, ptr: ctypes.c_void_p) -> None:
        if ptr and ptr.value:
            try:
                vt = cls._vtable(ptr)
                rel = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
                rel(ptr)
            except Exception:
                pass
            ptr.value = None

    def _reset_com_handles(self) -> None:
        if self._ac_ptr.value:
            try:
                ac_vt = self._vtable(self._ac_ptr)
                stop_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vt[11])
                stop_fn(self._ac_ptr)
            except Exception:
                pass
        self._release_com(self._cap_ptr)
        self._release_com(self._ac_ptr)
        self._release_com(self._dev_ptr)
        self._release_com(self._enum_ptr)
        self._release_com(self._async_op)
        self._handler_refs = None
        if self._wf_ptr.value and self._ole32 is not None:
            try:
                self._ole32.CoTaskMemFree(self._wf_ptr)
            except Exception:
                pass
            self._wf_ptr.value = None

    def _open_process_loopback(self, exclude_pid: int) -> None:
        mmdevapi = ctypes.windll.mmdevapi
        iid_unk = _GUID.from_str("{00000000-0000-0000-C000-000000000046}")
        iid_agile = _GUID.from_str("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        iid_comp = _GUID.from_str("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        iid_ac = _GUID.from_str("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
        iid_cap = _GUID.from_str("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")

        ev = threading.Event()
        res_box: dict[str, int] = {}

        QI_FN = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
        )
        ADDREF_FN = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        REL_FN = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        COMP_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

        def _eq_guid(a: _GUID, b: _GUID) -> bool:
            return ctypes.string_at(ctypes.byref(a), 16) == ctypes.string_at(ctypes.byref(b), 16)

        class _HandlerVTable(ctypes.Structure):
            _fields_ = [
                ("QueryInterface", QI_FN),
                ("AddRef", ADDREF_FN),
                ("Release", REL_FN),
                ("ActivateCompleted", COMP_FN),
            ]

        class _HandlerObj(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVTable))]

        def _qi(this: Any, riid: Any, ppv: Any) -> int:
            g = riid.contents
            if _eq_guid(g, iid_unk) or _eq_guid(g, iid_comp) or _eq_guid(g, iid_agile):
                ppv[0] = this
                return 0
            ppv[0] = None
            return -2147467262  # E_NOINTERFACE

        def _addref(_this: Any) -> int:
            return 1

        def _rel(_this: Any) -> int:
            return 1

        def _completed(_this: Any, op_ptr: Any) -> int:
            try:
                op_vt = self._vtable(ctypes.c_void_p(op_ptr))
                get_res = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_long),
                    ctypes.POINTER(ctypes.c_void_p),
                )(op_vt[3])
                act_hr = ctypes.c_long(-1)
                unk_ptr = ctypes.c_void_p()
                hr_call = get_res(op_ptr, ctypes.byref(act_hr), ctypes.byref(unk_ptr))
                res_box["hr"] = int(hr_call)
                res_box["act_hr"] = int(act_hr.value)
                res_box["ac"] = int(unk_ptr.value or 0)
            except Exception:
                res_box["hr"] = -1
            finally:
                ev.set()
            return 0

        qi_cb = QI_FN(_qi)
        add_cb = ADDREF_FN(_addref)
        rel_cb = REL_FN(_rel)
        comp_cb = COMP_FN(_completed)
        vtbl = _HandlerVTable(qi_cb, add_cb, rel_cb, comp_cb)
        hobj = _HandlerObj(ctypes.pointer(vtbl))

        # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1, PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
        ap = _AUDIOCLIENT_ACTIVATION_PARAMS(
            1, _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(int(exclude_pid), 1)
        )
        pv = _PROPVARIANT_BLOB(
            65, 0, 0, 0, _BLOB(ctypes.sizeof(ap), ctypes.addressof(ap))
        )
        self._handler_refs = (qi_cb, add_cb, rel_cb, comp_cb, vtbl, hobj, ap, pv)

        act_async = mmdevapi.ActivateAudioInterfaceAsync
        act_async.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_PROPVARIANT_BLOB),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        act_async.restype = ctypes.c_long

        hr = act_async(
            "VAD" + chr(92) + "Process_Loopback",
            ctypes.byref(iid_ac),
            ctypes.byref(pv),
            ctypes.byref(hobj),
            ctypes.byref(self._async_op),
        )
        if hr < 0:
            raise RuntimeError(f"ActivateAudioInterfaceAsync falló (0x{hr & 0xFFFFFFFF:08X})")
        if not ev.wait(2.5):
            raise RuntimeError("Timeout esperando Process_Loopback")
        if res_box.get("hr", -1) < 0 or res_box.get("act_hr", -1) < 0 or not res_box.get("ac"):
            raise RuntimeError(
                f"Process_Loopback no disponible (0x{res_box.get('act_hr', -1) & 0xFFFFFFFF:08X})"
            )

        self._ac_ptr = ctypes.c_void_p(res_box["ac"])
        self.channels = 2
        self.sample_rate = 48000
        self.bytes_per_frame = 8
        self.ffmpeg_fmt = "f32le"

        wf = _WAVEFORMATEX(3, 2, 48000, 48000 * 8, 8, 32, 0)
        ac_vt = self._vtable(self._ac_ptr)
        init_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(ac_vt[3])
        hr = init_fn(self._ac_ptr, 0, 0x00020000, 2000000, 0, ctypes.byref(wf), None)
        if hr < 0:
            raise RuntimeError(f"Initialize Process_Loopback falló (0x{hr & 0xFFFFFFFF:08X})")

        get_svc = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(ac_vt[14])
        hr = get_svc(self._ac_ptr, ctypes.byref(iid_cap), ctypes.byref(self._cap_ptr))
        if hr < 0 or not self._cap_ptr.value:
            raise RuntimeError(f"IAudioCaptureClient Process_Loopback falló (0x{hr & 0xFFFFFFFF:08X})")

        start_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vt[10])
        hr = start_fn(self._ac_ptr)
        if hr < 0:
            raise RuntimeError(f"Start Process_Loopback falló (0x{hr & 0xFFFFFFFF:08X})")
        self.process_excluded = True

    def _open_endpoint_loopback(self) -> None:
        ole32 = self._ole32
        clsid_enum = _GUID.from_str("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        iid_enum = _GUID.from_str("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        iid_ac = _GUID.from_str("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
        iid_cap = _GUID.from_str("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")

        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid_enum),
            None,
            23,
            ctypes.byref(iid_enum),
            ctypes.byref(self._enum_ptr),
        )
        if hr < 0 or not self._enum_ptr.value:
            raise RuntimeError(f"MMDeviceEnumerator falló (0x{hr & 0xFFFFFFFF:08X})")

        enum_vt = self._vtable(self._enum_ptr)
        get_def = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )(enum_vt[4])
        hr = get_def(self._enum_ptr, 0, 0, ctypes.byref(self._dev_ptr))
        if hr < 0 or not self._dev_ptr.value:
            raise RuntimeError(f"Sin dispositivo de salida WASAPI (0x{hr & 0xFFFFFFFF:08X})")

        dev_vt = self._vtable(self._dev_ptr)
        activate = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(dev_vt[3])
        hr = activate(
            self._dev_ptr, ctypes.byref(iid_ac), 23, None, ctypes.byref(self._ac_ptr)
        )
        if hr < 0 or not self._ac_ptr.value:
            raise RuntimeError(f"Activate IAudioClient falló (0x{hr & 0xFFFFFFFF:08X})")

        ac_vt = self._vtable(self._ac_ptr)
        get_mix = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(ac_vt[8])
        hr = get_mix(self._ac_ptr, ctypes.byref(self._wf_ptr))
        if hr < 0 or not self._wf_ptr.value:
            raise RuntimeError(f"GetMixFormat falló (0x{hr & 0xFFFFFFFF:08X})")

        wf = ctypes.cast(self._wf_ptr, ctypes.POINTER(_WAVEFORMATEX)).contents
        self.channels = max(1, int(wf.nChannels))
        self.sample_rate = max(8000, int(wf.nSamplesPerSec))
        bits = int(wf.wBitsPerSample)
        self.bytes_per_frame = max(2, int(wf.nBlockAlign))

        is_float = False
        if wf.wFormatTag == 3:
            is_float = True
        elif wf.wFormatTag == 0xFFFE and wf.cbSize >= 22:
            wfex = ctypes.cast(self._wf_ptr, ctypes.POINTER(_WAVEFORMATEXTENSIBLE)).contents
            if int(wfex.SubFormat.Data1) == 3:
                is_float = True

        if is_float and bits == 32:
            self.ffmpeg_fmt = "f32le"
        elif bits == 16:
            self.ffmpeg_fmt = "s16le"
        elif bits == 32:
            self.ffmpeg_fmt = "s32le"
        else:
            self.ffmpeg_fmt = "f32le"

        init_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(ac_vt[3])
        # AUDCLNT_SHAREMODE_SHARED=0, AUDCLNT_STREAMFLAGS_LOOPBACK=0x00020000
        hr = init_fn(self._ac_ptr, 0, 0x00020000, 2000000, 0, self._wf_ptr, None)
        if hr < 0:
            raise RuntimeError(f"Initialize WASAPI loopback falló (0x{hr & 0xFFFFFFFF:08X})")

        get_svc = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(ac_vt[14])
        hr = get_svc(self._ac_ptr, ctypes.byref(iid_cap), ctypes.byref(self._cap_ptr))
        if hr < 0 or not self._cap_ptr.value:
            raise RuntimeError(f"IAudioCaptureClient falló (0x{hr & 0xFFFFFFFF:08X})")

        start_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(ac_vt[10])
        hr = start_fn(self._ac_ptr)
        if hr < 0:
            raise RuntimeError(f"Start WASAPI loopback falló (0x{hr & 0xFFFFFFFF:08X})")
        self.process_excluded = False

    def open(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WASAPI loopback solo disponible en Windows.")
        ole32 = ctypes.windll.ole32
        self._ole32 = ole32
        hr_co = ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
        self._co_init = hr_co in (0, 1)

        if self._exclude_pid > 0:
            try:
                self._open_process_loopback(self._exclude_pid)
                return
            except Exception:
                self._reset_com_handles()

        self._open_endpoint_loopback()

    def start_feeding(self, sink: Any) -> None:
        if not self._cap_ptr.value:
            raise RuntimeError("WASAPI loopback no inicializado.")
        self._running = True

        cap_vt = self._vtable(self._cap_ptr)
        get_next = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)
        )(cap_vt[5])
        get_buf = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        )(cap_vt[3])
        rel_buf = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT)(cap_vt[4])

        bpf = self.bytes_per_frame
        sr = self.sample_rate
        tick_s = 0.02
        silence_tick = b"\x00" * max(bpf, int(round(sr * tick_s)) * bpf)

        def _worker() -> None:
            pkt_frames = wintypes.UINT(0)
            data_ptr = ctypes.c_void_p()
            num_frames = wintypes.UINT(0)
            flags = wintypes.DWORD(0)
            dev_pos = ctypes.c_uint64(0)
            qpc_pos = ctypes.c_uint64(0)
            next_tick = time.monotonic() + tick_s
            sent_frames = 0
            start_ts = time.monotonic()

            while self._running:
                now = time.monotonic()
                sleep_s = next_tick - now
                if sleep_s > 0.001:
                    time.sleep(sleep_s)
                next_tick += tick_s
                if (time.monotonic() - next_tick) > 0.15:
                    next_tick = time.monotonic() + tick_s

                chunks: list[bytes] = []
                frames_this_tick = 0
                try:
                    for _ in range(64):
                        if not self._running:
                            break
                        hr = get_next(self._cap_ptr, ctypes.byref(pkt_frames))
                        if hr != 0 or pkt_frames.value == 0:
                            break
                        hr = get_buf(
                            self._cap_ptr,
                            ctypes.byref(data_ptr),
                            ctypes.byref(num_frames),
                            ctypes.byref(flags),
                            ctypes.byref(dev_pos),
                            ctypes.byref(qpc_pos),
                        )
                        if hr != 0:
                            break
                        n_fr = int(num_frames.value)
                        try:
                            if n_fr > 0:
                                byte_len = n_fr * bpf
                                if int(flags.value) & 0x2:  # AUDCLNT_BUFFERFLAGS_SILENT
                                    chunks.append(b"\x00" * byte_len)
                                else:
                                    chunks.append(ctypes.string_at(data_ptr.value, byte_len))
                                frames_this_tick += n_fr
                        finally:
                            rel_buf(self._cap_ptr, num_frames.value)
                        if n_fr <= 0:
                            break
                except Exception:
                    break

                if not self._running:
                    break

                if frames_this_tick > 0:
                    payload = b"".join(chunks)
                    sent_frames += frames_this_tick
                else:
                    expected_frames = int((time.monotonic() - start_ts) * sr)
                    missing = expected_frames - sent_frames
                    if missing <= 0:
                        continue
                    pad_frames = min(missing, int(sr * 0.05))
                    payload = (
                        silence_tick
                        if pad_frames * bpf == len(silence_tick)
                        else (b"\x00" * (pad_frames * bpf))
                    )
                    sent_frames += pad_frames

                try:
                    sink.write(payload)
                    sink.flush()
                except Exception:
                    break

        self._thread = threading.Thread(target=_worker, daemon=True, name="wasapi-loopback")
        self._thread.start()

    def close(self) -> None:
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        self._reset_com_handles()
        if self._co_init and self._ole32 is not None:
            try:
                self._ole32.CoUninitialize()
            except Exception:
                pass
            self._co_init = False


def probe_best_encoder(ffmpeg_path: Path) -> str:
    """Detecta el mejor codificador H.264 disponible (NVENC > AMF > QSV > libx264)."""
    global _ENCODER_CACHE
    if _ENCODER_CACHE:
        return _ENCODER_CACHE

    flags = _no_window_creationflags()
    for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
        cmd = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.15:size=320x180:rate=30",
            "-vf",
            "format=yuv420p",
            "-c:v",
            enc,
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                timeout=3.5,
                creationflags=flags,
            )
            if res.returncode == 0:
                _ENCODER_CACHE = enc
                return enc
        except Exception:
            continue
    _ENCODER_CACHE = "libx264"
    return "libx264"


def _encoder_args(encoder: str, quality: str, fps: int) -> list[str]:
    _w, _h, br, max_br, buf_sz = _QUALITY_PRESETS.get(quality, _QUALITY_PRESETS["720p"])
    gop = str(max(12, fps // 2))
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ull",
            "-zerolatency",
            "1",
            "-rc-lookahead",
            "0",
            "-rc",
            "vbr",
            "-b:v",
            br,
            "-maxrate",
            max_br,
            "-bufsize",
            buf_sz,
            "-g",
            gop,
            "-bf",
            "0",
        ]
    if encoder == "h264_amf":
        return [
            "-c:v",
            "h264_amf",
            "-usage",
            "ultralowlatency",
            "-quality",
            "speed",
            "-b:v",
            br,
            "-maxrate",
            max_br,
            "-bufsize",
            buf_sz,
            "-g",
            gop,
            "-bf",
            "0",
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v",
            "h264_qsv",
            "-preset",
            "veryfast",
            "-b:v",
            br,
            "-maxrate",
            max_br,
            "-bufsize",
            buf_sz,
            "-g",
            gop,
            "-bf",
            "0",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-profile:v",
        "main",
        "-b:v",
        br,
        "-maxrate",
        max_br,
        "-bufsize",
        buf_sz,
        "-g",
        gop,
        "-bf",
        "0",
    ]


def build_ffmpeg_command(
    ffmpeg_path: Path,
    *,
    source: dict[str, Any],
    quality: str,
    fps: int,
    encoder: str,
    capture_backend: str,
    audio_info: tuple[str, int, int] | None = None,
    synthetic_audio: bool = False,
) -> list[str]:
    w, h, _br, _max_br, _buf_sz = _QUALITY_PRESETS.get(quality, _QUALITY_PRESETS["720p"])
    threads = "2" if encoder == "libx264" else "1"
    pix_fmt = "nv12" if encoder in ("h264_nvenc", "h264_amf", "h264_qsv") else "yuv420p"
    cmd: list[str] = [str(ffmpeg_path), "-hide_banner", "-loglevel", "warning", "-threads", threads]
    kind = str(source.get("kind") or "monitor")
    src_id = str(source.get("id") or "monitor:0")
    vf: str | None = None

    if src_id == "synthetic":
        cmd += [
            "-re",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={w}x{h}:rate={fps}",
        ]
        vf = f"format={pix_fmt}"
    elif kind == "window":
        win_title = str(source.get("title") or "").strip()
        cmd += [
            "-f",
            "gdigrab",
            "-framerate",
            str(fps),
            "-draw_mouse",
            "1",
            "-i",
            f"title={win_title}",
        ]
        vf = (
            f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2:flags=fast_bilinear,format={pix_fmt}"
        )
    elif capture_backend == "ddagrab_gpu":
        idx = int(source.get("index") or 0)
        src_w = int(source.get("width") or 0)
        src_h = int(source.get("height") or 0)
        out_w, out_h = _compute_even_scaled_size(src_w, src_h, w, h)
        if encoder == "h264_nvenc" and src_w > 0 and src_h > 0 and src_w == out_w and src_h == out_h:
            dda_graph = f"ddagrab=output_idx={idx}:framerate={fps}:draw_mouse=1"
        else:
            dda_graph = (
                f"ddagrab=output_idx={idx}:framerate={fps}:draw_mouse=1,"
                f"scale_d3d11=width={out_w}:height={out_h}:format=nv12"
            )
        cmd += [
            "-f",
            "lavfi",
            "-i",
            dda_graph,
        ]
        vf = None
    elif capture_backend == "ddagrab":
        idx = int(source.get("index") or 0)
        src_w = int(source.get("width") or 0)
        src_h = int(source.get("height") or 0)
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"ddagrab=output_idx={idx}:framerate={fps}:draw_mouse=1,hwdownload,format=bgra",
        ]
        if (
            src_w > 0
            and src_h > 0
            and src_w <= w
            and src_h <= h
            and (src_w % 2 == 0)
            and (src_h % 2 == 0)
        ):
            vf = f"format={pix_fmt}"
        else:
            vf = (
                f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease"
                f":force_divisible_by=2:flags=fast_bilinear,format={pix_fmt}"
            )
    else:
        left = int(source.get("left") or 0)
        top = int(source.get("top") or 0)
        mw = int(source.get("width") or 0)
        mh = int(source.get("height") or 0)
        cmd += [
            "-f",
            "gdigrab",
            "-framerate",
            str(fps),
            "-draw_mouse",
            "1",
        ]
        if mw > 0 and mh > 0:
            cmd += [
                "-offset_x",
                str(left),
                "-offset_y",
                str(top),
                "-video_size",
                f"{mw}x{mh}",
            ]
        cmd += ["-i", "desktop"]
        if mw > 0 and mh > 0 and mw <= w and mh <= h and (mw % 2 == 0) and (mh % 2 == 0):
            vf = f"format={pix_fmt}"
        else:
            vf = (
                f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease"
                f":force_divisible_by=2:flags=fast_bilinear,format={pix_fmt}"
            )

    has_audio = False
    if synthetic_audio and src_id == "synthetic":
        cmd += ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        has_audio = True
    elif audio_info is not None:
        a_fmt, a_sr, a_ch = audio_info
        cmd += [
            "-f",
            a_fmt,
            "-ar",
            str(a_sr),
            "-ac",
            str(a_ch),
            "-i",
            "pipe:0",
        ]
        has_audio = True

    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a:0"]

    if vf:
        cmd += ["-vf", vf]
    cmd += _encoder_args(encoder, quality, fps)
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]
    else:
        cmd += ["-an"]

    cmd += [
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets",
        "1",
        "pipe:1",
    ]
    return cmd


_WATCH_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>SwiftPlay · {title_esc}</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0b0c0e;
    --surface: #14161a;
    --border: #272b33;
    --text: #f1f3f6;
    --muted: #9299a8;
    --accent: #cfd5df;
    --red: #f87171;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }}
  header {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }}
  .live-pill {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 999px;
    background: rgba(239, 68, 68, 0.16);
    border: 1px solid rgba(248, 113, 113, 0.45);
    color: var(--red);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.4px;
  }}
  .title {{ font-size: 14px; font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .meta {{ font-size: 12px; color: var(--muted); }}
  .stage {{
    position: relative;
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
    min-height: 0;
  }}
  video {{
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #000;
  }}
  .overlay-btn {{
    position: absolute;
    padding: 12px 22px;
    border-radius: 12px;
    border: 1px solid #4b5262;
    background: rgba(20, 22, 26, 0.88);
    color: var(--text);
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    backdrop-filter: blur(8px);
  }}
  .overlay-btn:hover {{ background: rgba(38, 42, 50, 0.96); }}
  .ended-box {{
    position: absolute;
    inset: 0;
    display: none;
    align-items: center;
    justify-content: center;
    background: rgba(11, 12, 14, 0.86);
    font-size: 16px;
    font-weight: 600;
    color: var(--text);
    text-align: center;
    padding: 20px;
  }}
  footer {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 16px;
    background: var(--surface);
    border-top: 1px solid var(--border);
  }}
  .btn {{
    padding: 6px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: #1c1f25;
    color: var(--text);
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
  }}
  .btn:hover {{ border-color: #4b5262; background: #262a32; }}
  input[type="range"] {{ width: 120px; accent-color: var(--accent); }}
  #status {{ font-size: 12px; color: var(--muted); flex: 1; }}
</style>
</head>
<body>
  <header>
    <span class="live-pill" id="liveBadge">● EN VIVO</span>
    <span class="title" id="streamTitle">{title_esc}</span>
    <span class="meta" id="streamMeta">{meta_esc}</span>
  </header>
  <main class="stage" id="stage">
    <video id="vid" autoplay muted playsinline></video>
    <button class="overlay-btn" id="unmuteOverlay" style="display:none;">🔊 Activar sonido</button>
    <div class="ended-box" id="endedBox">Transmisión de pantalla finalizada.</div>
  </main>
  <footer>
    <button class="btn" id="muteBtn">🔊 Silenciar</button>
    <input type="range" id="volRange" min="0" max="100" value="85" />
    <span id="status">Conectando al stream...</span>
    <button class="btn" id="syncBtn">Ir al directo</button>
    <button class="btn" id="fsBtn">Pantalla completa</button>
  </footer>
<script>
(() => {{
  const token = {token_json};
  const vid = document.getElementById("vid");
  const unmuteOverlay = document.getElementById("unmuteOverlay");
  const endedBox = document.getElementById("endedBox");
  const liveBadge = document.getElementById("liveBadge");
  const statusEl = document.getElementById("status");
  const metaEl = document.getElementById("streamMeta");
  const muteBtn = document.getElementById("muteBtn");
  const volRange = document.getElementById("volRange");
  const syncBtn = document.getElementById("syncBtn");
  const fsBtn = document.getElementById("fsBtn");
  const stage = document.getElementById("stage");

  let ended = false;
  let hasAudio = false;
  let ms = null;
  let sb = null;
  const queue = [];

  function syncAudioUi() {{
    if (!hasAudio) {{
      unmuteOverlay.style.display = "none";
      muteBtn.style.display = "none";
      volRange.style.display = "none";
      return;
    }}
    muteBtn.style.display = "";
    volRange.style.display = "";
    unmuteOverlay.style.display = vid.muted ? "" : "none";
    muteBtn.textContent = vid.muted ? "🔇 Activar sonido" : "🔊 Silenciar";
  }}

  unmuteOverlay.onclick = () => {{
    vid.muted = false;
    vid.volume = Math.max(0, Math.min(1, Number(volRange.value) / 100));
    vid.play().catch(() => {{}});
    syncAudioUi();
  }};
  muteBtn.onclick = () => {{
    vid.muted = !vid.muted;
    if (!vid.muted) vid.volume = Math.max(0, Math.min(1, Number(volRange.value) / 100));
    syncAudioUi();
  }};
  volRange.oninput = () => {{
    const v = Math.max(0, Math.min(1, Number(volRange.value) / 100));
    vid.volume = v;
    if (v > 0 && vid.muted) vid.muted = false;
    syncAudioUi();
  }};
  syncBtn.onclick = () => {{
    try {{
      if (vid.buffered.length > 0) {{
        vid.currentTime = Math.max(0, vid.buffered.end(vid.buffered.length - 1) - 0.12);
      }}
    }} catch (_e) {{}}
  }};
  fsBtn.onclick = () => {{
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {{}});
    else stage.requestFullscreen().catch(() => {{}});
  }};

  function flushQueue() {{
    if (!sb || sb.updating || queue.length === 0) return;
    try {{
      const chunk = queue.shift();
      sb.appendBuffer(chunk);
    }} catch (err) {{
      statusEl.textContent = "Reintentando buffer...";
    }}
  }}

  function setupMediaSource(mime) {{
    if (ms) return;
    if (!window.MediaSource || !MediaSource.isTypeSupported(mime)) {{
      statusEl.textContent = "Usando modo compatible HTTP...";
      vid.src = "/stream.mp4?t=" + encodeURIComponent(token);
      vid.play().catch(() => {{}});
      return;
    }}
    ms = new MediaSource();
    vid.src = URL.createObjectURL(ms);
    ms.addEventListener("sourceopen", () => {{
      try {{
        sb = ms.addSourceBuffer(mime);
        sb.mode = "segments";
        sb.addEventListener("updateend", () => {{
          try {{
            if (!sb.updating && vid.buffered.length > 0) {{
              const end = vid.buffered.end(vid.buffered.length - 1);
              if (end - vid.currentTime > 1.6) {{
                vid.currentTime = Math.max(0, end - 0.15);
              }}
              const removeEnd = vid.currentTime - 3.0;
              if (removeEnd > vid.buffered.start(0) + 1.0 && !sb.updating) {{
                sb.remove(0, removeEnd);
                return;
              }}
            }}
          }} catch (_e) {{}}
          flushQueue();
        }});
        flushQueue();
      }} catch (err) {{
        statusEl.textContent = "Error MediaSource: " + err;
      }}
    }});
  }}

  function connectWs() {{
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/ws?t=" + encodeURIComponent(token));
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {{
      statusEl.textContent = "En directo (WebSocket)";
    }};
    ws.onmessage = (ev) => {{
      if (typeof ev.data === "string") {{
        let msg = null;
        try {{ msg = JSON.parse(ev.data); }} catch (_e) {{}}
        if (!msg) return;
        if (msg.type === "reset") {{
          queue.length = 0;
          ms = null;
          sb = null;
          vid.removeAttribute("src");
          try {{ vid.load(); }} catch (_e) {{}}
          if (msg.title) {{
            const tEl = document.getElementById("streamTitle");
            if (tEl) tEl.textContent = msg.title;
          }}
          if (msg.quality || msg.fps) {{
            metaEl.textContent = (msg.quality || "") + " · " + (msg.fps || 30) + " fps";
          }}
          statusEl.textContent = "Actualizando transmisión...";
        }} else if (msg.type === "meta") {{
          hasAudio = !!msg.has_audio;
          if (msg.quality || msg.fps) {{
            metaEl.textContent = (msg.quality || "") + " · " + (msg.fps || 30) + " fps";
          }}
          syncAudioUi();
          setupMediaSource(msg.mime || 'video/mp4; codecs="avc1.4D4028"');
          statusEl.textContent = "En directo (WebSocket)";
          vid.play().catch(() => {{}});
        }} else if (msg.type === "ended") {{
          ended = true;
          liveBadge.textContent = "FINALIZADA";
          endedBox.style.display = "flex";
          statusEl.textContent = "Transmisión finalizada.";
        }}
        return;
      }}
      queue.push(new Uint8Array(ev.data));
      if (queue.length > 18) queue.splice(0, queue.length - 10);
      flushQueue();
    }};
    ws.onclose = () => {{
      if (!ended) {{
        statusEl.textContent = "Reconectando...";
        setTimeout(() => {{
          if (!ended) location.reload();
        }}, 2000);
      }}
    }};
  }}
  connectWs();
}})();
</script>
</body>
</html>
"""


_RESET_SENTINEL = b"__SWIFTPLAY_RESET__"


class ScreenShareRelay:
    """Mini servidor HTTP + WebSocket local en aiohttp (solo loopback + token secreto)."""

    def __init__(
        self,
        *,
        title: str,
        quality: str,
        fps: int,
        encoder: str,
        on_stats: Callable[[int, int], None] | None = None,
    ) -> None:
        from aiohttp import web

        self.token = secrets.token_urlsafe(16)
        self.title = title or "Pantalla compartida"
        self.quality = quality
        self.fps = fps
        self.encoder = encoder
        self._on_stats = on_stats

        self.splitter = Mp4FragmentSplitter(on_fragment=self._on_fragment_ready)
        self.init_ready = asyncio.Event()
        self._ws_queues: set[asyncio.Queue[bytes | None]] = set()
        self._http_queues: set[asyncio.Queue[bytes | None]] = set()
        self._bytes_window = 0
        self._last_stats_ts = time.monotonic()
        self._ended = False

        self._app = web.Application()
        self._app.router.add_get("/watch", self._handle_watch)
        self._app.router.add_get("/ws", self._handle_ws)
        self._app.router.add_get("/stream.mp4", self._handle_http_stream)
        self._runner: Any = None
        self._site: Any = None
        self.port: int = 0

    def reset_stream(
        self,
        *,
        title: str,
        quality: str,
        fps: int,
        encoder: str,
    ) -> None:
        """Reinicia el splitter y notifica a clientes activos sin cerrar el túnel ni cambiar la URL."""
        self.title = title or "Pantalla compartida"
        self.quality = quality
        self.fps = fps
        self.encoder = encoder
        self.splitter = Mp4FragmentSplitter(on_fragment=self._on_fragment_ready)
        self.init_ready = asyncio.Event()
        self._bytes_window = 0
        self._last_stats_ts = time.monotonic()
        for q_set in (self._ws_queues, self._http_queues):
            for q in list(q_set):
                while q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(_RESET_SENTINEL)
                except asyncio.QueueFull:
                    pass

    @property
    def viewer_count(self) -> int:
        return len(self._ws_queues) + len(self._http_queues)

    def _authorized(self, request: Any) -> bool:
        got = str(request.query.get("t") or "")
        return bool(got) and hmac.compare_digest(got, self.token)

    def _on_fragment_ready(self, frag: bytes) -> None:
        if self.splitter.init_segment is not None and not self.init_ready.is_set():
            self.init_ready.set()
        self._bytes_window += len(frag)
        for q_set in (self._ws_queues, self._http_queues):
            for q in list(q_set):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(frag)
                except asyncio.QueueFull:
                    pass
        now = time.monotonic()
        if (now - self._last_stats_ts) >= 2.0:
            elapsed = max(0.5, now - self._last_stats_ts)
            kbps = int(round((self._bytes_window * 8) / elapsed / 1000.0))
            self._bytes_window = 0
            self._last_stats_ts = now
            if self._on_stats is not None:
                self._on_stats(self.viewer_count, kbps)

    def feed_bytes(self, chunk: bytes) -> None:
        self.splitter.feed(chunk)
        if self.splitter.init_segment is not None and not self.init_ready.is_set():
            self.init_ready.set()

    def mark_ended(self) -> None:
        self._ended = True
        self.init_ready.set()
        for q_set in (self._ws_queues, self._http_queues):
            for q in list(q_set):
                while q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def start(self) -> int:
        from aiohttp import web

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        sockets = self._site._server.sockets  # type: ignore[attr-defined]
        self.port = int(sockets[0].getsockname()[1])
        return self.port

    async def close(self) -> None:
        self.mark_ended()
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
            self._site = None

    def _emit_viewer_stats(self) -> None:
        if self._on_stats is not None:
            self._on_stats(self.viewer_count, 0)

    async def _handle_watch(self, request: Any) -> Any:
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=403, text="Token inválido.")
        html_body = _WATCH_HTML_TEMPLATE.format(
            title_esc=html.escape(self.title),
            meta_esc=html.escape(f"{self.quality} · {self.fps} fps"),
            token_json=json.dumps(self.token),
        )
        return web.Response(text=html_body, content_type="text/html", charset="utf-8")

    async def _handle_ws(self, request: Any) -> Any:
        from aiohttp import WSMsgType, web

        if not self._authorized(request):
            return web.Response(status=403, text="Token inválido.")

        ws = web.WebSocketResponse(max_msg_size=1024 * 64)
        await ws.prepare(request)

        try:
            await asyncio.wait_for(self.init_ready.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            await ws.close(code=1011, message=b"Stream init timeout")
            return ws

        if self._ended or self.splitter.init_segment is None:
            await ws.send_json({"type": "ended"})
            await ws.close()
            return ws

        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=6)
        self._ws_queues.add(q)
        self._emit_viewer_stats()

        async def _drain_rx() -> None:
            try:
                async for msg in ws:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
            except Exception:
                pass

        rx_task = asyncio.create_task(_drain_rx())
        try:
            await ws.send_json(
                {
                    "type": "meta",
                    "mime": self.splitter.codec_mime,
                    "has_audio": self.splitter.has_audio,
                    "title": self.title,
                    "quality": self.quality,
                    "fps": self.fps,
                    "encoder": self.encoder,
                }
            )
            await ws.send_bytes(self.splitter.init_segment)
            if self.splitter.latest_fragment is not None:
                await ws.send_bytes(self.splitter.latest_fragment)

            while not self._ended and not ws.closed:
                get_task = asyncio.create_task(q.get())
                done, _pending = await asyncio.wait(
                    {get_task, rx_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if rx_task in done:
                    if not get_task.done():
                        get_task.cancel()
                    break
                frag = get_task.result()
                if frag is None:
                    await ws.send_json({"type": "ended"})
                    break
                if frag is _RESET_SENTINEL:
                    await ws.send_json(
                        {
                            "type": "reset",
                            "title": self.title,
                            "quality": self.quality,
                            "fps": self.fps,
                        }
                    )
                    wait_init = asyncio.create_task(self.init_ready.wait())
                    done2, _pending2 = await asyncio.wait(
                        {wait_init, rx_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if rx_task in done2:
                        if not wait_init.done():
                            wait_init.cancel()
                        break
                    if self._ended or self.splitter.init_segment is None:
                        await ws.send_json({"type": "ended"})
                        break
                    await ws.send_json(
                        {
                            "type": "meta",
                            "mime": self.splitter.codec_mime,
                            "has_audio": self.splitter.has_audio,
                            "title": self.title,
                            "quality": self.quality,
                            "fps": self.fps,
                            "encoder": self.encoder,
                        }
                    )
                    await ws.send_bytes(self.splitter.init_segment)
                    if self.splitter.latest_fragment is not None:
                        await ws.send_bytes(self.splitter.latest_fragment)
                    continue
                await ws.send_bytes(frag)
        except Exception:
            pass
        finally:
            if not rx_task.done():
                rx_task.cancel()
            self._ws_queues.discard(q)
            self._emit_viewer_stats()
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
        return ws

    async def _handle_http_stream(self, request: Any) -> Any:
        from aiohttp import web

        if not self._authorized(request):
            return web.Response(status=403, text="Token inválido.")

        try:
            await asyncio.wait_for(self.init_ready.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            return web.Response(status=503, text="Stream aún iniciando.")

        init_seg = self.splitter.init_segment
        if self._ended or init_seg is None:
            return web.Response(status=410, text="Transmisión finalizada.")

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "video/mp4",
                "Cache-Control": "no-store, no-cache",
            },
        )
        await resp.prepare(request)

        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=6)
        self._http_queues.add(q)
        self._emit_viewer_stats()
        try:
            await resp.write(init_seg)
            if self.splitter.latest_fragment is not None:
                await resp.write(self.splitter.latest_fragment)
            while not self._ended:
                frag = await q.get()
                if frag is None or frag is _RESET_SENTINEL:
                    break
                await resp.write(frag)
        except Exception:
            pass
        finally:
            self._http_queues.discard(q)
            self._emit_viewer_stats()
        return resp


class ScreenShareWorkerApp:
    """Bucle IPC asyncio del worker de compartida de pantalla."""

    def __init__(self, proto_out: Any) -> None:
        self._proto_out = proto_out
        self._write_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

        self._relay: ScreenShareRelay | None = None
        self._ffmpeg_proc: subprocess.Popen[bytes] | None = None
        self._ffmpeg_reader_thread: threading.Thread | None = None
        self._ffmpeg_stderr_tail: list[str] = []
        self._wasapi: WasapiLoopbackCapture | None = None
        self._tunnel_proc: subprocess.Popen[str] | None = None
        self._sharing = False
        self._base_url = ""
        self._port = 0
        self._current_opts: dict[str, Any] = {}
        self._ram_task: asyncio.Task[Any] | None = None

    def _emit_raw(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            self._proto_out.write(line)
            self._proto_out.flush()

    def emit_event(self, event: str, data: Any = None) -> None:
        self._emit_raw({"type": "event", "event": event, "data": data})

    def emit_result(
        self, req_id: Any, ok: bool, data: Any = None, error: str | None = None
    ) -> None:
        self._emit_raw(
            {"type": "result", "id": req_id, "ok": ok, "data": data, "error": error}
        )

    def _terminate_proc(self, proc: Any, *, close_stdin: bool = False) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if close_stdin and proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                proc.terminate()
                proc.wait(timeout=1.2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _stop_ffmpeg_only(self) -> None:
        if self._wasapi is not None:
            try:
                self._wasapi.close()
            except Exception:
                pass
            self._wasapi = None
        ff = self._ffmpeg_proc
        self._ffmpeg_proc = None
        self._terminate_proc(ff, close_stdin=True)

    def _stop_capture_procs(self) -> None:
        self._sharing = False
        self._stop_ffmpeg_only()
        tun = self._tunnel_proc
        self._tunnel_proc = None
        self._terminate_proc(tun, close_stdin=False)

    def _active_child_pids(self) -> list[int]:
        pids: list[int] = []
        for proc in (self._ffmpeg_proc, self._tunnel_proc):
            if proc is not None and proc.poll() is None:
                try:
                    pid = int(proc.pid)
                    if pid > 0:
                        pids.append(pid)
                except Exception:
                    pass
        return pids

    def _start_ram_reporter(self) -> None:
        self._stop_ram_reporter()

        async def _ram_loop() -> None:
            from swiftplay.windows_os import (
                get_pid_memory_mb,
                get_process_memory_mb,
                trim_process_memory,
            )

            tick = 0
            while self._sharing:
                try:
                    tick += 1
                    ws, priv = get_process_memory_mb()
                    child_pids = self._active_child_pids()
                    for pid in child_pids:
                        p_ws, p_priv = get_pid_memory_mb(pid)
                        ws += p_ws
                        priv += p_priv
                    if tick % 12 == 0:
                        trim_process_memory(collect_gc=False)
                    self.emit_event(
                        "worker_ram",
                        {
                            "ws_mb": round(ws, 1),
                            "priv_mb": round(priv, 1),
                            "child_pids": child_pids,
                        },
                    )
                except Exception:
                    pass
                await asyncio.sleep(2.5)

        self._ram_task = asyncio.create_task(_ram_loop())

    def _stop_ram_reporter(self) -> None:
        task = self._ram_task
        self._ram_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _stop_share(self, *, emit_idle: bool = True) -> None:
        self._sharing = False
        self._stop_ram_reporter()
        self._stop_capture_procs()
        self._base_url = ""
        self._port = 0
        self._current_opts = {}
        if self._relay is not None:
            try:
                await self._relay.close()
            except Exception:
                pass
            self._relay = None
        if emit_idle:
            self.emit_event("worker_ram", {"ws_mb": 0.0, "priv_mb": 0.0, "child_pids": []})
            self.emit_event("state", {"state": "idle"})

    async def _spawn_ffmpeg_and_wait_init(
        self,
        cmd: list[str],
        relay: ScreenShareRelay,
        *,
        use_wasapi: bool,
        exclude_pid: int = 0,
        timeout_s: float = 3.5,
    ) -> bool:
        flags = _no_window_creationflags()
        wasapi: WasapiLoopbackCapture | None = None
        if use_wasapi:
            wasapi = WasapiLoopbackCapture(exclude_pid=exclude_pid)
            wasapi.open()
            self._wasapi = wasapi

        self._ffmpeg_stderr_tail.clear()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if use_wasapi else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=flags,
        )
        _assign_child_job(proc)
        self._ffmpeg_proc = proc

        if wasapi is not None and proc.stdin is not None:
            wasapi.start_feeding(proc.stdin)

        loop = asyncio.get_running_loop()

        def _read_stdout() -> None:
            out = proc.stdout
            if out is None:
                return
            fd = out.fileno()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                if not loop.is_closed():
                    loop.call_soon_threadsafe(relay.feed_bytes, chunk)

        def _read_stderr() -> None:
            err = proc.stderr
            if err is None:
                return
            while True:
                line = err.readline()
                if not line:
                    break
                txt = line.decode("utf-8", errors="replace").strip()
                if txt:
                    self._ffmpeg_stderr_tail.append(txt)
                    if len(self._ffmpeg_stderr_tail) > 12:
                        self._ffmpeg_stderr_tail.pop(0)

        t_out = threading.Thread(target=_read_stdout, daemon=True, name="ffmpeg-stdout")
        t_err = threading.Thread(target=_read_stderr, daemon=True, name="ffmpeg-stderr")
        t_out.start()
        t_err.start()
        self._ffmpeg_reader_thread = t_out

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if relay.init_ready.is_set() and relay.splitter.init_segment is not None:
                return True
            if proc.poll() is not None:
                return False
            await asyncio.sleep(0.05)
        return relay.init_ready.is_set() and relay.splitter.init_segment is not None

    async def _start_cloudflared(self, cloudflared_path: Path, port: int) -> str:
        flags = _no_window_creationflags()
        proc = subprocess.Popen(
            [
                str(cloudflared_path),
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        _assign_child_job(proc)
        self._tunnel_proc = proc

        loop = asyncio.get_running_loop()
        found_url: asyncio.Future[str] = loop.create_future()
        tail: list[str] = []

        def _reader() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                if found_url.done():
                    continue
                txt = line.strip()
                if txt:
                    tail.append(txt)
                    if len(tail) > 12:
                        tail.pop(0)
                m = _CLOUDFLARED_URL_RE.search(txt)
                if m and not found_url.done():
                    loop.call_soon_threadsafe(found_url.set_result, m.group(0).rstrip("/"))

        t = threading.Thread(target=_reader, daemon=True, name="cloudflared-reader")
        t.start()
        try:
            return await asyncio.wait_for(found_url, timeout=18.0)
        except asyncio.TimeoutError as exc:
            msg = tail[-1] if tail else "cloudflared no devolvió URL a tiempo."
            raise RuntimeError(f"No se pudo abrir el túnel temporal: {msg}") from exc

    async def _resolve_source_dict(self, source_id: str, title: str) -> tuple[dict[str, Any], str]:
        if source_id == "synthetic":
            src: dict[str, Any] = {
                "id": "synthetic",
                "kind": "synthetic",
                "label": title or "Señal de prueba",
            }
        else:
            sources = await asyncio.to_thread(list_capture_sources)
            src = next((s for s in sources if s.get("id") == source_id), None) or (
                sources[0]
                if sources
                else {"id": "monitor:0", "kind": "monitor", "index": 0, "label": "Monitor 1"}
            )
        final_title = title or str(src.get("label") or "Pantalla compartida")
        return src, final_title

    async def _launch_capture_with_fallback(
        self,
        ffmpeg_path: Path,
        relay: ScreenShareRelay,
        *,
        source: dict[str, Any],
        source_id: str,
        quality: str,
        fps: int,
        encoder: str,
        include_audio: bool,
        exclude_pid: int,
    ) -> bool:
        audio_info: tuple[str, int, int] | None = None
        use_wasapi = False
        audio_excludes_discord = False
        if include_audio and source_id != "synthetic":
            try:
                probe_cap = WasapiLoopbackCapture(exclude_pid=exclude_pid)
                probe_cap.open()
                audio_info = (probe_cap.ffmpeg_fmt, probe_cap.sample_rate, probe_cap.channels)
                audio_excludes_discord = bool(probe_cap.process_excluded)
                probe_cap.close()
                use_wasapi = True
            except Exception as exc:
                self.emit_event(
                    "warning",
                    {"message": f"Audio del sistema no disponible ({exc}). Se transmitirá solo video."},
                )
                audio_info = None
                use_wasapi = False

        attempts: list[tuple[str, bool]]
        if source_id == "synthetic":
            attempts = [("synthetic", False)]
        elif source.get("kind") == "monitor":
            if encoder in ("h264_nvenc", "h264_amf"):
                attempts = [
                    ("ddagrab_gpu", use_wasapi),
                    ("ddagrab", use_wasapi),
                    ("gdigrab", use_wasapi),
                ]
            else:
                attempts = [("ddagrab", use_wasapi), ("gdigrab", use_wasapi)]
            if use_wasapi:
                attempts.append(("gdigrab", False))
        else:
            attempts = [("gdigrab", use_wasapi)]
            if use_wasapi:
                attempts.append(("gdigrab", False))

        started_ok = False
        attempt_errors: list[str] = []
        used_audio_fallback = False
        for backend, attempt_wasapi in attempts:
            cmd = build_ffmpeg_command(
                ffmpeg_path,
                source=source,
                quality=quality,
                fps=fps,
                encoder=encoder,
                capture_backend=backend,
                audio_info=(audio_info if attempt_wasapi else None),
                synthetic_audio=(include_audio and source_id == "synthetic"),
            )
            try:
                started_ok = await self._spawn_ffmpeg_and_wait_init(
                    cmd,
                    relay,
                    use_wasapi=attempt_wasapi,
                    exclude_pid=exclude_pid,
                    timeout_s=(2.0 if backend == "ddagrab_gpu" else 3.5),
                )
            except Exception as exc:
                self._ffmpeg_stderr_tail.append(str(exc))
                started_ok = False
            if started_ok:
                used_audio_fallback = use_wasapi and not attempt_wasapi
                break
            tail_txt = " | ".join(self._ffmpeg_stderr_tail[-4:])
            attempt_errors.append(f"[{backend} audio={attempt_wasapi}] {tail_txt or 'sin señal'}")
            self._stop_ffmpeg_only()

        if used_audio_fallback:
            audio_excludes_discord = False
            self.emit_event(
                "warning",
                {"message": "Audio WASAPI no disponible en esta captura; transmitiendo solo video."},
            )

        if not started_ok:
            try:
                from swiftplay.paths import LOGS_DIR

                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                with open(LOGS_DIR / "screen_share_diag.log", "a", encoding="utf-8") as dfh:
                    dfh.write(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] src={source_id} "
                        + " ; ".join(attempt_errors)
                        + "\n"
                    )
            except Exception:
                pass
            err_detail = (
                self._ffmpeg_stderr_tail[-1]
                if self._ffmpeg_stderr_tail
                else "FFmpeg no generó fragmentos MP4."
            )
            raise RuntimeError(f"No se pudo iniciar la captura: {err_detail}")

        return bool(relay.splitter.has_audio and audio_excludes_discord)

    def _build_sharing_payload(
        self,
        *,
        relay: ScreenShareRelay,
        base_url: str,
        port: int,
        source_id: str,
        title: str,
        quality: str,
        fps: int,
        encoder: str,
        include_audio: bool,
        audio_excludes_discord: bool,
        reconfigured: bool = False,
    ) -> dict[str, Any]:
        token_q = f"?t={relay.token}"
        out: dict[str, Any] = {
            "state": "sharing",
            "source_id": source_id,
            "title": title,
            "quality": quality,
            "fps": fps,
            "encoder": encoder,
            "include_audio": bool(include_audio),
            "has_audio": relay.splitter.has_audio,
            "audio_excludes_discord": bool(audio_excludes_discord),
            "watch_url": f"{base_url}/watch{token_q}",
            "stream_url": f"{base_url}/stream.mp4{token_q}",
            "local_watch_url": f"http://127.0.0.1:{port}/watch{token_q}",
            "local_stream_url": f"http://127.0.0.1:{port}/stream.mp4{token_q}",
            "ws_url": f"{base_url.replace('https://', 'wss://').replace('http://', 'ws://')}/ws{token_q}",
            "token": relay.token,
            "port": port,
            "child_pids": self._active_child_pids(),
        }
        if reconfigured:
            out["reconfigured"] = True
        return out

    async def _start_share(self, req: dict[str, Any]) -> dict[str, Any]:
        await self._stop_share(emit_idle=False)

        ffmpeg_path = _find_binary("ffmpeg.exe")
        if ffmpeg_path is None:
            raise RuntimeError("No se encontró bin/ffmpeg.exe para capturar la pantalla.")

        use_tunnel = bool(req.get("use_tunnel", True))
        cloudflared_path: Path | None = None
        if use_tunnel:
            cloudflared_path = _find_binary("cloudflared.exe")
            if cloudflared_path is None:
                raise RuntimeError(
                    "Falta cloudflared.exe. Descárgalo desde el diálogo de Compartir pantalla."
                )

        source_id = str(req.get("source_id") or "monitor:0").strip()
        quality = str(req.get("quality") or "720p").strip()
        if quality not in _QUALITY_PRESETS:
            quality = "720p"
        fps = 60 if int(req.get("fps") or 30) >= 60 else 30
        include_audio = bool(req.get("include_audio", True))
        exclude_pid = max(0, int(req.get("exclude_pid") or 0))
        req_title = str(req.get("title") or "").strip()

        source, title = await self._resolve_source_dict(source_id, req_title)
        self.emit_event("state", {"state": "starting", "title": title})
        encoder = await asyncio.to_thread(probe_best_encoder, ffmpeg_path)

        last_viewers = 0
        last_kbps = 0

        def _on_relay_stats(viewers: int, kbps: int) -> None:
            nonlocal last_viewers, last_kbps
            last_viewers = viewers
            if kbps > 0:
                last_kbps = kbps
            cur_opts = self._current_opts
            self.emit_event(
                "stats",
                {
                    "viewers": last_viewers,
                    "bitrate_kbps": last_kbps,
                    "encoder": encoder,
                    "quality": str(cur_opts.get("quality") or quality),
                    "fps": int(cur_opts.get("fps") or fps),
                },
            )

        relay = ScreenShareRelay(
            title=title,
            quality=quality,
            fps=fps,
            encoder=encoder,
            on_stats=_on_relay_stats,
        )
        port = await relay.start()
        self._relay = relay
        self._port = port

        try:
            audio_excludes_discord = await self._launch_capture_with_fallback(
                ffmpeg_path,
                relay,
                source=source,
                source_id=source_id,
                quality=quality,
                fps=fps,
                encoder=encoder,
                include_audio=include_audio,
                exclude_pid=exclude_pid,
            )
        except Exception:
            await self._stop_share(emit_idle=False)
            raise

        base_url = f"http://127.0.0.1:{port}"
        if use_tunnel and cloudflared_path is not None:
            try:
                base_url = await self._start_cloudflared(cloudflared_path, port)
            except Exception:
                await self._stop_share(emit_idle=False)
                raise

        self._base_url = base_url
        self._current_opts = {
            "source_id": source_id,
            "quality": quality,
            "fps": fps,
            "include_audio": include_audio,
            "title": title,
            "exclude_pid": exclude_pid,
        }
        result = self._build_sharing_payload(
            relay=relay,
            base_url=base_url,
            port=port,
            source_id=source_id,
            title=title,
            quality=quality,
            fps=fps,
            encoder=encoder,
            include_audio=include_audio,
            audio_excludes_discord=audio_excludes_discord,
            reconfigured=False,
        )
        self._sharing = True
        self._start_ram_reporter()
        self.emit_event("state", result)
        return result

    async def _reconfigure_share(self, req: dict[str, Any]) -> dict[str, Any]:
        if not self._sharing or self._relay is None or not self._base_url:
            return await self._start_share(req)

        ffmpeg_path = _find_binary("ffmpeg.exe")
        if ffmpeg_path is None:
            raise RuntimeError("No se encontró bin/ffmpeg.exe.")

        prev = dict(self._current_opts)
        source_id = str(req.get("source_id") or prev.get("source_id") or "monitor:0").strip()
        quality = str(req.get("quality") or prev.get("quality") or "720p").strip()
        if quality not in _QUALITY_PRESETS:
            quality = "720p"
        fps_val = req.get("fps") if "fps" in req else prev.get("fps", 30)
        fps = 60 if int(fps_val or 30) >= 60 else 30
        include_audio = bool(
            req.get("include_audio") if "include_audio" in req else prev.get("include_audio", True)
        )
        exclude_pid = max(0, int(req.get("exclude_pid") or prev.get("exclude_pid") or 0))
        req_title = str(req.get("title") or "").strip()

        source, title = await self._resolve_source_dict(source_id, req_title)
        encoder = await asyncio.to_thread(probe_best_encoder, ffmpeg_path)
        relay = self._relay

        self._stop_ffmpeg_only()
        relay.reset_stream(title=title, quality=quality, fps=fps, encoder=encoder)
        audio_excludes_discord = await self._launch_capture_with_fallback(
            ffmpeg_path,
            relay,
            source=source,
            source_id=source_id,
            quality=quality,
            fps=fps,
            encoder=encoder,
            include_audio=include_audio,
            exclude_pid=exclude_pid,
        )

        self._current_opts = {
            "source_id": source_id,
            "quality": quality,
            "fps": fps,
            "include_audio": include_audio,
            "title": title,
            "exclude_pid": exclude_pid,
        }
        result = self._build_sharing_payload(
            relay=relay,
            base_url=self._base_url,
            port=self._port,
            source_id=source_id,
            title=title,
            quality=quality,
            fps=fps,
            encoder=encoder,
            include_audio=include_audio,
            audio_excludes_discord=audio_excludes_discord,
            reconfigured=True,
        )
        self.emit_event("state", result)
        return result

    async def handle_command(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        cmd = str(req.get("cmd") or "")
        try:
            if cmd == "ping":
                if req_id is not None:
                    self.emit_result(req_id, True, "pong")
                return
            if cmd == "list_sources":
                sources = await asyncio.to_thread(list_capture_sources)
                if req_id is not None:
                    self.emit_result(req_id, True, sources)
                return
            if cmd == "start":
                data = await self._start_share(req)
                if req_id is not None:
                    self.emit_result(req_id, True, data)
                return
            if cmd == "reconfigure":
                data = await self._reconfigure_share(req)
                if req_id is not None:
                    self.emit_result(req_id, True, data)
                return
            if cmd == "stop":
                await self._stop_share(emit_idle=True)
                if req_id is not None:
                    self.emit_result(req_id, True, None)
                return
            if cmd == "shutdown":
                await self._stop_share(emit_idle=False)
                if req_id is not None:
                    self.emit_result(req_id, True, None)
                if self._stop_event is not None:
                    self._stop_event.set()
                return
            if req_id is not None:
                self.emit_result(req_id, False, error=f"Comando desconocido: {cmd}")
        except Exception as exc:
            self.emit_event("error", {"message": str(exc)})
            if req_id is not None:
                self.emit_result(req_id, False, error=str(exc))

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        def _stdin_reader() -> None:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                raw = line.strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                except Exception:
                    continue
                if self._loop is not None and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self.handle_command(req), self._loop)
            if (
                self._loop is not None
                and not self._loop.is_closed()
                and self._stop_event is not None
            ):
                self._loop.call_soon_threadsafe(self._stop_event.set)

        t = threading.Thread(target=_stdin_reader, daemon=True, name="screenshare-stdin")
        t.start()

        await self._stop_event.wait()
        await self._stop_share(emit_idle=False)


def main() -> int:
    import locale

    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    proto_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass

    app = ScreenShareWorkerApp(proto_out)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
