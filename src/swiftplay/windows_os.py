"""Integración con Windows: inicio automático, acceso directo, RAM en vivo y explorador."""

from __future__ import annotations

import ctypes
import gc
import math
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from swiftplay.paths import APP_NAME, APP_ROOT, ICON_ICO_PATH, LAUNCH_SCRIPT_PATH

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "SwiftPlay"
APP_USER_MODEL_ID = "SwiftPlay.App.Desktop"

_VT_EMPTY = 0
_VT_LPWSTR = 31
_PID_RELAUNCH_COMMAND = 2
_PID_RELAUNCH_ICON = 3
_PID_RELAUNCH_DISPLAY_NAME = 4
_PID_APP_USER_MODEL_ID = 5


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [
        ("fmtid", _GUID),
        ("pid", wintypes.DWORD),
    ]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("pwszVal", wintypes.LPWSTR),
        ("_pad", ctypes.c_void_p),
    ]


_IID_IPROPERTYSTORE = _GUID(
    0x886D8EEB,
    0x8CF2,
    0x4446,
    (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99),
)
_FMTID_APPUSERMODEL = _GUID(
    0x9F4C2855,
    0x9F79,
    0x4B39,
    (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3),
)

_RELEASE_PROTO = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
_SET_VALUE_PROTO = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.c_void_p,
    ctypes.POINTER(_PROPERTYKEY),
    ctypes.POINTER(_PROPVARIANT),
)
_COMMIT_PROTO = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)


def _write_property_store(
    store_ptr: ctypes.c_void_p,
    entries: list[tuple[int, str | None]],
) -> bool:
    """Escribe propiedades en un IPropertyStore COM y libera la interfaz."""
    if not store_ptr:
        return False
    vtable_addr = ctypes.cast(store_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    vtable = ctypes.cast(vtable_addr, ctypes.POINTER(ctypes.c_void_p))
    release_fn = _RELEASE_PROTO(vtable[2])
    set_value_fn = _SET_VALUE_PROTO(vtable[6])
    commit_fn = _COMMIT_PROTO(vtable[7])

    keepalive: list[object] = []
    try:
        for pid, value in entries:
            pkey = _PROPERTYKEY(_FMTID_APPUSERMODEL, pid)
            pv = _PROPVARIANT()
            if value is None:
                pv.vt = _VT_EMPTY
            else:
                buf = ctypes.create_unicode_buffer(value)
                keepalive.append(buf)
                pv.vt = _VT_LPWSTR
                pv.pwszVal = ctypes.cast(buf, wintypes.LPWSTR)
            hr = set_value_fn(store_ptr, ctypes.byref(pkey), ctypes.byref(pv))
            if hr < 0:
                return False
        return commit_fn(store_ptr) >= 0
    finally:
        release_fn(store_ptr)


def set_process_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Asigna el AppUserModelID explícito del proceso antes de crear QApplication."""
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        return shell32.SetCurrentProcessExplicitAppUserModelID(app_id) >= 0
    except Exception:
        return False


def apply_window_taskbar_properties(hwnd: int) -> bool:
    """Fija AppUserModelID, comando de relanzamiento e ícono en el HWND para anclado."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        store_ptr = ctypes.c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(
            wintypes.HWND(hwnd),
            ctypes.byref(_IID_IPROPERTYSTORE),
            ctypes.byref(store_ptr),
        )
        if hr < 0 or not store_ptr:
            return False
        return _write_property_store(
            store_ptr,
            [
                (_PID_APP_USER_MODEL_ID, APP_USER_MODEL_ID),
                (_PID_RELAUNCH_COMMAND, build_launch_command()),
                (_PID_RELAUNCH_DISPLAY_NAME, APP_NAME),
                (_PID_RELAUNCH_ICON, f"{ICON_ICO_PATH},0"),
            ],
        )
    except Exception:
        return False


