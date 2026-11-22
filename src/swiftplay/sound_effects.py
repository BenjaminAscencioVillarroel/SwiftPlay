"""Motor ligero de efectos de sonido Discord en memoria PCM WAV + winmm.PlaySoundW."""

from __future__ import annotations

import ctypes
import functools
import io
import math
import struct
import sys
import threading
import wave
from typing import Any

_SND_ASYNC = 0x0001
_SND_NODEFAULT = 0x0002
_SND_MEMORY = 0x0004

SOUND_PACKS: tuple[tuple[str, str], ...] = (
    ("soft", "Suave (recomendado)"),
    ("minimal", "Ultra-sutil"),
)
VALID_SOUND_PACKS: tuple[str, ...] = tuple(k for k, _ in SOUND_PACKS)

SOUND_EVENTS: tuple[tuple[str, str, int], ...] = (
    ("mute_on", "Silenciar micrófono", 60),
    ("mute_off", "Activar micrófono", 60),
    ("deafen_on", "Ensordecer audio", 60),
    ("deafen_off", "Quitar ensordecido", 60),
    ("message", "Notificación de mensaje", 60),
    ("self_join", "Entrar al canal de voz", 60),
    ("self_leave", "Salir del canal de voz", 60),
    ("user_join", "Alguien entra al canal", 55),
    ("user_leave", "Alguien sale del canal", 55),
)
DEFAULT_SOUND_VOLUMES: dict[str, int] = {k: v for k, _, v in SOUND_EVENTS}

# Recetas (cutoff_hz, [(freq_hz, start_s, dur_s, gain, h2_ratio), ...])
_NoteSpec = tuple[float, float, float, float, float]
_PackSpec = tuple[float, tuple[_NoteSpec, ...]]

_SOUND_SPECS: dict[str, dict[str, _PackSpec]] = {
    "mute_on": {
        "soft": (2100.0, ((420.0, 0.0, 0.075, 0.30, 0.14), (290.0, 0.05, 0.125, 0.28, 0.08))),
        "minimal": (1450.0, ((380.0, 0.0, 0.055, 0.22, 0.0), (260.0, 0.04, 0.095, 0.20, 0.0))),
    },
    "mute_off": {
        "soft": (2200.0, ((320.0, 0.0, 0.065, 0.26, 0.12), (480.0, 0.045, 0.125, 0.30, 0.12))),
        "minimal": (1500.0, ((300.0, 0.0, 0.05, 0.20, 0.0), (430.0, 0.038, 0.095, 0.22, 0.0))),
    },
    "deafen_on": {
        "soft": (1350.0, ((392.0, 0.0, 0.085, 0.30, 0.10), (262.0, 0.065, 0.175, 0.32, 0.08))),
        "minimal": (1100.0, ((330.0, 0.0, 0.07, 0.22, 0.0), (220.0, 0.052, 0.135, 0.24, 0.0))),
    },
    "deafen_off": {
        "soft": (
            2400.0,
            (
                (294.0, 0.0, 0.075, 0.24, 0.10),
                (440.0, 0.055, 0.095, 0.26, 0.12),
                (523.0, 0.11, 0.165, 0.28, 0.10),
            ),
        ),
        "minimal": (
            1600.0,
            (
                (280.0, 0.0, 0.06, 0.18, 0.0),
                (392.0, 0.045, 0.075, 0.20, 0.0),
                (466.0, 0.09, 0.135, 0.22, 0.0),
            ),
        ),
    },
    "message": {
        "soft": (2600.0, ((660.0, 0.0, 0.075, 0.24, 0.16), (880.0, 0.05, 0.155, 0.26, 0.12))),
        "minimal": (1800.0, ((740.0, 0.0, 0.055, 0.17, 0.0), (880.0, 0.04, 0.11, 0.19, 0.0))),
    },
    "self_join": {
        "soft": (
            2300.0,
            (
                (392.0, 0.0, 0.095, 0.25, 0.12),
                (494.0, 0.07, 0.105, 0.27, 0.12),
                (587.0, 0.14, 0.21, 0.29, 0.10),
            ),
        ),
        "minimal": (1550.0, ((392.0, 0.0, 0.08, 0.19, 0.0), (523.0, 0.065, 0.17, 0.22, 0.0))),
    },
    "self_leave": {
        "soft": (
            2000.0,
            (
                (494.0, 0.0, 0.09, 0.26, 0.10),
                (370.0, 0.07, 0.105, 0.25, 0.10),
                (294.0, 0.14, 0.21, 0.25, 0.08),
            ),
        ),
        "minimal": (1450.0, ((440.0, 0.0, 0.08, 0.20, 0.0), (311.0, 0.065, 0.165, 0.20, 0.0))),
    },
    "user_join": {
        "soft": (2200.0, ((523.0, 0.0, 0.055, 0.24, 0.14), (659.0, 0.042, 0.10, 0.26, 0.10))),
        "minimal": (1550.0, ((494.0, 0.0, 0.045, 0.18, 0.0), (587.0, 0.035, 0.08, 0.19, 0.0))),
    },
    "user_leave": {
        "soft": (1900.0, ((440.0, 0.0, 0.055, 0.24, 0.10), (330.0, 0.042, 0.10, 0.24, 0.06))),
        "minimal": (1450.0, ((392.0, 0.0, 0.045, 0.18, 0.0), (294.0, 0.035, 0.08, 0.18, 0.0))),
    },
}

