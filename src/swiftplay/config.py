"""Configuración persistente de SwiftPlay."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from swiftplay.paths import CONFIG_PATH

VALID_SPEEDS: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5)
VALID_QUALITIES: tuple[str, ...] = ("360p", "480p", "720p", "1080p")
VALID_THEMES: tuple[str, ...] = ("matte_silver",)


def _default_music_dir() -> str:
    return str(Path.home() / "Music" / "SwiftPlay")


def _default_video_dir() -> str:
    return str(Path.home() / "Videos" / "SwiftPlay")


def clamp_speed(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    if v in VALID_SPEEDS:
        return v
    return min(VALID_SPEEDS, key=lambda s: abs(s - v))


def validate_quality(value: str) -> str:
    return value if value in VALID_QUALITIES else "720p"


def validate_theme(value: str) -> str:
    return value if value in VALID_THEMES else "matte_silver"


@dataclass
class AppConfig:
    theme: str = "matte_silver"
    audio_normalization: bool = False
    auto_pause_other_player: bool = True
    video_quality: str = "720p"
    video_speed: float = 1.0
    volume: int = 80
    video_volume: int = 80
    music_volume: int = 80
    video_audio_only: bool = False
    autoplay_music: bool = True
    minimize_to_tray: bool = True
    start_with_windows: bool = True
    music_download_dir: str = ""
    video_download_dir: str = ""
    mini_bar_always_on_top: bool = True

    def __post_init__(self) -> None:
        self.theme = validate_theme(self.theme)
        self.audio_normalization = bool(self.audio_normalization)
        self.auto_pause_other_player = bool(self.auto_pause_other_player)
        self.video_quality = validate_quality(self.video_quality)
        self.video_speed = clamp_speed(self.video_speed)
        self.volume = max(0, min(100, int(self.volume)))
        self.video_volume = max(0, min(100, int(self.video_volume)))
        self.music_volume = max(0, min(100, int(self.music_volume)))
        self.video_audio_only = bool(self.video_audio_only)
        self.autoplay_music = bool(self.autoplay_music)
        self.minimize_to_tray = bool(self.minimize_to_tray)
        self.start_with_windows = bool(self.start_with_windows)
        if not self.music_download_dir:
            self.music_download_dir = _default_music_dir()
        if not self.video_download_dir:
            self.video_download_dir = _default_video_dir()
        self.mini_bar_always_on_top = bool(self.mini_bar_always_on_top)


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}

    known_fields = {f for f in AppConfig.__dataclass_fields__}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    # Migrar config previo con un solo 'volume'
    if "volume" in filtered:
        if "video_volume" not in filtered:
            filtered["video_volume"] = filtered["volume"]
        if "music_volume" not in filtered:
            filtered["music_volume"] = filtered["volume"]
    return AppConfig(**filtered)


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)
