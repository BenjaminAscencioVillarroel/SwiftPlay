"""Subproceso worker aislado para Discord Voz (DAVE E2EE + Opus + audio en tiempo real).

Regla crítica de arquitectura:
- Este módulo NO importa discord, davey, nacl ni sounddevice al nivel superior.
- El proceso GUI de SwiftPlay nunca debe importar dichas librerías; solo lanza este
  módulo en un subproceso aislado cuando el usuario conecta el Bot de Discord.
"""

from __future__ import annotations

import array
import asyncio
from collections import deque
from datetime import datetime, timedelta
import json
import math
import os

for _env_k, _env_v in (
    ("OMP_NUM_THREADS", "1"),
    ("OMP_WAIT_POLICY", "PASSIVE"),
    ("OPENBLAS_NUM_THREADS", "1"),
    ("MKL_NUM_THREADS", "1"),
    ("NUMEXPR_NUM_THREADS", "1"),
    ("RAYON_NUM_THREADS", "1"),
    ("GGML_NUM_THREADS", "1"),
):
    os.environ.setdefault(_env_k, _env_v)
import queue
import re
import struct
import sys
import threading
import time
import warnings
from typing import Any, Callable, Literal

from swiftplay.screen_share_service import (
    encode_screen_share_presence,
    parse_screen_share_presence,
)

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = (SAMPLE_RATE * FRAME_MS) // 1000  # 960
MONO_FRAME_BYTES = FRAME_SAMPLES * 2  # 1920 bytes (int16 mono)
PCM_FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2  # 3840 bytes (int16 stereo)
SILENCE_PCM = b"\x00" * PCM_FRAME_BYTES
OPUS_SILENCE = b"\xf8\xff\xfe"

_AUDIOOP_CACHE: Any = None
_SCREEN_SHARE_URL_RE = re.compile(
    r"(https://[a-z0-9-]+\.trycloudflare\.com|http://127\.0\.0\.1:\d+)/watch\?t=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_SCREEN_SHARE_ENDED_MARKER = "Transmisión de pantalla finalizada"


def _extract_screen_share_urls(content: str) -> tuple[str, str] | None:
    txt = str(content or "")
    if not txt or _SCREEN_SHARE_ENDED_MARKER.lower() in txt.lower():
        return None
    m = _SCREEN_SHARE_URL_RE.search(txt)
    if not m:
        return None
    base = m.group(1).rstrip("/")
    tok = m.group(2)
    return f"{base}/watch?t={tok}", f"{base}/stream.mp4?t={tok}"


def _get_audioop() -> Any:
    """Obtiene el módulo audioop en C suprimiendo el DeprecationWarning de Python 3.12."""
    global _AUDIOOP_CACHE
    if _AUDIOOP_CACHE is False:
        return None
    if _AUDIOOP_CACHE is not None:
        return _AUDIOOP_CACHE
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop  # type: ignore

        _AUDIOOP_CACHE = audioop
    except ImportError:
        _AUDIOOP_CACHE = False
        return None
    return _AUDIOOP_CACHE


def _pcm_scale(pcm: bytes, factor: float) -> bytes:
    """Escala PCM int16 aplicando saturación segura."""
    if abs(factor - 1.0) < 0.01:
        return pcm
    if factor <= 0.0:
        return b"\x00" * len(pcm)
    aop = _get_audioop()
    if aop is not None:
        try:
            return aop.mul(pcm, 2, float(factor))
        except Exception:
            pass
    samples = array.array("h")
    samples.frombytes(pcm)
    for i, s in enumerate(samples):
        v = int(s * factor)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        samples[i] = v
    return samples.tobytes()


def _pcm_add(a: bytes, b: bytes) -> bytes:
    """Suma dos buffers PCM int16 de igual longitud con saturación."""
    aop = _get_audioop()
    if aop is not None:
        try:
            return aop.add(a, b, 2)
        except Exception:
            pass
    sa = array.array("h")
    sb = array.array("h")
    sa.frombytes(a)
    sb.frombytes(b)
    out = array.array(
        "h",
        (max(-32768, min(32767, x + y)) for x, y in zip(sa, sb)),
    )
    return out.tobytes()


def _pcm_rms(pcm: bytes) -> int:
    aop = _get_audioop()
    if aop is not None and pcm:
        try:
            return int(aop.rms(pcm, 2))
        except Exception:
            pass
    if not pcm:
        return 0
    sa = array.array("h")
    sa.frombytes(pcm)
    if not sa:
        return 0
    total = sum(x * x for x in sa)
    return int((total / len(sa)) ** 0.5)


def _pcm_max_abs(pcm: bytes) -> int:
    aop = _get_audioop()
    if aop is not None and pcm:
        try:
            return int(aop.max(pcm, 2))
        except Exception:
            pass
    if not pcm:
        return 0
    sa = array.array("h")
    sa.frombytes(pcm)
    return max((abs(x) for x in sa), default=0)


_VALID_NS_MODES: tuple[str, ...] = ("off", "webrtc", "ai", "ai_max")

_LEGACY_NS_MAP: dict[str, str] = {
    "low": "webrtc",
    "moderate": "webrtc",
    "high": "ai",
    "max": "ai_max",
}


def _normalize_ns_mode(mode: str | None) -> str:
    ns = str(mode or "ai").strip().lower()
    ns = _LEGACY_NS_MAP.get(ns, ns)
    return ns if ns in _VALID_NS_MODES else "ai"


_NS_MODE_TO_LEVEL: dict[str, int] = {
    "low": 0,
    "moderate": 1,
    "high": 2,
    "max": 3,
    "webrtc": 2,
}

_NS_SPEECH_PROB_THRESHOLDS: dict[str, tuple[float, float]] = {
    "low": (0.14, 0.06),
    "moderate": (0.20, 0.08),
    "high": (0.26, 0.09),
    "max": (0.32, 0.10),
    "webrtc": (0.26, 0.09),
}


class _WebRtcMicDsp:
    """Wrapper lazy y thread-safe de pywebrtc_audio.NoiseSuppressor (48 kHz mono)."""

    def __init__(self, mode: str = "webrtc") -> None:
        self._lock = threading.Lock()
        self._mode = self._norm(mode)
        self._ns: Any = None
        self._ns_mode: str | None = None

    @staticmethod
    def _norm(mode: str | None) -> str:
        m = str(mode or "webrtc").strip().lower()
        return m if m in ("off", "low", "moderate", "high", "max", "webrtc") else "webrtc"

    def _build_ns_locked(self) -> Any:
        if self._mode == "off":
            self._ns = None
            self._ns_mode = "off"
            return None
        from pywebrtc_audio import NoiseSuppressor

        level = _NS_MODE_TO_LEVEL.get(self._mode, 2)
        self._ns = NoiseSuppressor(sample_rate=SAMPLE_RATE, num_channels=1, level=level)
        self._ns_mode = self._mode
        return self._ns

    def reconfigure(self, mode: str) -> None:
        norm = self._norm(mode)
        with self._lock:
            if norm == self._mode:
                return
            self._mode = norm
            if norm == "off":
                self._ns = None
                self._ns_mode = "off"
            elif self._ns is not None:
                self._build_ns_locked()

    def reset(self) -> None:
        with self._lock:
            if self._ns is not None:
                try:
                    self._ns.reset()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._ns = None
            self._ns_mode = None

    def process_20ms(self, raw_mono: bytes) -> tuple[bytes, float]:
        if len(raw_mono) != MONO_FRAME_BYTES:
            return raw_mono, 1.0
        with self._lock:
            if self._mode == "off":
                return raw_mono, 1.0
            try:
                import numpy as np

                ns = self._ns
                if ns is None or self._ns_mode != self._mode:
                    ns = self._build_ns_locked()
                if ns is None:
                    return raw_mono, 1.0
                arr = np.frombuffer(raw_mono, dtype=np.int16)
                f1 = np.ascontiguousarray(arr[:480], dtype=np.int16)
                f2 = np.ascontiguousarray(arr[480:960], dtype=np.int16)
                out1 = ns.process(f1)
                p1 = float(ns.speech_probability)
                out2 = ns.process(f2)
                p2 = float(ns.speech_probability)
                return out1.tobytes() + out2.tobytes(), max(p1, p2)
            except Exception:
                return raw_mono, 1.0


class _SileroVoiceGate:
    """Detector de voz neuronal Silero VAD v6 (16 kHz diezmado desde 48 kHz mono)."""

    def __init__(self) -> None:
        self._vad: Any = None
        self._np: Any = None
        self._chunk_bytes = 1024  # 512 muestras int16 a 16 kHz
        self._buf = bytearray()
        self._last_prob = 0.0
        self._failed = False

    def _ensure_loaded(self) -> bool:
        if self._failed:
            return False
        if self._vad is not None and self._np is not None:
            return True
        try:
            import numpy as np
            from pysilero_vad import SileroVoiceActivityDetector

            self._vad = SileroVoiceActivityDetector()
            self._chunk_bytes = int(self._vad.chunk_bytes())
            self._np = np
            return True
        except Exception:
            self._failed = True
            return False

    def warmup(self) -> None:
        self._ensure_loaded()

    def reset(self) -> None:
        self._buf.clear()
        self._last_prob = 0.0
        if self._vad is not None:
            try:
                self._vad.reset()
            except Exception:
                pass

    def close(self) -> None:
        self._buf.clear()
        self._last_prob = 0.0
        self._vad = None
        self._np = None

    def process_20ms(self, pcm_48k_bytes: bytes) -> float | None:
        if not self._ensure_loaded():
            return None
        if len(pcm_48k_bytes) != MONO_FRAME_BYTES:
            return self._last_prob
        try:
            np = self._np
            arr = np.frombuffer(pcm_48k_bytes, dtype=np.int16)
            decim = arr.reshape(320, 3).mean(axis=1).astype(np.int16)
            self._buf.extend(decim.tobytes())
            cbytes = self._chunk_bytes
            while len(self._buf) >= cbytes:
                chunk = bytes(self._buf[:cbytes])
                del self._buf[:cbytes]
                self._last_prob = float(self._vad.process_chunk(chunk))
            return self._last_prob
        except Exception:
            return None


class _DeepFilterMicDsp:
    """Wrapper lazy y thread-safe de DeepFilterNet3 (deepfilternet-slim) + Silero VAD v6."""

    def __init__(self, mode: str = "ai") -> None:
        self._lock = threading.Lock()
        self._mode = _normalize_ns_mode(mode)
        self._lib: Any = None
        self._df_state: Any = None
        self._hop = 480
        self._in_buf: Any = None
        self._out_buf: Any = None
        self._out_full: Any = None
        self._in_ptr: Any = None
        self._out_ptr: Any = None
        self._np: Any = None
        self._ctypes: Any = None
        self._hp_prev_x = 0.0
        self._hp_prev_y = 0.0
        self._silero = _SileroVoiceGate()
        self._ready = False
        self._warming = False
        self._warmup_requested = False
        self._warmup_flag_lock = threading.Lock()
        self._failed = False

    def _apply_params_locked(self) -> None:
        if self._lib is None or not self._df_state or self._ctypes is None:
            return
        beta = 0.05 if self._mode == "ai_max" else 0.02
        self._lib.df_set_atten_lim(self._df_state, self._ctypes.c_float(100.0))
        self._lib.df_set_post_filter_beta(self._df_state, self._ctypes.c_float(beta))

    def _free_locked(self) -> None:
        self._ready = False
        st = self._df_state
        self._df_state = None
        self._in_buf = None
        self._out_buf = None
        self._out_full = None
        self._in_ptr = None
        self._out_ptr = None
        if st and self._lib is not None:
            try:
                self._lib.df_free(st)
            except Exception:
                pass

    def _ensure_ready_locked(self) -> bool:
        if self._failed or self._mode not in ("ai", "ai_max"):
            return False
        if self._df_state:
            return True
        try:
            import ctypes
            import numpy as np
            from deepfilternet_slim import load_library, runtime_assets

            if self._lib is None:
                lib = load_library()
                lib.df_create.argtypes = [ctypes.c_char_p, ctypes.c_float, ctypes.c_char_p]
                lib.df_create.restype = ctypes.c_void_p
                lib.df_get_frame_length.argtypes = [ctypes.c_void_p]
                lib.df_get_frame_length.restype = ctypes.c_size_t
                lib.df_set_atten_lim.argtypes = [ctypes.c_void_p, ctypes.c_float]
                lib.df_set_atten_lim.restype = None
                lib.df_set_post_filter_beta.argtypes = [ctypes.c_void_p, ctypes.c_float]
                lib.df_set_post_filter_beta.restype = None
                lib.df_process_frame.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.POINTER(ctypes.c_float),
                ]
                lib.df_process_frame.restype = ctypes.c_float
                lib.df_free.argtypes = [ctypes.c_void_p]
                lib.df_free.restype = None
                self._lib = lib
                self._ctypes = ctypes
                self._np = np

            assets = runtime_assets()
            st = self._lib.df_create(
                str(assets.model_path).encode("utf-8"),
                ctypes.c_float(100.0),
                None,
            )
            if not st:
                self._failed = True
                return False

            hop = int(self._lib.df_get_frame_length(st))
            if hop <= 0 or (FRAME_SAMPLES % hop) != 0:
                self._lib.df_free(st)
                self._failed = True
                return False

            self._df_state = st
            self._hop = hop
            self._in_buf = np.zeros(hop, dtype=np.float32)
            self._out_buf = np.zeros(hop, dtype=np.float32)
            self._out_full = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            self._in_ptr = self._in_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._out_ptr = self._out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._apply_params_locked()
            self._silero.warmup()
            self._ready = True
            return True
        except Exception:
            self._free_locked()
            self._failed = True
            return False

    @property
    def is_ready(self) -> bool:
        return bool(self._ready and self._df_state)

    def warmup(self) -> bool:
        with self._lock:
            return self._ensure_ready_locked()

    def start_warmup_async(self, *, trim_after: bool = True) -> None:
        """Precalienta DeepFilterNet3 y Silero VAD en segundo plano de forma idempotente."""
        if self._ready or self._failed or self._mode not in ("ai", "ai_max"):
            return
        with self._warmup_flag_lock:
            if self._ready or self._failed or self._mode not in ("ai", "ai_max"):
                return
            if self._warming:
                self._warmup_requested = True
                return
            self._warming = True
            self._warmup_requested = False

        def _bg() -> None:
            ok_any = False
            try:
                while True:
                    ok = False
                    try:
                        ok = self.warmup()
                        ok_any = ok_any or bool(ok)
                    except Exception:
                        pass
                    with self._warmup_flag_lock:
                        need_more = bool(
                            self._warmup_requested
                            and not self._ready
                            and not self._failed
                            and self._mode in ("ai", "ai_max")
                        )
                        self._warmup_requested = False
                        if not need_more:
                            self._warming = False
                            break
                if ok_any and trim_after:
                    try:
                        from swiftplay.windows_os import trim_process_memory

                        trim_process_memory(collect_gc=True)
                    except Exception:
                        pass
            finally:
                with self._warmup_flag_lock:
                    self._warming = False

        threading.Thread(
            target=_bg,
            daemon=True,
            name="discord-audio-warmup",
        ).start()

    def reconfigure(self, mode: str) -> None:
        norm = _normalize_ns_mode(mode)
        with self._lock:
            self._mode = norm
            if norm not in ("ai", "ai_max"):
                self._free_locked()
                self._hp_prev_x = 0.0
                self._hp_prev_y = 0.0
                self._silero.reset()
            else:
                self._failed = False
                if self._df_state:
                    self._apply_params_locked()

    def reset(self) -> None:
        if not self._df_state:
            return
        with self._lock:
            self._hp_prev_x = 0.0
            self._hp_prev_y = 0.0
            self._silero.reset()

    def close(self) -> None:
        with self._lock:
            self._free_locked()
            self._silero.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def process_20ms(
        self, raw_mono: bytes, *, is_speaking: bool = False
    ) -> tuple[bytes, float, float | None] | None:
        if len(raw_mono) != MONO_FRAME_BYTES:
            return raw_mono, 1.0, None
        with self._lock:
            if not self._ensure_ready_locked():
                return None
            try:
                if not is_speaking and _pcm_rms(raw_mono) < 15:
                    self._hp_prev_x = 0.0
                    self._hp_prev_y = 0.0
                    return b"\x00" * MONO_FRAME_BYTES, -15.0, 0.0

                np = self._np
                lib = self._lib
                st = self._df_state
                hop = self._hop

                arr = np.frombuffer(raw_mono, dtype=np.int16)
                x_list = (arr.astype(np.float32) * (1.0 / 32768.0)).tolist()

                # Filtro pasa-altos de 1 polo (~85 Hz) contra zumbido eléctrico y vibración
                px = self._hp_prev_x
                py = self._hp_prev_y
                hp = [0.0] * FRAME_SAMPLES
                alpha = 0.9889
                for i, xi in enumerate(x_list):
                    yi = alpha * (py + xi - px)
                    hp[i] = yi
                    px = xi
                    py = yi
                self._hp_prev_x = px
                self._hp_prev_y = py

                hp_arr = np.asarray(hp, dtype=np.float32)
                out_full = self._out_full
                max_lsnr = -15.0
                for off in range(0, FRAME_SAMPLES, hop):
                    np.copyto(self._in_buf, hp_arr[off : off + hop])
                    lsnr = float(lib.df_process_frame(st, self._in_ptr, self._out_ptr))
                    if lsnr > max_lsnr:
                        max_lsnr = lsnr
                    out_full[off : off + hop] = self._out_buf

                np.multiply(out_full, 32767.0, out=out_full)
                np.clip(out_full, -32768.0, 32767.0, out=out_full)
                clean_bytes = out_full.astype(np.int16).tobytes()
                silero_prob = self._silero.process_20ms(clean_bytes)
                return clean_bytes, max_lsnr, silero_prob
            except Exception:
                return None


