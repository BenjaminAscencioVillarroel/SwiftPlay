"""Configuración persistente de SwiftPlay."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from swiftplay.paths import CONFIG_PATH
from swiftplay.sound_effects import (
    DEFAULT_SOUND_VOLUMES,
    SOUND_EVENTS,
    VALID_SOUND_PACKS,
)
from swiftplay.themes import THEMES

VALID_SPEEDS: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5)
VALID_QUALITIES: tuple[str, ...] = ("360p", "480p", "720p", "1080p")
VALID_THEMES: tuple[str, ...] = tuple(THEMES.keys())
VALID_DISCORD_NS: tuple[str, ...] = ("off", "webrtc", "ai", "ai_max")
VALID_DISCORD_OVERLAY_MODES: tuple[str, ...] = ("off", "status", "full")
VALID_DISCORD_SOUND_PACKS: tuple[str, ...] = VALID_SOUND_PACKS
VALID_NOTIFY_COLORS: tuple[str, ...] = (
    "purple",
    "indigo",
    "blue",
    "pink",
    "emerald",
    "amber",
    "silver",
)
DEFAULT_DISCORD_SOUND_VOLUMES: dict[str, int] = dict(DEFAULT_SOUND_VOLUMES)
LEGACY_DISCORD_NS_MAP: dict[str, str] = {
    "low": "webrtc",
    "moderate": "webrtc",
    "high": "ai",
    "max": "ai_max",
}


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
    key = str(value or "").strip().lower()
    return key if key in VALID_THEMES else "matte_silver"


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
    discord_input_device: str = ""
    discord_output_device: str = ""
    discord_output_volume: int = 100
    discord_mic_gain: int = 100
    discord_vad_threshold: int = 18
    discord_mic_agc: bool = True
    discord_noise_suppression: str = "ai"
    discord_hotkey_mute: str = "Ctrl+Shift+M"
    discord_hotkey_deafen: str = "Ctrl+Shift+D"
    discord_hotkey_hangup: str = "Ctrl+Shift+H"
    discord_hotkey_share_start: str = "Ctrl+Alt+S"
    discord_hotkey_share_stop: str = "Ctrl+Alt+X"
    discord_hotkey_mute_priority: bool = True
    discord_share_last_source_id: str = "monitor:0"
    discord_share_last_quality: str = "720p"
    discord_share_last_fps: int = 30
    discord_share_last_audio: bool = True
    discord_share_auto_targets: list[str] = field(default_factory=list)
    discord_share_auto_target_names: dict[str, str] = field(default_factory=dict)
    discord_show_locked_channels: bool = False
    discord_overlay_enabled: bool = True
    discord_overlay_mode: str = "status"
    discord_overlay_opacity: int = 25
    discord_overlay_x: int | None = None
    discord_overlay_y: int | None = None
    discord_notify_chat_enabled: bool = True
    discord_notify_color: str = "purple"
    discord_notify_channels: list[str] = field(default_factory=list)
    discord_hidden_channels: list[str] = field(default_factory=list)
    discord_hidden_channel_names: dict[str, str] = field(default_factory=dict)
    discord_sounds_enabled: bool = True
    discord_sound_pack: str = "soft"
    discord_sound_volumes: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_DISCORD_SOUND_VOLUMES)
    )

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
        self.discord_input_device = str(self.discord_input_device or "").strip()
        self.discord_output_device = str(self.discord_output_device or "").strip()
        self.discord_output_volume = max(0, min(200, int(self.discord_output_volume)))
        self.discord_mic_gain = max(0, min(400, int(self.discord_mic_gain)))
        self.discord_vad_threshold = max(0, min(100, int(self.discord_vad_threshold)))
        self.discord_mic_agc = bool(self.discord_mic_agc)
        ns = str(self.discord_noise_suppression or "ai").strip().lower()
        ns = LEGACY_DISCORD_NS_MAP.get(ns, ns)
        self.discord_noise_suppression = ns if ns in VALID_DISCORD_NS else "ai"
        self.discord_hotkey_mute = str(self.discord_hotkey_mute or "").strip()
        self.discord_hotkey_deafen = str(self.discord_hotkey_deafen or "").strip()
        self.discord_hotkey_hangup = str(self.discord_hotkey_hangup or "").strip()
        self.discord_hotkey_share_start = str(self.discord_hotkey_share_start or "").strip()
        self.discord_hotkey_share_stop = str(self.discord_hotkey_share_stop or "").strip()
        self.discord_hotkey_mute_priority = bool(self.discord_hotkey_mute_priority)
        self.discord_show_locked_channels = bool(self.discord_show_locked_channels)
        ov_mode = str(self.discord_overlay_mode or "status").strip().lower()
        if ov_mode not in VALID_DISCORD_OVERLAY_MODES:
            ov_mode = "status"
        if not bool(self.discord_overlay_enabled):
            ov_mode = "off"
        self.discord_overlay_mode = ov_mode
        self.discord_overlay_enabled = ov_mode != "off"
        self.discord_overlay_opacity = max(10, min(100, int(self.discord_overlay_opacity)))

        def _norm_overlay_coord(raw_val: object) -> int | None:
            if raw_val is None or raw_val == "":
                return None
            try:
                return max(-10000, min(10000, int(raw_val)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        self.discord_overlay_x = _norm_overlay_coord(self.discord_overlay_x)
        self.discord_overlay_y = _norm_overlay_coord(self.discord_overlay_y)
        self.discord_sounds_enabled = bool(self.discord_sounds_enabled)
        s_pack = str(self.discord_sound_pack or "soft").strip().lower()
        self.discord_sound_pack = s_pack if s_pack in VALID_DISCORD_SOUND_PACKS else "soft"
        clean_vols: dict[str, int] = dict(DEFAULT_DISCORD_SOUND_VOLUMES)
        raw_vols = self.discord_sound_volumes if isinstance(self.discord_sound_volumes, dict) else {}
        for ev_key, _, _ in SOUND_EVENTS:
            if ev_key in raw_vols:
                try:
                    clean_vols[ev_key] = max(0, min(100, int(raw_vols[ev_key])))
                except (TypeError, ValueError):
                    pass
        self.discord_sound_volumes = clean_vols
        self.discord_notify_chat_enabled = bool(self.discord_notify_chat_enabled)
        ncol = str(self.discord_notify_color or "purple").strip().lower()
        self.discord_notify_color = ncol if ncol in VALID_NOTIFY_COLORS else "purple"
        seen_chs: list[str] = []
        for raw_cid in (self.discord_notify_channels or []):
            cid = str(raw_cid or "").strip()
            if cid and cid not in seen_chs:
                seen_chs.append(cid)
        self.discord_notify_channels = seen_chs
        hidden_chs: list[str] = []
        for raw_cid in (self.discord_hidden_channels or []):
            cid = str(raw_cid or "").strip()
            if cid and cid not in hidden_chs:
                hidden_chs.append(cid)
        self.discord_hidden_channels = hidden_chs
        raw_hidden_names = self.discord_hidden_channel_names or {}
        clean_hidden_names: dict[str, str] = {}
        if isinstance(raw_hidden_names, dict):
            for k, v in raw_hidden_names.items():
                cid = str(k or "").strip()
                label = str(v or "").strip()
                if cid and label and cid in hidden_chs:
                    clean_hidden_names[cid] = label
        self.discord_hidden_channel_names = clean_hidden_names
        self.discord_share_last_source_id = (
            str(self.discord_share_last_source_id or "monitor:0").strip() or "monitor:0"
        )
        sq = str(self.discord_share_last_quality or "720p").strip().lower()
        self.discord_share_last_quality = sq if sq in ("480p", "720p", "1080p") else "720p"
        try:
            sfps = int(self.discord_share_last_fps or 30)
        except (TypeError, ValueError):
            sfps = 30
        self.discord_share_last_fps = 60 if sfps >= 60 else 30
        self.discord_share_last_audio = bool(self.discord_share_last_audio)

        seen_auto_targets: list[str] = []
        for raw_tgt in (self.discord_share_auto_targets or []):
            tgt = str(raw_tgt or "").strip()
            if (tgt.startswith("channel:") or tgt.startswith("dm:")) and tgt.split(":", 1)[1].strip():
                kind, tid = tgt.split(":", 1)
                norm = f"{kind}:{tid.strip()}"
                if norm not in seen_auto_targets:
                    seen_auto_targets.append(norm)
        self.discord_share_auto_targets = seen_auto_targets

        raw_auto_names = self.discord_share_auto_target_names or {}
        clean_auto_names: dict[str, str] = {}
        if isinstance(raw_auto_names, dict):
            for k, v in raw_auto_names.items():
                key_s = str(k or "").strip()
                val_s = str(v or "").strip()
                if key_s and val_s and key_s in seen_auto_targets:
                    clean_auto_names[key_s] = val_s
        self.discord_share_auto_target_names = clean_auto_names


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
    if "discord_overlay_mode" not in filtered and "discord_overlay_enabled" in filtered:
        filtered["discord_overlay_mode"] = (
            "status" if filtered["discord_overlay_enabled"] else "off"
        )
    return AppConfig(**filtered)


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)
