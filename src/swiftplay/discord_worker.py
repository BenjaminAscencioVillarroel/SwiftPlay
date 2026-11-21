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
import json
import math
import os
import queue
import struct
import sys
import threading
import time
import warnings
from typing import Any, Callable, Literal

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = (SAMPLE_RATE * FRAME_MS) // 1000  # 960
MONO_FRAME_BYTES = FRAME_SAMPLES * 2  # 1920 bytes (int16 mono)
PCM_FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2  # 3840 bytes (int16 stereo)
SILENCE_PCM = b"\x00" * PCM_FRAME_BYTES
OPUS_SILENCE = b"\xf8\xff\xfe"

_AUDIOOP_CACHE: Any = None


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
        self._in_ptr: Any = None
        self._out_ptr: Any = None
        self._np: Any = None
        self._ctypes: Any = None
        self._hp_prev_x = 0.0
        self._hp_prev_y = 0.0
        self._silero = _SileroVoiceGate()
        self._failed = False

    def _apply_params_locked(self) -> None:
        if self._lib is None or not self._df_state or self._ctypes is None:
            return
        beta = 0.05 if self._mode == "ai_max" else 0.02
        self._lib.df_set_atten_lim(self._df_state, self._ctypes.c_float(100.0))
        self._lib.df_set_post_filter_beta(self._df_state, self._ctypes.c_float(beta))

    def _free_locked(self) -> None:
        st = self._df_state
        self._df_state = None
        self._in_buf = None
        self._out_buf = None
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
            self._in_ptr = self._in_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._out_ptr = self._out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._apply_params_locked()
            self._silero.warmup()
            return True
        except Exception:
            self._free_locked()
            self._failed = True
            return False

    def warmup(self) -> bool:
        with self._lock:
            return self._ensure_ready_locked()

    def reconfigure(self, mode: str) -> None:
        norm = _normalize_ns_mode(mode)
        with self._lock:
            self._mode = norm
            if norm not in ("ai", "ai_max"):
                self._free_locked()
                self._hp_prev_x = 0.0
                self._hp_prev_y = 0.0
                self._silero.reset()
            elif self._df_state:
                self._apply_params_locked()

    def reset(self) -> None:
        with self._lock:
            self._hp_prev_x = 0.0
            self._hp_prev_y = 0.0
            self._silero.reset()

    def close(self) -> None:
        with self._lock:
            self._free_locked()
            self._silero.reset()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def process_20ms(self, raw_mono: bytes) -> tuple[bytes, float, float | None] | None:
        if len(raw_mono) != MONO_FRAME_BYTES:
            return raw_mono, 1.0, None
        with self._lock:
            if not self._ensure_ready_locked():
                return None
            try:
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
                clean_parts = []
                max_lsnr = -15.0
                for off in range(0, FRAME_SAMPLES, hop):
                    np.copyto(self._in_buf, hp_arr[off : off + hop])
                    lsnr = float(lib.df_process_frame(st, self._in_ptr, self._out_ptr))
                    if lsnr > max_lsnr:
                        max_lsnr = lsnr
                    clean_parts.append(self._out_buf.copy())

                clean_f = np.concatenate(clean_parts) if len(clean_parts) > 1 else clean_parts[0]
                clean_i16 = np.clip(clean_f * 32767.0, -32768.0, 32767.0).astype(np.int16)
                clean_bytes = clean_i16.tobytes()
                silero_prob = self._silero.process_20ms(clean_bytes)
                return clean_bytes, max_lsnr, silero_prob
            except Exception:
                return None


