"""Wrapper sobre libmpv (python-mpv) dual (Video GPU o Solo-Audio ligero) con import lazy y reciclaje."""

from __future__ import annotations

import ctypes
import locale
import os
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal

from swiftplay.config import VALID_SPEEDS, clamp_speed
from swiftplay.paths import BIN_DIR

NORMALIZATION_FILTER = "lavfi=[dynaudnorm=f=150:g=15:p=0.9]"

_mpv_module: Any = None
_mpv_import_lock = threading.Lock()


def _get_mpv_module() -> Any:
    global _mpv_module
    if _mpv_module is not None:
        return _mpv_module
    with _mpv_import_lock:
        if _mpv_module is not None:
            return _mpv_module
        if hasattr(os, "add_dll_directory") and BIN_DIR.exists():
            os.add_dll_directory(str(BIN_DIR))
        bin_str = str(BIN_DIR)
        path_val = os.environ.get("PATH", "")
        if bin_str not in path_val.split(os.pathsep):
            os.environ["PATH"] = f"{bin_str}{os.pathsep}{path_val}"
        # CRÍTICO en Windows en español: libmpv requiere LC_NUMERIC = "C"
        locale.setlocale(locale.LC_NUMERIC, "C")
        import mpv  # noqa: E402

        _mpv_module = mpv
        return _mpv_module


def _is_shift_pressed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
    except Exception:
        return False


def _is_hwnd_visible(wid: int | None) -> bool:
    if sys.platform != "win32" or wid is None:
        return True
    try:
        hwnd = int(wid)
        if hwnd <= 0:
            return True
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return True