class VoiceModifierDsp:
    """Modificador de voz en tiempo real sobre frames de 20 ms @ 48 kHz mono (aislado en worker)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._pitch = 0
        self._tone = 0
        self._robot = 0
        self._echo = 0
        self._np: Any = None

        self._ring_len = 4096
        self._ring: Any = None
        self._ring_w = 0
        self._win_len = 960  # 20 ms Hann overlap (~10 ms de retardo medio)
        self._tap_phase = 0.0
        self._tone_lp = 0.0
        self._robot_phase = 0.0
        self._echo_len = 7200  # 150 ms @ 48 kHz
        self._echo_buf: Any = None
        self._echo_idx = 0
        self._echo_lp = 0.0

    def _ensure_buffers(self) -> Any:
        np = self._np
        if np is None:
            import numpy as np_mod

            self._np = np_mod
            np = np_mod
        if self._ring is None:
            self._ring = np.zeros(self._ring_len, dtype=np.float32)
        if self._echo_buf is None:
            self._echo_buf = np.zeros(self._echo_len, dtype=np.float32)
        return np

    def configure(
        self,
        *,
        enabled: bool | None = None,
        pitch: int | None = None,
        tone: int | None = None,
        robot: int | None = None,
        echo: int | None = None,
    ) -> None:
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            if pitch is not None:
                self._pitch = max(-12, min(12, int(pitch)))
            if tone is not None:
                self._tone = max(-100, min(100, int(tone)))
            if robot is not None:
                self._robot = max(0, min(100, int(robot)))
            if echo is not None:
                self._echo = max(0, min(100, int(echo)))

    def reset(self) -> None:
        with self._lock:
            if self._ring is not None:
                self._ring.fill(0.0)
            if self._echo_buf is not None:
                self._echo_buf.fill(0.0)
            self._ring_w = 0
            self._tap_phase = 0.0
            self._tone_lp = 0.0
            self._robot_phase = 0.0
            self._echo_idx = 0
            self._echo_lp = 0.0

    def process_20ms(self, raw_mono_frame: bytes) -> bytes:
        with self._lock:
            enabled = self._enabled
            pitch = self._pitch
            tone = self._tone
            robot = self._robot
            echo = self._echo
        if not enabled or (pitch == 0 and tone == 0 and robot == 0 and echo == 0):
            return raw_mono_frame
        if not raw_mono_frame:
            return raw_mono_frame

        np = self._ensure_buffers()
        x = np.frombuffer(raw_mono_frame, dtype=np.int16).astype(np.float32) * (1.0 / 32768.0)
        n = int(x.shape[0])
        if n <= 0:
            return raw_mono_frame

        # 1) Pitch shifter de doble tap con ventana Hann (sin clics, baja latencia)
        if pitch != 0:
            ratio = 2.0 ** (float(pitch) / 12.0)
            ring = self._ring
            rlen = self._ring_len
            wlen = self._win_len
            w_idx = (self._ring_w + np.arange(n, dtype=np.int32)) % rlen
            ring[w_idx] = x
            self._ring_w = (self._ring_w + n) % rlen

            step = (1.0 - ratio) / float(wlen)
            phases = (self._tap_phase + np.arange(n, dtype=np.float32) * step) % 1.0
            self._tap_phase = float((self._tap_phase + n * step) % 1.0)

            p1 = phases
            p2 = (phases + 0.5) % 1.0
            d1 = p1 * float(wlen)
            d2 = p2 * float(wlen)
            pos1 = (w_idx.astype(np.float32) - d1) % float(rlen)
            pos2 = (w_idx.astype(np.float32) - d2) % float(rlen)

            i1 = np.floor(pos1).astype(np.int32) % rlen
            f1 = pos1 - np.floor(pos1)
            s1 = ring[i1] * (1.0 - f1) + ring[(i1 + 1) % rlen] * f1

            i2 = np.floor(pos2).astype(np.int32) % rlen
            f2 = pos2 - np.floor(pos2)
            s2 = ring[i2] * (1.0 - f2) + ring[(i2 + 1) % rlen] * f2

            g1 = np.sin(np.pi * p1) ** 2
            g2 = 1.0 - g1
            x = s1 * g1 + s2 * g2

        # 2) Moldeador de timbre (cálido/oscuro para valores negativos, brillante/radio para positivos)
        if tone != 0:
            t_norm = float(tone) / 100.0
            if t_norm < 0.0:
                amt = -t_norm
                alpha = max(0.06, 0.34 - 0.25 * amt)
                lp = self._tone_lp
                out_t = np.empty_like(x)
                for i in range(n):
                    lp = lp + alpha * (float(x[i]) - lp)
                    out_t[i] = lp
                self._tone_lp = lp
                x = x * (1.0 - 0.85 * amt) + out_t * (0.85 * amt)
            else:
                amt = t_norm
                alpha = 0.22
                lp = self._tone_lp
                out_hp = np.empty_like(x)
                for i in range(n):
                    v = float(x[i])
                    lp = lp + alpha * (v - lp)
                    out_hp[i] = v - lp
                self._tone_lp = lp
                x = x * (1.0 - 0.30 * amt) + out_hp * (0.95 * amt)

        # 3) Modulador en anillo robótico (portadora continua 75 Hz + 150 Hz)
        if robot > 0:
            r_mix = (float(robot) / 100.0) * 0.85
            ph = self._robot_phase + (2.0 * math.pi * 75.0 / 48000.0) * np.arange(n, dtype=np.float32)
            self._robot_phase = float((self._robot_phase + n * (2.0 * math.pi * 75.0 / 48000.0)) % (2.0 * math.pi))
            carrier = 0.72 * np.sin(ph) + 0.28 * np.sin(2.0 * ph)
            x = x * (1.0 - r_mix) + (x * carrier * 1.35) * r_mix

        # 4) Eco / reverberación con realimentación amortiguada
        if echo > 0:
            e_amt = float(echo) / 100.0
            wet = 0.48 * e_amt
            fb = 0.42 * e_amt
            ebuf = self._echo_buf
            elen = self._echo_len
            eidx = self._echo_idx
            elp = self._echo_lp
            out_e = np.empty_like(x)
            for i in range(n):
                delayed = float(ebuf[eidx])
                elp = elp + 0.32 * (delayed - elp)
                dry_s = float(x[i])
                ebuf[eidx] = max(-1.2, min(1.2, dry_s + elp * fb))
                out_e[i] = dry_s + delayed * wet
                eidx += 1
                if eidx >= elen:
                    eidx = 0
            self._echo_idx = eidx
            self._echo_lp = elp
            x = out_e

        # Limitador suave para evitar saturación
        x = 0.96 * np.tanh(x * (1.0 / 0.96))
        pcm_out = np.clip(x * 32767.0, -32768.0, 32767.0).astype(np.int16)
        return pcm_out.tobytes()


class MicProcessor:
    """Supresión de ruido neuronal (DeepFilterNet3 + Silero / WebRTC), AGC suave, modificador de voz y VAD."""

    def __init__(
        self,
        *,
        mic_gain_pct: int = 100,
        agc_enabled: bool = True,
        vad_threshold: int = 18,
        noise_suppression: str = "ai",
        enable_webrtc: bool = False,
        enable_dsp: bool | None = None,
        hangover_sec: float = 0.60,
        preroll_frames: int = 1,
    ) -> None:
        self._lock = threading.Lock()
        self._mic_gain = max(0, min(400, int(mic_gain_pct))) / 100.0
        self._agc_enabled = bool(agc_enabled)
        self._agc_gain = 1.6
        self._voice_env = 0.0
        self._vad_threshold = max(0, min(100, int(vad_threshold)))
        self._noise_suppression = _normalize_ns_mode(noise_suppression)
        dsp_active = bool(enable_webrtc if enable_dsp is None else enable_dsp)
        self._enable_dsp = dsp_active
        self._enable_webrtc = dsp_active
        self._df_dsp = _DeepFilterMicDsp(self._noise_suppression) if dsp_active else None
        self._webrtc_dsp = _WebRtcMicDsp("webrtc") if dsp_active else None
        self._voice_fx = VoiceModifierDsp()
        self._hangover_sec = float(hangover_sec)
        self._preroll: deque[bytes] = deque(maxlen=max(1, int(preroll_frames)))
        self._speaking = False
        self._hangover_until = 0.0

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def warmup(self) -> None:
        with self._lock:
            ns_mode = self._noise_suppression
            df_dsp = self._df_dsp
        if df_dsp is not None and ns_mode in ("ai", "ai_max"):
            df_dsp.warmup()

    def ensure_warmup_async(self, *, trim_after: bool = True) -> None:
        with self._lock:
            ns_mode = self._noise_suppression
            df_dsp = self._df_dsp
        if df_dsp is not None and ns_mode in ("ai", "ai_max"):
            df_dsp.start_warmup_async(trim_after=trim_after)

    def close(self) -> None:
        self.release_dsp()

    def release_dsp(self) -> None:
        with self._lock:
            df_dsp = self._df_dsp
            webrtc_dsp = self._webrtc_dsp
        if df_dsp is not None:
            df_dsp.close()
        if webrtc_dsp is not None:
            webrtc_dsp.close()

    def configure(
        self,
        *,
        mic_gain_pct: int | None = None,
        agc_enabled: bool | None = None,
        vad_threshold: int | None = None,
        noise_suppression: str | None = None,
        voice_fx: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if mic_gain_pct is not None:
                self._mic_gain = max(0, min(400, int(mic_gain_pct))) / 100.0
            if agc_enabled is not None:
                self._agc_enabled = bool(agc_enabled)
                if self._agc_enabled:
                    self._agc_gain = max(0.6, min(5.0, self._agc_gain))
            if vad_threshold is not None:
                self._vad_threshold = max(0, min(100, int(vad_threshold)))
            if noise_suppression is not None:
                self._noise_suppression = _normalize_ns_mode(noise_suppression)
            cur_ns = self._noise_suppression
            df_dsp = self._df_dsp
            webrtc_dsp = self._webrtc_dsp
            vfx = self._voice_fx
        if df_dsp is not None:
            df_dsp.reconfigure(cur_ns)
        if webrtc_dsp is not None:
            webrtc_dsp.reconfigure("webrtc" if cur_ns != "off" else "off")
        if isinstance(voice_fx, dict):
            vfx.configure(
                enabled=voice_fx.get("enabled"),
                pitch=voice_fx.get("pitch"),
                tone=voice_fx.get("tone"),
                robot=voice_fx.get("robot"),
                echo=voice_fx.get("echo"),
            )

    def reset(self) -> None:
        with self._lock:
            self._preroll.clear()
            self._speaking = False
            self._hangover_until = 0.0
            self._agc_gain = 1.6
            self._voice_env = 0.0
            df_dsp = self._df_dsp
            webrtc_dsp = self._webrtc_dsp
            vfx = self._voice_fx
        if df_dsp is not None:
            df_dsp.reset()
        if webrtc_dsp is not None:
            webrtc_dsp.reset()
        vfx.reset()

    def check_hangover(self, now: float) -> bool:
        """Devuelve True si expiró el hangover estando en reposo y se cerró el micrófono."""
        with self._lock:
            if self._speaking and now > self._hangover_until:
                self._speaking = False
                self._voice_env = 0.0
                return True
            return False

    def process_frame(self, raw_mono_frame: bytes, now: float) -> tuple[list[bytes], int, bool]:
        if not raw_mono_frame:
            return [], 0, False

        with self._lock:
            mic_gain = self._mic_gain
            agc_enabled = self._agc_enabled
            vad_thr = self._vad_threshold
            ns_mode = self._noise_suppression
            df_dsp = self._df_dsp
            webrtc_dsp = self._webrtc_dsp
            speaking = self._speaking
            hangover_until = self._hangover_until
            hangover_sec = self._hangover_sec

        work_frame = raw_mono_frame
        use_dsp = False
        voice_ok = True

        if df_dsp is not None and ns_mode in ("ai", "ai_max"):
            if df_dsp.is_ready:
                df_res = df_dsp.process_20ms(raw_mono_frame, is_speaking=speaking)
                if df_res is not None:
                    work_frame, lsnr_db, silero_prob = df_res
                    use_dsp = True
                    if ns_mode == "ai":
                        if silero_prob is not None:
                            if speaking:
                                voice_ok = (silero_prob >= 0.12) or (lsnr_db >= -1.0)
                            else:
                                voice_ok = (silero_prob >= 0.30 and lsnr_db >= -6.0) or (lsnr_db >= 2.5)
                        else:
                            voice_ok = lsnr_db >= (-1.0 if speaking else 2.5)
                    else:
                        if silero_prob is not None:
                            if speaking:
                                voice_ok = (silero_prob >= 0.16) or (lsnr_db >= 2.0)
                            else:
                                voice_ok = (
                                    (silero_prob >= 0.45 and lsnr_db >= -4.0)
                                    or (silero_prob >= 0.65)
                                    or (lsnr_db >= 5.0)
                                )
                        else:
                            voice_ok = lsnr_db >= (0.0 if speaking else 5.0)
            else:
                df_dsp.start_warmup_async()

        if not use_dsp and webrtc_dsp is not None and ns_mode in ("webrtc", "ai", "ai_max"):
            work_frame, speech_prob = webrtc_dsp.process_20ms(raw_mono_frame)
            use_dsp = True
            open_prob, hold_prob = _NS_SPEECH_PROB_THRESHOLDS["webrtc"]
            voice_ok = speech_prob >= (hold_prob if speaking else open_prob)

        voice_for_agc = voice_ok if use_dsp else True
        work_rms = _pcm_rms(work_frame)

        with self._lock:
            if voice_for_agc:
                self._voice_env = min(1.0, self._voice_env + 0.65 * (1.0 - self._voice_env))
            else:
                self._voice_env = max(0.0, self._voice_env - 0.12 * self._voice_env)

            if agc_enabled and work_rms >= 75 and voice_for_agc:
                est_rms = work_rms * max(0.05, self._mic_gain)
                desired_agc = max(0.6, min(5.0, 3800.0 / est_rms))
                alpha = 0.35 if desired_agc < self._agc_gain else 0.06
                self._agc_gain = max(
                    0.6, min(5.0, self._agc_gain + alpha * (desired_agc - self._agc_gain))
                )

            if agc_enabled and use_dsp:
                applied_agc = 1.0 + (self._agc_gain - 1.0) * self._voice_env
            elif agc_enabled:
                applied_agc = self._agc_gain
            else:
                applied_agc = 1.0

            total_gain = mic_gain * applied_agc
            if use_dsp and ns_mode == "ai_max" and not voice_ok and self._voice_env < 0.25:
                total_gain *= 0.20 + 0.80 * (self._voice_env / 0.25)

        work_peak = _pcm_max_abs(work_frame)
        if work_peak > 0 and (work_peak * total_gain) > 29500.0:
            total_gain = 29500.0 / float(work_peak)

        processed = _pcm_scale(work_frame, total_gain)
        processed = self._voice_fx.process_20ms(processed)
        out_rms = _pcm_rms(processed)
        if out_rms <= 20:
            level = 0
        else:
            dbfs = 20.0 * math.log10(min(32768, out_rms) / 32768.0)
            level = max(0, min(100, int((dbfs + 60.0) * 2.0)))

        if vad_thr == 0:
            above = True
        else:
            effective_thr = max(0, vad_thr - 10) if speaking else vad_thr
            energy_ok = level >= effective_thr
            above = (energy_ok and voice_ok) if use_dsp else energy_ok

        if above:
            hangover_until = now + hangover_sec
            should_tx = True
        else:
            should_tx = speaking and (now <= hangover_until)

        with self._lock:
            self._hangover_until = hangover_until
            if should_tx:
                if not self._speaking:
                    self._speaking = True
                    frames = list(self._preroll)
                    frames.append(processed)
                    self._preroll.clear()
                    return frames, level, True
                return [processed], level, True
            self._speaking = False
            self._preroll.append(processed)
            return [], level, False


def _pcm_mono_to_stereo(mono: bytes) -> bytes:
    aop = _get_audioop()
    if aop is not None:
        try:
            return aop.tostereo(mono, 2, 1.0, 1.0)
        except Exception:
            pass
    sa = array.array("h")
    sa.frombytes(mono)
    out = array.array("h")
    for s in sa:
        out.append(s)
        out.append(s)
    return out.tobytes()


def _pcm_to_mono(pcm: bytes, channels: int) -> bytes:
    if channels <= 1 or not pcm:
        return pcm
    aop = _get_audioop()
    if aop is not None and channels == 2:
        try:
            left = aop.tomono(pcm, 2, 1.0, 0.0)
            right = aop.tomono(pcm, 2, 0.0, 1.0)
            rms_l = int(aop.rms(left, 2))
            rms_r = int(aop.rms(right, 2))
            if rms_l > rms_r * 4 and rms_l > 40:
                return left
            if rms_r > rms_l * 4 and rms_r > 40:
                return right
            return aop.tomono(pcm, 2, 0.5, 0.5)
        except Exception:
            pass
    if aop is not None:
        try:
            return aop.tomono(pcm, 2, 0.5, 0.5)
        except Exception:
            pass
    sa = array.array("h")
    sa.frombytes(pcm)
    if channels == 2:
        ls = [sa[i] for i in range(0, len(sa) - 1, 2)]
        rs = [sa[i + 1] for i in range(0, len(sa) - 1, 2)]
        rms_l = sum(abs(x) for x in ls)
        rms_r = sum(abs(x) for x in rs)
        if rms_l > rms_r * 4 and rms_l > 40 * max(1, len(ls)):
            return array.array("h", ls).tobytes()
        if rms_r > rms_l * 4 and rms_r > 40 * max(1, len(rs)):
            return array.array("h", rs).tobytes()
    out = array.array("h", ((sa[i] + sa[i + 1]) // 2 for i in range(0, len(sa) - 1, channels)))
    return out.tobytes()


def is_dave_encrypted_frame(payload: bytes) -> bool:
    """Detecta un frame cifrado con DAVE E2EE por su marcador final 0xFAFA y tamaño suplementario."""
    if len(payload) < 11 or payload[-2:] != b"\xfa\xfa":
        return False
    supp_size = payload[-3]
    return 11 <= supp_size <= len(payload)


def decrypt_discord_rtp_packet(
    data: bytes,
    mode: str,
    secret_key: bytes,
    ssrc_to_user: dict[int, int],
    dave_session: Any | None = None,
    dave_active: bool = False,
    aead_box: Any | None = None,
    dave_media_type: Any | None = None,
) -> tuple[int, int | None, bytes] | None:
    """Descifra un paquete RTP UDP entrante de Discord en el orden exacto DAVE.

    Secuencia requerida por el protocolo:
      RTP Parse -> Outer Transport Decrypt -> Strip Extension -> Strip Padding ->
      (Bypass OPUS_SILENCE) -> DAVE Decrypt -> Opus Frame.
    """
    if len(data) < 16:
        return None

    b0 = data[0]
    b1 = data[1]

    # Versión RTP debe ser 2
    if ((b0 >> 6) & 0x03) != 2:
        return None
    # Ignorar paquetes RTCP (PT 192..223) y cualquier payload no-Opus (PT 120 / 0x78)
    if 192 <= b1 <= 223 or (b1 & 0x7F) != 120:
        return None

    has_padding = bool(b0 & 0x20)
    has_ext = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F
    ssrc = struct.unpack_from(">I", data, 8)[0]
    base_len = 12 + 4 * csrc_count

    try:
        if mode == "aead_xchacha20_poly1305_rtpsize":
            aad_len = base_len + (4 if has_ext else 0)
            if len(data) <= aad_len + 20:
                return None
            ext_words = struct.unpack_from(">H", data, base_len + 2)[0] if has_ext else 0
            aad = data[:aad_len]
            ciphertext = data[aad_len:-4]
            nonce = data[-4:] + (b"\x00" * 20)
            if aead_box is None:
                import nacl.secret

                aead_box = nacl.secret.Aead(bytes(secret_key))
            plaintext = aead_box.decrypt(ciphertext, aad, nonce)
            if has_ext:
                ext_bytes = ext_words * 4
                if len(plaintext) < ext_bytes:
                    return None
                payload = plaintext[ext_bytes:]
            else:
                payload = plaintext
        elif mode == "xsalsa20_poly1305_lite":
            if len(data) <= base_len + 20:
                return None
            import nacl.secret

            box = nacl.secret.SecretBox(bytes(secret_key))
            nonce = data[-4:] + (b"\x00" * 20)
            plaintext = box.decrypt(data[base_len:-4], nonce)
            if has_ext:
                if len(plaintext) < 4:
                    return None
                ext_words = struct.unpack_from(">H", plaintext, 2)[0]
                ext_total = 4 + ext_words * 4
                if len(plaintext) < ext_total:
                    return None
                payload = plaintext[ext_total:]
            else:
                payload = plaintext
        elif mode == "xsalsa20_poly1305_suffix":
            if len(data) <= base_len + 40:
                return None
            import nacl.secret

            box = nacl.secret.SecretBox(bytes(secret_key))
            nonce = data[-24:]
            plaintext = box.decrypt(data[base_len:-24], nonce)
            if has_ext:
                if len(plaintext) < 4:
                    return None
                ext_words = struct.unpack_from(">H", plaintext, 2)[0]
                ext_total = 4 + ext_words * 4
                if len(plaintext) < ext_total:
                    return None
                payload = plaintext[ext_total:]
            else:
                payload = plaintext
        elif mode == "xsalsa20_poly1305":
            if len(data) <= base_len + 16:
                return None
            import nacl.secret

            box = nacl.secret.SecretBox(bytes(secret_key))
            nonce = data[:12] + (b"\x00" * 12)
            plaintext = box.decrypt(data[base_len:], nonce)
            if has_ext:
                if len(plaintext) < 4:
                    return None
                ext_words = struct.unpack_from(">H", plaintext, 2)[0]
                ext_total = 4 + ext_words * 4
                if len(plaintext) < ext_total:
                    return None
                payload = plaintext[ext_total:]
            else:
                payload = plaintext
        else:
            return None
    except Exception:
        return None

    # Paso 4: quitar padding RTP ANTES de DAVE Decrypt
    if has_padding and len(payload) > 0:
        pad_len = payload[-1]
        if 0 < pad_len <= len(payload):
            payload = payload[:-pad_len]
        else:
            return None

    if not payload:
        return None

    user_id = ssrc_to_user.get(ssrc)

    # Paso 5: el frame explícito de silencio Opus no va cifrado con DAVE
    if payload == OPUS_SILENCE:
        return ssrc, user_id, OPUS_SILENCE

    # Paso 6: descifrado DAVE (E2EE MLS)
    if dave_active and dave_session is not None and getattr(dave_session, "ready", False):
        if user_id is None:
            return None
        if dave_media_type is None:
            import davey

            dave_media_type = davey.MediaType.audio
        try:
            payload = dave_session.decrypt(user_id, dave_media_type, payload)
        except Exception:
            if is_dave_encrypted_frame(payload):
                return None
            try:
                can_pt = getattr(dave_session, "can_passthrough", None)
                if callable(can_pt) and can_pt(user_id):
                    pass
                else:
                    return None
            except Exception:
                return None

    # Nunca pasar ciphertext DAVE (0xFAFA) al decodificador Opus aunque DAVE aún no esté listo
    if is_dave_encrypted_frame(payload):
        return None

    if not payload:
        return None

    return ssrc, user_id, payload


def _seq_diff(a: int, b: int) -> int:
    """Diferencia con signo entre secuencias RTP de 16 bits."""
    return ((int(a) - int(b) + 0x8000) & 0xFFFF) - 0x8000


class UserJitterBuffer:
    """Jitter buffer adaptativo por usuario ordenado por secuencia RTP + PLC/FEC Opus."""

    def __init__(
        self,
        decoder_factory: Callable[[], Any] | None = None,
        prefill_frames: int = 2,
        max_packets: int = 12,
    ) -> None:
        self._lock = threading.Lock()
        self._decoder_factory = decoder_factory
        self._decoder: Any = None
        self._base_prefill = max(1, int(prefill_frames))
        self._prefill = self._base_prefill
        self._max_packets = max(6, int(max_packets))
        self._packets: dict[int, bytes] = {}
        self._priming = True
        self._next_seq: int | None = None
        self._first_pkt_ts = 0.0
        self._consecutive_missing = 0
        self._consecutive_blasts = 0
        self._excess_ticks = 0
        self._jitter_ms = 0.0
        self._adapt_boost = 0.0
        self._last_pkt_ts = 0.0
        self._last_pkt_seq: int | None = None
        self._end_of_speech = False
        self.last_rx_ts = time.monotonic()
        self._pcm_overflow = bytearray()

    def _effective_target_locked(self) -> int:
        jitter_target = self._base_prefill + int(self._jitter_ms // 22.0)
        boost_target = self._base_prefill + int(self._adapt_boost)
        return max(self._base_prefill, min(6, max(jitter_target, boost_target)))

    def _ensure_decoder(self) -> Any:
        if self._decoder is None:
            if self._decoder_factory is not None:
                self._decoder = self._decoder_factory()
            else:
                import discord.opus

                self._decoder = discord.opus.Decoder()
        return self._decoder

    def push(self, seq: int, opus_bytes: bytes, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else float(now)
        seq_u16 = int(seq) & 0xFFFF
        with self._lock:
            self.last_rx_ts = ts
            if not opus_bytes:
                return
            if opus_bytes == OPUS_SILENCE:
                self._end_of_speech = True
                if not self._packets and not self._pcm_overflow:
                    self._priming = True
                    self._next_seq = None
                    self._consecutive_missing = 0
                    self._excess_ticks = 0
                return

            self._end_of_speech = False
            if self._last_pkt_seq is not None:
                seq_delta = _seq_diff(seq_u16, self._last_pkt_seq)
                if 0 < seq_delta <= 8:
                    actual_dt_ms = max(0.0, (ts - self._last_pkt_ts) * 1000.0)
                    transit_err = abs(actual_dt_ms - seq_delta * 20.0)
                    self._jitter_ms += (min(transit_err, 250.0) - self._jitter_ms) / 16.0
            self._last_pkt_ts = ts
            self._last_pkt_seq = seq_u16

            if not self._priming and self._next_seq is not None:
                diff = _seq_diff(seq_u16, self._next_seq)
                if abs(diff) > 200:
                    self._packets.clear()
                    self._pcm_overflow.clear()
                    self._priming = True
                    self._next_seq = None
                    self._consecutive_missing = 0
                    self._excess_ticks = 0
                    self._first_pkt_ts = ts
                elif diff < 0:
                    if diff >= -10:
                        self._adapt_boost = min(4.0, self._adapt_boost + 0.85)
                    return
            if self._priming and not self._packets:
                self._first_pkt_ts = ts
            self._packets[seq_u16] = opus_bytes
            while len(self._packets) > self._max_packets:
                anchor = next(iter(self._packets))
                oldest = min(self._packets.keys(), key=lambda s: _seq_diff(s, anchor))
                self._packets.pop(oldest, None)
                if self._next_seq == oldest:
                    self._next_seq = (oldest + 1) & 0xFFFF

    def pop_pcm(self, now: float | None = None) -> bytes | None:
        cur = time.monotonic() if now is None else float(now)
        with self._lock:
            if len(self._pcm_overflow) >= PCM_FRAME_BYTES:
                out = bytes(self._pcm_overflow[:PCM_FRAME_BYTES])
                del self._pcm_overflow[:PCM_FRAME_BYTES]
                return out

            eff_target = self._effective_target_locked()

            if self._priming:
                if not self._packets:
                    return None
                waited = cur - self._first_pkt_ts
                wait_cap = min(0.12, 0.028 + 0.018 * (eff_target - self._base_prefill))
                if len(self._packets) < eff_target and waited < wait_cap:
                    return None
                anchor = next(iter(self._packets))
                self._next_seq = min(self._packets.keys(), key=lambda s: _seq_diff(s, anchor))
                self._priming = False
                self._consecutive_missing = 0
                self._excess_ticks = 0

            if self._next_seq is None:
                self._priming = True
                return None

            # Limitar latencia máxima según el nivel de jitter actual del usuario
            hard_cap = max(5, eff_target + 3)
            while len(self._packets) > hard_cap:
                anchor = next(iter(self._packets))
                oldest = min(self._packets.keys(), key=lambda s: _seq_diff(s, anchor))
                self._packets.pop(oldest, None)
                self._excess_ticks = 0
                if self._next_seq is None or _seq_diff(oldest, self._next_seq) >= 0:
                    self._next_seq = (oldest + 1) & 0xFFFF

            if len(self._packets) >= (eff_target + 2):
                self._excess_ticks += 1
                if self._excess_ticks >= 18:
                    self._excess_ticks = 0
                    anchor = next(iter(self._packets))
                    oldest = min(self._packets.keys(), key=lambda s: _seq_diff(s, anchor))
                    self._packets.pop(oldest, None)
                    if self._next_seq is None or _seq_diff(oldest, self._next_seq) >= 0:
                        self._next_seq = (oldest + 1) & 0xFFFF
            else:
                self._excess_ticks = 0

            dec = self._ensure_decoder()
            pcm: bytes | None = None
            pkt = self._packets.pop(self._next_seq, None)
            if pkt is not None:
                self._next_seq = (self._next_seq + 1) & 0xFFFF
                self._consecutive_missing = 0
                self._adapt_boost = max(0.0, self._adapt_boost - 0.008)
                try:
                    pcm = dec.decode(pkt, fec=False)
                except Exception:
                    pcm = None
            else:
                if self._end_of_speech and not self._packets:
                    self._priming = True
                    self._next_seq = None
                    self._consecutive_missing = 0
                    self._excess_ticks = 0
                    return None

                if not self._end_of_speech:
                    self._adapt_boost = min(4.0, self._adapt_boost + 0.60)

                next2 = (self._next_seq + 1) & 0xFFFF
                pkt2 = self._packets.get(next2)
                if pkt2 is not None:
                    try:
                        pcm = dec.decode(pkt2, fec=True)
                    except Exception:
                        try:
                            pcm = dec.decode(None, fec=False)
                        except Exception:
                            pcm = None
                    self._next_seq = next2
                    self._consecutive_missing += 1
                else:
                    future = [
                        s for s in self._packets if _seq_diff(s, self._next_seq) > 0
                    ]
                    if future and self._consecutive_missing >= 2:
                        jump_seq = min(future, key=lambda s: _seq_diff(s, self._next_seq))
                        jump_pkt = self._packets.pop(jump_seq, None)
                        self._next_seq = (jump_seq + 1) & 0xFFFF
                        self._consecutive_missing = 0
                        if jump_pkt is not None:
                            try:
                                pcm = dec.decode(jump_pkt, fec=False)
                            except Exception:
                                pcm = None
                    elif self._consecutive_missing < 3:
                        try:
                            pcm = dec.decode(None, fec=False)
                        except Exception:
                            pcm = None
                        self._next_seq = (self._next_seq + 1) & 0xFFFF
                        self._consecutive_missing += 1
                        if pcm:
                            if self._consecutive_missing == 2:
                                pcm = _pcm_scale(pcm, 0.72)
                            elif self._consecutive_missing >= 3:
                                pcm = _pcm_scale(pcm, 0.40)
                    else:
                        self._priming = True
                        self._next_seq = None
                        self._consecutive_missing = 0
                        self._consecutive_blasts = 0
                        self._excess_ticks = 0
                        self._pcm_overflow.clear()
                        return None

            if not pcm:
                return SILENCE_PCM
            # Guardia anti-explosión digital (ciphertext DAVE / Opus corrupto a escala completa)
            if _pcm_rms(pcm) >= 18500 and _pcm_max_abs(pcm) >= 32500:
                self._consecutive_blasts += 1
                if self._consecutive_blasts >= 2:
                    self._decoder = None
                return SILENCE_PCM
            self._consecutive_blasts = 0
            if len(pcm) == PCM_FRAME_BYTES:
                return pcm
            if len(pcm) > PCM_FRAME_BYTES:
                self._pcm_overflow.extend(pcm[PCM_FRAME_BYTES:])
                return pcm[:PCM_FRAME_BYTES]
            return pcm + (b"\x00" * (PCM_FRAME_BYTES - len(pcm)))

    def clear(self) -> None:
        with self._lock:
            self._packets.clear()
            self._pcm_overflow.clear()
            self._priming = True
            self._next_seq = None
            self._consecutive_missing = 0
            self._consecutive_blasts = 0
            self._excess_ticks = 0
            self._jitter_ms = 0.0
            self._adapt_boost = 0.0
            self._last_pkt_ts = 0.0
            self._last_pkt_seq = None
            self._end_of_speech = False

    def close(self) -> None:
        with self._lock:
            self._packets.clear()
            self._pcm_overflow.clear()
            self._decoder = None
            self._priming = True
            self._next_seq = None
            self._consecutive_blasts = 0
            self._excess_ticks = 0
            self._jitter_ms = 0.0
            self._adapt_boost = 0.0
            self._last_pkt_ts = 0.0
            self._last_pkt_seq = None
            self._end_of_speech = False


class VoiceAudioMixer:
    """Mezclador PCM estéreo 48 kHz en tiempo real con jitter buffer por participante."""

    def __init__(self, max_queue_frames: int = 8) -> None:
        self._max_frames = max(2, int(max_queue_frames))
        self._lock = threading.Lock()
        self._queues: dict[int, deque[bytes]] = {}
        self._jitter: dict[int, UserJitterBuffer] = {}
        self._user_volumes: dict[int, float] = {}
        self._master_volume: float = 1.0
        self._deafened: bool = False

    def set_deafened(self, deafened: bool) -> None:
        with self._lock:
            self._deafened = bool(deafened)
            if self._deafened:
                self._queues.clear()
                jbufs = list(self._jitter.values())
            else:
                jbufs = []
        for jb in jbufs:
            jb.clear()

    def set_master_volume(self, vol_pct: int) -> None:
        pct = max(0, min(200, int(vol_pct)))
        with self._lock:
            self._master_volume = pct / 100.0

    def set_user_volume(self, user_id: int, vol_pct: int) -> None:
        pct = max(0, min(200, int(vol_pct)))
        with self._lock:
            self._user_volumes[int(user_id)] = pct / 100.0

    def remove_user(self, user_id: int) -> None:
        uid = int(user_id)
        with self._lock:
            self._queues.pop(uid, None)
            jb = self._jitter.pop(uid, None)
        if jb is not None:
            jb.close()

    def clear(self) -> None:
        with self._lock:
            self._queues.clear()
            jbufs = list(self._jitter.values())
            self._jitter.clear()
        for jb in jbufs:
            jb.close()

    def push_opus(
        self,
        user_id: int,
        seq: int,
        opus_bytes: bytes,
        decoder_factory: Callable[[], Any] | None = None,
        now: float | None = None,
    ) -> None:
        if not opus_bytes:
            return
        uid = int(user_id)
        with self._lock:
            if self._deafened:
                return
            jb = self._jitter.get(uid)
            if jb is None:
                jb = UserJitterBuffer(decoder_factory=decoder_factory)
                self._jitter[uid] = jb
        jb.push(seq, opus_bytes, now=now)

    def push_pcm(self, user_id: int, pcm: bytes) -> None:
        if not pcm:
            return
        uid = int(user_id)
        with self._lock:
            if self._deafened:
                return
            q = self._queues.get(uid)
            if q is None:
                q = deque(maxlen=self._max_frames)
                self._queues[uid] = q
            if len(pcm) == PCM_FRAME_BYTES:
                q.append(pcm)
                return
            offset = 0
            n = len(pcm)
            while offset < n:
                chunk = pcm[offset : offset + PCM_FRAME_BYTES]
                if len(chunk) < PCM_FRAME_BYTES:
                    chunk = chunk + (b"\x00" * (PCM_FRAME_BYTES - len(chunk)))
                q.append(chunk)
                offset += PCM_FRAME_BYTES

    def mix_frame(self, now: float | None = None) -> bytes:
        """Extrae y mezcla un frame de 20 ms (3840 bytes) de todos los usuarios activos."""
        cur = time.monotonic() if now is None else float(now)
        frames: list[tuple[bytes, float]] = []
        jitter_snap: list[tuple[UserJitterBuffer, float]] = []
        stale_jbufs: list[UserJitterBuffer] = []

        with self._lock:
            if self._deafened:
                return SILENCE_PCM
            master = self._master_volume
            empty_uids: list[int] = []
            for uid, q in self._queues.items():
                if q:
                    vol = self._user_volumes.get(uid, 1.0)
                    frames.append((q.popleft(), vol))
                else:
                    empty_uids.append(uid)
            for uid in empty_uids:
                del self._queues[uid]

            stale_uids: list[int] = []
            for uid, jb in self._jitter.items():
                if (cur - jb.last_rx_ts) > 15.0:
                    stale_uids.append(uid)
                else:
                    jitter_snap.append((jb, self._user_volumes.get(uid, 1.0)))
            for uid in stale_uids:
                stale_jbufs.append(self._jitter.pop(uid))

        for jb in stale_jbufs:
            jb.close()

        for jb, vol in jitter_snap:
            pcm = jb.pop_pcm(cur)
            if pcm is not None:
                frames.append((pcm, vol))

        if not frames or master <= 0.0:
            return SILENCE_PCM

        mixed: bytes | None = None
        for raw, vol in frames:
            if vol <= 0.0:
                continue
            scaled = _pcm_scale(raw, vol)
            if mixed is None:
                mixed = scaled
            else:
                mixed = _pcm_add(mixed, scaled)

        if mixed is None:
            return SILENCE_PCM

        if abs(master - 1.0) >= 0.01:
            mixed = _pcm_scale(mixed, master)
        return mixed


def list_audio_devices() -> dict[str, list[dict[str, Any]]]:
    """Lista dispositivos de micrófono y salida deduplicando múltiples HostAPI en Windows."""
    import sounddevice as sd

    inputs: list[dict[str, Any]] = [
        {
            "id": -1,
            "name": "",
            "label": "Predeterminado del sistema",
            "is_default": True,
        }
    ]
    outputs: list[dict[str, Any]] = [
        {
            "id": -1,
            "name": "",
            "label": "Predeterminado del sistema",
            "is_default": True,
        }
    ]

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception:
        return {"inputs": inputs, "outputs": outputs}

    def _host_rank(host_name: str) -> int:
        hn = host_name.lower()
        if "wasapi" in hn:
            return 0
        if "mme" in hn:
            return 1
        if "directsound" in hn:
            return 2
        if "wdm" in hn:
            return 4
        return 3

    best_in: dict[str, tuple[int, dict[str, Any]]] = {}
    best_out: dict[str, tuple[int, dict[str, Any]]] = {}

    for idx, dev in enumerate(devices):
        name = str(dev.get("name") or "").strip()
        if not name:
            continue
        api_idx = int(dev.get("hostapi") or 0)
        api_name = (
            str(hostapis[api_idx].get("name") or "")
            if 0 <= api_idx < len(hostapis)
            else ""
        )
        rank = _host_rank(api_name)
        key = name.lower()
        sr = int(dev.get("default_samplerate") or SAMPLE_RATE)

        max_in = int(dev.get("max_input_channels") or 0)
        if max_in > 0:
            item = {
                "id": idx,
                "name": name,
                "label": name,
                "hostapi": api_name,
                "channels": max_in,
                "default_samplerate": sr,
                "is_default": idx == default_in,
            }
            prev = best_in.get(key)
            if prev is None or rank < prev[0]:
                best_in[key] = (rank, item)

        max_out = int(dev.get("max_output_channels") or 0)
        if max_out > 0:
            item = {
                "id": idx,
                "name": name,
                "label": name,
                "hostapi": api_name,
                "channels": max_out,
                "default_samplerate": sr,
                "is_default": idx == default_out,
            }
            prev = best_out.get(key)
            if prev is None or rank < prev[0]:
                best_out[key] = (rank, item)

    inputs.extend(
        sorted((v[1] for v in best_in.values()), key=lambda d: d["name"].lower())
    )
    outputs.extend(
        sorted((v[1] for v in best_out.values()), key=lambda d: d["name"].lower())
    )
    return {"inputs": inputs, "outputs": outputs}


def _hostapi_rank(hostapi_name: str) -> int:
    """Orden de prioridad de HostAPI Windows: WASAPI > MME > DirectSound > otros > WDM-KS."""
    low = (hostapi_name or "").lower()
    if "wasapi" in low:
        return 0
    if "mme" in low:
        return 1
    if "directsound" in low:
        return 2
    if "wdm-ks" in low:
        return 4
    return 3


def resolve_device_candidates(
    device_name: str, kind: Literal["input", "output"]
) -> list[int | None]:
    """Devuelve índices PortAudio candidatos por prioridad de HostAPI, terminando en None."""
    target = (device_name or "").strip().lower()
    if not target:
        try:
            import sounddevice as sd

            apis = sd.query_hostapis()
            ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
            def_key = "default_input_device" if kind == "input" else "default_output_device"
            for api in apis:
                if "wasapi" in str(api.get("name") or "").lower():
                    raw_idx = api.get(def_key, -1)
                    if raw_idx is not None and int(raw_idx) >= 0:
                        idx = int(raw_idx)
                        info = sd.query_devices(idx)
                        if isinstance(info, dict) and int(info.get(ch_key) or 0) > 0:
                            return [idx, None]
        except Exception:
            pass
        return [None]

    seen: set[int | None] = set()
    ordered: list[int | None] = []

    try:
        import sounddevice as sd

        devs = sd.query_devices()
        apis = sd.query_hostapis()
        ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
        scored: list[tuple[int, int, int]] = []
        prefix = target[:18]

        for idx, dev in enumerate(devs):
            if int(dev.get(ch_key) or 0) <= 0:
                continue
            name_l = str(dev.get("name") or "").strip().lower()
            if not name_l:
                continue
            if name_l == target:
                tier = 0
            elif name_l.startswith(prefix) or (len(name_l) >= 18 and target.startswith(name_l)):
                tier = 1
            else:
                continue
            api_idx = int(dev.get("hostapi") or 0)
            api_name = ""
            if 0 <= api_idx < len(apis):
                api_name = str(apis[api_idx].get("name") or "")
            scored.append((tier, _hostapi_rank(api_name), idx))

        scored.sort()
        non_wdm = [item for item in scored if item[1] < 4]
        chosen = non_wdm if non_wdm else scored
        for _, _, idx in chosen:
            if idx not in seen:
                seen.add(idx)
                ordered.append(idx)
    except Exception:
        pass

    if not ordered:
        try:
            listing = list_audio_devices()
            items = listing["inputs"] if kind == "input" else listing["outputs"]
            for it in items:
                dev_id = int(it.get("id", -1))
                name_l = str(it.get("name") or "").strip().lower()
                if dev_id >= 0 and name_l == target and dev_id not in seen:
                    seen.add(dev_id)
                    ordered.append(dev_id)
            for it in items:
                dev_id = int(it.get("id", -1))
                name_l = str(it.get("name") or "").strip().lower()
                if dev_id >= 0 and name_l and name_l.startswith(target[:18]) and dev_id not in seen:
                    seen.add(dev_id)
                    ordered.append(dev_id)
        except Exception:
            pass

    ordered.append(None)
    return ordered


def resolve_device_index(
    device_name: str, kind: Literal["input", "output"]
) -> int | None:
    """Resuelve un nombre persistido al índice PortAudio principal (o None = predeterminado)."""
    cands = resolve_device_candidates(device_name, kind)
    return cands[0] if cands else None


class AudioEngine:
    """Captura micrófono y reproduce mezcla estéreo usando sounddevice (PortAudio)."""

    def __init__(
        self,
        mixer: VoiceAudioMixer,
        on_send_pcm: Callable[[bytes], None],
        on_speaking_state: Callable[[bool], None],
        on_mic_level: Callable[[int, bool], None],
        on_warning: Callable[[str], None],
    ) -> None:
        self._mixer = mixer
        self._on_send_pcm = on_send_pcm
        self._on_speaking_state = on_speaking_state
        self._on_mic_level = on_mic_level
        self._on_warning = on_warning

        self._lock = threading.Lock()
        self._running = False
        self._input_stream: Any = None
        self._output_stream: Any = None
        self._in_sr = SAMPLE_RATE
        self._in_channels = 1
        self._out_sr = SAMPLE_RATE
        self._out_channels = CHANNELS
        self._out_rate_state: Any = None
        self._out_buf = bytearray()
        self._in_rate_state: Any = None
        self._in_mono_buf = bytearray()

        self._mic_queue: queue.Queue[bytes] = queue.Queue(maxsize=4)
        self._mic_thread: threading.Thread | None = None

        self._muted = False
        self._loopback = False
        self._loopback_lock = threading.Lock()
        self._loopback_queue: deque[bytes] = deque(maxlen=12)
        self._mic_proc = MicProcessor(noise_suppression="ai", enable_webrtc=True)
        self._local_speaking = False
        self._last_vu_emit = 0.0

        self._input_device_name = ""
        self._output_device_name = ""
        self._started_at = 0.0
        self._in_opened_at = 0.0
        self._out_opened_at = 0.0
        self._in_cb_count = 0
        self._out_cb_count = 0
        self._last_in_cb_ts = 0.0
        self._last_out_cb_ts = 0.0
        self._in_candidate_skip = 0
        self._out_candidate_skip = 0
        self._in_recoveries = 0
        self._out_recoveries = 0
        self._warmup_lock = threading.Lock()
        self._warmup_started = False

    def configure(
        self,
        *,
        muted: bool | None = None,
        loopback: bool | None = None,
        mic_gain_pct: int | None = None,
        agc_enabled: bool | None = None,
        vad_threshold: int | None = None,
        noise_suppression: str | None = None,
        voice_fx: dict[str, Any] | None = None,
        input_device_name: str | None = None,
        output_device_name: str | None = None,
    ) -> None:
        if loopback is not None:
            self._loopback = bool(loopback)
            if not self._loopback:
                with self._loopback_lock:
                    self._loopback_queue.clear()
        if muted is not None:
            self._muted = bool(muted)
            if self._muted and not self._loopback:
                self._mic_proc.reset()
                if self._local_speaking:
                    self._local_speaking = False
                    self._on_speaking_state(False)
                    self._on_mic_level(0, False)
        self._mic_proc.configure(
            mic_gain_pct=mic_gain_pct,
            agc_enabled=agc_enabled,
            vad_threshold=vad_threshold,
            noise_suppression=noise_suppression,
            voice_fx=voice_fx,
        )
        if noise_suppression is not None and self._running:
            self._mic_proc.ensure_warmup_async(trim_after=False)
        if input_device_name is not None:
            new_in = str(input_device_name or "").strip()
            if new_in != self._input_device_name:
                self._input_device_name = new_in
                self._in_candidate_skip = 0
                self._in_recoveries = 0
        if output_device_name is not None:
            new_out = str(output_device_name or "").strip()
            if new_out != self._output_device_name:
                self._output_device_name = new_out
                self._out_candidate_skip = 0
                self._out_recoveries = 0

    @property
    def is_speaking(self) -> bool:
        return bool(self._running and not self._muted and self._local_speaking)

    @property
    def is_open_mic(self) -> bool:
        return bool(self._mic_proc._vad_threshold <= 0)

    def warmup_async(self) -> None:
        """Precalienta DeepFilterNet3 y Silero VAD en segundo plano al entrar en voz."""
        self._mic_proc.ensure_warmup_async(trim_after=not self._running)

    def release_dsp_idle(self) -> None:
        """Libera buffers de DSP neuronal cuando no hay llamada de voz activa."""
        try:
            self._mic_proc.release_dsp()
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._started_at = time.monotonic()
            self._in_candidate_skip = 0
            self._out_candidate_skip = 0
            self._in_recoveries = 0
            self._out_recoveries = 0
            self._out_buf.clear()
            self._out_rate_state = None
            self._in_mono_buf.clear()
            self._in_rate_state = None
            self._mic_proc.reset()
            self._local_speaking = False
            with self._loopback_lock:
                self._loopback_queue.clear()
            while not self._mic_queue.empty():
                try:
                    self._mic_queue.get_nowait()
                except queue.Empty:
                    break

        try:
            from swiftplay.windows_os import set_realtime_audio_process_priority

            set_realtime_audio_process_priority(True)
        except Exception:
            pass
        self._open_output_stream()
        self._open_input_stream()
        self._mic_thread = threading.Thread(
            target=self._mic_sender_loop,
            daemon=True,
            name="discord-mic-sender",
        )
        self._mic_thread.start()

    def restart_streams(self) -> None:
        if not self._running:
            return
        self.stop()
        self.start()

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    @staticmethod
    def _safe_query_device(
        sd: Any, dev_idx: int | None, default_slot: int
    ) -> dict[str, Any] | None:
        try:
            if dev_idx is None:
                defs = getattr(getattr(sd, "default", None), "device", None)
                real_idx = (
                    defs[default_slot]
                    if isinstance(defs, (list, tuple)) and len(defs) > default_slot
                    else None
                )
                if real_idx is None or int(real_idx) < 0:
                    return None
                info = sd.query_devices(int(real_idx))
            else:
                info = sd.query_devices(dev_idx)
            return info if isinstance(info, dict) else None
        except Exception:
            return None

    def check_and_recover_streams(self, now: float | None = None) -> tuple[bool, bool]:
        """Reabre streams congelados al iniciar o bloqueados a mitad de llamada."""
        if not self._running:
            return False, False
        t = time.monotonic() if now is None else float(now)
        rec_in = False
        rec_out = False

        in_cold_stale = (t - self._in_opened_at) >= 1.2 and (
            self._input_stream is None
            or self._in_cb_count == 0
            or not getattr(self._input_stream, "active", True)
        )
        in_mid_stall = (
            self._in_cb_count > 0
            and self._last_in_cb_ts > 0.0
            and (t - self._last_in_cb_ts) >= 2.0
        )
        if in_mid_stall and (self._last_in_cb_ts - self._in_opened_at) >= 10.0:
            self._in_recoveries = 0
        if (in_cold_stale or in_mid_stall) and self._in_recoveries < 6:
            with self._lock:
                old_in = self._input_stream
                self._input_stream = None
            self._close_stream(old_in)
            if in_cold_stale:
                self._in_candidate_skip += 1
            self._in_recoveries += 1
            self._open_input_stream()
            rec_in = True

        out_cold_stale = (t - self._out_opened_at) >= 1.2 and (
            self._output_stream is None
            or self._out_cb_count == 0
            or not getattr(self._output_stream, "active", True)
        )
        out_mid_stall = (
            self._out_cb_count > 0
            and self._last_out_cb_ts > 0.0
            and (t - self._last_out_cb_ts) >= 1.5
        )
        if out_mid_stall and (self._last_out_cb_ts - self._out_opened_at) >= 10.0:
            self._out_recoveries = 0
        if (out_cold_stale or out_mid_stall) and self._out_recoveries < 6:
            with self._lock:
                old_out = self._output_stream
                self._output_stream = None
            self._close_stream(old_out)
            if out_cold_stale:
                self._out_candidate_skip += 1
            self._out_recoveries += 1
            self._open_output_stream()
            rec_out = True

        return rec_in, rec_out

    def stop(self) -> None:
        with self._lock:
            self._running = False
            in_s = self._input_stream
            out_s = self._output_stream
            self._input_stream = None
            self._output_stream = None

        if self._local_speaking:
            self._local_speaking = False
            try:
                self._on_speaking_state(False)
            except Exception:
                pass
        try:
            self._on_mic_level(0, False)
        except Exception:
            pass

        try:
            self._mic_queue.put_nowait(b"")
        except queue.Full:
            pass

        try:
            for s in (in_s, out_s):
                self._close_stream(s)

            if self._mic_thread is not None and self._mic_thread.is_alive():
                self._mic_thread.join(timeout=0.8)
            self._mic_thread = None
        finally:
            try:
                from swiftplay.windows_os import set_realtime_audio_process_priority

                set_realtime_audio_process_priority(False)
            except Exception:
                pass

    def close(self) -> None:
        self.stop()
        self._mic_proc.close()

    def _open_output_stream(self) -> None:
        import sounddevice as sd

        self._out_cb_count = 0
        self._last_out_cb_ts = 0.0
        self._out_opened_at = time.monotonic()
        dev_cands = resolve_device_candidates(self._output_device_name, "output")
        n_cands = len(dev_cands)
        skip = self._out_candidate_skip % n_cands if n_cands else 0
        rotated = dev_cands[skip:] + dev_cands[:skip] if n_cands else [None]

        last_err: Exception | None = None
        for offset, dev_idx in enumerate(rotated):
            dev_info = self._safe_query_device(sd, dev_idx, 1)
            if dev_idx is None and dev_info is None:
                continue
            def_sr = (
                int(dev_info.get("default_samplerate") or SAMPLE_RATE)
                if isinstance(dev_info, dict)
                else SAMPLE_RATE
            )
            max_ch = (
                int(dev_info.get("max_output_channels") or 2)
                if isinstance(dev_info, dict)
                else 2
            )
            if max_ch <= 0 and dev_info is not None:
                continue
            ch = 2 if max_ch >= 2 else 1

            rate_pairs: list[tuple[int, int]] = [(SAMPLE_RATE, ch)]
            if def_sr != SAMPLE_RATE:
                rate_pairs.append((def_sr, ch))
            for sr, nch in rate_pairs:
                for lat in (0.02, "low", 0.035):
                    try:
                        self._out_sr = sr
                        self._out_channels = nch
                        self._out_rate_state = None
                        self._out_buf.clear()
                        stream = sd.RawOutputStream(
                            samplerate=sr,
                            blocksize=0,
                            device=dev_idx,
                            channels=nch,
                            dtype="int16",
                            latency=lat,
                            callback=self._output_callback,
                        )
                        stream.start()
                        self._output_stream = stream
                        if offset > 0 and n_cands > 0:
                            self._out_candidate_skip = (skip + offset) % n_cands
                        self._out_opened_at = time.monotonic()
                        return
                    except Exception as exc:
                        last_err = exc

        if last_err is not None:
            self._on_warning(f"No se pudo abrir la salida de audio: {last_err}")
        else:
            self._on_warning("No se pudo abrir ninguna salida de audio disponible.")

    def _open_input_stream(self) -> None:
        import sounddevice as sd

        self._in_cb_count = 0
        self._last_in_cb_ts = 0.0
        self._in_opened_at = time.monotonic()
        dev_cands = resolve_device_candidates(self._input_device_name, "input")
        n_cands = len(dev_cands)
        skip = self._in_candidate_skip % n_cands if n_cands else 0
        rotated = dev_cands[skip:] + dev_cands[:skip] if n_cands else [None]

        last_err: Exception | None = None
        for offset, dev_idx in enumerate(rotated):
            dev_info = self._safe_query_device(sd, dev_idx, 0)
            if dev_info is None:
                continue
            max_ch = int(dev_info.get("max_input_channels") or 0)
            if max_ch <= 0:
                continue
            def_sr = int(dev_info.get("default_samplerate") or SAMPLE_RATE)
            pref_ch = 2 if max_ch >= 2 else 1

            rate_pairs: list[tuple[int, int]] = [(SAMPLE_RATE, pref_ch)]
            if def_sr != SAMPLE_RATE:
                rate_pairs.append((def_sr, pref_ch))
            if pref_ch == 2:
                rate_pairs.append((SAMPLE_RATE, 1))
                if def_sr != SAMPLE_RATE:
                    rate_pairs.append((def_sr, 1))

            for sr, nch in rate_pairs:
                for lat in (0.02, "low", 0.03):
                    try:
                        self._in_sr = sr
                        self._in_channels = nch
                        self._in_rate_state = None
                        self._in_mono_buf.clear()
                        stream = sd.RawInputStream(
                            samplerate=sr,
                            blocksize=0,
                            device=dev_idx,
                            channels=nch,
                            dtype="int16",
                            latency=lat,
                            callback=self._input_callback,
                        )
                        stream.start()
                        self._input_stream = stream
                        if offset > 0 and n_cands > 0:
                            self._in_candidate_skip = (skip + offset) % n_cands
                        self._in_opened_at = time.monotonic()
                        return
                    except Exception as exc:
                        last_err = exc

        if last_err is not None:
            self._on_warning(f"Sin micrófono disponible (modo solo escucha): {last_err}")
        else:
            self._on_warning("Sin micrófono predeterminado (modo solo escucha).")

    def _output_callback(self, outdata: Any, frames: int, _time_info: Any, _status: Any) -> None:
        needed = frames * self._out_channels * 2
        if not self._running:
            outdata[:] = b"\x00" * needed
            return
        self._out_cb_count += 1
        self._last_out_cb_ts = time.monotonic()
        try:
            aop = _get_audioop()
            while len(self._out_buf) < needed:
                if self._loopback:
                    with self._loopback_lock:
                        pcm_48k = (
                            self._loopback_queue.popleft()
                            if self._loopback_queue
                            else SILENCE_PCM
                        )
                    master_vol = getattr(self._mixer, "_master_volume", 1.0)
                    if pcm_48k != SILENCE_PCM and abs(master_vol - 1.0) > 0.01:
                        pcm_48k = _pcm_scale(pcm_48k, master_vol)
                else:
                    pcm_48k = self._mixer.mix_frame()
                chunk = pcm_48k
                if self._out_channels == 1:
                    chunk = _pcm_to_mono(chunk, 2)
                if self._out_sr != SAMPLE_RATE and aop is not None:
                    chunk, self._out_rate_state = aop.ratecv(
                        chunk, 2, self._out_channels, SAMPLE_RATE, self._out_sr, self._out_rate_state
                    )
                self._out_buf.extend(chunk)
            outdata[:] = bytes(self._out_buf[:needed])
            del self._out_buf[:needed]
        except Exception:
            outdata[:] = b"\x00" * needed

    def _input_callback(self, indata: Any, _frames: int, _time_info: Any, _status: Any) -> None:
        if not self._running:
            return
        self._in_cb_count += 1
        self._last_in_cb_ts = time.monotonic()
        if self._muted and not self._loopback:
            return
        raw = bytes(indata)
        if not raw:
            return
        try:
            self._mic_queue.put_nowait(raw)
        except queue.Full:
            try:
                self._mic_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._mic_queue.put_nowait(raw)
            except queue.Full:
                pass

    def _mic_sender_loop(self) -> None:
        aop = _get_audioop()
        mmcss_handle = 0
        try:
            from swiftplay.windows_os import (
                register_mmcss_audio_thread,
                revert_mmcss_audio_thread,
            )

            mmcss_handle = register_mmcss_audio_thread("Pro Audio", high_priority=True)
        except Exception:
            mmcss_handle = 0

        try:
            while self._running:
                try:
                    wait_s = 0.08 if self._local_speaking else 0.25
                    raw = self._mic_queue.get(timeout=wait_s)
                except queue.Empty:
                    now = time.monotonic()
                    if self._mic_proc.check_hangover(now) and self._local_speaking:
                        self._local_speaking = False
                        self._on_speaking_state(False)
                        self._on_mic_level(0, False)
                    continue

                if not self._running:
                    break
                # Si el sistema sufrió un micro-cuelgue de CPU/disco, descartar backlog viejo de captura
                if self._mic_queue.qsize() >= 3:
                    latest = raw
                    while True:
                        try:
                            nxt = self._mic_queue.get_nowait()
                        except queue.Empty:
                            break
                        if not nxt:
                            latest = nxt
                            break
                        latest = nxt
                    raw = latest
                    max_in_backlog = MONO_FRAME_BYTES * 2
                    if len(self._in_mono_buf) > max_in_backlog:
                        del self._in_mono_buf[:-MONO_FRAME_BYTES]

                if not raw or (self._muted and not self._loopback):
                    continue

                mono = _pcm_to_mono(raw, self._in_channels) if self._in_channels > 1 else raw
                if self._in_sr != SAMPLE_RATE and aop is not None:
                    try:
                        mono, self._in_rate_state = aop.ratecv(
                            mono, 2, 1, self._in_sr, SAMPLE_RATE, self._in_rate_state
                        )
                    except Exception:
                        continue

                self._in_mono_buf.extend(mono)
                while len(self._in_mono_buf) >= MONO_FRAME_BYTES and self._running:
                    frame = bytes(self._in_mono_buf[:MONO_FRAME_BYTES])
                    del self._in_mono_buf[:MONO_FRAME_BYTES]

                    if self._muted and not self._loopback:
                        continue

                    now = time.monotonic()
                    tx_frames, level, should_tx = self._mic_proc.process_frame(frame, now)

                    if (now - self._last_vu_emit) >= 0.09:
                        self._last_vu_emit = now
                        try:
                            self._on_mic_level(level, should_tx)
                        except Exception:
                            pass

                    if self._loopback:
                        if self._local_speaking:
                            self._local_speaking = False
                            self._on_speaking_state(False)
                        if should_tx:
                            with self._loopback_lock:
                                for mono_out in tx_frames:
                                    self._loopback_queue.append(_pcm_mono_to_stereo(mono_out))
                    elif should_tx:
                        if not self._local_speaking:
                            self._local_speaking = True
                            self._on_speaking_state(True)
                        for mono_out in tx_frames:
                            self._on_send_pcm(_pcm_mono_to_stereo(mono_out))
                    elif self._local_speaking:
                        self._local_speaking = False
                        self._on_speaking_state(False)
                        self._on_mic_level(level, False)
        finally:
            if mmcss_handle:
                try:
                    from swiftplay.windows_os import revert_mmcss_audio_thread

                    revert_mmcss_audio_thread(mmcss_handle)
                except Exception:
                    pass


def _make_voice_client_cls(on_init: Callable[[Any], None] | None = None):
    """Construye dinámicamente SwiftVoiceClient sin importar discord al nivel de módulo."""
    import davey
    import discord
    from discord.errors import ConnectionClosed
    from discord.voice_state import ConnectionFlowState, VoiceConnectionState

    class _SwiftVoiceConnectionState(VoiceConnectionState):
        """VoiceConnectionState con IP Discovery sin carrera de socket y tiempos por fase."""

        def __init__(self, voice_client: discord.VoiceClient, *, hook: Any = None) -> None:
            super().__init__(voice_client, hook=hook)
            self.last_close_code: int = 0
            self.last_close_reason: str = ""
            self.connect_timings: dict[str, Any] = {
                "t0": time.perf_counter(),
                "op4_ms": 0,
                "ws_ms": 0,
                "ip_ms": 0,
                "key_ms": 0,
                "attempts": 0,
                "close_codes": [],
            }

        async def _inner_connect(
            self, reconnect: bool, self_deaf: bool, self_mute: bool, resume: bool
        ) -> None:
            t0 = time.perf_counter()
            self.connect_timings = {
                "t0": t0,
                "op4_ms": 0,
                "ws_ms": 0,
                "ip_ms": 0,
                "key_ms": 0,
                "attempts": 0,
                "close_codes": [],
            }
            for i in range(3):
                self.connect_timings["attempts"] = i + 1
                await self._voice_connect(self_deaf=self_deaf, self_mute=self_mute)
                if self.state is ConnectionFlowState.disconnected:
                    self.state = ConnectionFlowState.set_guild_voice_state

                await self._wait_for_state(ConnectionFlowState.got_both_voice_updates)
                self.connect_timings["op4_ms"] = max(0, int((time.perf_counter() - t0) * 1000))

                try:
                    self.ws = await self._connect_websocket(resume)
                    self.connect_timings["ws_ms"] = max(0, int((time.perf_counter() - t0) * 1000))
                    await self._handshake_websocket()
                    break
                except ConnectionClosed as exc:
                    codes = self.connect_timings["close_codes"]
                    codes.append(int(getattr(exc, "code", 0) or 0))
                    if reconnect and i < 2:
                        wait = 0.4 + i * 0.4
                        try:
                            await asyncio.wait_for(self.disconnect(cleanup=False), timeout=1.5)
                        except Exception:
                            pass
                        await asyncio.sleep(wait)
                        continue
                    try:
                        await asyncio.wait_for(self.disconnect(), timeout=1.5)
                    except Exception:
                        pass
                    raise

        async def _connect_websocket(self, resume: bool) -> Any:
            ws = await super()._connect_websocket(resume)
            state = self

            async def _fast_discover_ip() -> tuple[str, int]:
                packet = bytearray(74)
                struct.pack_into(">H", packet, 0, 1)
                struct.pack_into(">H", packet, 2, 70)
                struct.pack_into(">I", packet, 4, state.ssrc)

                fut: asyncio.Future[bytes] = state.voice_client.loop.create_future()

                def _on_ip_pkt(data: bytes) -> None:
                    if len(data) == 74 and data[1] == 0x02 and not fut.done():
                        state.voice_client.loop.call_soon_threadsafe(
                            lambda d=data: fut.set_result(d) if not fut.done() else None
                        )

                state.add_socket_listener(_on_ip_pkt)
                try:
                    sr = getattr(state, "_socket_reader", None)
                    if sr is not None:
                        sr.resume(force=True)
                except Exception:
                    pass
                try:
                    for attempt in range(6):
                        try:
                            await ws.loop.sock_sendall(state.socket, packet)
                        except Exception:
                            pass
                        try:
                            recv = await asyncio.wait_for(asyncio.shield(fut), timeout=0.35)
                            break
                        except asyncio.TimeoutError:
                            if attempt >= 5:
                                recv = await fut
                            continue
                finally:
                    state.remove_socket_listener(_on_ip_pkt)

                ip_start = 8
                ip_end = recv.index(0, ip_start)
                ip = recv[ip_start:ip_end].decode("ascii")
                port = struct.unpack_from(">H", recv, len(recv) - 2)[0]
                return ip, port

            ws.discover_ip = _fast_discover_ip  # type: ignore[method-assign]
            orig_poll = ws.poll_event

            async def _tracked_poll_event() -> Any:
                try:
                    return await orig_poll()
                except ConnectionClosed as exc:
                    state.last_close_code = int(getattr(exc, "code", 0) or 0)
                    state.last_close_reason = str(getattr(exc, "reason", "") or "")
                    raise

            ws.poll_event = _tracked_poll_event  # type: ignore[method-assign]
            return ws

        async def _handshake_websocket(self) -> None:
            t0 = float(self.connect_timings.get("t0") or time.perf_counter())
            while not self.ip:
                await self.ws.poll_event()
            self.connect_timings["ip_ms"] = max(0, int((time.perf_counter() - t0) * 1000))
            self.state = ConnectionFlowState.got_ip_discovery
            while self.ws.secret_key is None:
                await self.ws.poll_event()
            self.connect_timings["key_ms"] = max(0, int((time.perf_counter() - t0) * 1000))
            self.last_close_code = 0
            self.last_close_reason = ""
            self.state = ConnectionFlowState.connected
            try:
                sr = getattr(self, "_socket_reader", None)
                if sr is not None:
                    sr.resume(force=True)
            except Exception:
                pass

    class SwiftVoiceClient(discord.VoiceClient):
        """VoiceClient con recepción UDP DAVE E2EE + envío directo de micrófono."""

        def __init__(self, client: discord.Client, channel: Any) -> None:
            super().__init__(client, channel)
            self.ssrc_to_user: dict[int, int] = {}
            self.connected_user_ids: set[int] = set()
            self._decoders: dict[int, discord.opus.Decoder] = {}
            self._decoder_lock = threading.Lock()
            self._send_lock = threading.Lock()
            self._aead_cache: tuple[bytes, Any] | None = None
            self._listener_registered = False
            self._self_user_id = int(client.user.id) if client.user else 0
            self.deafened = False
            self._speaking_announced = False

            self.tx_packets = 0
            self.rx_packets = 0
            self.rx_decrypt_errors = 0
            self.tx_errors = 0
            self._last_tx_err_ts = 0.0

            self.on_opus: Callable[[int, int, bytes], None] | None = None
            self.on_pcm: Callable[[int, bytes], None] | None = None
            self.on_user_removed: Callable[[int], None] | None = None
            self.on_speaking_change: Callable[[int, bool], None] | None = None
            self.on_dave_change: Callable[[bool, str], None] | None = None
            self.on_warning: Callable[[str], None] | None = None
            if on_init is not None:
                try:
                    on_init(self)
                except Exception:
                    pass

        def _ensure_voice_encoder(self) -> None:
            if self.encoder is discord.utils.MISSING:
                self.encoder = discord.opus.Encoder(
                    application="voip",
                    bitrate=64,
                    fec=True,
                    expected_packet_loss=0.1,
                    signal_type="voice",
                )

        def create_connection_state(self) -> VoiceConnectionState:
            return _SwiftVoiceConnectionState(self, hook=self._on_voice_ws_message)

        async def connect(
            self,
            *,
            timeout: float = 60.0,
            reconnect: bool = True,
            self_deaf: bool = False,
            self_mute: bool = False,
        ) -> None:
            self.deafened = bool(self_deaf)
            if self.channel is not None and getattr(self.channel, "voice_states", None):
                for raw_uid in self.channel.voice_states:
                    u = _safe_int(raw_uid)
                    if u and u != self._self_user_id:
                        self.connected_user_ids.add(u)
            await super().connect(
                timeout=timeout,
                reconnect=reconnect,
                self_deaf=self_deaf,
                self_mute=self_mute,
            )
            self._ensure_voice_encoder()
            if not self._listener_registered:
                self._connection.add_socket_listener(self._on_udp_packet)
                self._listener_registered = True
            try:
                import socket

                sock = getattr(self._connection, "socket", None)
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
            except Exception:
                pass
            try:
                sr = getattr(self._connection, "_socket_reader", None)
                if sr is not None:
                    sr.resume(force=True)
            except Exception:
                pass

        async def kickstart_voice(self, *, keep_speaking: bool = False) -> None:
            """Espera breve a DAVE MLS si aplica y envía silencio Opus sin cifrar para abrir flujo UDP."""
            try:
                if not self.is_connected():
                    return
                dave_proto = int(getattr(self._connection, "dave_protocol_version", 0) or 0)
                if dave_proto > 0 and not self._connection.can_encrypt:
                    for _ in range(12):
                        if not self.is_connected() or self._connection.can_encrypt:
                            break
                        await asyncio.sleep(0.05)
                if not self.is_connected():
                    return
                self._ensure_voice_encoder()
                if hasattr(self, "ws") and self.ws is not discord.utils.MISSING:
                    await self.ws.speak(discord.SpeakingState.voice)
                for _ in range(5):
                    self.send_opus_silence()
                    await asyncio.sleep(0.02)
                if (
                    not keep_speaking
                    and hasattr(self, "ws")
                    and self.ws is not discord.utils.MISSING
                ):
                    await self.ws.speak(discord.SpeakingState.none)
                self._speaking_announced = bool(keep_speaking)
            except Exception:
                pass
            self._emit_dave_status()

        def _emit_dave_status(self) -> None:
            if self.on_dave_change is None:
                return
            try:
                active = bool(self._connection.can_encrypt)
                code = str(self.voice_privacy_code or "")
                self.on_dave_change(active, code)
            except Exception:
                pass

        async def _on_voice_ws_message(self, _ws: Any, msg: dict[str, Any]) -> None:
            op = msg.get("op")
            data = msg.get("d")
            if not isinstance(data, dict):
                if op in (21, 22, 24):
                    self._emit_dave_status()
                return

            if op == 5:  # SPEAKING
                uid = _safe_int(data.get("user_id"))
                ssrc = _safe_int(data.get("ssrc"))
                if uid and uid != self._self_user_id:
                    self.connected_user_ids.add(uid)
                if uid and ssrc:
                    self.ssrc_to_user[ssrc] = uid
                if uid and self.on_speaking_change is not None:
                    self.on_speaking_change(uid, bool(data.get("speaking")))
            elif op == 11:  # CLIENTS_CONNECT
                for raw_u in data.get("user_ids") or ():
                    u = _safe_int(raw_u)
                    if u and u != self._self_user_id:
                        self.connected_user_ids.add(u)
            elif op == 12:  # CLIENT_CONNECT
                uid = _safe_int(data.get("user_id"))
                ssrc = _safe_int(data.get("audio_ssrc"))
                if uid and uid != self._self_user_id:
                    self.connected_user_ids.add(uid)
                if uid and ssrc:
                    self.ssrc_to_user[ssrc] = uid
            elif op == 13:  # CLIENT_DISCONNECT
                uid = _safe_int(data.get("user_id"))
                if uid:
                    self.connected_user_ids.discard(uid)
                    stale_ssrcs = [s for s, u in self.ssrc_to_user.items() if u == uid]
                    for s in stale_ssrcs:
                        self.ssrc_to_user.pop(s, None)
                        with self._decoder_lock:
                            self._decoders.pop(s, None)
                    if self.on_speaking_change is not None:
                        self.on_speaking_change(uid, False)
                    if self.on_user_removed is not None:
                        self.on_user_removed(uid)
            elif op in (4, 21, 22, 24):
                self._emit_dave_status()

        def _get_aead_box(self, key_bytes: bytes) -> Any:
            import nacl.secret

            if self._aead_cache is not None and self._aead_cache[0] == key_bytes:
                return self._aead_cache[1]
            box = nacl.secret.Aead(key_bytes)
            self._aead_cache = (key_bytes, box)
            return box

        def _collect_unmapped_remote_users(self, ds: Any) -> list[int]:
            mapped_uids = set(self.ssrc_to_user.values())
            cands: list[int] = []
            if ds is not None and hasattr(ds, "get_user_ids"):
                try:
                    for raw_u in ds.get_user_ids():
                        u = _safe_int(raw_u)
                        if u and u != self._self_user_id and u not in mapped_uids and u not in cands:
                            cands.append(u)
                except Exception:
                    pass
            for u in self.connected_user_ids:
                if u and u != self._self_user_id and u not in mapped_uids and u not in cands:
                    cands.append(u)
            if not cands and self.channel is not None and getattr(self.channel, "voice_states", None):
                for raw_u in self.channel.voice_states:
                    u = _safe_int(raw_u)
                    if u and u != self._self_user_id and u not in mapped_uids and u not in cands:
                        cands.append(u)
            return cands

        def _on_udp_packet(self, data: bytes) -> None:
            if self.deafened or not self.is_connected() or len(data) < 16:
                return
            sk = self._connection.secret_key
            mode = self._connection.mode
            if sk is discord.utils.MISSING or mode is discord.utils.MISSING:
                return

            b1 = data[1]
            is_opus_rtp = ((data[0] >> 6) & 0x03) == 2 and not (192 <= b1 <= 223) and (b1 & 0x7F) == 120
            seq = struct.unpack_from(">H", data, 2)[0]
            ssrc = struct.unpack_from(">I", data, 8)[0] if is_opus_rtp else 0
            key_bytes = bytes(sk)
            aead_box = (
                self._get_aead_box(key_bytes)
                if mode == "aead_xchacha20_poly1305_rtpsize"
                else None
            )
            ds = self._connection.dave_session
            dave_active = bool(self._connection.can_encrypt)

            if is_opus_rtp and ssrc and ssrc not in self.ssrc_to_user:
                cands = self._collect_unmapped_remote_users(ds)
                if len(cands) == 1:
                    self.ssrc_to_user[ssrc] = cands[0]
                elif 1 < len(cands) <= 6 and dave_active and ds is not None and getattr(ds, "ready", False):
                    for cand_uid in cands:
                        probe_res = decrypt_discord_rtp_packet(
                            data=data,
                            mode=str(mode),
                            secret_key=key_bytes,
                            ssrc_to_user={ssrc: cand_uid},
                            dave_session=ds,
                            dave_active=True,
                            aead_box=aead_box,
                            dave_media_type=davey.MediaType.audio,
                        )
                        if probe_res is not None and probe_res[2] != OPUS_SILENCE:
                            self.ssrc_to_user[ssrc] = cand_uid
                            break

            res = decrypt_discord_rtp_packet(
                data=data,
                mode=str(mode),
                secret_key=key_bytes,
                ssrc_to_user=self.ssrc_to_user,
                dave_session=ds,
                dave_active=dave_active,
                aead_box=aead_box,
                dave_media_type=davey.MediaType.audio,
            )
            if res is None:
                if is_opus_rtp:
                    if ssrc in self.ssrc_to_user:
                        self.rx_decrypt_errors += 1
                return
            ssrc, uid, opus_bytes = res
            if uid is None or uid == self._self_user_id:
                return

            if self.on_opus is not None:
                self.on_opus(uid, seq, opus_bytes)
                if opus_bytes != OPUS_SILENCE:
                    self.rx_packets += 1
                    if self.on_speaking_change is not None:
                        self.on_speaking_change(uid, True)
                return

            if opus_bytes == OPUS_SILENCE:
                return

            with self._decoder_lock:
                dec = self._decoders.get(ssrc)
                if dec is None:
                    dec = discord.opus.Decoder()
                    self._decoders[ssrc] = dec
                try:
                    pcm = dec.decode(opus_bytes, fec=False)
                except Exception:
                    return

            if pcm and self.on_pcm is not None:
                self.rx_packets += 1
                self.on_pcm(uid, pcm)
            if self.on_speaking_change is not None:
                self.on_speaking_change(uid, True)

        def send_mic_pcm(self, stereo_pcm: bytes) -> None:
            if not self.is_connected():
                return
            with self._send_lock:
                if not self.is_connected():
                    return
                self._ensure_voice_encoder()
                try:
                    self.send_audio_packet(stereo_pcm, encode=True)
                    self.tx_packets += 1
                except Exception as exc:
                    self.tx_errors += 1
                    now = time.monotonic()
                    if self.on_warning is not None and (now - self._last_tx_err_ts) >= 5.0:
                        self._last_tx_err_ts = now
                        self.on_warning(f"Error enviando micrófono por Discord: {exc}")

        def send_opus_silence(self) -> None:
            if not self.is_connected():
                return
            with self._send_lock:
                if not self.is_connected():
                    return
                try:
                    self.checked_add("sequence", 1, 65535)
                    header = bytearray(12)
                    header[0] = 0x80
                    header[1] = 0x78
                    struct.pack_into(">H", header, 2, self.sequence)
                    struct.pack_into(">I", header, 4, self.timestamp)
                    struct.pack_into(">I", header, 8, self.ssrc)
                    enc_fn = getattr(self, "_encrypt_" + self.mode)
                    packet = enc_fn(header, OPUS_SILENCE)
                    self._connection.send_packet(packet)
                    self.checked_add(
                        "timestamp", discord.opus.Encoder.SAMPLES_PER_FRAME, 4294967295
                    )
                except Exception:
                    pass

        async def set_speaking_state(self, speaking: bool) -> None:
            try:
                if hasattr(self, "ws") and self.ws is not discord.utils.MISSING and self.is_connected():
                    state = discord.SpeakingState.voice if speaking else discord.SpeakingState.none
                    await self.ws.speak(state)
            except Exception:
                pass

        def cleanup(self) -> None:
            if self._listener_registered:
                self._listener_registered = False
                try:
                    self._connection.remove_socket_listener(self._on_udp_packet)
                except Exception:
                    pass
            with self._decoder_lock:
                self._decoders.clear()
            self._aead_cache = None
            super().cleanup()

    return SwiftVoiceClient


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


_STATUS_ORDER: dict[str, int] = {
    "online": 0,
    "dnd": 1,
    "idle": 2,
    "offline": 3,
}


def _extract_presence_screen_share(member: Any) -> dict[str, Any] | None:
    """Extrae un beacon `sws1|...` de pantalla compartida desde las actividades del miembro."""
    acts = list(getattr(member, "activities", None) or ())
    if not acts:
        single = getattr(member, "activity", None)
        if single is not None:
            acts = [single]
    for act in acts:
        for field in ("state", "name", "details"):
            val = getattr(act, field, None)
            if not val:
                continue
            parsed = parse_screen_share_presence(str(val))
            if parsed is not None:
                return parsed
    return None


def _format_member_activity(member: Any) -> str:
    """Devuelve texto breve de actividad o estado personalizado de un miembro."""
    ss = _extract_presence_screen_share(member)
    if ss is not None:
        return f"🔴 Compartiendo pantalla ({ss['quality']} {ss['fps']}fps)"

    acts = list(getattr(member, "activities", None) or ())
    if not acts:
        single = getattr(member, "activity", None)
        if single is not None:
            acts = [single]

    custom_text = ""
    for act in acts:
        raw_t = getattr(act, "type", "")
        t_str = str(getattr(raw_t, "name", raw_t) or "").strip().lower()
        name = str(getattr(act, "name", "") or "").strip()
        state = str(getattr(act, "state", "") or "").strip()

        if "custom" in t_str:
            if not custom_text:
                custom_text = state or name
            continue

        txt = ""
        if "playing" in t_str and name:
            txt = f"Jugando a {name}"
        elif "listening" in t_str:
            title = str(getattr(act, "title", "") or "").strip()
            txt = f"Escuchando {title or name}" if (title or name) else ""
        elif "streaming" in t_str and name:
            txt = f"En vivo: {name}"
        elif "watching" in t_str and name:
            txt = f"Viendo {name}"
        elif "competing" in t_str and name:
            txt = f"Compitiendo en {name}"
        else:
            txt = state or name

        if txt:
            return txt[:61] + "..." if len(txt) > 64 else txt

    if custom_text:
        return custom_text[:61] + "..." if len(custom_text) > 64 else custom_text
    return ""


def _member_avatar_url(obj: Any) -> str:
    """Obtiene la URL del avatar del usuario/miembro en 128px si está disponible."""
    av = getattr(obj, "display_avatar", None) or getattr(obj, "avatar", None)
    if av is None:
        return ""
    try:
        sized = av.with_size(128) if hasattr(av, "with_size") else av
        return str(getattr(sized, "url", "") or getattr(av, "url", "") or "")
    except Exception:
        try:
            return str(getattr(av, "url", "") or "")
        except Exception:
            return ""


def _serialize_guild_members(
    guild: Any, limit: int = 200, channel: Any = None
) -> tuple[int, list[dict[str, Any]]]:
    """Serializa miembros de un servidor ordenados por presencia (y filtrados por canal opcional)."""
    items: list[dict[str, Any]] = []
    online_count = 0
    for m in list(getattr(guild, "members", None) or ()):
        if channel is not None:
            try:
                perms = channel.permissions_for(m)
                if not bool(getattr(perms, "view_channel", True)):
                    continue
            except Exception:
                pass
        raw_st = getattr(m, "status", "offline")
        st = str(getattr(raw_st, "value", raw_st) or "offline").strip().lower()
        if st not in _STATUS_ORDER:
            st = "offline"
        if st != "offline":
            online_count += 1
        uid = _safe_int(getattr(m, "id", 0))
        name = str(getattr(m, "display_name", "") or getattr(m, "name", "") or uid)
        is_bot = bool(getattr(m, "bot", False))
        items.append(
            {
                "id": str(uid),
                "name": name,
                "is_bot": is_bot,
                "avatar_url": _member_avatar_url(m),
                "status": st,
                "activity": _format_member_activity(m),
            }
        )

    items.sort(
        key=lambda it: (
            _STATUS_ORDER.get(it["status"], 3),
            1 if it["is_bot"] else 0,
            it["name"].lower(),
        )
    )
    cap = max(1, int(limit))
    return online_count, items[:cap]


def _serialize_chat_message(msg: Any, bot_id: int = 0) -> dict[str, Any]:
    """Serializa un mensaje de Discord acotando tamaño y sin descargar adjuntos."""
    author = getattr(msg, "author", None)
    author_id = _safe_int(getattr(author, "id", 0))
    author_name = str(
        getattr(author, "display_name", "")
        or getattr(author, "name", "")
        or author_id
        or "Usuario"
    )
    is_bot = bool(getattr(author, "bot", False))
    is_self = bool(bot_id and author_id == int(bot_id))
    content = str(getattr(msg, "content", "") or "")[:2000]

    attachments: list[dict[str, Any]] = []
    for att in list(getattr(msg, "attachments", None) or ())[:8]:
        attachments.append(
            {
                "name": str(getattr(att, "filename", "") or "archivo")[:120],
                "url": str(getattr(att, "url", "") or ""),
                "proxy_url": str(getattr(att, "proxy_url", "") or ""),
                "content_type": str(getattr(att, "content_type", "") or ""),
                "size": _safe_int(getattr(att, "size", 0)),
                "width": _safe_int(getattr(att, "width", 0)),
                "height": _safe_int(getattr(att, "height", 0)),
            }
        )

    reply_to: dict[str, str] | None = None
    ref = getattr(msg, "reference", None)
    if ref is not None:
        ref_id = str(getattr(ref, "message_id", "") or "")
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and hasattr(resolved, "author"):
            r_author = getattr(resolved, "author", None)
            r_author_name = str(
                getattr(r_author, "display_name", "")
                or getattr(r_author, "name", "")
                or getattr(r_author, "id", "")
                or "Usuario"
            )
            r_content = str(getattr(resolved, "content", "") or "").strip()[:120]
            if not r_content:
                r_atts = list(getattr(resolved, "attachments", None) or ())
                if r_atts:
                    r_content = f"📎 {str(getattr(r_atts[0], 'filename', '') or 'archivo')[:60]}"
            reply_to = {
                "id": ref_id,
                "author_name": r_author_name,
                "content": r_content,
            }
        elif ref_id:
            reply_to = {
                "id": ref_id,
                "author_name": "",
                "content": "",
            }

    ts_str = ""
    date_label = ""
    created_ts = 0.0
    created = getattr(msg, "created_at", None)
    if created is not None:
        try:
            created_ts = float(created.timestamp())
            local_dt = created.astimezone() if getattr(created, "tzinfo", None) is not None else created
            ts_str = local_dt.strftime("%H:%M")
            msg_date = local_dt.date()
            today = datetime.now().date()
            if msg_date == today:
                date_label = "Hoy"
            elif msg_date == today - timedelta(days=1):
                date_label = "Ayer"
            else:
                date_label = local_dt.strftime("%d/%m/%Y")
        except Exception:
            ts_str = ""
            date_label = ""

    return {
        "id": str(getattr(msg, "id", "") or ""),
        "author_id": str(author_id),
        "author_name": author_name,
        "author_avatar_url": _member_avatar_url(author),
        "is_self": is_self,
        "is_bot": is_bot,
        "content": content,
        "edited": bool(getattr(msg, "edited_at", None) is not None),
        "reply_to": reply_to,
        "attachments": attachments,
        "timestamp": ts_str,
        "date_label": date_label,
        "created_ts": created_ts,
    }


class DiscordWorkerApp:
    """Controlador asyncio dentro del subproceso aislado de Discord."""

    def __init__(self, proto_out: Any) -> None:
        self._proto_out = proto_out
        self._write_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

        self._client: Any = None
        self._bot_task: asyncio.Task[Any] | None = None
        self._housekeeping_task: asyncio.Task[Any] | None = None
        self._join_task: asyncio.Task[Any] | None = None
        self._voice_lock = asyncio.Lock()
        self._vc: Any = None
        self._connecting_vc: Any = None
        self._voice_cls: Any = None
        self._post_join_task: asyncio.Task[Any] | None = None
        self._joining_channel_id: str = ""
        self._last_join_diag: str = ""
        self._desired_voice_target: tuple[int, int] | None = None
        self._desired_voice_meta: tuple[str, str] = ("", "")
        self._voice_reconnecting: bool = False
        self._vc_disconnected_since: float = 0.0
        self._voice_drop_task: asyncio.Task[Any] | None = None
        self._voice_rejoin_attempts: int = 0
        self._initial_ready_done: bool = False
        self._manual_disconnect: bool = False
        self._gw_backoff: float = 2.0

        self._mixer = VoiceAudioMixer()
        self._audio: AudioEngine | None = None
        self._muted = False
        self._deafened = False
        self._mic_test_active = False
        self._mic_test_saved_flags: tuple[bool, bool] | None = None
        self._speaking_timestamps: dict[int, float] = {}
        self._speaking_lock = threading.Lock()
        self._member_name_cache: dict[int, tuple[str, bool, str]] = {}
        self._member_fetch_inflight: set[tuple[int, int]] = set()
        self._notify_channel_ids: set[int] = set()
        self._state = "disconnected"
        self._dave_active = False
        self._dave_was_active = False
        self._dave_code = ""
        self._resync_task: asyncio.Task[Any] | None = None
        self._last_emitted_speaking: dict[str, bool] = {}

        self._privileged_intents_ok = False
        self._active_guild_id = 0
        self._active_chat_kind = ""
        self._active_chat_id = 0
        self._dm_channels: dict[int, int] = {}
        self._presence_dirty_guilds: set[int] = set()
        self._chunked_guilds: set[int] = set()
        self._last_presence_emit = 0.0
        self._local_share_presence: dict[str, Any] | None = None
        self._presence_shares: dict[int, dict[str, Any]] = {}

    def _emit_json(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                self._proto_out.write(line)
                self._proto_out.flush()
            except Exception:
                pass

    def emit_event(self, event: str, data: Any = None) -> None:
        self._emit_json({"type": "event", "event": event, "data": data})

    def emit_result(self, req_id: Any, ok: bool, data: Any = None, error: str = "") -> None:
        payload: dict[str, Any] = {"type": "result", "id": req_id, "ok": ok}
        if ok:
            payload["data"] = data
        else:
            payload["error"] = error
        self._emit_json(payload)

    def _ensure_audio_engine(self) -> AudioEngine:
        if self._audio is None:
            self._audio = AudioEngine(
                mixer=self._mixer,
                on_send_pcm=self._on_mic_pcm,
                on_speaking_state=self._on_local_speaking_state,
                on_mic_level=lambda rms, tx: self.emit_event(
                    "mic_level", {"rms": rms, "transmitting": tx}
                ),
                on_warning=lambda msg: self.emit_event("warning", {"message": msg}),
            )
        return self._audio

    def _on_mic_pcm(self, stereo_pcm: bytes) -> None:
        vc = self._vc
        if vc is not None and vc.is_connected():
            vc.send_mic_pcm(stereo_pcm)

    def _on_local_speaking_state(self, speaking: bool) -> None:
        vc = self._vc
        loop = self._loop
        if vc is not None and loop is not None and not loop.is_closed():
            if not speaking:
                for _ in range(5):
                    vc.send_opus_silence()
            asyncio.run_coroutine_threadsafe(vc.set_speaking_state(speaking), loop)
        with self._speaking_lock:
            if speaking:
                self._speaking_timestamps[0] = time.monotonic()
            else:
                self._speaking_timestamps.pop(0, None)
        self._emit_speaking_snapshot()

    def _on_remote_speaking(self, user_id: int, speaking: bool) -> None:
        with self._speaking_lock:
            if speaking:
                self._speaking_timestamps[user_id] = time.monotonic()
            else:
                self._speaking_timestamps.pop(user_id, None)

    def _emit_speaking_snapshot(self) -> None:
        with self._speaking_lock:
            snap: dict[str, bool] = {}
            for uid in self._speaking_timestamps:
                snap["local" if uid == 0 else str(uid)] = True
        if snap != self._last_emitted_speaking:
            self._last_emitted_speaking = dict(snap)
            self.emit_event("speaking", snap)

    def _schedule_dave_resync(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if self._resync_task is not None and not self._resync_task.done():
            return
        self._resync_task = loop.create_task(self._resync_after_dave())

    async def _resync_after_dave(self) -> None:
        vc = self._vc
        if vc is None or not vc.is_connected():
            return
        # Descartar audio previo al handshake MLS DAVE y reactivar flujo bidireccional
        self._mixer.clear()
        engine = self._audio
        keep_speaking = bool(
            engine is not None
            and not self._muted
            and (engine.is_speaking or engine.is_open_mic)
        )
        await vc.kickstart_voice(keep_speaking=keep_speaking)
        if engine is not None and engine._running:
            rec_in, rec_out = engine.check_and_recover_streams()
            if rec_in or rec_out:
                parts = []
                if rec_in:
                    parts.append("micrófono")
                if rec_out:
                    parts.append("salida")
                self.emit_event(
                    "warning",
                    {
                        "message": f"Audio resincronizado tras cifrado DAVE ({', '.join(parts)})."
                    },
                )

    def _on_dave_change(self, active: bool, code: str) -> None:
        became_active = bool(active) and not self._dave_was_active
        self._dave_active = bool(active)
        if active:
            self._dave_was_active = True
        self._dave_code = str(code or "")
        self.emit_event("voice_state", self._build_voice_state_dict())
        if became_active:
            self._schedule_dave_resync()

    def _schedule_member_fetch(self, guild: Any, uid: int) -> None:
        if guild is None or self._loop is None or self._loop.is_closed():
            return
        key = (int(guild.id), int(uid))
        if key in self._member_fetch_inflight:
            return
        self._member_fetch_inflight.add(key)

        async def _fetch() -> None:
            try:
                member = await guild.fetch_member(uid)
                if member is not None:
                    name = str(getattr(member, "display_name", "") or getattr(member, "name", "") or uid)
                    is_bot = bool(getattr(member, "bot", False))
                    av_url = _member_avatar_url(member)
                    if len(self._member_name_cache) >= 96:
                        self._member_name_cache.clear()
                    self._member_name_cache[uid] = (name, is_bot, av_url)
                    self._emit_snapshots()
            except Exception:
                pass
            finally:
                self._member_fetch_inflight.discard(key)

        self._loop.create_task(_fetch())

    def _resolve_member_name(
        self, guild: Any, member_or_id: Any
    ) -> tuple[int, str, bool, str]:
        if isinstance(member_or_id, int):
            uid = member_or_id
            m = guild.get_member(uid) if guild is not None else None
            if m is None and self._client is not None:
                m = self._client.get_user(uid)
            if m is not None:
                name = str(getattr(m, "display_name", "") or getattr(m, "name", "") or uid)
                is_bot = bool(getattr(m, "bot", False))
                av_url = _member_avatar_url(m)
                self._member_name_cache[uid] = (name, is_bot, av_url)
                return uid, name, is_bot, av_url
            cached = self._member_name_cache.get(uid)
            if cached is not None:
                return uid, cached[0], cached[1], cached[2]
            self._schedule_member_fetch(guild, uid)
            return uid, f"Usuario {uid}", False, ""

        uid = int(getattr(member_or_id, "id", 0) or 0)
        name = str(
            getattr(member_or_id, "display_name", "")
            or getattr(member_or_id, "name", "")
            or uid
        )
        is_bot = bool(getattr(member_or_id, "bot", False))
        av_url = _member_avatar_url(member_or_id)
        if uid:
            if len(self._member_name_cache) >= 96:
                self._member_name_cache.clear()
            self._member_name_cache[uid] = (name, is_bot, av_url)
        return uid, name, is_bot, av_url

    def _resolve_member_status(self, guild: Any, uid: int) -> str:
        m = guild.get_member(uid) if guild is not None else None
        if m is None:
            return "offline"
        raw_st = getattr(m, "status", "offline")
        st = str(getattr(raw_st, "value", raw_st) or "offline").strip().lower()
        return st if st in _STATUS_ORDER else "offline"

    def _collect_known_presences(self) -> dict[str, str]:
        client = self._client
        out: dict[str, str] = {}
        if client is None or not client.is_ready():
            return out
        for g in list(getattr(client, "guilds", None) or ()):
            for m in list(getattr(g, "members", None) or ()):
                uid = _safe_int(getattr(m, "id", 0))
                if not uid:
                    continue
                raw_st = getattr(m, "status", "offline")
                st = str(getattr(raw_st, "value", raw_st) or "offline").strip().lower()
                if st not in _STATUS_ORDER:
                    st = "offline"
                k = str(uid)
                prev = out.get(k)
                if prev is None or _STATUS_ORDER.get(st, 3) < _STATUS_ORDER.get(prev, 3):
                    out[k] = st
        return out

    async def _set_screen_share_presence(self, share: dict[str, Any] | None) -> None:
        import discord

        if isinstance(share, dict) and str(share.get("watch_url") or "").strip():
            self._local_share_presence = dict(share)
        else:
            self._local_share_presence = None

        client = self._client
        if client is None or not client.is_ready():
            return
        if self._local_share_presence:
            s = self._local_share_presence
            beacon = encode_screen_share_presence(
                str(s.get("watch_url") or ""),
                quality=str(s.get("quality") or "720p"),
                fps=int(s.get("fps") or 30),
                has_audio=bool(s.get("has_audio", True)),
                rev=int(s.get("rev") or 1),
            )
            if beacon:
                await client.change_presence(
                    status=discord.Status.online,
                    activity=discord.CustomActivity(name=beacon),
                )
                return
        await client.change_presence(status=discord.Status.online, activity=None)

    def _sync_member_presence_share(
        self, guild: Any, member: Any, *, emit_snapshots: bool = True
    ) -> bool:
        if member is None:
            return False
        uid = _safe_int(getattr(member, "id", 0))
        client = self._client
        bot_id = int(client.user.id) if client and client.user else 0
        if not uid or (bot_id and uid == bot_id):
            return False

        info = _extract_presence_screen_share(member)
        if info is not None:
            voice = getattr(member, "voice", None)
            vch = getattr(voice, "channel", None)
            ch_id_str = str(getattr(vch, "id", "") or "") if vch is not None else ""
            ch_name = str(getattr(vch, "name", "") or "") if vch is not None else ""
            g_id_str = str(getattr(guild, "id", "") or "") if guild is not None else ""
            g_name = str(getattr(guild, "name", "") or "") if guild is not None else ""
            author_name = str(
                getattr(member, "display_name", "") or getattr(member, "name", "") or uid
            )
            payload: dict[str, Any] = {
                "guild_id": g_id_str,
                "guild_name": g_name,
                "channel_id": ch_id_str,
                "channel_name": ch_name,
                "message_id": "",
                "author_id": str(uid),
                "author_name": author_name,
                "author_avatar_url": _member_avatar_url(member),
                "watch_url": info["watch_url"],
                "stream_url": info["stream_url"],
                "token": info["token"],
                "quality": info["quality"],
                "fps": info["fps"],
                "has_audio": info["has_audio"],
                "rev": info["rev"],
                "source": "presence",
                "is_self": False,
            }
            new_sig = (
                payload["stream_url"],
                payload["quality"],
                payload["fps"],
                payload["has_audio"],
                payload["rev"],
                payload["channel_id"],
            )
            prev = self._presence_shares.get(uid)
            prev_sig = (
                (
                    prev.get("stream_url"),
                    prev.get("quality"),
                    prev.get("fps"),
                    prev.get("has_audio"),
                    prev.get("rev"),
                    prev.get("channel_id"),
                )
                if prev is not None
                else None
            )
            if new_sig == prev_sig:
                return False
            self._presence_shares[uid] = payload
            self.emit_event("screen_share_detected", payload)
            if emit_snapshots:
                self._emit_snapshots()
            return True

        if uid not in self._presence_shares:
            return False
        if client is not None:
            for og in list(getattr(client, "guilds", ()) or ()):
                if guild is not None and getattr(og, "id", None) == getattr(guild, "id", None):
                    continue
                om = og.get_member(uid) if hasattr(og, "get_member") else None
                if om is not None and _extract_presence_screen_share(om) is not None:
                    return False
        self._presence_shares.pop(uid, None)
        self.emit_event(
            "screen_share_ended",
            {"author_id": str(uid), "source": "presence"},
        )
        if emit_snapshots:
            self._emit_snapshots()
        return True

    def _scan_guild_presence_shares(
        self, guild: Any, *, emit_snapshots: bool = False
    ) -> bool:
        if guild is None:
            return False
        changed = False
        for m in list(getattr(guild, "members", None) or ()):
            if self._sync_member_presence_share(guild, m, emit_snapshots=False):
                changed = True
        if changed and emit_snapshots:
            self._emit_snapshots()
        return changed

    def _build_guilds_snapshot(self) -> list[dict[str, Any]]:
        client = self._client
        if client is None or not client.is_ready():
            return []
        guilds_out: list[dict[str, Any]] = []
        for g in sorted(client.guilds, key=lambda x: str(x.name).lower()):
            vcs: list[dict[str, Any]] = []
            for ch in sorted(g.voice_channels, key=lambda c: (c.position, str(c.name).lower())):
                members_list: list[dict[str, Any]] = []
                for uid_key, vs in ch.voice_states.items():
                    uid, name, is_bot, av_url = self._resolve_member_name(g, int(uid_key))
                    members_list.append(
                        {
                            "id": str(uid),
                            "name": name,
                            "is_bot": is_bot,
                            "avatar_url": av_url,
                            "status": self._resolve_member_status(g, uid),
                            "muted": bool(getattr(vs, "self_mute", False) or getattr(vs, "mute", False)),
                            "deafened": bool(getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False)),
                            "screen_share": dict(self._presence_shares[uid]) if uid in self._presence_shares else None,
                        }
                    )
                perms = ch.permissions_for(g.me) if g.me is not None else None
                vcs.append(
                    {
                        "id": str(ch.id),
                        "name": str(ch.name),
                        "can_connect": bool(perms.connect) if perms is not None else True,
                        "can_speak": bool(perms.speak) if perms is not None else True,
                        "members": members_list,
                    }
                )
            tcs: list[dict[str, Any]] = []
            for tc in sorted(
                list(getattr(g, "text_channels", None) or ()),
                key=lambda c: (getattr(c, "position", 0), str(c.name).lower()),
            ):
                perms_t = tc.permissions_for(g.me) if g.me is not None else None
                if perms_t is not None and not getattr(perms_t, "view_channel", True):
                    continue
                tcs.append(
                    {
                        "id": str(tc.id),
                        "name": str(tc.name),
                        "can_read": bool(getattr(perms_t, "read_message_history", True))
                        if perms_t is not None
                        else True,
                        "can_send": bool(getattr(perms_t, "send_messages", True))
                        if perms_t is not None
                        else True,
                    }
                )
            guilds_out.append(
                {
                    "id": str(g.id),
                    "name": str(g.name),
                    "voice_channels": vcs,
                    "text_channels": tcs,
                }
            )
        return guilds_out

    def _build_voice_state_dict(self) -> dict[str, Any]:
        vc = self._vc
        if (
            vc is not None
            and not vc.is_connected()
            and self._voice_reconnecting
            and self._desired_voice_target is not None
        ):
            gid_d, cid_d = self._desired_voice_target
            g_name_d, ch_name_d = self._desired_voice_meta
            parts_d: list[dict[str, Any]] = []
            ch_obj = getattr(vc, "channel", None)
            if ch_obj is None and self._client is not None:
                g_obj = self._client.get_guild(gid_d)
                ch_obj = g_obj.get_channel(cid_d) if g_obj is not None else None
            if ch_obj is not None and getattr(ch_obj, "guild", None) is not None:
                g_ref = ch_obj.guild
                for uid_key, vs in (getattr(ch_obj, "voice_states", None) or {}).items():
                    uid, name, is_bot, av_url = self._resolve_member_name(g_ref, int(uid_key))
                    parts_d.append(
                        {
                            "id": str(uid),
                            "name": name,
                            "is_bot": is_bot,
                            "avatar_url": av_url,
                            "status": self._resolve_member_status(g_ref, uid),
                            "is_self": bool(
                                self._client and self._client.user and uid == self._client.user.id
                            ),
                            "muted": bool(getattr(vs, "self_mute", False) or getattr(vs, "mute", False)),
                            "deafened": bool(
                                getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False)
                            ),
                            "screen_share": dict(self._presence_shares[uid])
                            if uid in self._presence_shares
                            else None,
                        }
                    )
            return {
                "in_voice": True,
                "reconnecting": True,
                "joining_channel_id": str(cid_d),
                "guild_id": str(gid_d),
                "guild_name": g_name_d,
                "channel_id": str(cid_d),
                "channel_name": ch_name_d or "Voz",
                "muted": self._muted,
                "deafened": self._deafened,
                "mic_test": self._mic_test_active,
                "dave_active": False,
                "dave_code": "",
                "connect_diag": "Reconectando voz...",
                "participants": parts_d,
            }

        if vc is None or not vc.is_connected() or getattr(vc, "channel", None) is None:
            return {
                "in_voice": False,
                "reconnecting": False,
                "joining_channel_id": self._joining_channel_id,
                "guild_id": "",
                "channel_id": "",
                "channel_name": "",
                "guild_name": "",
                "muted": self._muted,
                "deafened": self._deafened,
                "mic_test": self._mic_test_active,
                "dave_active": False,
                "dave_code": "",
                "connect_diag": self._last_join_diag,
                "participants": [],
            }

        ch = vc.channel
        g = ch.guild
        participants: list[dict[str, Any]] = []
        for uid_key, vs in ch.voice_states.items():
            uid, name, is_bot, av_url = self._resolve_member_name(g, int(uid_key))
            participants.append(
                {
                    "id": str(uid),
                    "name": name,
                    "is_bot": is_bot,
                    "avatar_url": av_url,
                    "status": self._resolve_member_status(g, uid),
                    "is_self": bool(self._client and self._client.user and uid == self._client.user.id),
                    "muted": bool(getattr(vs, "self_mute", False) or getattr(vs, "mute", False)),
                    "deafened": bool(getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False)),
                    "screen_share": dict(self._presence_shares[uid]) if uid in self._presence_shares else None,
                }
            )
        return {
            "in_voice": True,
            "reconnecting": bool(self._voice_reconnecting),
            "joining_channel_id": self._joining_channel_id,
            "guild_id": str(g.id),
            "guild_name": str(g.name),
            "channel_id": str(ch.id),
            "channel_name": str(ch.name),
            "muted": self._muted,
            "deafened": self._deafened,
            "mic_test": self._mic_test_active,
            "dave_active": bool(self._vc._connection.can_encrypt) if self._vc else self._dave_active,
            "dave_code": str(self._vc.voice_privacy_code or "") if self._vc else self._dave_code,
            "tx_packets": int(getattr(self._vc, "tx_packets", 0)),
            "rx_packets": int(getattr(self._vc, "rx_packets", 0)),
            "rx_decrypt_errors": int(getattr(self._vc, "rx_decrypt_errors", 0)),
            "tx_errors": int(getattr(self._vc, "tx_errors", 0)),
            "connect_diag": self._last_join_diag,
            "participants": participants,
        }

    def _emit_snapshots(self) -> None:
        self.emit_event("guilds", self._build_guilds_snapshot())
        self.emit_event("voice_state", self._build_voice_state_dict())

    def _emit_guild_members(self, guild_id: int) -> None:
        client = self._client
        if client is None or not client.is_ready() or not guild_id:
            return
        guild = client.get_guild(int(guild_id))
        if guild is None:
            return
        online_count, members = _serialize_guild_members(guild, limit=200)
        self.emit_event(
            "guild_members",
            {
                "guild_id": str(guild.id),
                "online_count": online_count,
                "total_cached": len(getattr(guild, "members", None) or ()),
                "members": members,
            },
        )

    def _build_client(self, use_privileged: bool) -> Any:
        import socket
        import aiohttp
        import discord

        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.dm_messages = True
        if use_privileged:
            intents.members = True
            intents.presences = True
            intents.message_content = True
        connector: Any = None
        try:
            asyncio.get_running_loop()
            connector = aiohttp.TCPConnector(limit=0, family=socket.AF_INET)
        except RuntimeError:
            connector = None
        kwargs: dict[str, Any] = {
            "intents": intents,
            "max_messages": None,
            "chunk_guilds_at_startup": False,
        }
        if connector is not None:
            kwargs["connector"] = connector
        return discord.Client(**kwargs)

    def _attach_client_events(self, client: Any) -> None:
        @client.event
        async def on_disconnect() -> None:
            self._write_voice_diag("GATEWAY_DISCONNECT")

        @client.event
        async def on_resumed() -> None:
            self._gw_backoff = 2.0
            self._write_voice_diag("GATEWAY_RESUMED")

        @client.event
        async def on_ready() -> None:
            is_initial = not self._initial_ready_done
            self._initial_ready_done = True
            self._gw_backoff = 2.0
            self._write_voice_diag(f"GATEWAY_READY initial={is_initial}")
            self._state = "connected"
            user = client.user
            self.emit_event(
                "state",
                {
                    "state": "connected",
                    "bot_id": str(user.id) if user else "",
                    "bot_name": str(user.display_name or user.name) if user else "Bot",
                    "bot_avatar_url": _member_avatar_url(user) if user else "",
                    "privileged_intents_ok": bool(self._privileged_intents_ok),
                    "presences": self._collect_known_presences(),
                },
            )
            for g in list(getattr(client, "guilds", ()) or ()):
                self._scan_guild_presence_shares(g, emit_snapshots=False)
            self._emit_snapshots()
            if self._local_share_presence is not None:
                try:
                    await self._set_screen_share_presence(self._local_share_presence)
                except Exception:
                    pass
            if self._active_guild_id:
                await self._select_guild(self._active_guild_id)
            try:
                self._ensure_audio_engine().warmup_async()
            except Exception:
                pass
            if (
                is_initial
                and self._vc is None
                and not self._joining_channel_id
                and self._desired_voice_target is None
            ):
                try:
                    async def _clear_orphan_voice_sessions() -> None:
                        for g in list(getattr(client, "guilds", ()) or ()):
                            me = getattr(g, "me", None)
                            vs = getattr(me, "voice", None) if me is not None else None
                            if (
                                vs is not None
                                and getattr(vs, "channel", None) is not None
                                and getattr(g, "voice_client", None) is None
                            ):
                                try:
                                    await g.change_voice_state(channel=None)
                                except Exception:
                                    pass

                    asyncio.create_task(_clear_orphan_voice_sessions())
                except Exception:
                    pass
            elif (
                self._desired_voice_target is not None
                and not self._manual_disconnect
                and not self._joining_channel_id
                and (self._vc is None or not self._vc.is_connected())
            ):
                asyncio.create_task(self._schedule_auto_rejoin("gateway_ready"))
            try:
                from swiftplay.windows_os import trim_process_memory

                trim_process_memory(collect_gc=True)
            except Exception:
                pass

        @client.event
        async def on_guild_join(_guild: Any) -> None:
            self._emit_snapshots()

        @client.event
        async def on_guild_remove(_guild: Any) -> None:
            self._emit_snapshots()

        @client.event
        async def on_guild_channel_create(_channel: Any) -> None:
            self._emit_snapshots()

        @client.event
        async def on_guild_channel_delete(_channel: Any) -> None:
            self._emit_snapshots()

        @client.event
        async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
            if client.user and member.id == client.user.id:
                if after.channel is not None:
                    self._desired_voice_target = (
                        int(after.channel.guild.id),
                        int(after.channel.id),
                    )
                    self._desired_voice_meta = (
                        str(getattr(after.channel.guild, "name", "") or ""),
                        str(getattr(after.channel, "name", "") or ""),
                    )
                elif (
                    after.channel is None
                    and self._vc is not None
                    and not self._joining_channel_id
                    and not self._manual_disconnect
                ):
                    conn = getattr(self._vc, "_connection", None)
                    expecting = bool(getattr(conn, "_expecting_disconnect", False))
                    code = int(getattr(conn, "last_close_code", 0) or 0)
                    self._write_voice_diag(
                        f"VOICE_STATE_NONE expecting={expecting} code={code}"
                    )
                    if self._voice_drop_task is None or self._voice_drop_task.done():
                        self._voice_drop_task = asyncio.create_task(
                            self._verify_voice_drop_after_grace(self._vc)
                        )
                    return
            self._sync_member_presence_share(getattr(member, "guild", None), member, emit_snapshots=False)
            self._emit_snapshots()

        @client.event
        async def on_presence_update(_before: Any, after: Any) -> None:
            g = getattr(after, "guild", None)
            if g is not None and int(g.id) == self._active_guild_id:
                self._presence_dirty_guilds.add(int(g.id))
            uid = _safe_int(getattr(after, "id", 0))
            if uid:
                raw_st = getattr(after, "status", "offline")
                st = str(getattr(raw_st, "value", raw_st) or "offline").strip().lower()
                if st not in _STATUS_ORDER:
                    st = "offline"
                self.emit_event("presence", {"user_id": str(uid), "status": st})
            self._sync_member_presence_share(g, after, emit_snapshots=True)

        @client.event
        async def on_member_join(member: Any) -> None:
            g = getattr(member, "guild", None)
            if g is not None and int(g.id) == self._active_guild_id:
                self._presence_dirty_guilds.add(int(g.id))

        @client.event
        async def on_member_remove(member: Any) -> None:
            g = getattr(member, "guild", None)
            if g is not None and int(g.id) == self._active_guild_id:
                self._presence_dirty_guilds.add(int(g.id))

        @client.event
        async def on_member_update(_before: Any, after: Any) -> None:
            g = getattr(after, "guild", None)
            if g is not None and int(g.id) == self._active_guild_id:
                self._presence_dirty_guilds.add(int(g.id))

        @client.event
        async def on_message(message: Any) -> None:
            bot_id = int(client.user.id) if client.user else 0
            payload = _serialize_chat_message(message, bot_id=bot_id)
            guild = getattr(message, "guild", None)
            ch = getattr(message, "channel", None)
            ch_id = _safe_int(getattr(ch, "id", 0))
            if guild is not None:
                ss_urls = _extract_screen_share_urls(payload.get("content") or "")
                if ss_urls is not None:
                    watch_u, stream_u = ss_urls
                    self.emit_event(
                        "screen_share_detected",
                        {
                            "guild_id": str(guild.id),
                            "guild_name": str(getattr(guild, "name", "") or ""),
                            "channel_id": str(ch_id),
                            "channel_name": str(getattr(ch, "name", "") or "canal"),
                            "message_id": str(payload.get("id") or ""),
                            "author_id": str(payload.get("author_id") or ""),
                            "author_name": str(payload.get("author_name") or "Usuario"),
                            "author_avatar_url": str(payload.get("author_avatar_url") or ""),
                            "watch_url": watch_u,
                            "stream_url": stream_u,
                            "content": str(payload.get("content") or ""),
                            "is_self": bool(payload.get("is_self", False)),
                        },
                    )
                if self._active_chat_kind == "channel" and ch_id == self._active_chat_id:
                    self.emit_event(
                        "chat_message",
                        {
                            "kind": "channel",
                            "target_id": str(ch_id),
                            "guild_id": str(guild.id),
                            "message": payload,
                        },
                    )
                if not payload["is_self"] and ch_id in self._notify_channel_ids:
                    preview = payload["content"] or (
                        f"📎 {payload['attachments'][0]['name']}" if payload["attachments"] else ""
                    )
                    self.emit_event(
                        "channel_message_incoming",
                        {
                            "guild_id": str(guild.id),
                            "guild_name": str(getattr(guild, "name", "") or ""),
                            "channel_id": str(ch_id),
                            "channel_name": str(getattr(ch, "name", "") or "canal"),
                            "author_name": payload["author_name"],
                            "author_avatar_url": str(payload.get("author_avatar_url") or ""),
                            "preview": preview[:80],
                        },
                    )
                return

            peer_id = 0
            recip = getattr(ch, "recipient", None)
            if recip is not None:
                peer_id = _safe_int(getattr(recip, "id", 0))
            elif not payload["is_self"]:
                peer_id = _safe_int(payload["author_id"])
            else:
                for uid_k, mapped_cid in self._dm_channels.items():
                    if mapped_cid == ch_id:
                        peer_id = uid_k
                        break
            if not peer_id:
                return
            if recip is None:
                try:
                    recip = client.get_user(peer_id)
                except Exception:
                    recip = None
            self._dm_channels[peer_id] = ch_id
            peer_name = str(
                getattr(recip, "display_name", "")
                or getattr(recip, "name", "")
                or ("" if payload["is_self"] else payload["author_name"])
                or ""
            )
            peer_avatar = _member_avatar_url(recip) if recip is not None else ""
            ss_urls = _extract_screen_share_urls(payload.get("content") or "")
            if ss_urls is not None:
                watch_u, stream_u = ss_urls
                self.emit_event(
                    "screen_share_detected",
                    {
                        "guild_id": "",
                        "guild_name": "",
                        "channel_id": str(ch_id),
                        "channel_name": f"DM · {peer_name}",
                        "message_id": str(payload.get("id") or ""),
                        "author_id": str(payload.get("author_id") or ""),
                        "author_name": str(payload.get("author_name") or peer_name),
                        "author_avatar_url": str(
                            payload.get("author_avatar_url") or peer_avatar or ""
                        ),
                        "watch_url": watch_u,
                        "stream_url": stream_u,
                        "content": str(payload.get("content") or ""),
                        "is_self": bool(payload.get("is_self", False)),
                        "is_dm": True,
                        "user_id": str(peer_id),
                    },
                )
            if self._active_chat_kind == "dm" and peer_id == self._active_chat_id:
                self.emit_event(
                    "chat_message",
                    {
                        "kind": "dm",
                        "target_id": str(peer_id),
                        "peer_name": peer_name,
                        "peer_avatar_url": peer_avatar,
                        "message": payload,
                    },
                )
            if not payload["is_self"]:
                preview = payload["content"] or (
                    f"📎 {payload['attachments'][0]['name']}" if payload["attachments"] else ""
                )
                self.emit_event(
                    "dm_incoming",
                    {
                        "user_id": str(peer_id),
                        "author_name": payload["author_name"],
                        "author_avatar_url": str(
                            payload.get("author_avatar_url") or peer_avatar or ""
                        ),
                        "peer_name": peer_name,
                        "peer_avatar_url": peer_avatar,
                        "preview": preview[:80],
                        "message": payload,
                    },
                )

        @client.event
        async def on_raw_message_edit(raw_payload: Any) -> None:
            raw_data = getattr(raw_payload, "data", None)
            if not isinstance(raw_data, dict) or "content" not in raw_data:
                return
            ch_id = _safe_int(getattr(raw_payload, "channel_id", 0))
            msg_id = str(getattr(raw_payload, "message_id", "") or "")
            if not ch_id or not msg_id:
                return
            new_content = str(raw_data.get("content") or "")[:2000]
            guild_id = _safe_int(getattr(raw_payload, "guild_id", 0))
            if _SCREEN_SHARE_ENDED_MARKER.lower() in new_content.lower():
                self.emit_event(
                    "screen_share_ended",
                    {
                        "guild_id": str(guild_id) if guild_id else "",
                        "channel_id": str(ch_id),
                        "message_id": msg_id,
                    },
                )
            if guild_id:
                kind = "channel"
                tid = ch_id
                if self._active_chat_kind != "channel" or tid != self._active_chat_id:
                    return
            else:
                kind = "dm"
                tid = 0
                for uid_k, mapped_cid in self._dm_channels.items():
                    if mapped_cid == ch_id:
                        tid = uid_k
                        break
                if not tid:
                    recip = getattr(client.get_channel(ch_id), "recipient", None)
                    tid = _safe_int(getattr(recip, "id", 0))
                    if tid:
                        self._dm_channels[tid] = ch_id
                if not tid:
                    return
            self.emit_event(
                "chat_message_edited",
                {
                    "kind": kind,
                    "target_id": str(tid),
                    "message_id": msg_id,
                    "content": new_content,
                    "edited": True,
                },
            )

    async def _connect_bot(self) -> None:
        import discord
        from discord.errors import ConnectionClosed
        from swiftplay.security import load_discord_token_decrypted

        token = load_discord_token_decrypted()
        if not token:
            self._state = "error"
            self.emit_event("state", {"state": "error", "message": "No hay Bot Token guardado en DPAPI."})
            return

        if self._client is not None:
            await self._disconnect_bot()

        self._manual_disconnect = False
        self._initial_ready_done = False
        self._gw_backoff = 2.0
        self._state = "connecting"
        self.emit_event("state", {"state": "connecting"})
        self._voice_cls = _make_voice_client_cls(on_init=self._wire_voice_client)
        fatal_gateway_codes = {4004, 4010, 4011, 4012, 4013, 4014}

        async def _runner() -> None:
            try:
                while not self._manual_disconnect and (
                    self._stop_event is None or not self._stop_event.is_set()
                ):
                    self._privileged_intents_ok = True
                    client = self._build_client(True)
                    self._client = client
                    self._attach_client_events(client)
                    fatal_err: str | None = None
                    try:
                        try:
                            await client.start(token, reconnect=True)
                        except discord.PrivilegedIntentsRequired:
                            self._privileged_intents_ok = False
                            try:
                                await client.close()
                            except Exception:
                                pass
                            self.emit_event(
                                "warning",
                                {
                                    "message": (
                                        "Activa 'Presence Intent', 'Server Members Intent' y 'Message Content Intent' "
                                        "en Discord Developer Portal para ver estados y chat completo. Conectando en modo Voz..."
                                    )
                                },
                            )
                            client2 = self._build_client(False)
                            self._client = client2
                            self._attach_client_events(client2)
                            await client2.start(token, reconnect=True)
                    except discord.LoginFailure:
                        self._write_voice_diag("GATEWAY_LOGIN_FAIL")
                        fatal_err = "Bot Token inválido o revocado en Discord Developer Portal."
                    except asyncio.CancelledError:
                        raise
                    except ConnectionClosed as exc:
                        code = int(getattr(exc, "code", 0) or 0)
                        reason = str(getattr(exc, "reason", "") or "")
                        self._write_voice_diag(f"GATEWAY_CLOSED code={code} reason={reason}")
                        if code in fatal_gateway_codes:
                            fatal_err = f"Discord cerró el Gateway (código {code})."
                    except Exception as exc:
                        self._write_voice_diag(f"GATEWAY_ERROR {type(exc).__name__}: {exc}")

                    if fatal_err is not None:
                        self._state = "error"
                        self.emit_event("state", {"state": "error", "message": fatal_err})
                        break

                    if self._manual_disconnect or (
                        self._stop_event is not None and self._stop_event.is_set()
                    ):
                        break

                    saved_voice = self._desired_voice_target
                    saved_meta = self._desired_voice_meta
                    dead_client = self._client
                    self._client = None
                    await self._leave_channel_internal(trim_memory=False, clear_desired=False)
                    self._desired_voice_target = saved_voice
                    self._desired_voice_meta = saved_meta
                    if dead_client is not None:
                        try:
                            await asyncio.wait_for(dead_client.close(), timeout=2.0)
                        except Exception:
                            pass

                    self._state = "connecting"
                    self.emit_event("state", {"state": "connecting", "reconnecting": True})
                    self.emit_event(
                        "warning",
                        {"message": "Conexión con Discord interrumpida. Reconectando..."},
                    )
                    wait_s = min(30.0, max(2.0, float(self._gw_backoff)))
                    self._gw_backoff = min(30.0, wait_s * 2.0)
                    await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                pass
            finally:
                await self._leave_channel_internal(clear_desired=True)

        self._bot_task = asyncio.create_task(_runner())

    def _stop_voice_audio_only(self) -> None:
        if self._audio is not None:
            self._audio.stop()
        self._mixer.clear()
        with self._speaking_lock:
            self._speaking_timestamps.clear()
        self._emit_speaking_snapshot()

    async def _cancel_inflight_join(self) -> None:
        pj = self._post_join_task
        self._post_join_task = None
        if pj is not None and not pj.done() and pj is not asyncio.current_task():
            pj.cancel()
            try:
                await pj
            except (asyncio.CancelledError, Exception):
                pass
        task = self._join_task
        self._join_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._joining_channel_id = ""

    async def _verify_voice_drop_after_grace(self, vc_ref: Any) -> None:
        if vc_ref is None or self._manual_disconnect or self._desired_voice_target is None:
            return
        self._voice_reconnecting = True
        self.emit_event("voice_state", self._build_voice_state_dict())

        t0 = time.monotonic()
        try:
            while (time.monotonic() - t0) < 12.0:
                await asyncio.sleep(0.25)
                if (
                    self._manual_disconnect
                    or self._joining_channel_id
                    or self._desired_voice_target is None
                    or self._vc is not vc_ref
                ):
                    return
                if vc_ref.is_connected():
                    self._voice_reconnecting = False
                    self._vc_disconnected_since = 0.0
                    self._voice_rejoin_attempts = 0
                    try:
                        sr = getattr(getattr(vc_ref, "_connection", None), "_socket_reader", None)
                        if sr is not None:
                            sr.resume(force=True)
                    except Exception:
                        pass
                    self._write_voice_diag("VOICE_RECOVERED")
                    asyncio.create_task(
                        vc_ref.kickstart_voice(keep_speaking=not self._muted)
                    )
                    self._emit_snapshots()
                    return
                conn = getattr(vc_ref, "_connection", None)
                runner = getattr(conn, "_runner", None) if conn is not None else None
                connector = getattr(conn, "_connector", None) if conn is not None else None
                runner_alive = (runner is not None and not runner.done()) or (
                    connector is not None and not connector.done()
                )
                if not runner_alive and (time.monotonic() - t0) >= 1.2:
                    break
        except asyncio.CancelledError:
            return

        if (
            self._manual_disconnect
            or self._joining_channel_id
            or self._vc is not vc_ref
            or self._desired_voice_target is None
        ):
            return

        conn = getattr(vc_ref, "_connection", None)
        code = int(getattr(conn, "last_close_code", 0) or 0)
        expecting = bool(getattr(conn, "_expecting_disconnect", False))
        gid = self._desired_voice_target[0]
        guild = self._client.get_guild(gid) if self._client is not None else None
        me = getattr(guild, "me", None) if guild is not None else None
        me_vs = getattr(me, "voice", None) if me is not None else None
        me_in_voice = bool(me_vs is not None and getattr(me_vs, "channel", None) is not None)

        if code in (4014, 4022) and not expecting and not me_in_voice:
            self._write_voice_diag(f"VOICE_EXTERNAL_KICK code={code}")
            self._desired_voice_target = None
            self._desired_voice_meta = ("", "")
            self._voice_reconnecting = False
            self.emit_event(
                "warning", {"message": "Fuiste desconectado del canal de voz."}
            )
            await self._leave_channel_internal(clear_desired=True)
            return

        self._write_voice_diag(f"VOICE_DROP_REJOIN code={code} expecting={expecting}")
        await self._schedule_auto_rejoin(f"voice_drop_{code}")

    async def _schedule_auto_rejoin(self, reason: str) -> None:
        if (
            self._manual_disconnect
            or self._desired_voice_target is None
            or self._client is None
            or not self._client.is_ready()
            or self._joining_channel_id
        ):
            return

        if self._vc is not None and self._vc.is_connected():
            self._voice_reconnecting = False
            self._voice_rejoin_attempts = 0
            self._vc_disconnected_since = 0.0
            self._emit_snapshots()
            return

        while (
            self._voice_rejoin_attempts < 3
            and not self._manual_disconnect
            and self._desired_voice_target is not None
            and self._client is not None
            and self._client.is_ready()
            and not self._joining_channel_id
        ):
            self._voice_rejoin_attempts += 1
            attempt = self._voice_rejoin_attempts
            delay = (1.0, 2.5, 5.0)[min(attempt - 1, 2)]
            if attempt == 1:
                self.emit_event(
                    "warning", {"message": "Reconectando al canal de voz..."}
                )
            await asyncio.sleep(delay)
            if (
                self._manual_disconnect
                or self._desired_voice_target is None
                or self._client is None
                or not self._client.is_ready()
                or self._joining_channel_id
            ):
                return
            if self._vc is not None and self._vc.is_connected():
                self._voice_reconnecting = False
                self._voice_rejoin_attempts = 0
                self._vc_disconnected_since = 0.0
                self._emit_snapshots()
                return

            gid, cid = self._desired_voice_target
            self._write_voice_diag(
                f"VOICE_AUTO_REJOIN attempt={attempt} reason={reason} ch={cid}"
            )
            try:
                await self._join_channel(gid, cid, is_rejoin=True)
                self._voice_reconnecting = False
                self._voice_rejoin_attempts = 0
                self._vc_disconnected_since = 0.0
                self._write_voice_diag("VOICE_AUTO_REJOIN_OK")
                return
            except Exception as exc:
                self._write_voice_diag(
                    f"VOICE_AUTO_REJOIN_FAIL attempt={attempt} err={exc}"
                )

        if self._voice_rejoin_attempts >= 3 and self._desired_voice_target is not None:
            self._write_voice_diag(f"VOICE_REJOIN_EXHAUSTED reason={reason}")
            self._desired_voice_target = None
            self._desired_voice_meta = ("", "")
            self._voice_reconnecting = False
            self.emit_event(
                "warning",
                {"message": "Se perdió la conexión de voz tras varios reintentos."},
            )
            await self._leave_channel_internal(clear_desired=True)

    async def _leave_channel_internal(
        self, *, trim_memory: bool = True, clear_desired: bool = True
    ) -> None:
        dt = self._voice_drop_task
        self._voice_drop_task = None
        if dt is not None and not dt.done() and dt is not asyncio.current_task():
            dt.cancel()
            try:
                await dt
            except (asyncio.CancelledError, Exception):
                pass
        pj = self._post_join_task
        self._post_join_task = None
        if pj is not None and not pj.done() and pj is not asyncio.current_task():
            pj.cancel()
            try:
                await pj
            except (asyncio.CancelledError, Exception):
                pass
        self._mixer.clear()
        with self._speaking_lock:
            self._speaking_timestamps.clear()
        self._emit_speaking_snapshot()
        audio = self._audio
        if audio is not None:
            try:
                if audio._running:
                    await asyncio.to_thread(audio.stop)
                else:
                    audio.stop()
            except Exception:
                pass
        candidates: list[Any] = []
        if self._vc is not None:
            candidates.append(self._vc)
            self._vc = None
        if self._client is not None:
            for existing in list(getattr(self._client, "voice_clients", []) or []):
                if existing not in candidates:
                    candidates.append(existing)
        for vc in candidates:
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=2.5)
            except Exception:
                pass
        self._joining_channel_id = ""
        self._connecting_vc = None
        self._vc_disconnected_since = 0.0
        self._voice_reconnecting = False
        if clear_desired:
            self._desired_voice_target = None
            self._desired_voice_meta = ("", "")
            self._voice_rejoin_attempts = 0
        self._dave_active = False
        self._dave_code = ""
        self._mic_test_active = False
        self._mic_test_saved_flags = None
        if self._audio is not None:
            self._audio.configure(loopback=False)
            self._audio.release_dsp_idle()
        if trim_memory:
            try:
                from swiftplay.windows_os import trim_process_memory

                trim_process_memory(collect_gc=True)
            except Exception:
                pass
        self._emit_snapshots()

    async def _disconnect_bot(self) -> None:
        self._manual_disconnect = True
        self._desired_voice_target = None
        self._desired_voice_meta = ("", "")
        self._voice_reconnecting = False
        self._voice_rejoin_attempts = 0
        await self._cancel_inflight_join()
        await self._leave_channel_internal(clear_desired=True)
        if self._audio is not None:
            self._audio.close()
        client = self._client
        if client is not None and self._local_share_presence is not None:
            try:
                import discord

                await asyncio.wait_for(
                    client.change_presence(status=discord.Status.online, activity=None),
                    timeout=1.0,
                )
            except Exception:
                pass
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        if self._bot_task is not None and not self._bot_task.done():
            self._bot_task.cancel()
            try:
                await self._bot_task
            except Exception:
                pass
        self._bot_task = None
        self._privileged_intents_ok = False
        self._active_guild_id = 0
        self._active_chat_kind = ""
        self._active_chat_id = 0
        self._dm_channels.clear()
        self._presence_dirty_guilds.clear()
        self._chunked_guilds.clear()
        self._presence_shares.clear()
        self._local_share_presence = None
        self._state = "disconnected"
        self.emit_event("state", {"state": "disconnected"})

    async def _select_guild(self, guild_id: int) -> None:
        gid = _safe_int(guild_id)
        self._active_guild_id = gid
        self._presence_dirty_guilds.discard(gid)
        client = self._client
        if client is None or not client.is_ready() or not gid:
            return
        guild = client.get_guild(gid)
        if guild is None:
            return
        if self._privileged_intents_ok and gid not in self._chunked_guilds:
            try:
                await asyncio.wait_for(guild.chunk(cache=True), timeout=6.0)
                self._chunked_guilds.add(gid)
            except Exception:
                pass
        self._scan_guild_presence_shares(guild, emit_snapshots=True)
        self._emit_guild_members(gid)

    async def _open_text_channel(self, guild_id: int, channel_id: int, limit: int = 50) -> None:
        client = self._client
        if client is None or not client.is_ready():
            raise RuntimeError("El bot no está conectado a Discord.")
        gid = _safe_int(guild_id)
        cid = _safe_int(channel_id)
        guild = client.get_guild(gid)
        if guild is None:
            raise RuntimeError("Servidor no encontrado.")
        ch = guild.get_channel(cid)
        if ch is None:
            raise RuntimeError("Canal de texto no encontrado.")

        perms = ch.permissions_for(guild.me) if guild.me is not None else None
        can_read = bool(getattr(perms, "read_message_history", True)) if perms is not None else True
        can_send = bool(getattr(perms, "send_messages", True)) if perms is not None else True
        can_attach = (
            bool(can_send and getattr(perms, "attach_files", True))
            if perms is not None
            else can_send
        )

        self._active_chat_kind = "channel"
        self._active_chat_id = cid
        bot_id = int(client.user.id) if client.user else 0
        msgs: list[dict[str, Any]] = []
        if can_read:
            lim = max(1, min(80, int(limit)))
            try:
                async for m in ch.history(limit=lim):
                    msgs.append(_serialize_chat_message(m, bot_id=bot_id))
                msgs.reverse()
            except Exception:
                pass
        ch_online, ch_members = _serialize_guild_members(guild, limit=200, channel=ch)
        self.emit_event(
            "chat_history",
            {
                "kind": "channel",
                "target_id": str(cid),
                "guild_id": str(gid),
                "title": f"#{ch.name}",
                "can_read": can_read,
                "can_send": can_send,
                "can_attach": can_attach,
                "messages": msgs,
                "channel_members": ch_members,
                "channel_online_count": ch_online,
            },
        )

    async def _open_dm(self, user_id: int, limit: int = 50) -> None:
        client = self._client
        if client is None or not client.is_ready():
            raise RuntimeError("El bot no está conectado a Discord.")
        uid = _safe_int(user_id)
        if not uid:
            raise RuntimeError("Usuario inválido para DM.")

        user = client.get_user(uid)
        if user is None:
            for g in client.guilds:
                m = g.get_member(uid)
                if m is not None:
                    user = m
                    break
        if user is None:
            user = await client.fetch_user(uid)
        if user is None:
            raise RuntimeError("No se encontró al usuario.")
        if bool(getattr(user, "bot", False)):
            raise RuntimeError("No se puede abrir DM con otro bot.")

        dm = getattr(user, "dm_channel", None)
        if dm is None:
            dm = await user.create_dm()
        self._dm_channels[uid] = int(dm.id)
        self._active_chat_kind = "dm"
        self._active_chat_id = uid

        bot_id = int(client.user.id) if client.user else 0
        msgs: list[dict[str, Any]] = []
        lim = max(1, min(80, int(limit)))
        try:
            async for m in dm.history(limit=lim):
                msgs.append(_serialize_chat_message(m, bot_id=bot_id))
            msgs.reverse()
        except Exception:
            pass

        display = str(getattr(user, "display_name", "") or getattr(user, "name", "") or uid)
        self.emit_event(
            "chat_history",
            {
                "kind": "dm",
                "target_id": str(uid),
                "title": f"DM · {display}",
                "peer_name": display,
                "peer_avatar_url": _member_avatar_url(user),
                "can_read": True,
                "can_send": True,
                "can_attach": True,
                "messages": msgs,
            },
        )

    async def _send_chat_message(
        self,
        kind: str,
        target_id: int,
        content: str,
        file_paths: list[str] | None = None,
        cleanup_paths: list[str] | None = None,
        reply_to_id: int = 0,
    ) -> None:
        from pathlib import Path
        import discord
        from swiftplay.paths import USER_DATA_DIR

        client = self._client
        if client is None or not client.is_ready():
            raise RuntimeError("El bot no está conectado a Discord.")
        text = str(content or "").strip()
        if len(text) > 2000:
            text = text[:2000]

        valid_paths = [
            str(p) for p in (file_paths or []) if str(p or "").strip() and os.path.isfile(str(p))
        ][:10]
        if not text and not valid_paths:
            return

        max_bytes = 25 * 1024 * 1024
        for fp in valid_paths:
            if os.path.getsize(fp) > max_bytes:
                raise RuntimeError(
                    f"El archivo '{os.path.basename(fp)}' supera el límite de 25 MB."
                )

        tid = _safe_int(target_id)
        if kind == "dm":
            dm_cid = self._dm_channels.get(tid)
            ch = client.get_channel(dm_cid) if dm_cid else None
            if ch is None:
                user = client.get_user(tid) or await client.fetch_user(tid)
                ch = getattr(user, "dm_channel", None) or await user.create_dm()
                self._dm_channels[tid] = int(ch.id)
        else:
            ch = client.get_channel(tid)
            if ch is None:
                for g in client.guilds:
                    ch = g.get_channel(tid)
                    if ch is not None:
                        break
        if ch is None:
            raise RuntimeError("Canal de chat no disponible.")

        discord_files: list[Any] = []
        try:
            for fp in valid_paths:
                discord_files.append(discord.File(fp, filename=os.path.basename(fp)))
            send_kwargs: dict[str, Any] = {}
            if reply_to_id > 0:
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=int(reply_to_id),
                    channel_id=int(ch.id),
                    fail_if_not_exists=False,
                )
                send_kwargs["mention_author"] = False
            if discord_files:
                sent_msg = await ch.send(content=text or None, files=discord_files, **send_kwargs)
            else:
                sent_msg = await ch.send(text, **send_kwargs)
            return {
                "message_id": str(getattr(sent_msg, "id", "") or ""),
                "channel_id": str(getattr(ch, "id", "") or ""),
            }
        except discord.Forbidden as exc:
            raise RuntimeError(
                "El bot no tiene permiso para enviar mensajes o adjuntar archivos aquí."
            ) from exc
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 413:
                raise RuntimeError(
                    "El archivo supera el límite permitido por Discord (10 MB en servidores sin boost)."
                ) from exc
            raise
        finally:
            for df in discord_files:
                try:
                    df.close()
                except Exception:
                    pass
            tmp_dir = (USER_DATA_DIR / "tmp_uploads").resolve()
            for cp in cleanup_paths or []:
                try:
                    resolved = Path(str(cp)).resolve()
                    if resolved.is_relative_to(tmp_dir) and resolved.is_file():
                        resolved.unlink(missing_ok=True)
                except Exception:
                    pass

    async def _edit_chat_message(
        self,
        kind: str,
        target_id: int,
        message_id: int,
        content: str,
    ) -> None:
        import discord

        client = self._client
        if client is None or not client.is_ready():
            raise RuntimeError("El bot no está conectado a Discord.")
        text = str(content or "").strip()
        if not text:
            raise RuntimeError("El mensaje editado no puede quedar vacío.")
        if len(text) > 2000:
            text = text[:2000]
        mid = _safe_int(message_id)
        if not mid:
            raise RuntimeError("Mensaje inválido para editar.")

        tid = _safe_int(target_id)
        if kind == "dm":
            dm_cid = self._dm_channels.get(tid)
            ch = client.get_channel(dm_cid) if dm_cid else None
            if ch is None:
                user = client.get_user(tid) or await client.fetch_user(tid)
                ch = getattr(user, "dm_channel", None) or await user.create_dm()
                self._dm_channels[tid] = int(ch.id)
        else:
            ch = client.get_channel(tid)
            if ch is None:
                for g in client.guilds:
                    ch = g.get_channel(tid)
                    if ch is not None:
                        break
        if ch is None:
            raise RuntimeError("Canal de chat no disponible.")

        try:
            edited_msg = await ch.get_partial_message(mid).edit(content=text)
        except discord.Forbidden as exc:
            raise RuntimeError("Solo se pueden editar mensajes enviados por el propio bot.") from exc
        except discord.NotFound as exc:
            raise RuntimeError("El mensaje ya no existe en Discord.") from exc

        bot_id = int(client.user.id) if client.user else 0
        payload = _serialize_chat_message(edited_msg, bot_id=bot_id)
        payload["edited"] = True
        self.emit_event(
            "chat_message_edited",
            {
                "kind": str(kind),
                "target_id": str(tid),
                "message_id": str(mid),
                "content": payload["content"],
                "edited": True,
                "message": payload,
            },
        )

    def _wire_voice_client(self, vc: Any) -> None:
        import discord.opus

        self._connecting_vc = vc
        vc.deafened = self._deafened
        vc.on_opus = lambda uid, seq, pkt, _dec=discord.opus.Decoder: self._mixer.push_opus(
            uid, seq, pkt, decoder_factory=_dec
        )
        vc.on_pcm = self._mixer.push_pcm
        vc.on_user_removed = self._mixer.remove_user
        vc.on_speaking_change = self._on_remote_speaking
        vc.on_dave_change = self._on_dave_change
        vc.on_warning = lambda msg: self.emit_event("warning", {"message": msg})

    @staticmethod
    def _write_voice_diag(line: str) -> None:
        try:
            from swiftplay.paths import LOGS_DIR

            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOGS_DIR / "discord_voice_diag.log"
            if log_path.exists() and log_path.stat().st_size > 200_000:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
                log_path.write_text("\n".join(tail) + "\n", encoding="utf-8")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] {line}\n")
        except Exception:
            pass

    @staticmethod
    def _format_voice_diag(vc: Any, tot_ms: int, *, audio_ms: int | None = None) -> str:
        conn = getattr(vc, "_connection", None)
        tm = getattr(conn, "connect_timings", None) if conn is not None else None
        parts: list[str] = []
        if conn is not None and getattr(conn, "state", None) is not None:
            st_name = getattr(conn.state, "name", str(conn.state))
            if st_name and st_name != "connected":
                parts.append(f"state={st_name}")
        if isinstance(tm, dict):
            op4 = int(tm.get("op4_ms") or 0)
            ws = int(tm.get("ws_ms") or 0)
            ip = int(tm.get("ip_ms") or 0)
            key = int(tm.get("key_ms") or 0)
            att = int(tm.get("attempts") or 1)
            codes = tm.get("close_codes") or []
            parts.append(f"op4 {op4}ms · ws {ws}ms · udp {ip}ms · key {key}ms")
            if att > 1:
                parts.append(f"intentos={att}")
            if codes:
                parts.append(f"codes={codes}")
        if audio_ms is not None:
            parts.append(f"audio {audio_ms}ms")
        detail = " · ".join(parts)
        return f"Conexión: {tot_ms} ms ({detail})" if detail else f"Conexión: {tot_ms} ms"

    async def _finish_post_join(
        self,
        vc: Any,
        t_start: float,
    ) -> None:
        audio_ms: int | None = None
        try:
            engine = self._ensure_audio_engine()
            engine.configure(muted=self._muted, loopback=self._mic_test_active)
            try:
                if not engine._running:
                    await asyncio.to_thread(engine.start)
                audio_ms = max(0, int((time.perf_counter() - t_start) * 1000))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.emit_event("warning", {"message": f"Audio inicializado con aviso: {exc}"})

            if self._vc is not vc or not vc.is_connected():
                return

            engine.warmup_async()
            prev_dave = (self._dave_active, self._dave_code)
            keep_speaking = bool(
                not self._muted and (engine.is_speaking or engine.is_open_mic)
            )
            await vc.kickstart_voice(keep_speaking=keep_speaking)
            if self._vc is not vc or not vc.is_connected():
                return

            tot_ms = max(0, int((time.perf_counter() - t_start) * 1000))
            self._last_join_diag = self._format_voice_diag(vc, tot_ms, audio_ms=audio_ms)
            self._write_voice_diag(self._last_join_diag)
            cur_dave = (bool(vc._connection.can_encrypt), str(vc.voice_privacy_code or ""))
            if cur_dave != prev_dave:
                self._dave_active, self._dave_code = cur_dave
                if self._dave_active:
                    self._dave_was_active = True
            self._emit_snapshots()
        except asyncio.CancelledError:
            return
        except Exception:
            pass

    async def _set_mic_test(self, enabled: bool) -> None:
        want = bool(enabled)
        if want:
            if not self._mic_test_active:
                self._mic_test_saved_flags = (self._muted, self._deafened)
                self._mic_test_active = True
            self._muted = True
            self._deafened = True
            self._mixer.set_deafened(True)
            engine = self._ensure_audio_engine()
            engine.configure(muted=True, loopback=True)
            engine.warmup_async()
            if not engine._running:
                await asyncio.to_thread(engine.start)
            await self._sync_voice_flags()
            return

        if not self._mic_test_active:
            return
        self._mic_test_active = False
        prev_m, prev_d = self._mic_test_saved_flags or (False, False)
        self._mic_test_saved_flags = None
        self._muted = bool(prev_m)
        self._deafened = bool(prev_d)
        self._mixer.set_deafened(self._deafened)
        if self._audio is not None:
            self._audio.configure(muted=self._muted, loopback=False)
            if (self._vc is None or not self._vc.is_connected()) and self._audio._running:
                await asyncio.to_thread(self._audio.stop)
                self._audio.release_dsp_idle()
        await self._sync_voice_flags()

    async def _join_channel(
        self, guild_id: int, channel_id: int, *, is_rejoin: bool = False
    ) -> None:
        async with self._voice_lock:
            if self._client is None or not self._client.is_ready():
                raise RuntimeError("El bot aún no está conectado a Discord.")
            guild = self._client.get_guild(guild_id)
            if guild is None:
                raise RuntimeError("Servidor no encontrado para este bot.")
            channel = guild.get_channel(channel_id)
            if channel is None:
                raise RuntimeError("Canal de voz no encontrado.")

            if guild.me is not None:
                perms = channel.permissions_for(guild.me)
                if not perms.connect:
                    raise RuntimeError(
                        f"El bot no tiene permiso 'Conectar' en '{channel.name}'. "
                        "Ajusta el rol o los permisos del canal en Discord."
                    )
                if not perms.speak:
                    self.emit_event(
                        "warning",
                        {
                            "message": (
                                f"Conectando a '{channel.name}' en modo solo escucha "
                                "(el bot no tiene permiso 'Hablar')."
                            )
                        },
                    )

            self._desired_voice_target = (int(guild_id), int(channel_id))
            self._desired_voice_meta = (
                str(getattr(guild, "name", "") or ""),
                str(getattr(channel, "name", "") or ""),
            )
            if not is_rejoin:
                self._voice_rejoin_attempts = 0
                self._voice_reconnecting = False
            self._vc_disconnected_since = 0.0

            if self._mic_test_active:
                self._mic_test_active = False
                prev_m, prev_d = self._mic_test_saved_flags or (False, False)
                self._mic_test_saved_flags = None
                self._muted = bool(prev_m)
                self._deafened = bool(prev_d)
                self._mixer.set_deafened(self._deafened)
                if self._audio is not None:
                    self._audio.configure(muted=self._muted, loopback=False)

            existing_vc = self._vc or guild.voice_client
            t_start = time.perf_counter()
            if (
                existing_vc is not None
                and self._voice_cls is not None
                and isinstance(existing_vc, self._voice_cls)
                and existing_vc.is_connected()
                and existing_vc.channel is not None
                and existing_vc.channel.guild.id == guild_id
            ):
                self._vc = existing_vc
                conn_ex = getattr(existing_vc, "_connection", None)
                if conn_ex is not None:
                    conn_ex.timeout = 25.0
                if existing_vc.channel.id == channel_id:
                    engine = self._ensure_audio_engine()
                    engine.configure(muted=self._muted, loopback=False)
                    engine.warmup_async()
                    if not engine._running:
                        await asyncio.to_thread(engine.start)
                    self._last_join_diag = "Conexión: ya activo en sala"
                    self._voice_reconnecting = False
                    self._emit_snapshots()
                    return
                self._joining_channel_id = str(channel_id)
                self._emit_snapshots()
                try:
                    await existing_vc.move_to(channel, timeout=10.0)
                    t_end = time.perf_counter()
                    self._last_join_diag = f"Cambio de sala: {int((t_end - t_start) * 1000)} ms"
                    self._write_voice_diag(self._last_join_diag)
                finally:
                    self._joining_channel_id = ""
                if conn_ex is not None:
                    conn_ex.timeout = 25.0
                self._mixer.clear()
                already_dave = bool(existing_vc._connection.can_encrypt)
                self._dave_was_active = already_dave
                self._dave_active = already_dave
                self._dave_code = str(existing_vc.voice_privacy_code or "")
                self._voice_reconnecting = False
                self._vc_disconnected_since = 0.0
                self._emit_snapshots()
                self._post_join_task = asyncio.create_task(
                    self._finish_post_join(existing_vc, t_start)
                )
                return

            await self._leave_channel_internal(trim_memory=False, clear_desired=False)
            if (
                guild.me is not None
                and getattr(guild.me, "voice", None) is not None
                and getattr(guild.me.voice, "channel", None) is not None
                and guild.voice_client is None
            ):
                try:
                    await guild.change_voice_state(channel=None)
                except Exception:
                    pass
                deadline_clr = time.monotonic() + 1.2
                while (
                    guild.me is not None
                    and getattr(guild.me, "voice", None) is not None
                    and getattr(guild.me.voice, "channel", None) is not None
                    and time.monotonic() < deadline_clr
                ):
                    await asyncio.sleep(0.05)

            self._joining_channel_id = str(channel_id)
            self._emit_snapshots()
            self._connecting_vc = None
            vc: Any = None
            try:
                for attempt_idx, (inner_to, outer_to) in enumerate(((7.0, 9.0), (8.0, 10.0))):
                    try:
                        vc = await asyncio.wait_for(
                            channel.connect(
                                cls=self._voice_cls,
                                self_mute=self._muted,
                                self_deaf=self._deafened,
                                timeout=inner_to,
                            ),
                            timeout=outer_to,
                        )
                        break
                    except asyncio.TimeoutError as exc:
                        pending_vc = self._connecting_vc or guild.voice_client
                        fail_diag = self._format_voice_diag(
                            pending_vc, max(0, int((time.perf_counter() - t_start) * 1000))
                        )
                        if attempt_idx == 0:
                            self._write_voice_diag(f"RETRY_1 {fail_diag}")
                            await self._leave_channel_internal(
                                trim_memory=False, clear_desired=False
                            )
                            self._joining_channel_id = str(channel_id)
                            try:
                                await guild.change_voice_state(channel=None)
                            except Exception:
                                pass
                            await asyncio.sleep(0.35)
                            continue
                        self._last_join_diag = fail_diag
                        self._write_voice_diag(f"TIMEOUT {fail_diag}")
                        await self._leave_channel_internal(
                            trim_memory=False, clear_desired=not is_rejoin
                        )
                        raise RuntimeError(
                            f"Tiempo de espera agotado al entrar en '{channel.name}'. "
                            "Revisa permisos del canal o conexión UDP."
                        ) from exc
            except Exception as exc:
                if not isinstance(exc, RuntimeError) or "Tiempo de espera agotado" not in str(exc):
                    pending_vc = self._connecting_vc or guild.voice_client
                    fail_diag = self._format_voice_diag(
                        pending_vc, max(0, int((time.perf_counter() - t_start) * 1000))
                    )
                    self._write_voice_diag(f"ERROR {exc}: {fail_diag}")
                    await self._leave_channel_internal(
                        trim_memory=False, clear_desired=not is_rejoin
                    )
                raise
            finally:
                self._joining_channel_id = ""
                self._connecting_vc = None

            self._wire_voice_client(vc)
            conn = getattr(vc, "_connection", None)
            if conn is not None:
                conn.timeout = 25.0
            self._connecting_vc = None
            self._vc = vc
            self._voice_reconnecting = False
            self._vc_disconnected_since = 0.0
            self._mixer.clear()
            already_dave = bool(vc._connection.can_encrypt)
            self._dave_was_active = already_dave
            self._dave_active = already_dave
            self._dave_code = str(vc.voice_privacy_code or "")
            tot_ms = max(0, int((time.perf_counter() - t_start) * 1000))
            self._last_join_diag = self._format_voice_diag(vc, tot_ms)
            self._emit_snapshots()
            self._post_join_task = asyncio.create_task(
                self._finish_post_join(vc, t_start)
            )

    async def _sync_voice_flags(self) -> None:
        vc = self._vc
        if vc is not None and vc.is_connected() and vc.channel is not None:
            vc.deafened = self._deafened
            try:
                await vc.channel.guild.change_voice_state(
                    channel=vc.channel,
                    self_mute=self._muted,
                    self_deaf=self._deafened,
                )
            except Exception:
                pass
        self._emit_snapshots()

    async def _housekeeping_loop(self) -> None:
        from swiftplay.windows_os import get_process_memory_mb, trim_process_memory

        ram_tick = 0.0
        trim_tick = time.monotonic()
        while self._stop_event is not None and not self._stop_event.is_set():
            await asyncio.sleep(0.25)
            now = time.monotonic()

            # Expirar indicadores de habla remotos tras 450 ms sin paquetes
            stale = []
            with self._speaking_lock:
                for uid, ts in self._speaking_timestamps.items():
                    if uid != 0 and (now - ts) > 0.45:
                        stale.append(uid)
                for uid in stale:
                    self._speaking_timestamps.pop(uid, None)
            self._emit_speaking_snapshot()

            if (
                self._vc is not None
                and not self._joining_channel_id
                and not self._manual_disconnect
            ):
                if self._vc.is_connected():
                    self._vc_disconnected_since = 0.0
                    if self._voice_reconnecting:
                        self._voice_reconnecting = False
                        self._voice_rejoin_attempts = 0
                        try:
                            sr = getattr(
                                getattr(self._vc, "_connection", None), "_socket_reader", None
                            )
                            if sr is not None:
                                sr.resume(force=True)
                        except Exception:
                            pass
                        self._write_voice_diag("VOICE_RECOVERED_WATCHDOG")
                        asyncio.create_task(
                            self._vc.kickstart_voice(keep_speaking=not self._muted)
                        )
                        self.emit_event("voice_state", self._build_voice_state_dict())
                else:
                    if self._vc_disconnected_since <= 0.0:
                        self._vc_disconnected_since = now
                        if self._desired_voice_target is not None and not self._voice_reconnecting:
                            self._voice_reconnecting = True
                            self.emit_event("voice_state", self._build_voice_state_dict())
                    elif (now - self._vc_disconnected_since) >= 1.5 and (
                        self._voice_drop_task is None or self._voice_drop_task.done()
                    ):
                        self._voice_drop_task = asyncio.create_task(
                            self._verify_voice_drop_after_grace(self._vc)
                        )

            if self._vc is not None and self._vc.is_connected():
                new_active = bool(self._vc._connection.can_encrypt)
                new_code = str(self._vc.voice_privacy_code or "")
                if new_active != self._dave_active or new_code != self._dave_code:
                    became_active = new_active and not self._dave_was_active
                    self._dave_active = new_active
                    if new_active:
                        self._dave_was_active = True
                    self._dave_code = new_code
                    self.emit_event("voice_state", self._build_voice_state_dict())
                    if became_active:
                        self._schedule_dave_resync()
                if self._audio is not None and self._audio._running:
                    rec_in, rec_out = self._audio.check_and_recover_streams(now)
                    if rec_in or rec_out:
                        parts = []
                        if rec_in:
                            parts.append("micrófono")
                        if rec_out:
                            parts.append("salida")
                        self.emit_event(
                            "warning",
                            {
                                "message": f"Audio recuperado automáticamente ({', '.join(parts)})."
                            },
                        )

            if (
                self._active_guild_id
                and self._active_guild_id in self._presence_dirty_guilds
                and (now - self._last_presence_emit) >= 0.75
            ):
                self._presence_dirty_guilds.discard(self._active_guild_id)
                self._last_presence_emit = now
                self._emit_guild_members(self._active_guild_id)

            if (now - ram_tick) >= 3.0:
                ram_tick = now
                ws, priv = get_process_memory_mb()
                self.emit_event("worker_ram", {"ws_mb": round(ws, 1), "priv_mb": round(priv, 1)})
                if self._vc is not None and self._vc.is_connected():
                    self.emit_event("voice_state", self._build_voice_state_dict())

            in_call = self._vc is not None and self._vc.is_connected()
            if (now - trim_tick) >= 60.0:
                trim_tick = now
                if not in_call:
                    trim_process_memory(collect_gc=True)

    async def handle_command(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        cmd = str(req.get("cmd") or "")
        try:
            if cmd == "ping":
                if req_id is not None:
                    self.emit_result(req_id, True, "pong")
                return

            if cmd == "list_devices":
                devices = await asyncio.to_thread(list_audio_devices)
                self.emit_event("devices", devices)
                if req_id is not None:
                    self.emit_result(req_id, True, devices)
                return

            if cmd == "connect_bot":
                await self._connect_bot()
            elif cmd == "disconnect_bot":
                await self._disconnect_bot()
            elif cmd == "refresh_guilds":
                self._emit_snapshots()
            elif cmd == "set_notify_channels":
                raw_ids = req.get("channel_ids") or []
                self._notify_channel_ids = {
                    _safe_int(x) for x in raw_ids if _safe_int(x) > 0
                }
            elif cmd == "select_guild":
                await self._select_guild(_safe_int(req.get("guild_id")))
            elif cmd == "open_text_channel":
                await self._open_text_channel(
                    _safe_int(req.get("guild_id")),
                    _safe_int(req.get("channel_id")),
                    limit=_safe_int(req.get("limit", 50)) or 50,
                )
            elif cmd == "open_dm":
                await self._open_dm(
                    _safe_int(req.get("user_id")),
                    limit=_safe_int(req.get("limit", 50)) or 50,
                )
            elif cmd == "send_chat_message":
                sent_info = await self._send_chat_message(
                    str(req.get("kind") or "channel"),
                    _safe_int(req.get("target_id")),
                    str(req.get("content") or ""),
                    file_paths=list(req.get("file_paths") or []),
                    cleanup_paths=list(req.get("cleanup_paths") or []),
                    reply_to_id=_safe_int(req.get("reply_to_id")),
                )
                if req_id is not None:
                    self.emit_result(req_id, True, sent_info)
                return
            elif cmd == "edit_chat_message":
                await self._edit_chat_message(
                    str(req.get("kind") or "channel"),
                    _safe_int(req.get("target_id")),
                    _safe_int(req.get("message_id")),
                    str(req.get("content") or ""),
                )
            elif cmd == "join_channel":
                await self._cancel_inflight_join()
                gid = _safe_int(req.get("guild_id"))
                cid = _safe_int(req.get("channel_id"))
                task = asyncio.create_task(self._join_channel(gid, cid))
                self._join_task = task
                try:
                    await task
                finally:
                    if self._join_task is task:
                        self._join_task = None
            elif cmd == "leave_channel":
                await self._cancel_inflight_join()
                await self._leave_channel_internal()
            elif cmd == "set_mute":
                if self._mic_test_active:
                    self._mic_test_active = False
                    self._mic_test_saved_flags = None
                    if self._audio is not None:
                        self._audio.configure(loopback=False)
                self._muted = bool(req.get("muted", False))
                self._ensure_audio_engine().configure(muted=self._muted, loopback=False)
                await self._sync_voice_flags()
            elif cmd == "set_deaf":
                if self._mic_test_active:
                    self._mic_test_active = False
                    self._mic_test_saved_flags = None
                    if self._audio is not None:
                        self._audio.configure(loopback=False)
                self._deafened = bool(req.get("deafened", False))
                self._mixer.set_deafened(self._deafened)
                if self._deafened:
                    self._muted = True
                    self._ensure_audio_engine().configure(muted=True, loopback=False)
                await self._sync_voice_flags()
            elif cmd == "set_mic_test":
                await self._set_mic_test(bool(req.get("enabled", False)))
            elif cmd == "set_output_volume":
                self._mixer.set_master_volume(_safe_int(req.get("volume", 100)))
            elif cmd == "set_mic_gain":
                self._ensure_audio_engine().configure(mic_gain_pct=_safe_int(req.get("gain", 100)))
            elif cmd == "set_mic_agc":
                self._ensure_audio_engine().configure(agc_enabled=bool(req.get("enabled", True)))
            elif cmd == "set_noise_suppression":
                self._ensure_audio_engine().configure(
                    noise_suppression=str(req.get("mode") or "ai")
                )
            elif cmd == "set_vad_threshold":
                self._ensure_audio_engine().configure(
                    vad_threshold=_safe_int(req.get("threshold", 18))
                )
            elif cmd == "set_voice_fx":
                self._ensure_audio_engine().configure(
                    voice_fx={
                        "enabled": bool(req.get("enabled", False)),
                        "pitch": _safe_int(req.get("pitch", 0)),
                        "tone": _safe_int(req.get("tone", 0)),
                        "robot": _safe_int(req.get("robot", 0)),
                        "echo": _safe_int(req.get("echo", 0)),
                    }
                )
            elif cmd == "set_user_volume":
                self._mixer.set_user_volume(
                    _safe_int(req.get("user_id")),
                    _safe_int(req.get("volume", 100)),
                )
            elif cmd == "set_devices":
                in_dev = str(req.get("input_device") or "")
                out_dev = str(req.get("output_device") or "")
                engine = self._ensure_audio_engine()
                engine.configure(input_device_name=in_dev, output_device_name=out_dev)
                engine.restart_streams()
            elif cmd == "configure_audio":
                engine = self._ensure_audio_engine()
                cfg_kwargs: dict[str, Any] = {}
                if "input_device" in req:
                    cfg_kwargs["input_device_name"] = str(req.get("input_device") or "")
                if "output_device" in req:
                    cfg_kwargs["output_device_name"] = str(req.get("output_device") or "")
                if "mic_gain" in req:
                    cfg_kwargs["mic_gain_pct"] = _safe_int(req.get("mic_gain", 100))
                if "mic_agc" in req:
                    cfg_kwargs["agc_enabled"] = bool(req.get("mic_agc"))
                if "noise_suppression" in req:
                    cfg_kwargs["noise_suppression"] = str(req.get("noise_suppression") or "ai")
                if "vad_threshold" in req:
                    cfg_kwargs["vad_threshold"] = _safe_int(req.get("vad_threshold", 18))
                if isinstance(req.get("voice_fx"), dict):
                    cfg_kwargs["voice_fx"] = dict(req["voice_fx"])

                flags_changed = False
                if "muted" in req:
                    new_muted = bool(req.get("muted"))
                    if new_muted != self._muted:
                        flags_changed = True
                    self._muted = new_muted
                    cfg_kwargs["muted"] = new_muted
                if "deafened" in req:
                    new_deaf = bool(req.get("deafened"))
                    if new_deaf != self._deafened:
                        flags_changed = True
                    self._deafened = new_deaf
                    self._mixer.set_deafened(new_deaf)
                if "output_volume" in req:
                    self._mixer.set_master_volume(_safe_int(req.get("output_volume", 100)))

                if cfg_kwargs:
                    engine.configure(**cfg_kwargs)
                if req.get("restart_streams") and engine._running:
                    engine.restart_streams()
                if flags_changed and self._vc is not None and self._vc.is_connected():
                    await self._sync_voice_flags()
            elif cmd == "set_screen_share_presence":
                await self._set_screen_share_presence(req.get("share"))
            elif cmd == "shutdown":
                await self._disconnect_bot()
                if req_id is not None:
                    self.emit_result(req_id, True, None)
                if self._stop_event is not None:
                    self._stop_event.set()
                return
            else:
                if req_id is not None:
                    self.emit_result(req_id, False, error=f"Comando desconocido: {cmd}")
                return

            if req_id is not None:
                self.emit_result(req_id, True, None)
        except Exception as exc:
            self.emit_event("error", {"message": str(exc)})
            if req_id is not None:
                self.emit_result(req_id, False, error=str(exc))

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._housekeeping_task = asyncio.create_task(self._housekeeping_loop())

        def _stdin_reader() -> None:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                raw = line.strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                except Exception:
                    continue
                if self._loop is not None and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self.handle_command(req), self._loop)
            if self._loop is not None and not self._loop.is_closed() and self._stop_event is not None:
                self._loop.call_soon_threadsafe(self._stop_event.set)

        t = threading.Thread(target=_stdin_reader, daemon=True, name="discord-stdin-reader")
        t.start()

        await self._stop_event.wait()
        if self._housekeeping_task is not None and not self._housekeeping_task.done():
            self._housekeeping_task.cancel()
        await self._disconnect_bot()


def main() -> int:
    import locale
    import site

    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        site_dir = os.path.join(venv_env, "Lib", "site-packages")
        if os.path.isdir(site_dir):
            site.addsitedir(site_dir)
    try:
        from swiftplay.windows_os import set_process_app_user_model_id

        set_process_app_user_model_id()
    except Exception:
        pass

    proto_out = sys.stdout
    sys.stdout = sys.stderr

    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass

    app = DiscordWorkerApp(proto_out)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
