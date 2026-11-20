"""Rutas centrales de SwiftPlay."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "SwiftPlay"
APP_ID = "SwiftPlay"

# Directorio raíz del proyecto
APP_ROOT = Path(__file__).resolve().parent.parent.parent
BIN_DIR = APP_ROOT / "bin"
ASSETS_DIR = APP_ROOT / "assets"

USER_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_ID
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = USER_DATA_DIR / "config.json"
DB_PATH = USER_DATA_DIR / "library.db"
SESSION_PATH = USER_DATA_DIR / "session.dpapi"
SESSION_STATE_PATH = USER_DATA_DIR / "session_state.json"
ACCOUNT_PROFILE_PATH = USER_DATA_DIR / "account_profile.json"
ICON_ICO_PATH = ASSETS_DIR / "swiftplay.ico"
ICON_PNG_PATH = ASSETS_DIR / "swiftplay.png"
LAUNCH_SCRIPT_PATH = APP_ROOT / "launch_swiftplay.pyw"