class MediaPlayer(QObject):
    """Controla libmpv embebido en HWND (Video) o en modo headless solo-audio (Música)."""

    position_changed = Signal(float)
    duration_changed = Signal(float)
    paused_changed = Signal(bool)
    track_ended = Signal()
    buffering_changed = Signal(bool)
    error_occurred = Signal(str)
    vo_error_detected = Signal(str)
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
        self._unloading = False
        self._user_stopped = False
        self._has_media = False
        self._is_live = False
        self._live_seekable = False
        self._last_was_live: bool | None = None
        self._streams_played = 0
        self._loading_generation = 0
        self._wid = wid
        self._is_audio_only_instance = wid is None
        self._recycle_after = 8 if self._is_audio_only_instance else 4
        self._volume = max(0, min(100, int(initial_volume)))
        self._speed = clamp_speed(initial_speed)
        self._normalization = bool(normalization)
        self._track_mouse = False
        self._last_mouse_xy: tuple[int | None, int | None] | None = None
        self._last_mouse_emit_ts = 0.0
        self._last_pos_emit_ts = 0.0
        self._last_pos_emit_val = -10.0
        self._applied_af: str | None = None
        self._active_vo = "gpu"
        self._vo_error_count = 0
        self._vo_needs_reinit = False
        self._started_hidden = False
        self._last_vo_reinit_ts = 0.0
        self._last_vo_error = ""
        self._mpv: Any = None

        if not lazy:
            self._ensure_mpv()

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._mpv is not None

    @property
    def has_active_stream(self) -> bool:
        with self._lock:
            return self._mpv is not None and self._has_media

    @property
    def is_paused(self) -> bool:
        with self._lock:
            if self._mpv is None:
                return True
            try:
                return bool(self._mpv.pause)
            except Exception:
                return True

    def _on_mpv_log(self, level: str, prefix: str, text: str) -> None:
        if self._shutdown or self._unloading:
            return
        if str(level or "").lower() in ("error", "fatal") and "vo" in str(prefix or "").lower():
            msg = f"{prefix}: {text}".strip()[:240]
            self._last_vo_error = msg
            self._vo_needs_reinit = True
            self._vo_error_count += 1
            self.vo_error_detected.emit(msg)

    def _ensure_mpv(self) -> Any:
        with self._lock:
            if self._shutdown:
                return None
            if self._mpv is not None:
                return self._mpv

            mpv_mod = _get_mpv_module()
            # CRÍTICO en Windows en español: libmpv requiere LC_NUMERIC = "C"
            locale.setlocale(locale.LC_NUMERIC, "C")

            target_af = NORMALIZATION_FILTER if self._normalization else ""
            kwargs: dict[str, Any] = dict(
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
                cache_secs=3,
                cache_pause_initial="no",
                vd_lavc_threads=2,
                demuxer_lavf_probesize=1048576,
                demuxer_lavf_analyzeduration=0.5,
                demuxer_donate_buffer="no",
                demuxer_hysteresis_secs=0,
                stream_buffer_size="128KiB",
                audio_buffer=0.1,
                demuxer_lavf_o=(
                    "http_persistent=0,reconnect=1,reconnect_streamed=1,reconnect_delay_max=3"
                ),
                volume=self._volume,
                speed=self._speed,
                af=target_af,
            )
            if self._wid is not None:
                kwargs["wid"] = str(int(self._wid))
                kwargs["vo"] = self._active_vo
                kwargs["gpu_api"] = "d3d11"
                kwargs["d3d11_flip"] = "no"
                kwargs["hwdec"] = "d3d11va-copy"
                kwargs["swapchain_depth"] = 2
                kwargs["gpu_shader_cache"] = "no"
                kwargs["icc_cache"] = "no"
                kwargs["demuxer_max_bytes"] = "3MiB"
                kwargs["demuxer_max_back_bytes"] = "512KiB"
                kwargs["cursor_autohide"] = "no"
                kwargs["msg_level"] = "all=no,vo=error"
                kwargs["log_handler"] = self._on_mpv_log
            else:
                kwargs["vo"] = "null"
                kwargs["vid"] = "no"
                kwargs["demuxer_max_bytes"] = "2MiB"
                kwargs["demuxer_max_back_bytes"] = "256KiB"

            try:
                self._mpv = mpv_mod.MPV(**kwargs)
            except Exception:
                if self._wid is not None:
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs.pop("gpu_api", None)
                    fallback_kwargs["d3d11_flip"] = "no"
                    fallback_kwargs["hwdec"] = "auto-safe"
                    self._mpv = mpv_mod.MPV(**fallback_kwargs)
                else:
                    raise

            self._applied_af = target_af
            self._register_observers()
            return self._mpv

    def _register_observers(self) -> None:
        player = self._mpv
        if player is None:
            return

        @player.property_observer("time-pos")
        def _on_time_pos(_name, value):
            if (
                self._shutdown
                or self._unloading
                or value is None
                or (self._is_live and not self._live_seekable)
            ):
                return
            pos = float(value)
            now = time.monotonic()
            if (now - self._last_pos_emit_ts) >= 0.25 or abs(
                pos - self._last_pos_emit_val
            ) >= 0.45:
                self._last_pos_emit_ts = now
                self._last_pos_emit_val = pos
                self.position_changed.emit(pos)

        @player.property_observer("duration")
        def _on_duration(_name, value):
            if not self._shutdown and not self._unloading and value is not None:
                self.duration_changed.emit(float(value))

        @player.property_observer("pause")
        def _on_pause(_name, value):
            if not self._shutdown and not self._unloading and value is not None:
                self.paused_changed.emit(bool(value))

        @player.property_observer("paused-for-cache")
        def _on_buffering(_name, value):
            if not self._shutdown and not self._unloading and value is not None:
                self.buffering_changed.emit(bool(value))

        @player.event_callback("end-file")
        def _on_end_file(event):
            if self._shutdown or self._unloading:
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
            if reason in (0, "eof", b"eof"):
                self._has_media = False
                if not self._user_stopped:
                    self.track_ended.emit()
            elif reason in (4, "error", b"error"):
                self._has_media = False
                self.error_occurred.emit(
                    f"Error de reproducción en libmpv (código {error_code})."
                )

        if not self._is_audio_only_instance:
            try:
                @player.on_key_press("WHEEL_UP")
                def _on_wheel_up():
                    if not self._shutdown and not self._unloading:
                        self.wheel_scrolled.emit(1 if _is_shift_pressed() else 5)

                @player.on_key_press("WHEEL_DOWN")
                def _on_wheel_down():
                    if not self._shutdown and not self._unloading:
                        self.wheel_scrolled.emit(-1 if _is_shift_pressed() else -5)

                @player.on_key_press("Shift+WHEEL_UP")
                def _on_shift_wheel_up():
                    if not self._shutdown and not self._unloading:
                        self.wheel_scrolled.emit(1)

                @player.on_key_press("Shift+WHEEL_DOWN")
                def _on_shift_wheel_down():
                    if not self._shutdown and not self._unloading:
                        self.wheel_scrolled.emit(-1)

                @player.on_key_press("MBTN_LEFT")
                def _on_left_click():
                    if not self._shutdown and not self._unloading:
                        self.clicked.emit()

                @player.on_key_press("MBTN_LEFT_DBL")
                def _on_dbl_click():
                    if not self._shutdown and not self._unloading:
                        self.double_clicked.emit()

                @player.property_observer("mouse-pos")
                def _on_mouse_pos(_name, value):
                    if (
                        self._shutdown
                        or self._unloading
                        or not self._track_mouse
                        or not isinstance(value, dict)
                    ):
                        return
                    now = time.monotonic()
                    if (now - self._last_mouse_emit_ts) < 0.08:
                        return
                    xy = (value.get("x"), value.get("y"))
                    if xy != self._last_mouse_xy:
                        self._last_mouse_xy = xy
                        if value.get("hover", True):
                            self._last_mouse_emit_ts = now
                            self.mouse_moved.emit()
            except Exception:
                pass

    def set_mouse_tracking(self, enabled: bool) -> None:
        self._track_mouse = bool(enabled)
        if not self._track_mouse:
            self._last_mouse_xy = None

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
        is_live: bool = False,
        allow_seek: bool = False,
        low_latency: bool = False,
    ) -> None:
        with self._lock:
            live_flag = bool(is_live)
            live_seekable = bool(live_flag and allow_seek and not low_latency)
            if self._mpv is not None and (
                self._streams_played >= self._recycle_after
                or (self._last_was_live is not None and self._last_was_live != live_flag)
            ):
                self.unload()

            player = self._ensure_mpv()
            if player is None:
                return

            self._loading_generation += 1
            self._user_stopped = False
            self._has_media = True
            self._is_live = live_flag
            self._live_seekable = live_seekable
            self._last_pos_emit_ts = 0.0
            self._last_pos_emit_val = -10.0

            if self._is_audio_only_instance:
                player.vid = "no"
            else:
                self._started_hidden = bool(video_enabled and not _is_hwnd_visible(self._wid))
                self._vo_needs_reinit = False
                player.vid = "auto" if video_enabled else "no"

            if low_latency:
                player["cache"] = "no"
                player["cache-secs"] = 0.4
                player["demuxer-lavf-probesize"] = 32768
                player["demuxer-lavf-analyzeduration"] = 0.25
                player["audio-buffer"] = 0.05
                player["demuxer-max-bytes"] = "1MiB"
                player["demuxer-max-back-bytes"] = "64KiB"
                player.start = "none"
                player.speed = 1.0
            elif live_flag and not live_seekable:
                player["cache"] = "yes"
                player["cache-secs"] = 2
                player["demuxer-lavf-probesize"] = 1048576
                player["demuxer-lavf-analyzeduration"] = 0.5
                player["audio-buffer"] = 0.1
                player["demuxer-max-bytes"] = "2MiB"
                player["demuxer-max-back-bytes"] = "128KiB"
                player.start = "none"
                player.speed = 1.0
            elif live_flag and live_seekable:
                player["cache"] = "yes"
                player["cache-secs"] = 3
                player["demuxer-lavf-probesize"] = 1048576
                player["demuxer-lavf-analyzeduration"] = 0.5
                player["audio-buffer"] = 0.1
                player["demuxer-max-bytes"] = "4MiB"
                player["demuxer-max-back-bytes"] = "512KiB"
                player.start = "none"
                player.speed = self._speed
            else:
                player["cache"] = "yes"
                player["cache-secs"] = 3
                player["demuxer-lavf-probesize"] = 1048576
                player["demuxer-lavf-analyzeduration"] = 0.5
                player["audio-buffer"] = 0.1
                player["demuxer-max-bytes"] = (
                    "2MiB" if self._is_audio_only_instance else "3MiB"
                )
                player["demuxer-max-back-bytes"] = (
                    "256KiB" if self._is_audio_only_instance else "512KiB"
                )
                player.start = f"+{start_pos:.1f}" if start_pos > 0.5 else "none"
                player.speed = self._speed

            player.volume = self._volume
            target_af = NORMALIZATION_FILTER if self._normalization else ""
            if target_af != self._applied_af:
                player.af = target_af
                self._applied_af = target_af

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
            player.loadfile(primary_url, "replace")
            self._streams_played += 1
            self._last_was_live = live_flag

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
            self._user_stopped = True
            self._has_media = False
            self._is_live = False
            self._live_seekable = False
            if not self._shutdown and self._mpv is not None:
                try:
                    self._mpv["audio-files"] = []
                except Exception:
                    pass
                for cmd in ("stop", "playlist-clear", "drop-buffers"):
                    try:
                        self._mpv.command(cmd)
                    except Exception:
                        pass

    def unload(self) -> None:
        """Libera la instancia de libmpv manteniendo el objeto reutilizable al dar Play."""
        with self._lock:
            self._user_stopped = True
            self._has_media = False
            self._is_live = False
            self._live_seekable = False
            self._last_was_live = None
            self._streams_played = 0
            self._applied_af = None
            self._last_mouse_xy = None
            self._vo_needs_reinit = False
            self._started_hidden = False
            self._vo_error_count = 0
            self._active_vo = "gpu"
            self._last_vo_error = ""
            if self._mpv is not None:
                self._unloading = True
                try:
                    self._mpv.terminate()
                except Exception:
                    pass
                finally:
                    self._unloading = False
                    self._mpv = None

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
            target_af = NORMALIZATION_FILTER if self._normalization else ""
            if not self._shutdown and self._mpv is not None:
                if target_af != self._applied_af:
                    self._mpv.af = target_af
                    self._applied_af = target_af

    def set_video_enabled(self, enabled: bool) -> None:
        with self._lock:
            if (
                not self._shutdown
                and not self._is_audio_only_instance
                and self._mpv is not None
            ):
                self._mpv.vid = "auto" if enabled else "no"

    def refresh_video_output(self, *, force_reinit: bool = False) -> bool:
        """Fuerza re-presentación del frame actual cuando el HWND vuelve de estar oculto o tras error D3D11."""
        with self._lock:
            if (
                self._shutdown
                or self._unloading
                or self._is_audio_only_instance
                or self._mpv is None
                or not self._has_media
            ):
                return False
            if not _is_hwnd_visible(self._wid):
                return False
            vo_cfg = True
            try:
                vo_cfg = bool(self._mpv.vo_configured)
            except Exception:
                vo_cfg = True
            need_reinit = bool(
                force_reinit or self._vo_needs_reinit or self._started_hidden or not vo_cfg
            )
            if need_reinit:
                now = time.monotonic()
                if (now - self._last_vo_reinit_ts) < 0.8:
                    self._started_hidden = False
                    return False
                try:
                    if self._vo_error_count >= 2 and self._active_vo == "gpu":
                        self._active_vo = "gpu-next"
                    self._mpv["vo"] = "null"
                    self._mpv["vo"] = self._active_vo
                    self._last_vo_reinit_ts = now
                    self._vo_needs_reinit = False
                    self._started_hidden = False
                    return True
                except Exception:
                    return False
            try:
                self._mpv.video_pan_x = 0.0001
                self._mpv.video_pan_x = 0.0
            except Exception:
                pass
            return False

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
            self._has_media = False
            self._is_live = False
            self._live_seekable = False
            self._last_was_live = None
            self._streams_played = 0
            self._applied_af = None
            self._vo_needs_reinit = False
            self._started_hidden = False
            self._vo_error_count = 0
            self._active_vo = "gpu"
            self._last_vo_error = ""
            if self._mpv is not None:
                self._unloading = True
                try:
                    self._mpv.terminate()
                except Exception:
                    pass
                finally:
                    self._unloading = False
                    self._mpv = None