def clear_window_taskbar_properties(hwnd: int) -> None:
    """Limpia las propiedades de barra de tareas del HWND al cerrar la app."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        store_ptr = ctypes.c_void_p()
        hr = shell32.SHGetPropertyStoreForWindow(
            wintypes.HWND(hwnd),
            ctypes.byref(_IID_IPROPERTYSTORE),
            ctypes.byref(store_ptr),
        )
        if hr >= 0 and store_ptr:
            _write_property_store(
                store_ptr,
                [
                    (_PID_APP_USER_MODEL_ID, None),
                    (_PID_RELAUNCH_COMMAND, None),
                    (_PID_RELAUNCH_DISPLAY_NAME, None),
                    (_PID_RELAUNCH_ICON, None),
                ],
            )
    except Exception:
        pass


def set_shortcut_app_user_model_id(
    shortcut_path: Path, app_id: str = APP_USER_MODEL_ID
) -> bool:
    """Estampa el AppUserModelID directamente en un archivo .lnk."""
    if sys.platform != "win32" or not shortcut_path.exists():
        return False
    co_hr = -1
    ole32 = None
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        co_hr = ole32.CoInitializeEx(None, 0x2)

        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreFromParsingName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long
        store_ptr = ctypes.c_void_p()
        hr = shell32.SHGetPropertyStoreFromParsingName(
            str(shortcut_path),
            None,
            2,  # GPS_READWRITE
            ctypes.byref(_IID_IPROPERTYSTORE),
            ctypes.byref(store_ptr),
        )
        if hr < 0 or not store_ptr:
            return False
        return _write_property_store(store_ptr, [(_PID_APP_USER_MODEL_ID, app_id)])
    except Exception:
        return False
    finally:
        if ole32 is not None and co_hr in (0, 1):
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def allow_set_foreground_window() -> None:
    """Permite que la instancia ya abierta tome el foco al recibir mensaje IPC."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow(ctypes.c_uint32(0xFFFFFFFF))
    except Exception:
        pass


def bring_window_to_front(hwnd: int) -> None:
    """Trae el HWND principal al primer plano de Windows."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    except Exception:
        pass


def get_pythonw_executable() -> Path:
    venv_pyw = APP_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        return venv_pyw
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return exe
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return exe


def build_launch_command() -> str:
    pyw = get_pythonw_executable()
    return f'"{pyw}" "{LAUNCH_SCRIPT_PATH}"'


def is_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, build_launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass


def get_desktop_shortcut_path() -> Path:
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                return Path(buf.value) / f"{APP_NAME}.lnk"
        except Exception:
            pass
    return Path.home() / "Desktop" / f"{APP_NAME}.lnk"


def get_start_menu_shortcut_path() -> Path:
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PROGRAMS = 0x0002
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0002, None, 0, buf) == 0:
                return Path(buf.value) / f"{APP_NAME}.lnk"
        except Exception:
            pass
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def _create_shortcut(shortcut_path: Path) -> Path:
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    if shortcut_path.exists():
        try:
            shortcut_path.unlink()
        except OSError:
            pass

    pyw = str(get_pythonw_executable())
    script = str(LAUNCH_SCRIPT_PATH)
    workdir = str(APP_ROOT)
    icon = str(ICON_ICO_PATH)

    ps_script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{shortcut_path}'); "
        f"$s.TargetPath = '{pyw}'; "
        f"$s.Arguments = '\"{script}\"'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        f"$s.IconLocation = '{icon},0'; "
        "$s.Description = 'SwiftPlay - YouTube y YouTube Music ultraliviano'; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    set_shortcut_app_user_model_id(shortcut_path)
    if sys.platform == "win32":
        try:
            # SHCNE_ASSOCCHANGED = 0x08000000, SHCNF_IDLIST = 0x0000
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        except Exception:
            pass
    return shortcut_path


def create_desktop_shortcut() -> Path:
    return _create_shortcut(get_desktop_shortcut_path())


def create_start_menu_shortcut() -> Path:
    return _create_shortcut(get_start_menu_shortcut_path())


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def get_process_memory_mb() -> tuple[float, float]:
    """Devuelve (WorkingSetSize_MB, PrivateUsage_MB) del proceso actual en Windows."""
    if sys.platform != "win32":
        return 0.0, 0.0
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        fn = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if fn is None:
            fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        fn.restype = wintypes.BOOL

        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        h_proc = kernel32.GetCurrentProcess()
        if fn(h_proc, ctypes.byref(counters), counters.cb):
            return (
                counters.WorkingSetSize / (1024 * 1024),
                counters.PrivateUsage / (1024 * 1024),
            )
    except Exception:
        pass
    return 0.0, 0.0


def get_pid_memory_mb(pid: int) -> tuple[float, float]:
    """Devuelve (WorkingSetSize_MB, PrivateUsage_MB) para cualquier PID activo en Windows."""
    if sys.platform != "win32" or not pid or int(pid) <= 0:
        return 0.0, 0.0
    h_proc = None
    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        h_proc = kernel32.OpenProcess(0x1000, False, wintypes.DWORD(int(pid)))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h_proc:
            return 0.0, 0.0
        fn = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if fn is None:
            fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        fn.restype = wintypes.BOOL

        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if fn(h_proc, ctypes.byref(counters), counters.cb):
            return (
                counters.WorkingSetSize / (1024 * 1024),
                counters.PrivateUsage / (1024 * 1024),
            )
    except Exception:
        pass
    finally:
        if h_proc:
            try:
                kernel32.CloseHandle(h_proc)
            except Exception:
                pass
    return 0.0, 0.0


def get_process_ram_mb() -> float:
    """Devuelve el WorkingSetSize actual del proceso en MB."""
    return get_process_memory_mb()[0]


def trim_process_memory(*, collect_gc: bool = True) -> None:
    """Libera objetos Python sin usar y compacta el working set del proceso en Windows."""
    if collect_gc:
        gc.collect()
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        h_proc = kernel32.GetCurrentProcess()
        kernel32.SetProcessWorkingSetSize.argtypes = [
            wintypes.HANDLE,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL
        kernel32.SetProcessWorkingSetSize(h_proc, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:
        pass


def open_folder_in_explorer(path: str) -> None:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(target))  # noqa: S606


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def flash_window_taskbar(hwnd: int, count: int = 3) -> None:
    """Ilumina discretamente el icono en la barra de tareas de Windows sin robar foco."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]
        user32.FlashWindowEx.restype = wintypes.BOOL
        info = _FLASHWINFO()
        info.cbSize = ctypes.sizeof(_FLASHWINFO)
        info.hwnd = wintypes.HWND(int(hwnd))
        info.dwFlags = 0x00000002 | 0x0000000C  # FLASHW_TRAY | FLASHW_TIMERNOFG
        info.uCount = max(1, int(count))
        info.dwTimeout = 0
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