class MicProcessor:
    """Supresión de ruido neuronal (DeepFilterNet3 + Silero / WebRTC), AGC suave, limitador y VAD."""

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
        preroll_frames: int = 4,
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

    def close(self) -> None:
        with self._lock:
            df_dsp = self._df_dsp
        if df_dsp is not None:
            df_dsp.close()

    def configure(
        self,
        *,
        mic_gain_pct: int | None = None,
        agc_enabled: bool | None = None,
        vad_threshold: int | None = None,
        noise_suppression: str | None = None,
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
        if df_dsp is not None:
            df_dsp.reconfigure(cur_ns)
        if webrtc_dsp is not None:
            webrtc_dsp.reconfigure("webrtc" if cur_ns != "off" else "off")

    def reset(self) -> None:
        with self._lock:
            self._preroll.clear()
            self._speaking = False
            self._hangover_until = 0.0
            self._agc_gain = 1.6
            self._voice_env = 0.0
            df_dsp = self._df_dsp
            webrtc_dsp = self._webrtc_dsp
        if df_dsp is not None:
            df_dsp.reset()
        if webrtc_dsp is not None:
            webrtc_dsp.reset()

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
            df_res = df_dsp.process_20ms(raw_mono_frame)
            if df_res is not None:
                work_frame, lsnr_db, silero_prob = df_res
                use_dsp = True
                if ns_mode == "ai":
                    if silero_prob is not None:
                        if speaking:
                            voice_ok = (silero_prob >= 0.12) or (lsnr_db >= -1.0)
                        else:
                            voice_ok = (silero_prob >= 0.30 and lsnr_db >= -6.0) or (lsnr_db >= 6.0)
                    else:
                        voice_ok = lsnr_db >= (-1.0 if speaking else 3.0)
                else:
                    if silero_prob is not None:
                        if speaking:
                            voice_ok = (silero_prob >= 0.16) or (lsnr_db >= 2.0)
                        else:
                            voice_ok = (silero_prob >= 0.45 and lsnr_db >= -4.0) or (
                                silero_prob >= 0.65
                            )
                    else:
                        voice_ok = lsnr_db >= (0.0 if speaking else 5.0)
            elif webrtc_dsp is not None:
                work_frame, speech_prob = webrtc_dsp.process_20ms(raw_mono_frame)
                use_dsp = True
                open_prob, hold_prob = _NS_SPEECH_PROB_THRESHOLDS["webrtc"]
                voice_ok = speech_prob >= (hold_prob if speaking else open_prob)
        elif webrtc_dsp is not None and ns_mode == "webrtc":
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
            return None

    if not payload:
        return None

    return ssrc, user_id, payload


def _seq_diff(a: int, b: int) -> int:
    """Diferencia con signo entre secuencias RTP de 16 bits."""
    return ((int(a) - int(b) + 0x8000) & 0xFFFF) - 0x8000


class UserJitterBuffer:
    """Jitter buffer ordenado por secuencia RTP + PLC/FEC Opus para un participante."""

    def __init__(
        self,
        decoder_factory: Callable[[], Any] | None = None,
        prefill_frames: int = 3,
        max_packets: int = 12,
    ) -> None:
        self._lock = threading.Lock()
        self._decoder_factory = decoder_factory
        self._decoder: Any = None
        self._prefill = max(1, int(prefill_frames))
        self._max_packets = max(4, int(max_packets))
        self._packets: dict[int, bytes] = {}
        self._priming = True
        self._next_seq: int | None = None
        self._first_pkt_ts = 0.0
        self._consecutive_missing = 0
        self.last_rx_ts = time.monotonic()
        self._pcm_overflow = bytearray()

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
            if not opus_bytes or opus_bytes == OPUS_SILENCE:
                return
            if not self._priming and self._next_seq is not None:
                if _seq_diff(seq_u16, self._next_seq) < 0:
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

            if self._priming:
                if not self._packets:
                    return None
                waited = cur - self._first_pkt_ts
                if len(self._packets) < self._prefill and waited < 0.055:
                    return None
                anchor = next(iter(self._packets))
                self._next_seq = min(self._packets.keys(), key=lambda s: _seq_diff(s, anchor))
                self._priming = False
                self._consecutive_missing = 0

            if self._next_seq is None:
                self._priming = True
                return None

            # Limitar latencia máxima si se acumulan demasiados paquetes
            while len(self._packets) > 7:
                self._packets.pop(self._next_seq, None)
                self._next_seq = (self._next_seq + 1) & 0xFFFF

            dec = self._ensure_decoder()
            pcm: bytes | None = None
            pkt = self._packets.pop(self._next_seq, None)
            if pkt is not None:
                self._next_seq = (self._next_seq + 1) & 0xFFFF
                self._consecutive_missing = 0
                try:
                    pcm = dec.decode(pkt, fec=False)
                except Exception:
                    pcm = None
            else:
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
                elif self._consecutive_missing < 3:
                    try:
                        pcm = dec.decode(None, fec=False)
                    except Exception:
                        pcm = None
                    self._next_seq = (self._next_seq + 1) & 0xFFFF
                    self._consecutive_missing += 1
                else:
                    self._priming = True
                    self._next_seq = None
                    self._consecutive_missing = 0
                    self._packets.clear()
                    self._pcm_overflow.clear()
                    return None

            if not pcm:
                return SILENCE_PCM
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

    def close(self) -> None:
        with self._lock:
            self._packets.clear()
            self._pcm_overflow.clear()
            self._decoder = None
            self._priming = True
            self._next_seq = None


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
        for _, _, idx in scored:
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

        self._mic_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._mic_thread: threading.Thread | None = None

        self._muted = False
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
        mic_gain_pct: int | None = None,
        agc_enabled: bool | None = None,
        vad_threshold: int | None = None,
        noise_suppression: str | None = None,
        input_device_name: str | None = None,
        output_device_name: str | None = None,
    ) -> None:
        if muted is not None:
            self._muted = bool(muted)
            if self._muted:
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
        )
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
        """Precalienta DeepFilterNet3 y Silero VAD en segundo plano tras conectar el bot."""
        with self._warmup_lock:
            if self._warmup_started:
                return
            self._warmup_started = True

        def _run() -> None:
            try:
                self._mic_proc.warmup()
            except Exception:
                with self._warmup_lock:
                    self._warmup_started = False

        threading.Thread(
            target=_run,
            daemon=True,
            name="discord-audio-warmup",
        ).start()

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
            while not self._mic_queue.empty():
                try:
                    self._mic_queue.get_nowait()
                except queue.Empty:
                    break

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
        """Reabre streams que no emitieron callbacks PortAudio tras >= 1.2s desde su apertura."""
        if not self._running:
            return False, False
        t = time.monotonic() if now is None else float(now)
        rec_in = False
        rec_out = False

        in_stale = (t - self._in_opened_at) >= 1.2 and (
            self._input_stream is None
            or self._in_cb_count == 0
            or not getattr(self._input_stream, "active", True)
        )
        if in_stale and self._in_recoveries < 6:
            with self._lock:
                old_in = self._input_stream
                self._input_stream = None
            self._close_stream(old_in)
            self._in_candidate_skip += 1
            self._in_recoveries += 1
            self._open_input_stream()
            rec_in = True

        out_stale = (t - self._out_opened_at) >= 1.2 and (
            self._output_stream is None
            or self._out_cb_count == 0
            or not getattr(self._output_stream, "active", True)
        )
        if out_stale and self._out_recoveries < 6:
            with self._lock:
                old_out = self._output_stream
                self._output_stream = None
            self._close_stream(old_out)
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

        for s in (in_s, out_s):
            self._close_stream(s)

        if self._mic_thread is not None and self._mic_thread.is_alive():
            self._mic_thread.join(timeout=0.8)
        self._mic_thread = None

    def close(self) -> None:
        self.stop()
        self._mic_proc.close()

    def _open_output_stream(self) -> None:
        import sounddevice as sd

        self._out_cb_count = 0
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
                        latency=0.06,
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
                        latency=0.04,
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
        try:
            aop = _get_audioop()
            while len(self._out_buf) < needed:
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
        if self._muted:
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
        self._mic_proc.warmup()
        aop = _get_audioop()
        while self._running:
            try:
                raw = self._mic_queue.get(timeout=0.08)
            except queue.Empty:
                now = time.monotonic()
                if self._mic_proc.check_hangover(now) and self._local_speaking:
                    self._local_speaking = False
                    self._on_speaking_state(False)
                    self._on_mic_level(0, False)
                continue

            if not self._running:
                break
            if not raw or self._muted:
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

                if self._muted:
                    continue

                now = time.monotonic()
                tx_frames, level, should_tx = self._mic_proc.process_frame(frame, now)

                if (now - self._last_vu_emit) >= 0.09:
                    self._last_vu_emit = now
                    try:
                        self._on_mic_level(level, should_tx)
                    except Exception:
                        pass

                if should_tx:
                    if not self._local_speaking:
                        self._local_speaking = True
                        self._on_speaking_state(True)
                    for mono_out in tx_frames:
                        self._on_send_pcm(_pcm_mono_to_stereo(mono_out))
                elif self._local_speaking:
                    self._local_speaking = False
                    self._on_speaking_state(False)
                    self._on_mic_level(level, False)


