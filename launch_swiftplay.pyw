"""Lanzador sin consola (.pyw) para SwiftPlay."""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path

# pythonw.exe no asigna consola; evitar fallos si alguna librería escribe en stdout/stderr
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _resolve_profile_app_id() -> str:
    import re

    raw = ""
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        s = str(arg or "").strip()
        if s.startswith("--profile="):
            raw = s.split("=", 1)[1]
            break
        if s == "--profile" and i + 1 < len(args):
            raw = str(args[i + 1] or "")
            break
    if not raw:
        raw = str(os.environ.get("SWIFTPLAY_PROFILE") or "")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw).strip("_-")[:32]
    if not cleaned or cleaned.lower() in ("default", "main"):
        return "SwiftPlay"
    return f"SwiftPlay_{cleaned}"


def _show_crash_dialog(err_text: str) -> None:
    log_dir = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / _resolve_profile_app_id()
    )
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "crash.log"
        log_file.write_text(err_text, encoding="utf-8")
        log_hint = f"\n\nDetalle guardado en:\n{log_file}"
    except Exception:
        log_hint = ""

    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"No se pudo iniciar SwiftPlay:\n\n{err_text[:900]}{log_hint}",
                "SwiftPlay — Error de inicio",
                0x10,
            )
        except Exception:
            pass


if __name__ == "__main__":
    try:
        from swiftplay.main import main

        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        _show_crash_dialog(traceback.format_exc())
        raise SystemExit(1)