@dataclass(slots=True)
class ResourceSnapshot:
    ws_mb: float = 0.0
    priv_mb: float = 0.0
    app_cpu_pct: float = 0.0
    sys_cpu_pct: float = 0.0
    app_gpu_pct: float = 0.0
    sys_gpu_pct: float = 0.0


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _PDH_FMT_COUNTERVALUE_UNION(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("AnsiStringValue", ctypes.c_char_p),
        ("WideStringValue", ctypes.c_wchar_p),
    ]


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("union", _PDH_FMT_COUNTERVALUE_UNION),
    ]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", _PDH_FMT_COUNTERVALUE),
    ]


def _ft_to_int(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


class SystemResourceMonitor:
    """Monitor liviano nativo de RAM, CPU% y GPU% (Win32 API + PDH sin dependencias)."""

    _PDH_FMT_DOUBLE = 0x00000200
    _PDH_MORE_DATA = 0x800007D2

    def __init__(self, pid: int | None = None) -> None:
        self._pid = int(pid or os.getpid())
        self._last_proc_cpu: int | None = None
        self._last_sys_total: int | None = None
        self._last_sys_idle: int | None = None
        self._pdh = None
        self._pdh_query: ctypes.c_void_p | None = None
        self._pdh_gpu_counter: ctypes.c_void_p | None = None
        self._proc_handle: wintypes.HANDLE | None = None
        self._init_native()

    def _init_native(self) -> None:
        if sys.platform != "win32":
            return
        try:
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            self._proc_handle = k32.GetCurrentProcess()
            k32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.GetSystemTimes.argtypes = [
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]
            k32.GetSystemTimes.restype = wintypes.BOOL
            self._sample_cpu_pct()
        except Exception:
            self._proc_handle = None

        try:
            pdh = ctypes.windll.pdh
            pdh.PdhOpenQueryW.argtypes = [
                wintypes.LPCWSTR,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            pdh.PdhOpenQueryW.restype = ctypes.c_long
            pdh.PdhAddEnglishCounterW.argtypes = [
                ctypes.c_void_p,
                wintypes.LPCWSTR,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
            pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            pdh.PdhCollectQueryData.restype = ctypes.c_long
            pdh.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W),
            ]
            pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
            pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            pdh.PdhCloseQuery.restype = ctypes.c_long

            q = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) == 0:
                c = ctypes.c_void_p()
                if (
                    pdh.PdhAddEnglishCounterW(
                        q,
                        r"\GPU Engine(*)\Utilization Percentage",
                        0,
                        ctypes.byref(c),
                    )
                    == 0
                ):
                    pdh.PdhCollectQueryData(q)
                    self._pdh = pdh
                    self._pdh_query = q
                    self._pdh_gpu_counter = c
                else:
                    pdh.PdhCloseQuery(q)
        except Exception:
            self._pdh = None
            self._pdh_query = None
            self._pdh_gpu_counter = None

    def _sample_cpu_pct(self) -> tuple[float, float]:
        if sys.platform != "win32" or self._proc_handle is None:
            return 0.0, 0.0
        try:
            k32 = ctypes.windll.kernel32
            ft_create, ft_exit, ft_p_kern, ft_p_user = (
                _FILETIME(),
                _FILETIME(),
                _FILETIME(),
                _FILETIME(),
            )
            ft_idle, ft_s_kern, ft_s_user = _FILETIME(), _FILETIME(), _FILETIME()
            if not k32.GetProcessTimes(
                self._proc_handle,
                ctypes.byref(ft_create),
                ctypes.byref(ft_exit),
                ctypes.byref(ft_p_kern),
                ctypes.byref(ft_p_user),
            ):
                return 0.0, 0.0
            if not k32.GetSystemTimes(
                ctypes.byref(ft_idle),
                ctypes.byref(ft_s_kern),
                ctypes.byref(ft_s_user),
            ):
                return 0.0, 0.0

            proc_total = _ft_to_int(ft_p_kern) + _ft_to_int(ft_p_user)
            sys_idle = _ft_to_int(ft_idle)
            sys_total = _ft_to_int(ft_s_kern) + _ft_to_int(ft_s_user)

            if self._last_proc_cpu is None or self._last_sys_total is None or self._last_sys_idle is None:
                self._last_proc_cpu = proc_total
                self._last_sys_total = sys_total
                self._last_sys_idle = sys_idle
                return 0.0, 0.0

            d_proc = max(0, proc_total - self._last_proc_cpu)
            d_total = sys_total - self._last_sys_total
            d_idle = max(0, sys_idle - self._last_sys_idle)
            self._last_proc_cpu = proc_total
            self._last_sys_total = sys_total
            self._last_sys_idle = sys_idle

            if d_total <= 0:
                return 0.0, 0.0
            app_pct = max(0.0, min(100.0, (d_proc / float(d_total)) * 100.0))
            sys_busy = max(0, d_total - d_idle)
            sys_pct = max(0.0, min(100.0, (sys_busy / float(d_total)) * 100.0))
            return app_pct, sys_pct
        except Exception:
            return 0.0, 0.0

    def _sample_gpu_pct(self) -> tuple[float, float]:
        if sys.platform != "win32" or self._pdh is None or not self._pdh_query or not self._pdh_gpu_counter:
            return 0.0, 0.0
        try:
            if self._pdh.PdhCollectQueryData(self._pdh_query) != 0:
                return 0.0, 0.0
            buf_sz = wintypes.DWORD(0)
            cnt = wintypes.DWORD(0)
            rc = self._pdh.PdhGetFormattedCounterArrayW(
                self._pdh_gpu_counter,
                self._PDH_FMT_DOUBLE,
                ctypes.byref(buf_sz),
                ctypes.byref(cnt),
                None,
            )
            if (rc & 0xFFFFFFFF) != self._PDH_MORE_DATA or buf_sz.value == 0:
                return 0.0, 0.0

            raw = ctypes.create_string_buffer(buf_sz.value)
            arr_ptr = ctypes.cast(raw, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W))
            if (
                self._pdh.PdhGetFormattedCounterArrayW(
                    self._pdh_gpu_counter,
                    self._PDH_FMT_DOUBLE,
                    ctypes.byref(buf_sz),
                    ctypes.byref(cnt),
                    arr_ptr,
                )
                != 0
            ):
                return 0.0, 0.0

            app_by_eng: dict[str, float] = {}
            sys_by_eng: dict[str, float] = {}
            pid_prefix = f"pid_{self._pid}_"
            n_items = int(cnt.value)
            for idx in range(n_items):
                item = arr_ptr[idx]
                status = int(item.FmtValue.CStatus)
                if status not in (0, 1):
                    continue
                val = float(item.FmtValue.union.doubleValue)
                if not math.isfinite(val) or val < 0.0:
                    continue
                name = str(item.szName or "")
                eng_pos = name.find("engtype_")
                eng_key = name[eng_pos:] if eng_pos >= 0 else "other"
                luid_pos = name.find("luid_")
                sys_key = name[luid_pos:] if luid_pos >= 0 else eng_key
                sys_by_eng[sys_key] = sys_by_eng.get(sys_key, 0.0) + val
                if name.startswith(pid_prefix):
                    app_by_eng[eng_key] = app_by_eng.get(eng_key, 0.0) + val

            app_gpu = max(app_by_eng.values(), default=0.0)
            sys_gpu = max(sys_by_eng.values(), default=0.0)
            return max(0.0, min(100.0, app_gpu)), max(0.0, min(100.0, sys_gpu))
        except Exception:
            return 0.0, 0.0

    def sample(self) -> ResourceSnapshot:
        ws_mb, priv_mb = get_process_memory_mb()
        app_cpu, sys_cpu = self._sample_cpu_pct()
        app_gpu, sys_gpu = self._sample_gpu_pct()
        return ResourceSnapshot(
            ws_mb=ws_mb,
            priv_mb=priv_mb,
            app_cpu_pct=app_cpu,
            sys_cpu_pct=sys_cpu,
            app_gpu_pct=app_gpu,
            sys_gpu_pct=sys_gpu,
        )

    def close(self) -> None:
        if self._pdh is not None and self._pdh_query is not None:
            try:
                self._pdh.PdhCloseQuery(self._pdh_query)
            except Exception:
                pass
        self._pdh = None
        self._pdh_query = None
        self._pdh_gpu_counter = None


