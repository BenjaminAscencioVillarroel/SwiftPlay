"""Wrapper sobre libmpv (python-mpv) dual (Video GPU o Solo-Audio ligero)."""

from __future__ import annotations

import locale
import os
import threading

from PySide6.QtCore import QObject, Signal

from swiftplay.config import VALID_SPEEDS, clamp_speed
from swiftplay.paths import BIN_DIR

if hasattr(os, "add_dll_directory") and BIN_DIR.exists():
    os.add_dll_directory(str(BIN_DIR))
os.environ["PATH"] = f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"

import mpv  # noqa: E402

NORMALIZATION_FILTER = "lavfi=[dynaudnorm=f=150:g=15:p=0.9]"


class MediaPlayer(QObject):
    """Controla libmpv embebido en HWND (Video) o en modo headless solo-audio (Música)."""

    position_changed = Signal(float)
    duration_changed = Signal(float)
    paused_changed = Signal(bool)
    track_ended = Signal()
    buffering_changed = Signal(bool)
    error_occurred = Signal(str)
    wheel_scrolled = Signal(int)
    mouse_moved = Signal()
    clicked = Signal()
    double_clicked = Signal()

    def __init__(
        self,
        wid: int | None = None,
        initial_volume: int = 80,
        initial_speed: float = 1.0,
        normalization: bool = False,
        lazy: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._shutdown = False
        self._user_stopped = False
        self._loading_generation = 0
        self._wid = wid
        self._is_audio_only_instance = wid is None
        self._volume = max(0, min(100, int(initial_volume)))
        self._speed = clamp_speed(initial_speed)
        self._normalization = bool(normalization)
        self._last_mouse_xy: tuple[int | None, int | None] | None = None
        self._mpv: mpv.MPV | None = None

        if not lazy:
            self._ensure_mpv()

    def _ensure_mpv(self) -> mpv.MPV | None:
        with self._lock:
            if self._shutdown:
                return None
            if self._mpv is not None:
                return self._mpv

            # CRÍTICO en Windows en español: libmpv requiere LC_NUMERIC = "C"
            locale.setlocale(locale.LC_NUMERIC, "C")

            kwargs: dict = dict(
                idle="yes",
                keep_open="no",
                osc=False,
                osd_level=0,
                input_default_bindings=False,
                input_vo_keyboard=False,
                sub="no",
                sub_auto="no",
                audio_pitch_correction=True,
                ytdl=False,
                cache="yes",
                cache_secs=12,
                volume=self._volume,
                speed=self._speed,
                af=NORMALIZATION_FILTER if self._normalization else "",
            )
            if self._wid is not None:
                kwargs["wid"] = str(int(self._wid))
                kwargs["hwdec"] = "auto-safe"
                kwargs["vo"] = "gpu"
                kwargs["demuxer_max_bytes"] = "12MiB"
                kwargs["demuxer_max_back_bytes"] = "4MiB"
                kwargs["cursor_autohide"] = "no"
            else:
                kwargs["vo"] = "null"
                kwargs["vid"] = "no"
                kwargs["demuxer_max_bytes"] = "4MiB"
                kwargs["demuxer_max_back_bytes"] = "1MiB"

            self._mpv = mpv.MPV(**kwargs)
            self._register_observers()
            return self._mpv

    def _register_observers(self) -> None:
        player = self._mpv
        if player is None:
            return

        @player.property_observer("time-pos")
        def _on_time_pos(_name, value):
            if not self._shutdown and value is not None:
                self.position_changed.emit(float(value))

        @player.property_observer("duration")
        def _on_duration(_name, value):
            if not self._shutdown and value is not None:
                self.duration_changed.emit(float(value))

        @player.property_observer("pause")
        def _on_pause(_name, value):
            if not self._shutdown and value is not None:
                self.paused_changed.emit(bool(value))

        @player.property_observer("paused-for-cache")
        def _on_buffering(_name, value):
            if not self._shutdown and value is not None:
                self.buffering_changed.emit(bool(value))

        @player.event_callback("end-file")
        def _on_end_file(event):
            if self._shutdown:
                return
            reason = None
            error_code = 0
            data = getattr(event, "data", None)
            if isinstance(data, dict):
                reason = data.get("reason")
                error_code = data.get("error", 0)
            elif data is not None:
                reason = getattr(data, "reason", None)
                error_code = getattr(data, "error", 0)
            elif isinstance(event, dict):
                ev = event.get("event", {})
                reason = ev.get("reason")
                error_code = ev.get("error", 0)

            # Solo EOF genuino (0 / 'eof') dispara autoplay/siguiente pista
            if reason in (0, "eof", b"eof") and not self._user_stopped:
                self.track_ended.emit()
            elif reason in (4, "error", b"error"):
                self.error_occurred.emit(
                    f"Error de reproducción en libmpv (código {error_code})."
                )

        if not self._is_audio_only_instance:
            try:
                @player.on_key_press("WHEEL_UP")
                def _on_wheel_up():
                    if not self._shutdown:
                        self.wheel_scrolled.emit(1)

                @player.on_key_press("WHEEL_DOWN")
                def _on_wheel_down():
                    if not self._shutdown:
                        self.wheel_scrolled.emit(-1)

                @player.on_key_press("MBTN_LEFT")
                def _on_left_click():
                    if not self._shutdown:
                        self.clicked.emit()

                @player.on_key_press("MBTN_LEFT_DBL")
                def _on_dbl_click():
                    if not self._shutdown:
                        self.double_clicked.emit()

                @player.property_observer("mouse-pos")
                def _on_mouse_pos(_name, value):
                    if self._shutdown or not isinstance(value, dict):
                        return
                    xy = (value.get("x"), value.get("y"))
                    if xy != self._last_mouse_xy:
                        self._last_mouse_xy = xy
                        if value.get("hover", True):
                            self.mouse_moved.emit()
            except Exception:
                pass

    def get_property(self, name: str):
        with self._lock:
            player = self._ensure_mpv()
            if player is None:
                return None
            return getattr(player, name.replace("-", "_"))

    def play_stream(
        self,
        primary_url: str,
        audio_url: str | None = None,
        video_enabled: bool = True,
        http_headers: dict[str, str] | None = None,
        start_pos: float = 0.0,
    ) -> None:
        with self._lock:
            player = self._ensure_mpv()
            if player is None:
                return
            self._loading_generation += 1
            self._user_stopped = False

            if self._is_audio_only_instance:
                player.vid = "no"
            else:
                player.vid = "auto" if video_enabled else "no"
            player.start = f"+{start_pos:.1f}" if start_pos > 0.5 else "none"
            player.volume = self._volume
            player.speed = self._speed
            player.af = NORMALIZATION_FILTER if self._normalization else ""

            headers = http_headers or {}
            ua = headers.get("User-Agent")
            if ua:
                player.user_agent = ua
            extra_headers = [
                f"{k}: {v}"
                for k, v in headers.items()
                if k.lower() not in {"user-agent", "host", "accept-encoding"}
            ]
            player.http_header_fields = extra_headers
            player["audio-files"] = [audio_url] if audio_url else []
            player.pause = False
            player.play(primary_url)

    def set_pause(self, paused: bool) -> None:
        with self._lock:
            if not self._shutdown and self._mpv is not None:
                self._mpv.pause = bool(paused)

    def toggle_pause(self) -> None:
        with self._lock:
            if not self._shutdown and self._mpv is not None:
                self._mpv.pause = not bool(self._mpv.pause)

    def stop(self) -> None:
        with self._lock:
            if not self._shutdown and self._mpv is not None:
                self._user_stopped = True
                self._mpv.command("stop")

    def seek(self, seconds: float) -> None:
        with self._lock:
            if not self._shutdown and self._mpv is not None:
                self._mpv.seek(max(0.0, float(seconds)), "absolute")

    def seek_relative(self, delta: float) -> None:
        with self._lock:
            if not self._shutdown and self._mpv is not None:
                self._mpv.seek(float(delta), "relative")

    def set_volume(self, volume: int) -> None:
        vol = max(0, min(100, int(volume)))
        with self._lock:
            self._volume = vol
            if not self._shutdown and self._mpv is not None:
                self._mpv.volume = vol

    def set_speed(self, speed: float) -> None:
        validated = clamp_speed(speed)
        if validated not in VALID_SPEEDS:
            validated = 1.0
        with self._lock:
            self._speed = validated
            if not self._shutdown and self._mpv is not None:
                self._mpv.speed = validated

    def set_normalization(self, enabled: bool) -> None:
        with self._lock:
            self._normalization = bool(enabled)
            if not self._shutdown and self._mpv is not None:
                self._mpv.af = NORMALIZATION_FILTER if self._normalization else ""

    def set_video_enabled(self, enabled: bool) -> None:
        with self._lock:
            if (
                not self._shutdown
                and not self._is_audio_only_instance
                and self._mpv is not None
            ):
                self._mpv.vid = "auto" if enabled else "no"

    def set_cursor_autohide(self, value: int | str) -> None:
        with self._lock:
            if (
                not self._shutdown
                and not self._is_audio_only_instance
                and self._mpv is not None
            ):
                self._mpv.cursor_autohide = value

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._user_stopped = True
            if self._mpv is not None:
                try:
                    self._mpv.terminate()
                except Exception:
                    pass
                self._mpv = None