_SAMPLE_RATE = 24000
_winmm_play_fn: Any = None


def _get_winmm_playsound() -> Any:
    global _winmm_play_fn
    if sys.platform != "win32":
        return None
    if _winmm_play_fn is not None:
        return _winmm_play_fn
    try:
        fn = ctypes.windll.winmm.PlaySoundW
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        fn.restype = ctypes.c_bool
        _winmm_play_fn = fn
    except Exception:
        _winmm_play_fn = None
    return _winmm_play_fn


@functools.lru_cache(maxsize=96)
def synthesize_sound_wav(event_id: str, pack: str = "soft", volume_pct: int = 60) -> bytes:
    """Genera un clip WAV PCM mono 16-bit en memoria con envolvente suave y filtro pasa-bajos."""
    vol = max(0, min(100, int(volume_pct)))
    if vol <= 0:
        return b""
    spec_by_pack = _SOUND_SPECS.get(str(event_id or "").strip())
    if not spec_by_pack:
        return b""
    pack_key = pack if pack in spec_by_pack else "soft"
    cutoff_hz, notes = spec_by_pack[pack_key]

    vol_scale = ((vol / 100.0) ** 1.25) * 1.85
    total_dur = max(start + dur for _, start, dur, _, _ in notes) + 0.014
    n_samples = max(1, int(total_dur * _SAMPLE_RATE))
    buf = [0.0] * n_samples
    attack_s = 0.008
    two_pi = 2.0 * math.pi

    for freq, start_s, dur_s, gain, h2_ratio in notes:
        peak = gain * vol_scale
        i0 = max(0, int(start_s * _SAMPLE_RATE))
        i1 = min(n_samples, int((start_s + dur_s) * _SAMPLE_RATE))
        for idx in range(i0, i1):
            t = (idx - i0) / _SAMPLE_RATE
            if t < attack_s:
                env = t / attack_s
            else:
                decay_t = (t - attack_s) / max(0.001, dur_s - attack_s)
                env = math.exp(-5.5 * decay_t)
            sample = math.sin(two_pi * freq * t) * peak * env
            if h2_ratio > 0.0 and t < dur_s * 0.75:
                h2_decay = (
                    (t / attack_s)
                    if t < attack_s
                    else math.exp(-6.2 * ((t - attack_s) / max(0.001, dur_s * 0.75 - attack_s)))
                )
                sample += math.sin(two_pi * (freq * 2.0) * t) * (peak * h2_ratio) * h2_decay
            buf[idx] += sample

    # Filtro pasa-bajos de primer orden para calidez (evita brillo áspero)
    dt = 1.0 / _SAMPLE_RATE
    rc = 1.0 / (two_pi * max(200.0, float(cutoff_hz)))
    alpha = dt / (rc + dt)
    prev = 0.0
    for idx in range(n_samples):
        prev = prev + alpha * (buf[idx] - prev)
        buf[idx] = max(-0.98, min(0.98, prev))

    pcm = struct.pack(f"<{n_samples}h", *(int(s * 32767.0) for s in buf))
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm)
    return out.getvalue()


class SoundEffectPlayer:
    """Reproductor asíncrono sin bloqueos para avisos sonoros de Discord en Windows."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_cbuf: Any = None
        self._output_enabled: bool = True
        self._played_history: list[str] = []

    def set_output_enabled(self, enabled: bool) -> None:
        self._output_enabled = bool(enabled)

    def _play_winmm_buffer(self, wav_bytes: bytes) -> bool:
        play_fn = _get_winmm_playsound()
        if play_fn is None or not wav_bytes:
            return False
        with self._lock:
            try:
                play_fn(None, None, 0)
            except Exception:
                pass
            cbuf = ctypes.create_string_buffer(wav_bytes, len(wav_bytes))
            self._active_cbuf = cbuf
            try:
                return bool(play_fn(cbuf, None, _SND_MEMORY | _SND_ASYNC | _SND_NODEFAULT))
            except Exception:
                return False

    def play_raw_wav(self, wav_bytes: bytes) -> bool:
        """Reproduce bytes WAV PCM directamente por WinMM (útil para pruebas silenciosas)."""
        if not wav_bytes or sys.platform != "win32":
            return False
        return self._play_winmm_buffer(wav_bytes)

    def play(
        self,
        event_id: str,
        *,
        pack: str = "soft",
        volume_pct: int = 60,
        enabled: bool = True,
        force: bool = False,
    ) -> bool:
        if (not enabled and not force) or int(volume_pct) <= 0:
            return False
        wav_bytes = synthesize_sound_wav(event_id, pack, int(volume_pct))
        if not wav_bytes:
            return False
        with self._lock:
            self._played_history.append(event_id)
            if len(self._played_history) > 32:
                self._played_history = self._played_history[-32:]
        if self._output_enabled and sys.platform == "win32":
            self._play_winmm_buffer(wav_bytes)
        return True

    def stop(self) -> None:
        with self._lock:
            play_fn = _get_winmm_playsound()
            if self._output_enabled and play_fn is not None:
                try:
                    play_fn(None, None, 0)
                except Exception:
                    pass
            self._active_cbuf = None