class _WIN_MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
    ]


def is_win32_disabled_parent_click(message: object) -> bool:
    """Detecta clic sobre ventana padre bloqueada por un diálogo modal (WM_SETCURSOR + HTERROR)."""
    if sys.platform != "win32" or not message:
        return False
    try:
        msg = _WIN_MSG.from_address(int(message))  # type: ignore[arg-type]
        if int(msg.message) != 0x0020:
            return False
        hit = int(msg.lParam) & 0xFFFF
        mouse_msg = (int(msg.lParam) >> 16) & 0xFFFF
        return hit == 0xFFFE and mouse_msg in (0x0201, 0x0204, 0x0207)
    except Exception:
        return False


def _prepend_env_paths(env: dict[str, str], var_name: str, paths: list[str]) -> None:
    cur = [p for p in env.get(var_name, "").split(os.pathsep) if p]
    merged: list[str] = []
    seen: set[str] = set()
    for p in [*paths, *cur]:
        norm = os.path.normcase(os.path.normpath(p))
        if p and norm not in seen:
            seen.add(norm)
            merged.append(p)
    if merged:
        env[var_name] = os.pathsep.join(merged)


def resolve_worker_python_and_env(app_root: Path = APP_ROOT) -> tuple[str, dict[str, str]]:
    """Resuelve pythonw.exe unificado y entorno .venv para agrupar workers bajo la GUI en Windows."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    src_dir = app_root / "src"
    venv_dir: Path | None = None
    for cand in (
        Path(sys.executable).parent.parent,
        Path(sys.executable).resolve().parent.parent,
        app_root / ".venv",
    ):
        if (cand / "pyvenv.cfg").exists():
            venv_dir = cand
            break

    py_paths: list[str] = [str(src_dir)]
    has_site = False
    if venv_dir is not None:
        env["VIRTUAL_ENV"] = str(venv_dir)
        scripts_dir = venv_dir / "Scripts"
        if scripts_dir.exists():
            _prepend_env_paths(env, "PATH", [str(scripts_dir)])
        site_pkgs = venv_dir / "Lib" / "site-packages"
        if site_pkgs.exists():
            py_paths.append(str(site_pkgs))
            has_site = True
    _prepend_env_paths(env, "PYTHONPATH", py_paths)

    if sys.platform != "win32":
        return sys.executable, env

    base_exe = Path(getattr(sys, "_base_executable", sys.executable))
    candidates: list[Path] = []
    if has_site or venv_dir is None:
        candidates.append(base_exe.with_name("pythonw.exe"))
    candidates.append(Path(sys.executable).with_name("pythonw.exe"))
    for cand_pyw in candidates:
        if cand_pyw.exists():
            return str(cand_pyw), env
    return sys.executable, env


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_CHILD_JOB_LOCK = threading.Lock()
_CHILD_JOB_HANDLE: int = 0
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def _ensure_child_job_handle() -> int:
    global _CHILD_JOB_HANDLE
    if sys.platform != "win32":
        return 0
    with _CHILD_JOB_LOCK:
        if _CHILD_JOB_HANDLE:
            return _CHILD_JOB_HANDLE
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return 0
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                k32.CloseHandle(job)
                return 0
            _CHILD_JOB_HANDLE = int(job)
            return _CHILD_JOB_HANDLE
        except Exception:
            return 0


def assign_process_to_child_job(proc: subprocess.Popen[Any]) -> bool:
    """Vincula un subproceso al Job Object del padre (cierre automático al finalizar tarea)."""
    if sys.platform != "win32" or proc is None:
        return False
    job = _ensure_child_job_handle()
    if not job:
        return False
    handle = getattr(proc, "_handle", None)
    if not handle:
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        return bool(k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(handle))))
    except Exception:
        return False