def _make_voice_client_cls():
    """Construye dinámicamente SwiftVoiceClient sin importar discord al nivel de módulo."""
    import davey
    import discord
    from discord.voice_state import VoiceConnectionState

    class SwiftVoiceClient(discord.VoiceClient):
        """VoiceClient con recepción UDP DAVE E2EE + envío directo de micrófono."""

        def __init__(self, client: discord.Client, channel: Any) -> None:
            super().__init__(client, channel)
            self.ssrc_to_user: dict[int, int] = {}
            self._decoders: dict[int, discord.opus.Decoder] = {}
            self._decoder_lock = threading.Lock()
            self._send_lock = threading.Lock()
            self._aead_cache: tuple[bytes, Any] | None = None
            self._listener_registered = False
            self._self_user_id = int(client.user.id) if client.user else 0
            self.deafened = False

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
            return VoiceConnectionState(self, hook=self._on_voice_ws_message)

        async def connect(
            self,
            *,
            timeout: float = 60.0,
            reconnect: bool = True,
            self_deaf: bool = False,
            self_mute: bool = False,
        ) -> None:
            self.deafened = bool(self_deaf)
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

        async def kickstart_voice(self, *, keep_speaking: bool = False) -> None:
            """Envía un pulso breve de silencio Opus tras iniciar el motor de audio/DAVE."""
            try:
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
                if uid and ssrc:
                    self.ssrc_to_user[ssrc] = uid
                if uid and self.on_speaking_change is not None:
                    self.on_speaking_change(uid, bool(data.get("speaking")))
            elif op == 12:  # CLIENT_CONNECT
                uid = _safe_int(data.get("user_id"))
                ssrc = _safe_int(data.get("audio_ssrc"))
                if uid and ssrc:
                    self.ssrc_to_user[ssrc] = uid
            elif op == 13:  # CLIENT_DISCONNECT
                uid = _safe_int(data.get("user_id"))
                if uid:
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
            key_bytes = bytes(sk)
            aead_box = (
                self._get_aead_box(key_bytes)
                if mode == "aead_xchacha20_poly1305_rtpsize"
                else None
            )
            res = decrypt_discord_rtp_packet(
                data=data,
                mode=str(mode),
                secret_key=key_bytes,
                ssrc_to_user=self.ssrc_to_user,
                dave_session=self._connection.dave_session,
                dave_active=bool(self._connection.can_encrypt),
                aead_box=aead_box,
                dave_media_type=davey.MediaType.audio,
            )
            if res is None:
                if is_opus_rtp:
                    ssrc = struct.unpack_from(">I", data, 8)[0]
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
                    self.send_audio_packet(OPUS_SILENCE, encode=False)
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


