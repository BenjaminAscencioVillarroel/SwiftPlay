"""Cliente IPC liviano en Qt para compartida de pantalla aislada (0 MB en reposo).

Nunca importa aiohttp, websockets, numpy ni librerías pesadas en el proceso GUI.
"""

from __future__ import annotations

import atexit
from concurrent.futures import Future
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import urllib.request
import weakref
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from swiftplay.paths import BIN_DIR, USER_DATA_DIR
from swiftplay.windows_os import assign_process_to_child_job, resolve_worker_python_and_env

_ACTIVE_SCREEN_SERVICES: weakref.WeakSet[ScreenShareService] = weakref.WeakSet()

SCREEN_SHARE_ENDED_MARKER = "Transmisión de pantalla finalizada"
CLOUDFLARED_DOWNLOAD_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
)
_SCREEN_SHARE_URL_RE = re.compile(
    r"(https://[a-z0-9-]+\.trycloudflare\.com|http://127\.0\.0\.1:\d+)/watch\?t=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_PRESENCE_SHARE_RE = re.compile(
    r"sws1\|([a-z0-9-]+|127\.0\.0\.1:\d+)\|([A-Za-z0-9_-]{8,64})\|(\d{3,4}p)\|(\d{2})\|([01])(?:\|(\d+))?",
    re.IGNORECASE,
)


def _cleanup_all_screen_services() -> None:
    for svc in list(_ACTIVE_SCREEN_SERVICES):
        try:
            svc.close()
        except Exception:
            pass


atexit.register(_cleanup_all_screen_services)


def find_ffmpeg() -> Path | None:
    for cand in (BIN_DIR / "ffmpeg.exe", USER_DATA_DIR / "bin" / "ffmpeg.exe"):
        if cand.is_file():
            return cand
    found = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    return Path(found) if found else None


def find_cloudflared() -> Path | None:
    for cand in (
        USER_DATA_DIR / "bin" / "cloudflared.exe",
        BIN_DIR / "cloudflared.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "cloudflared"
        / "cloudflared.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "cloudflared"
        / "cloudflared.exe",
    ):
        if cand.is_file():
            return cand
    found = shutil.which("cloudflared.exe") or shutil.which("cloudflared")
    return Path(found) if found else None


