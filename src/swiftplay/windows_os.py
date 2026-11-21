"""Integración con Windows: inicio automático, acceso directo, RAM en vivo y explorador."""

from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
from ctypes import wintypes
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


def get_process_ram_mb() -> float:
    """Devuelve el WorkingSetSize actual del proceso en MB."""
    return get_process_memory_mb()[0]


def trim_process_memory() -> None:
    """Libera objetos Python sin usar y compacta el working set del proceso en Windows."""
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
