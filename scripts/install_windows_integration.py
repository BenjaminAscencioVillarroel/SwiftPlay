"""Limpia integración previa de YT Lite, crea el acceso directo SwiftPlay y habilita inicio con Windows."""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from swiftplay.windows_os import (  # noqa: E402
    RUN_KEY_PATH,
    build_launch_command,
    create_desktop_shortcut,
    create_start_menu_shortcut,
    get_desktop_shortcut_path,
    get_start_menu_shortcut_path,
    is_startup_enabled,
    set_startup_enabled,
)


def _cleanup_legacy_ytlite() -> None:
    for folder in (
        get_desktop_shortcut_path().parent,
        get_start_menu_shortcut_path().parent,
    ):
        legacy_lnk = folder / "YT Lite.lnk"
        if legacy_lnk.exists():
            legacy_lnk.unlink()
            print(f"Eliminado acceso directo anterior: {legacy_lnk}")

    if sys.platform == "win32":
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, "YTLite")
                print("Eliminada entrada de registro anterior: YTLite")
        except FileNotFoundError:
            pass
        except OSError:
            pass


def main() -> None:
    _cleanup_legacy_ytlite()
    shortcut = create_desktop_shortcut()
    start_shortcut = create_start_menu_shortcut()
    set_startup_enabled(True)
    print(f"Acceso directo creado: {shortcut}")
    print(f"Acceso directo en Menú Inicio: {start_shortcut}")
    print(f"Inicio con Windows habilitado: {is_startup_enabled()}")
    print(f"Comando de inicio: {build_launch_command()}")


if __name__ == "__main__":
    main()
