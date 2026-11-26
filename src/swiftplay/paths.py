"""Rutas centrales de SwiftPlay."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

APP_NAME = "SwiftPlay"
RESTART_EXIT_CODE = 88


def parse_profile_name(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resuelve el nombre de perfil aislado desde `--profile` o `SWIFTPLAY_PROFILE`."""
    args = list(sys.argv[1:] if argv is None else argv)
    raw = ""
    for i, arg in enumerate(args):
        s = str(arg or "").strip()
        if s.startswith("--profile="):
            raw = s.split("=", 1)[1]
            break
        if s == "--profile" and i + 1 < len(args):
            raw = str(args[i + 1] or "")
            break
    if not raw:
        source_env = os.environ if env is None else env
        raw = str(source_env.get("SWIFTPLAY_PROFILE") or "")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw).strip("_-")[:32]
    if not cleaned or cleaned.lower() in ("default", "main"):
        return ""
    return cleaned


PROFILE_NAME: str = parse_profile_name()
IS_ISOLATED_PROFILE: bool = bool(PROFILE_NAME)
if PROFILE_NAME:
    # Propagar a subprocesos worker (ytdlp_worker, discord_worker, screen_share_worker)
    os.environ["SWIFTPLAY_PROFILE"] = PROFILE_NAME

APP_ID = f"SwiftPlay_{PROFILE_NAME}" if PROFILE_NAME else "SwiftPlay"

# Directorio raíz del proyecto
APP_ROOT = Path(__file__).resolve().parent.parent.parent
BIN_DIR = APP_ROOT / "bin"
ASSETS_DIR = APP_ROOT / "assets"
LOGS_DIR = APP_ROOT / "logs"

_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
BASE_USER_DATA_DIR = _LOCAL_APP_DATA / "SwiftPlay"
USER_DATA_DIR = _LOCAL_APP_DATA / APP_ID
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

RAM_LOG_PATH = (USER_DATA_DIR / "ram_log.txt") if IS_ISOLATED_PROFILE else (LOGS_DIR / "ram_log.txt")

CONFIG_PATH = USER_DATA_DIR / "config.json"
DB_PATH = USER_DATA_DIR / "library.db"
SESSION_PATH = USER_DATA_DIR / "session.dpapi"
DISCORD_TOKEN_PATH = USER_DATA_DIR / "discord_token.dpapi"
SESSION_STATE_PATH = USER_DATA_DIR / "session_state.json"
ACCOUNT_PROFILE_PATH = USER_DATA_DIR / "account_profile.json"
ICON_ICO_PATH = ASSETS_DIR / "swiftplay.ico"
ICON_PNG_PATH = ASSETS_DIR / "swiftplay.png"
LAUNCH_SCRIPT_PATH = APP_ROOT / "launch_swiftplay.pyw"
