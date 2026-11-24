"""Cliente IPC ultraliviano en Qt para el subproceso aislado de Discord Voz.

Nunca importa discord, davey, nacl ni sounddevice en el proceso GUI (0 MB en reposo).
"""

from __future__ import annotations

import atexit
from concurrent.futures import Future
import json
import os
import subprocess
import sys
import threading
import weakref
from typing import Any

from PySide6.QtCore import QObject, Signal

from swiftplay.config import AppConfig
from swiftplay.paths import APP_ROOT
from swiftplay.windows_os import assign_process_to_child_job, resolve_worker_python_and_env

_ACTIVE_SERVICES: weakref.WeakSet[DiscordService] = weakref.WeakSet()


def _cleanup_all_discord_services() -> None:
    for svc in list(_ACTIVE_SERVICES):
        try:
            svc.close()
        except Exception:
            pass


atexit.register(_cleanup_all_discord_services)


def _get_worker_python() -> str:
    exe = Path_exec()
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[:-11] + "python.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def Path_exec() -> str:
    return sys.executable


class DiscordService(QObject):
    """Gestiona el ciclo de vida del worker discord_worker.py vía stdin/stdout JSON."""

    state_changed = Signal(str, dict)
    guilds_updated = Signal(list)
    voice_state_updated = Signal(dict)
    speaking_updated = Signal(dict)
    mic_level_updated = Signal(int, bool)
    devices_updated = Signal(dict)
    worker_ram_updated = Signal(float, float)
    guild_members_updated = Signal(dict)
    presence_updated = Signal(dict)
    chat_history_loaded = Signal(dict)
    chat_message_received = Signal(dict)
    chat_message_edited = Signal(dict)
    channel_message_incoming = Signal(dict)
    dm_incoming = Signal(dict)
    screen_share_detected = Signal(dict)
    screen_share_ended = Signal(dict)
    error_occurred = Signal(str)
    warning_occurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._closed = False
        self._req_seq = 0
        self._pending: dict[int, Future[Any]] = {}

        self._state = "disconnected"
        self._bot_id = ""
        self._bot_name = ""
        self._bot_avatar_url = ""
        self._in_voice = False
        self._active_channel_name = ""
        _ACTIVE_SERVICES.add(self)

    @property
    def is_worker_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def worker_pid(self) -> int | None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return int(self._proc.pid)
            return None

    @property
    def is_connected(self) -> bool:
        return self._state == "connected"

    @property
    def is_in_voice(self) -> bool:
        return self._in_voice

    @property
    def active_channel_name(self) -> str:
        return self._active_channel_name

    @property
    def bot_id(self) -> str:
        return self._bot_id

    @property
    def bot_name(self) -> str:
        return self._bot_name

    @property
    def bot_avatar_url(self) -> str:
        return self._bot_avatar_url

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        src_dir = str(APP_ROOT / "src")
        prev_pp = env.get("PYTHONPATH", "")
        if src_dir not in prev_pp.split(os.pathsep):
            env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{prev_pp}" if prev_pp else src_dir
        return env

    def _ensure_worker(self) -> subprocess.Popen[str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("DiscordService cerrado.")
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            worker_exe, worker_env = resolve_worker_python_and_env(APP_ROOT)
            cmd = [worker_exe, "-u", "-m", "swiftplay.discord_worker"]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(APP_ROOT),
                env=worker_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assign_process_to_child_job(proc)
            self._proc = proc
            t = threading.Thread(
                target=self._reader_loop,
                args=(proc,),
                daemon=True,
                name="discord-ipc-reader",
            )
            self._reader_thread = t
            t.start()
            return proc

    def _reader_loop(self, proc: subprocess.Popen[str]) -> None:
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            for line in stdout:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "result":
                    req_id = msg.get("id")
                    fut: Future[Any] | None = None
                    with self._lock:
                        if isinstance(req_id, int):
                            fut = self._pending.pop(req_id, None)
                    if fut is not None and not fut.done():
                        if msg.get("ok"):
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(
                                RuntimeError(str(msg.get("error") or "Error en worker Discord"))
                            )
                elif mtype == "event":
                    self._handle_event(str(msg.get("event") or ""), msg.get("data"))
        finally:
            self._fail_pending("El worker de Discord finalizó.")
            reset_needed = False
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    reset_needed = True
            if reset_needed and not self._closed:
                self._state = "disconnected"
                self._bot_name = ""
                self._bot_avatar_url = ""
                self._in_voice = False
                self._active_channel_name = ""
                try:
                    self.worker_ram_updated.emit(0.0, 0.0)
                    self.state_changed.emit("disconnected", {})
                except RuntimeError:
                    pass

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(RuntimeError(reason))

    def _handle_event(self, event: str, data: Any) -> None:
        if self._closed:
            return
        try:
            if event == "state" and isinstance(data, dict):
                st = str(data.get("state") or "disconnected")
                self._state = st
                if st == "connected":
                    self._bot_id = str(data.get("bot_id") or "")
                    self._bot_name = str(data.get("bot_name") or "Bot")
                    self._bot_avatar_url = str(data.get("bot_avatar_url") or "")
                elif st in ("disconnected", "error"):
                    self._bot_id = ""
                    self._bot_name = ""
                    self._bot_avatar_url = ""
                    self._in_voice = False
                    self._active_channel_name = ""
                self.state_changed.emit(st, data)
                if st == "error" and data.get("message"):
                    self.error_occurred.emit(str(data["message"]))
            elif event == "guilds" and isinstance(data, list):
                self.guilds_updated.emit(data)
            elif event == "voice_state" and isinstance(data, dict):
                self._in_voice = bool(data.get("in_voice", False))
                self._active_channel_name = str(data.get("channel_name") or "")
                self.voice_state_updated.emit(data)
            elif event == "speaking" and isinstance(data, dict):
                self.speaking_updated.emit(data)
            elif event == "mic_level" and isinstance(data, dict):
                self.mic_level_updated.emit(
                    int(data.get("rms") or 0),
                    bool(data.get("transmitting", False)),
                )
            elif event == "devices" and isinstance(data, dict):
                self.devices_updated.emit(data)
            elif event == "worker_ram" and isinstance(data, dict):
                self.worker_ram_updated.emit(
                    float(data.get("ws_mb") or 0.0),
                    float(data.get("priv_mb") or 0.0),
                )
            elif event == "guild_members" and isinstance(data, dict):
                self.guild_members_updated.emit(data)
            elif event == "presence" and isinstance(data, dict):
                self.presence_updated.emit(data)
            elif event == "chat_history" and isinstance(data, dict):
                self.chat_history_loaded.emit(data)
            elif event == "chat_message" and isinstance(data, dict):
                self.chat_message_received.emit(data)
            elif event == "chat_message_edited" and isinstance(data, dict):
                self.chat_message_edited.emit(data)
            elif event == "channel_message_incoming" and isinstance(data, dict):
                self.channel_message_incoming.emit(data)
            elif event == "dm_incoming" and isinstance(data, dict):
                self.dm_incoming.emit(data)
            elif event == "screen_share_detected" and isinstance(data, dict):
                self.screen_share_detected.emit(data)
            elif event == "screen_share_ended" and isinstance(data, dict):
                self.screen_share_ended.emit(data)
            elif event == "error" and isinstance(data, dict):
                self.error_occurred.emit(str(data.get("message") or "Error de Discord"))
            elif event == "warning" and isinstance(data, dict):
                self.warning_occurred.emit(str(data.get("message") or ""))
        except RuntimeError:
            pass

    def _send_command(
        self,
        cmd: str,
        payload: dict[str, Any] | None = None,
        *,
        spawn: bool = True,
        await_result: bool = False,
    ) -> Future[Any] | None:
        if self._closed:
            return None
        proc = self._ensure_worker() if spawn else self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return None

        msg: dict[str, Any] = {"cmd": cmd}
        if payload:
            msg.update(payload)

        fut: Future[Any] | None = None
        with self._lock:
            if await_result:
                self._req_seq += 1
                req_id = self._req_seq
                msg["id"] = req_id
                fut = Future()
                self._pending[req_id] = fut
            try:
                proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as exc:
                if await_result and fut is not None:
                    self._pending.pop(msg["id"], None)
                    fut.set_exception(exc)
        return fut

    def ping(self, timeout: float = 10.0) -> str:
        fut = self._send_command("ping", spawn=True, await_result=True)
        if fut is None:
            raise RuntimeError("No se pudo iniciar el worker de Discord.")
        return str(fut.result(timeout=timeout))

    def list_devices_sync(self, timeout: float = 10.0) -> dict[str, Any]:
        fut = self._send_command("list_devices", spawn=True, await_result=True)
        if fut is None:
            raise RuntimeError("No se pudo consultar dispositivos de audio.")
        res = fut.result(timeout=timeout)
        return res if isinstance(res, dict) else {"inputs": [], "outputs": []}

    def sync_audio_config(
        self,
        cfg: AppConfig,
        *,
        muted: bool | None = None,
        deafened: bool | None = None,
        spawn: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "input_device": str(cfg.discord_input_device),
            "output_device": str(cfg.discord_output_device),
            "output_volume": int(cfg.discord_output_volume),
            "mic_gain": int(cfg.discord_mic_gain),
            "mic_agc": bool(cfg.discord_mic_agc),
            "noise_suppression": str(cfg.discord_noise_suppression),
            "vad_threshold": int(cfg.discord_vad_threshold),
            "voice_fx": {
                "enabled": bool(cfg.discord_voice_fx_enabled),
                "pitch": int(cfg.discord_voice_fx_pitch),
                "tone": int(cfg.discord_voice_fx_tone),
                "robot": int(cfg.discord_voice_fx_robot),
                "echo": int(cfg.discord_voice_fx_echo),
            },
        }
        if muted is not None:
            payload["muted"] = bool(muted)
        if deafened is not None:
            payload["deafened"] = bool(deafened)
        self._send_command("configure_audio", payload, spawn=spawn)

    def connect_bot(self, cfg: AppConfig) -> None:
        self._state = "connecting"
        self.state_changed.emit("connecting", {})
        self._ensure_worker()
        self._send_command("connect_bot")
        self.sync_audio_config(cfg)
        self.set_notify_channels(cfg.discord_notify_channels)
        self._send_command("list_devices")

    def set_notify_channels(self, channel_ids: list[str]) -> None:
        clean = [str(cid).strip() for cid in (channel_ids or []) if str(cid or "").strip()]
        self._send_command("set_notify_channels", {"channel_ids": clean}, spawn=False)

    def refresh_devices(self, *, spawn: bool = False) -> None:
        self._send_command("list_devices", spawn=spawn)

    def refresh_guilds(self) -> None:
        self._send_command("refresh_guilds", spawn=False)

    def join_channel(self, guild_id: int | str, channel_id: int | str) -> None:
        self._send_command(
            "join_channel",
            {"guild_id": str(guild_id), "channel_id": str(channel_id)},
            spawn=False,
        )

    def leave_channel(self) -> None:
        self._send_command("leave_channel", spawn=False)

    def set_mute(self, muted: bool) -> None:
        self._send_command("set_mute", {"muted": bool(muted)}, spawn=False)

    def set_deaf(self, deafened: bool) -> None:
        self._send_command("set_deaf", {"deafened": bool(deafened)}, spawn=False)

    def set_mic_test(self, enabled: bool) -> None:
        self._send_command("set_mic_test", {"enabled": bool(enabled)}, spawn=False)

    def set_output_volume(self, volume: int) -> None:
        self._send_command("set_output_volume", {"volume": int(volume)}, spawn=False)

    def set_mic_gain(self, gain: int) -> None:
        self._send_command("set_mic_gain", {"gain": int(gain)}, spawn=False)

    def set_mic_agc(self, enabled: bool) -> None:
        self._send_command("set_mic_agc", {"enabled": bool(enabled)}, spawn=False)

    def set_noise_suppression(self, mode: str) -> None:
        self._send_command("set_noise_suppression", {"mode": str(mode)}, spawn=False)

    def set_vad_threshold(self, threshold: int) -> None:
        self._send_command("set_vad_threshold", {"threshold": int(threshold)}, spawn=False)

    def set_voice_fx(
        self,
        *,
        enabled: bool,
        pitch: int,
        tone: int,
        robot: int,
        echo: int,
    ) -> None:
        self._send_command(
            "set_voice_fx",
            {
                "enabled": bool(enabled),
                "pitch": int(pitch),
                "tone": int(tone),
                "robot": int(robot),
                "echo": int(echo),
            },
            spawn=False,
        )

    def set_user_volume(self, user_id: int | str, volume: int) -> None:
        self._send_command(
            "set_user_volume",
            {"user_id": str(user_id), "volume": int(volume)},
            spawn=False,
        )

    def set_devices(self, input_device: str, output_device: str) -> None:
        self._send_command(
            "set_devices",
            {"input_device": str(input_device), "output_device": str(output_device)},
            spawn=False,
        )

    def select_guild(self, guild_id: int | str) -> None:
        self._send_command("select_guild", {"guild_id": str(guild_id)}, spawn=False)

    def open_text_channel(
        self, guild_id: int | str, channel_id: int | str, limit: int = 50
    ) -> None:
        self._send_command(
            "open_text_channel",
            {"guild_id": str(guild_id), "channel_id": str(channel_id), "limit": int(limit)},
            spawn=False,
        )

    def open_dm(self, user_id: int | str, limit: int = 50) -> None:
        self._send_command(
            "open_dm",
            {"user_id": str(user_id), "limit": int(limit)},
            spawn=False,
        )

    def send_chat_message(
        self,
        kind: str,
        target_id: int | str,
        content: str,
        file_paths: list[str] | None = None,
        cleanup_paths: list[str] | None = None,
        reply_to_id: int | str | None = None,
        *,
        await_result: bool = False,
    ) -> Future[Any] | None:
        payload: dict[str, Any] = {
            "kind": str(kind),
            "target_id": str(target_id),
            "content": str(content),
        }
        if file_paths:
            payload["file_paths"] = [str(p) for p in file_paths if str(p or "").strip()]
        if cleanup_paths:
            payload["cleanup_paths"] = [str(p) for p in cleanup_paths if str(p or "").strip()]
        if reply_to_id is not None and str(reply_to_id).strip():
            payload["reply_to_id"] = str(reply_to_id).strip()
        return self._send_command(
            "send_chat_message",
            payload,
            spawn=False,
            await_result=await_result,
        )

    def edit_chat_message(
        self,
        kind: str,
        target_id: int | str,
        message_id: int | str,
        content: str,
    ) -> None:
        self._send_command(
            "edit_chat_message",
            {
                "kind": str(kind),
                "target_id": str(target_id),
                "message_id": str(message_id),
                "content": str(content),
            },
            spawn=False,
        )

    def set_screen_share_presence(self, share: dict[str, Any] | None) -> None:
        self._send_command(
            "set_screen_share_presence",
            {"share": dict(share) if isinstance(share, dict) else None},
            spawn=False,
        )

    def disconnect_and_kill(self) -> None:
        proc: subprocess.Popen[str] | None = None
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None and proc.stdin is not None:
                    proc.stdin.write('{"cmd": "shutdown"}\n')
                    proc.stdin.flush()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.wait(timeout=1.5)
                except Exception:
                    pass
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.8)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._fail_pending("Worker Discord desconectado.")
        self._state = "disconnected"
        self._bot_id = ""
        self._bot_name = ""
        self._bot_avatar_url = ""
        self._in_voice = False
        self._active_channel_name = ""
        if not self._closed:
            try:
                self.worker_ram_updated.emit(0.0, 0.0)
                self.state_changed.emit("disconnected", {})
            except RuntimeError:
                pass

    def stop_if_idle(self) -> None:
        if self.is_worker_running and not self._in_voice and self._state in ("disconnected", "error"):
            self.disconnect_and_kill()

    def close(self) -> None:
        self._closed = True
        self.disconnect_and_kill()