def download_cloudflared(
    progress_cb: Callable[[int, int], None] | None = None,
) -> Path:
    """Descarga cloudflared-windows-amd64.exe de forma atómica en %LOCALAPPDATA%\\SwiftPlay\\bin."""
    dest_dir = USER_DATA_DIR / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "cloudflared.exe"
    tmp = dest_dir / "cloudflared.exe.tmp"

    req = urllib.request.Request(
        CLOUDFLARED_DOWNLOAD_URL,
        headers={"User-Agent": "SwiftPlay/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(131072)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total)
    tmp.replace(dest)
    return dest


def format_screen_share_announcement(
    watch_url: str,
    quality: str,
    fps: int,
    has_audio: bool,
    title: str = "",
) -> str:
    clean_title = (title or "Pantalla compartida").strip()
    audio_txt = "con audio del sistema" if has_audio else "sin audio"
    return (
        f"🖥️ **Pantalla compartida en vivo** · {clean_title} ({quality} {fps}fps · {audio_txt})\n"
        f"Ver en navegador (Discord oficial): {watch_url}\n"
        f"*(En SwiftPlay aparece el botón directo «Ver aquí»)*"
    )


def is_screen_share_ended_message(content: str) -> bool:
    return SCREEN_SHARE_ENDED_MARKER.lower() in str(content or "").lower()


def parse_screen_share_message(content: str) -> dict[str, str] | None:
    text = str(content or "")
    if not text or is_screen_share_ended_message(text):
        return None
    m = _SCREEN_SHARE_URL_RE.search(text)
    if not m:
        return None
    base = m.group(1).rstrip("/")
    token = m.group(2)
    watch_url = f"{base}/watch?t={token}"
    stream_url = f"{base}/stream.mp4?t={token}"
    first_line = text.splitlines()[0].strip() if text.splitlines() else "Pantalla compartida"
    return {
        "watch_url": watch_url,
        "stream_url": stream_url,
        "token": token,
        "summary": first_line,
    }


def encode_screen_share_presence(
    watch_url: str,
    *,
    quality: str = "720p",
    fps: int = 30,
    has_audio: bool = True,
    rev: int = 1,
) -> str:
    """Codifica un beacon compacto <=128 caracteres para descubrir transmisiones vía presencia Discord."""
    m = _SCREEN_SHARE_URL_RE.search(str(watch_url or "").strip())
    if not m:
        return ""
    raw_base = m.group(1).lower()
    token = m.group(2)
    if raw_base.startswith("http://127.0.0.1:"):
        host = raw_base[len("http://") :]
    elif raw_base.startswith("https://") and raw_base.endswith(".trycloudflare.com"):
        host = raw_base[len("https://") : -len(".trycloudflare.com")]
    else:
        return ""
    q = str(quality or "720p").strip()
    if q not in ("480p", "720p", "1080p"):
        q = "720p"
    f = 60 if int(fps or 30) >= 60 else 30
    a = 1 if has_audio else 0
    r = max(1, int(rev or 1))
    core = f"sws1|{host}|{token}|{q}|{f}|{a}|{r}"
    full = f"🔴 En vivo · {core}"
    if len(full) <= 128:
        return full
    return core if len(core) <= 128 else ""


def parse_screen_share_presence(text: str) -> dict[str, Any] | None:
    """Decodifica un beacon `sws1|...` presente en el estado/actividad de Discord."""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _PRESENCE_SHARE_RE.search(raw)
    if not m:
        return None
    host = m.group(1).lower()
    token = m.group(2)
    quality = m.group(3)
    fps = int(m.group(4))
    audio_flag = m.group(5)
    rev = int(m.group(6) or 1)
    base = f"http://{host}" if host.startswith("127.0.0.1:") else f"https://{host}.trycloudflare.com"
    return {
        "watch_url": f"{base}/watch?t={token}",
        "stream_url": f"{base}/stream.mp4?t={token}",
        "token": token,
        "quality": quality,
        "fps": fps,
        "has_audio": audio_flag == "1",
        "rev": rev,
        "source": "presence",
    }


class ScreenShareService(QObject):
    """Gestiona el ciclo de vida de screen_share_worker.py vía JSON-lines."""

    state_changed = Signal(str, dict)
    stats_updated = Signal(dict)
    worker_ram_updated = Signal(float, float)
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
        self._state = "idle"
        self._active_info: dict[str, Any] = {}
        self._child_pids: list[int] = []
        _ACTIVE_SCREEN_SERVICES.add(self)

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
    def active_pids(self) -> list[int]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return []
            pids: list[int] = [int(self._proc.pid)]
            seen = set(pids)
            for pid in self._child_pids:
                if pid > 0 and pid not in seen:
                    seen.add(pid)
                    pids.append(pid)
            return pids

    @property
    def is_sharing(self) -> bool:
        return self._state == "sharing"

    @property
    def active_info(self) -> dict[str, Any]:
        return dict(self._active_info)

    def _ensure_worker(self) -> subprocess.Popen[str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("ScreenShareService cerrado.")
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            worker_exe, worker_env = resolve_worker_python_and_env(Path(__file__).resolve().parent.parent.parent)
            cmd = [worker_exe, "-u", "-m", "swiftplay.screen_share_worker"]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=worker_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assign_process_to_child_job(proc)
            self._proc = proc
            self._reader_thread = threading.Thread(
                target=self._read_loop,
                args=(proc,),
                daemon=True,
                name="screen-share-ipc-reader",
            )
            self._reader_thread.start()
            return proc

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
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
                                RuntimeError(str(msg.get("error") or "Error en worker de pantalla"))
                            )
                elif mtype == "event":
                    self._handle_event(str(msg.get("event") or ""), msg.get("data"))
        finally:
            self._fail_pending("Worker de pantalla detenido.")
            reset_needed = False
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._child_pids = []
                    reset_needed = True
            if reset_needed and not self._closed:
                self._state = "idle"
                self._active_info = {}
                try:
                    self.state_changed.emit("idle", {})
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
                st = str(data.get("state") or "idle")
                self._state = st
                self._active_info = dict(data) if st == "sharing" else {}
                with self._lock:
                    if st == "sharing":
                        raw_pids = data.get("child_pids")
                        if isinstance(raw_pids, list):
                            self._child_pids = [int(p) for p in raw_pids if int(p or 0) > 0]
                    else:
                        self._child_pids = []
                self.state_changed.emit(st, data)
            elif event == "stats" and isinstance(data, dict):
                self.stats_updated.emit(data)
            elif event == "worker_ram" and isinstance(data, dict):
                raw_pids = data.get("child_pids")
                if isinstance(raw_pids, list):
                    with self._lock:
                        self._child_pids = [int(p) for p in raw_pids if int(p or 0) > 0]
                self.worker_ram_updated.emit(
                    float(data.get("ws_mb") or 0.0),
                    float(data.get("priv_mb") or 0.0),
                )
            elif event == "error" and isinstance(data, dict):
                self.error_occurred.emit(str(data.get("message") or "Error de pantalla compartida"))
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
            raise RuntimeError("No se pudo iniciar el worker de pantalla.")
        return str(fut.result(timeout=timeout))

    def list_sources_sync(self, timeout: float = 8.0) -> list[dict[str, Any]]:
        fut = self._send_command("list_sources", spawn=True, await_result=True)
        if fut is None:
            return []
        res = fut.result(timeout=timeout)
        return list(res) if isinstance(res, list) else []

    def start_share(
        self,
        *,
        source_id: str,
        quality: str = "720p",
        fps: int = 30,
        include_audio: bool = True,
        title: str = "",
        use_tunnel: bool = True,
        exclude_pid: int = 0,
        await_result: bool = False,
        timeout: float = 25.0,
    ) -> dict[str, Any] | None:
        payload = {
            "source_id": str(source_id),
            "quality": str(quality),
            "fps": int(fps),
            "include_audio": bool(include_audio),
            "title": str(title),
            "use_tunnel": bool(use_tunnel),
            "exclude_pid": max(0, int(exclude_pid or 0)),
        }
        fut = self._send_command("start", payload, spawn=True, await_result=await_result)
        if await_result and fut is not None:
            res = fut.result(timeout=timeout)
            return dict(res) if isinstance(res, dict) else None
        return None

    def reconfigure_share(
        self,
        *,
        source_id: str,
        quality: str = "720p",
        fps: int = 30,
        include_audio: bool = True,
        title: str = "",
        exclude_pid: int = 0,
        await_result: bool = False,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        payload = {
            "source_id": str(source_id),
            "quality": str(quality),
            "fps": int(fps),
            "include_audio": bool(include_audio),
            "title": str(title),
            "exclude_pid": max(0, int(exclude_pid or 0)),
        }
        spawn = not self.is_worker_running
        fut = self._send_command("reconfigure", payload, spawn=spawn, await_result=await_result)
        if await_result and fut is not None:
            res = fut.result(timeout=timeout)
            return dict(res) if isinstance(res, dict) else None
        return None

    def stop_share(self) -> None:
        proc: subprocess.Popen[str] | None = None
        with self._lock:
            proc = self._proc
            self._proc = None
            self._child_pids = []
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
        self._fail_pending("Compartida de pantalla detenida.")
        self._state = "idle"
        self._active_info = {}
        if not self._closed:
            try:
                self.worker_ram_updated.emit(0.0, 0.0)
                self.state_changed.emit("idle", {})
            except RuntimeError:
                pass

    def close(self) -> None:
        self._closed = True
        self.stop_share()