def _format_member_activity(member: Any) -> str:
    """Devuelve texto breve de actividad o estado personalizado de un miembro."""
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


def _serialize_guild_members(
    guild: Any, limit: int = 200
) -> tuple[int, list[dict[str, Any]]]:
    """Serializa miembros de un servidor ordenados por presencia (online/dnd/idle/offline)."""
    items: list[dict[str, Any]] = []
    online_count = 0
    for m in list(getattr(guild, "members", None) or ()):
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

    attachments: list[dict[str, str]] = []
    for att in list(getattr(msg, "attachments", None) or ())[:4]:
        attachments.append(
            {
                "name": str(getattr(att, "filename", "") or "archivo")[:80],
                "url": str(getattr(att, "url", "") or ""),
            }
        )

    ts_str = ""
    created = getattr(msg, "created_at", None)
    if created is not None:
        try:
            local_dt = created.astimezone() if getattr(created, "tzinfo", None) is not None else created
            ts_str = local_dt.strftime("%H:%M")
        except Exception:
            ts_str = ""

    return {
        "id": str(getattr(msg, "id", "") or ""),
        "author_id": str(author_id),
        "author_name": author_name,
        "is_self": is_self,
        "is_bot": is_bot,
        "content": content,
        "attachments": attachments,
        "timestamp": ts_str,
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
        self._voice_cls: Any = None
        self._joining_channel_id: str = ""

        self._mixer = VoiceAudioMixer()
        self._audio: AudioEngine | None = None
        self._muted = False
        self._deafened = False
        self._speaking_timestamps: dict[int, float] = {}
        self._speaking_lock = threading.Lock()
        self._member_name_cache: dict[int, tuple[str, bool]] = {}
        self._member_fetch_inflight: set[tuple[int, int]] = set()
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
                    if len(self._member_name_cache) >= 96:
                        self._member_name_cache.clear()
                    self._member_name_cache[uid] = (name, is_bot)
                    self._emit_snapshots()
            except Exception:
                pass
            finally:
                self._member_fetch_inflight.discard(key)

        self._loop.create_task(_fetch())

    def _resolve_member_name(self, guild: Any, member_or_id: Any) -> tuple[int, str, bool]:
        if isinstance(member_or_id, int):
            uid = member_or_id
            m = guild.get_member(uid) if guild is not None else None
            if m is None and self._client is not None:
                m = self._client.get_user(uid)
            if m is not None:
                name = str(getattr(m, "display_name", "") or getattr(m, "name", "") or uid)
                is_bot = bool(getattr(m, "bot", False))
                self._member_name_cache[uid] = (name, is_bot)
                return uid, name, is_bot
            cached = self._member_name_cache.get(uid)
            if cached is not None:
                return uid, cached[0], cached[1]
            self._schedule_member_fetch(guild, uid)
            return uid, f"Usuario {uid}", False

        uid = int(getattr(member_or_id, "id", 0) or 0)
        name = str(
            getattr(member_or_id, "display_name", "")
            or getattr(member_or_id, "name", "")
            or uid
        )
        is_bot = bool(getattr(member_or_id, "bot", False))
        if uid:
            if len(self._member_name_cache) >= 96:
                self._member_name_cache.clear()
            self._member_name_cache[uid] = (name, is_bot)
        return uid, name, is_bot

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
                    uid, name, is_bot = self._resolve_member_name(g, int(uid_key))
                    members_list.append(
                        {
                            "id": str(uid),
                            "name": name,
                            "is_bot": is_bot,
                            "muted": bool(getattr(vs, "self_mute", False) or getattr(vs, "mute", False)),
                            "deafened": bool(getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False)),
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
        if vc is None or not vc.is_connected() or getattr(vc, "channel", None) is None:
            return {
                "in_voice": False,
                "joining_channel_id": self._joining_channel_id,
                "guild_id": "",
                "channel_id": "",
                "channel_name": "",
                "guild_name": "",
                "muted": self._muted,
                "deafened": self._deafened,
                "dave_active": False,
                "dave_code": "",
                "participants": [],
            }

        ch = vc.channel
        g = ch.guild
        participants: list[dict[str, Any]] = []
        for uid_key, vs in ch.voice_states.items():
            uid, name, is_bot = self._resolve_member_name(g, int(uid_key))
            participants.append(
                {
                    "id": str(uid),
                    "name": name,
                    "is_bot": is_bot,
                    "is_self": bool(self._client and self._client.user and uid == self._client.user.id),
                    "muted": bool(getattr(vs, "self_mute", False) or getattr(vs, "mute", False)),
                    "deafened": bool(getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False)),
                }
            )
        return {
            "in_voice": True,
            "joining_channel_id": self._joining_channel_id,
            "guild_id": str(g.id),
            "guild_name": str(g.name),
            "channel_id": str(ch.id),
            "channel_name": str(ch.name),
            "muted": self._muted,
            "deafened": self._deafened,
            "dave_active": bool(self._vc._connection.can_encrypt) if self._vc else self._dave_active,
            "dave_code": str(self._vc.voice_privacy_code or "") if self._vc else self._dave_code,
            "tx_packets": int(getattr(self._vc, "tx_packets", 0)),
            "rx_packets": int(getattr(self._vc, "rx_packets", 0)),
            "rx_decrypt_errors": int(getattr(self._vc, "rx_decrypt_errors", 0)),
            "tx_errors": int(getattr(self._vc, "tx_errors", 0)),
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
        return discord.Client(
            intents=intents,
            max_messages=None,
            chunk_guilds_at_startup=False,
        )

    def _attach_client_events(self, client: Any) -> None:
        @client.event
        async def on_ready() -> None:
            self._state = "connected"
            user = client.user
            self.emit_event(
                "state",
                {
                    "state": "connected",
                    "bot_id": str(user.id) if user else "",
                    "bot_name": str(user.display_name or user.name) if user else "Bot",
                    "privileged_intents_ok": bool(self._privileged_intents_ok),
                },
            )
            self._emit_snapshots()
            if self._active_guild_id:
                await self._select_guild(self._active_guild_id)
            try:
                self._ensure_audio_engine().warmup_async()
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
            if (
                client.user
                and member.id == client.user.id
                and after.channel is None
                and self._vc is not None
                and not self._joining_channel_id
            ):
                # Bot desconectado externamente del canal de voz
                asyncio.create_task(self._leave_channel_internal())
                return
            self._emit_snapshots()

        @client.event
        async def on_presence_update(_before: Any, after: Any) -> None:
            g = getattr(after, "guild", None)
            if g is not None and int(g.id) == self._active_guild_id:
                self._presence_dirty_guilds.add(int(g.id))

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
            self._dm_channels[peer_id] = ch_id
            if self._active_chat_kind == "dm" and peer_id == self._active_chat_id:
                self.emit_event(
                    "chat_message",
                    {
                        "kind": "dm",
                        "target_id": str(peer_id),
                        "message": payload,
                    },
                )
            elif not payload["is_self"]:
                preview = payload["content"] or (
                    "[adjunto]" if payload["attachments"] else ""
                )
                self.emit_event(
                    "dm_incoming",
                    {
                        "user_id": str(peer_id),
                        "author_name": payload["author_name"],
                        "preview": preview[:80],
                    },
                )

    async def _connect_bot(self) -> None:
        import discord
        from swiftplay.security import load_discord_token_decrypted

        token = load_discord_token_decrypted()
        if not token:
            self._state = "error"
            self.emit_event("state", {"state": "error", "message": "No hay Bot Token guardado en DPAPI."})
            return

        if self._client is not None:
            await self._disconnect_bot()

        self._state = "connecting"
        self.emit_event("state", {"state": "connecting"})
        self._voice_cls = _make_voice_client_cls()

        async def _runner() -> None:
            try:
                self._privileged_intents_ok = True
                client = self._build_client(True)
                self._client = client
                self._attach_client_events(client)
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
                self._state = "error"
                self.emit_event(
                    "state",
                    {
                        "state": "error",
                        "message": "Bot Token inválido o revocado en Discord Developer Portal.",
                    },
                )
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._state = "error"
                self.emit_event("state", {"state": "error", "message": str(exc)})
            finally:
                await self._leave_channel_internal()

        self._bot_task = asyncio.create_task(_runner())

    def _stop_voice_audio_only(self) -> None:
        if self._audio is not None:
            self._audio.stop()
        self._mixer.clear()
        with self._speaking_lock:
            self._speaking_timestamps.clear()
        self._emit_speaking_snapshot()

    async def _cancel_inflight_join(self) -> None:
        task = self._join_task
        self._join_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._joining_channel_id = ""

    async def _leave_channel_internal(self) -> None:
        self._stop_voice_audio_only()
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
                await vc.disconnect(force=True)
            except Exception:
                pass
        self._joining_channel_id = ""
        self._dave_active = False
        self._dave_code = ""
        self._emit_snapshots()

    async def _disconnect_bot(self) -> None:
        await self._cancel_inflight_join()
        await self._leave_channel_internal()
        if self._audio is not None:
            self._audio.close()
        client = self._client
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
        self.emit_event(
            "chat_history",
            {
                "kind": "channel",
                "target_id": str(cid),
                "guild_id": str(gid),
                "title": f"#{ch.name}",
                "can_read": can_read,
                "can_send": can_send,
                "messages": msgs,
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
                "can_read": True,
                "can_send": True,
                "messages": msgs,
            },
        )

    async def _send_chat_message(self, kind: str, target_id: int, content: str) -> None:
        import discord

        client = self._client
        if client is None or not client.is_ready():
            raise RuntimeError("El bot no está conectado a Discord.")
        text = str(content or "").strip()
        if not text:
            return
        if len(text) > 2000:
            text = text[:2000]

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
            await ch.send(text)
        except discord.Forbidden as exc:
            raise RuntimeError("El bot no tiene permiso para enviar mensajes aquí.") from exc

    async def _join_channel(self, guild_id: int, channel_id: int) -> None:
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

            existing_vc = self._vc or guild.voice_client
            if (
                existing_vc is not None
                and self._voice_cls is not None
                and isinstance(existing_vc, self._voice_cls)
                and existing_vc.is_connected()
                and existing_vc.channel is not None
                and existing_vc.channel.guild.id == guild_id
            ):
                self._vc = existing_vc
                if existing_vc.channel.id == channel_id:
                    engine = self._ensure_audio_engine()
                    engine.configure(muted=self._muted)
                    engine.start()
                    self._emit_snapshots()
                    return
                self._joining_channel_id = str(channel_id)
                self._emit_snapshots()
                try:
                    await existing_vc.move_to(channel)
                finally:
                    self._joining_channel_id = ""
            else:
                await self._leave_channel_internal()
                self._joining_channel_id = str(channel_id)
                self._emit_snapshots()
                try:
                    import discord.opus

                    vc = await channel.connect(
                        cls=self._voice_cls,
                        self_mute=self._muted,
                        self_deaf=self._deafened,
                        timeout=12.0,
                    )
                    vc.deafened = self._deafened
                    vc.on_opus = lambda uid, seq, pkt, _dec=discord.opus.Decoder: self._mixer.push_opus(
                        uid, seq, pkt, decoder_factory=_dec
                    )
                    vc.on_pcm = self._mixer.push_pcm
                    vc.on_user_removed = self._mixer.remove_user
                    vc.on_speaking_change = self._on_remote_speaking
                    vc.on_dave_change = self._on_dave_change
                    vc.on_warning = lambda msg: self.emit_event("warning", {"message": msg})
                    self._vc = vc
                except asyncio.TimeoutError as exc:
                    await self._leave_channel_internal()
                    raise RuntimeError(
                        f"Tiempo de espera agotado al entrar en '{channel.name}'. "
                        "Revisa permisos del canal o conexión UDP."
                    ) from exc
                except Exception:
                    await self._leave_channel_internal()
                    raise
                finally:
                    self._joining_channel_id = ""

            self._mixer.clear()
            engine = self._ensure_audio_engine()
            engine.configure(muted=self._muted)
            engine.start()
            vc = self._vc
            if vc is not None and vc.is_connected():
                already_dave = bool(vc._connection.can_encrypt)
                self._dave_was_active = already_dave
                self._dave_active = already_dave
                self._dave_code = str(vc.voice_privacy_code or "")
                keep_speaking = bool(
                    not self._muted and (engine.is_speaking or engine.is_open_mic)
                )
                await vc.kickstart_voice(keep_speaking=keep_speaking)
            else:
                self._dave_was_active = False
                self._dave_active = False
                self._dave_code = ""
            self._emit_snapshots()

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
        from swiftplay.windows_os import get_process_memory_mb

        ram_tick = 0.0
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

    async def handle_command(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        cmd = str(req.get("cmd") or "")
        try:
            if cmd == "ping":
                if req_id is not None:
                    self.emit_result(req_id, True, "pong")
                return

            if cmd == "list_devices":
                devices = list_audio_devices()
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
                await self._send_chat_message(
                    str(req.get("kind") or "channel"),
                    _safe_int(req.get("target_id")),
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
                self._muted = bool(req.get("muted", False))
                self._ensure_audio_engine().configure(muted=self._muted)
                await self._sync_voice_flags()
            elif cmd == "set_deaf":
                self._deafened = bool(req.get("deafened", False))
                self._mixer.set_deafened(self._deafened)
                await self._sync_voice_flags()
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

    proto_out = sys.stdout
    if sys.stderr is not None:
        sys.stdout = sys.stderr
    else:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

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
