"""Subproceso worker aislado para yt-dlp con auto-apagado por inactividad."""

from __future__ import annotations

import atexit
import contextlib
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Iterator

from swiftplay.paths import APP_ROOT, BIN_DIR
from swiftplay.windows_os import (
    assign_process_to_child_job,
    resolve_worker_python_and_env,
    set_process_app_user_model_id,
)

_IDLE_TIMEOUT_SEC = 45.0
_MAX_WORKER_REQUESTS = 15
_ACTIVE_CLIENTS: weakref.WeakSet[YtdlpWorkerClient] = weakref.WeakSet()


def _cleanup_all_clients() -> None:
    for client in list(_ACTIVE_CLIENTS):
        try:
            client.close()
        except Exception:
            pass


atexit.register(_cleanup_all_clients)


def _get_worker_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


class YtdlpWorkerClient:
    """Gestiona un subproceso worker de yt-dlp para mantener el proceso GUI libre de yt_dlp."""

    def __init__(
        self,
        idle_timeout: float = _IDLE_TIMEOUT_SEC,
        max_requests: int = _MAX_WORKER_REQUESTS,
    ) -> None:
        self._idle_timeout = float(idle_timeout)
        self._max_requests = max(1, int(max_requests))
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._closed = False
        _ACTIVE_CLIENTS.add(self)

    @property
    def worker_pid(self) -> int | None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return int(self._proc.pid)
            return None

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        src_dir = str(APP_ROOT / "src")
        prev_pp = env.get("PYTHONPATH", "")
        if src_dir not in prev_pp.split(os.pathsep):
            env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{prev_pp}" if prev_pp else src_dir
        return env

    def _spawn(self, oneshot: bool = False) -> subprocess.Popen[str]:
        worker_exe, worker_env = resolve_worker_python_and_env(APP_ROOT)
        cmd = [worker_exe, "-u", "-m", "swiftplay.ytdlp_worker"]
        if oneshot:
            cmd.append("--oneshot")
        else:
            cmd.extend(
                [
                    "--idle-timeout",
                    str(self._idle_timeout),
                    "--max-requests",
                    str(self._max_requests),
                ]
            )
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
        return proc

    def _terminate_proc(self, proc: subprocess.Popen[str] | None) -> None:
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=0.8)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _ensure_proc(self) -> subprocess.Popen[str]:
        if self._closed:
            raise RuntimeError("Worker yt-dlp cerrado.")
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._terminate_proc(self._proc)
        self._proc = self._spawn(oneshot=False)
        return self._proc

    def request(self, payload: dict[str, Any], timeout: float = 45.0) -> Any:
        with self._lock:
            last_err: Exception | None = None
            for attempt in range(2):
                if self._closed:
                    raise RuntimeError("Worker yt-dlp cerrado.")
                proc = self._ensure_proc()
                timed_out = threading.Event()

                def _kill_on_timeout(p: subprocess.Popen[str] = proc) -> None:
                    timed_out.set()
                    try:
                        p.kill()
                    except Exception:
                        pass

                timer = threading.Timer(timeout, _kill_on_timeout)
                timer.daemon = True
                timer.start()
                try:
                    if proc.stdin is None or proc.stdout is None:
                        raise RuntimeError("Pipes del worker no disponibles.")
                    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    proc.stdin.flush()

                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            raise EOFError("El worker yt-dlp cerró la conexión.")
                        line = line.strip()
                        if not line:
                            continue
                        msg = json.loads(line)
                        if msg.get("type") == "result":
                            if not msg.get("ok"):
                                raise RuntimeError(str(msg.get("error") or "Error en worker yt-dlp."))
                            return msg.get("data")
                except (BrokenPipeError, EOFError, OSError) as exc:
                    last_err = exc
                    self._terminate_proc(proc)
                    if self._proc is proc:
                        self._proc = None
                    if timed_out.is_set():
                        raise TimeoutError("Tiempo de espera agotado en worker yt-dlp.") from exc
                    if attempt == 0 and not self._closed:
                        continue
                    raise RuntimeError(f"Error de comunicación con worker yt-dlp: {exc}") from exc
                finally:
                    timer.cancel()

            raise RuntimeError(f"No se pudo completar la petición en worker yt-dlp: {last_err}")

    def run_download(
        self,
        payload: dict[str, Any],
        progress_cb: Callable[[float, str], None] | None = None,
        timeout: float = 3600.0,
    ) -> str:
        if self._closed:
            raise RuntimeError("Worker yt-dlp cerrado.")
        proc = self._spawn(oneshot=True)
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            try:
                proc.kill()
            except Exception:
                pass

        timer = threading.Timer(timeout, _kill_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError("Pipes de descarga no disponibles.")
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            proc.stdin.close()

            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "progress":
                    if progress_cb is not None:
                        progress_cb(float(msg.get("pct") or 0.0), str(msg.get("msg") or ""))
                elif mtype == "result":
                    if not msg.get("ok"):
                        raise RuntimeError(str(msg.get("error") or "Error al descargar."))
                    return str(msg.get("data") or "")

            if timed_out.is_set():
                raise TimeoutError("Tiempo de espera agotado durante la descarga.")
            raise RuntimeError("El proceso de descarga finalizó sin respuesta.")
        finally:
            timer.cancel()
            self._terminate_proc(proc)

    def close(self) -> None:
        proc: subprocess.Popen[str] | None = None
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None and proc.stdin is not None:
                    proc.stdin.write('{"cmd": "shutdown"}\n')
                    proc.stdin.flush()
            except Exception:
                pass
            self._terminate_proc(proc)

    def stop_idle(self) -> None:
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
            self._terminate_proc(proc)

    def reset_for_auth(self) -> None:
        self.stop_idle()


# ----------------------------------------------------------------------
# Código ejecutado únicamente dentro del subproceso worker aislado
# ----------------------------------------------------------------------


@contextlib.contextmanager
def _cookiefile_context() -> Iterator[str | None]:
    from swiftplay.security import load_cookies_decrypted

    decrypted = load_cookies_decrypted()
    if not decrypted:
        yield None
        return

    fd, tmp_path = tempfile.mkstemp(prefix="sp_ck_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(decrypted)
        yield tmp_path
    finally:
        try:
            p = Path(tmp_path)
            if p.exists():
                size = p.stat().st_size
                if size > 0:
                    p.write_bytes(b"\x00" * size)
                p.unlink(missing_ok=True)
        except Exception:
            pass


def _base_ydl_opts(cookiefile: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "js_runtimes": {"node": {}},
        "ffmpeg_location": str(BIN_DIR),
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _make_youtube_ydl(opts: dict[str, Any], load_extractors: bool = True):
    from yt_dlp import YoutubeDL

    if not load_extractors:
        return YoutubeDL(opts, auto_init=False)

    try:
        from yt_dlp.extractor.youtube import YoutubeIE, YoutubeSearchIE, YoutubeYtBeIE

        ydl = YoutubeDL(opts, auto_init=False)
        for cls in (YoutubeIE, YoutubeSearchIE, YoutubeYtBeIE):
            ydl.add_info_extractor(cls(ydl))
        return ydl
    except ImportError:
        return YoutubeDL(opts)


def _should_retry_without_cookies(err_text: str) -> bool:
    """Detecta fallos de extracción/descarga YT con cookies para reintentar sin sesión."""
    low = str(err_text or "").lower()
    return any(
        tok in low
        for tok in (
            "requested format is not available",
            "no video formats found",
            "signature solving failed",
            "n challenge",
            "the page needs to be reloaded",
            "po token",
            "http error 403",
            "url reproducible",
        )
    )


def _should_retry_with_cookies(err_text: str) -> bool:
    """Detecta errores donde probar con sesión/cookies puede destrabar videos privados o restringidos."""
    low = str(err_text or "").lower()
    return any(
        tok in low
        for tok in (
            "sign in",
            "confirm your age",
            "age-restricted",
            "private video",
            "members-only",
            "members only",
            "join this channel",
            "login required",
            "cookies",
            "not a bot",
            "requested format is not available",
            "no video formats found",
            "http error 403",
        )
    )


def _handle_resolve_youtube(req: dict[str, Any]) -> dict[str, Any]:
    from swiftplay.youtube_service import QUALITY_HEIGHT_MAP, _pick_best_thumb

    video_id = str(req.get("video_id") or "")
    audio_only = bool(req.get("audio_only", False))
    quality = str(req.get("quality") or "720p")
    url = f"https://www.youtube.com/watch?v={video_id}"
    max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
    if audio_only:
        fmt = (
            "bestaudio[protocol*=m3u8]/"
            "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"
        )
    else:
        fmt = (
            f"bestvideo[height<={max_h}][vcodec!*=av01][protocol*=m3u8]+bestaudio[protocol*=m3u8]/"
            f"bestvideo[height<={max_h}][protocol*=m3u8]+bestaudio[protocol*=m3u8]/"
            f"best[height<={max_h}][protocol*=m3u8]/"
            f"bestvideo[height<={max_h}][vcodec!*=av01]+bestaudio/"
            f"bestvideo[height<={max_h}]+bestaudio/"
            f"best[height<={max_h}]/best"
        )

    def _extract(cookiefile: str | None, *, skip_js: bool = False) -> dict[str, Any]:
        opts = _base_ydl_opts(cookiefile)
        opts["format"] = fmt
        opts["noplaylist"] = True
        opts["skip_download"] = True
        opts["noprogress"] = True
        opts["no_color"] = True
        if skip_js:
            opts["js_runtimes"] = {}
            opts["extractor_args"] = {"youtube": {"player_skip": ["configs", "js"]}}
        else:
            opts["extractor_args"] = {"youtube": {"player_skip": ["configs"]}}
        with _make_youtube_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not isinstance(info, dict):
            raise RuntimeError("No se pudo obtener la información del stream.")

        requested = info.get("requested_formats")
        primary_url = ""
        audio_url: str | None = None
        headers: dict[str, str] = {}

        if requested and len(requested) >= 2:
            v_fmt = requested[0]
            a_fmt = requested[1]
            if audio_only:
                primary_url = str(a_fmt.get("url") or v_fmt.get("url") or "")
                headers = dict(a_fmt.get("http_headers") or {})
            else:
                primary_url = str(v_fmt.get("url") or "")
                audio_url = str(a_fmt.get("url") or "") or None
                headers = dict(v_fmt.get("http_headers") or {})
        else:
            primary_url = str(info.get("url") or "")
            headers = dict(info.get("http_headers") or {})

        if not primary_url:
            raise RuntimeError("yt-dlp no devolvió una URL reproducible.")

        is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
        return {
            "video_id": video_id,
            "primary_url": primary_url,
            "audio_url": audio_url,
            "http_headers": headers,
            "duration": float(info.get("duration") or 0.0),
            "title": str(info.get("title") or ""),
            "author": str(info.get("channel") or info.get("uploader") or ""),
            "thumb_url": _pick_best_thumb(info.get("thumbnails"), video_id),
            "video_enabled": not audio_only,
            "is_live": is_live,
            "live_start_ts": (
                float(info.get("release_timestamp") or 0.0) if is_live else 0.0
            ),
        }

    try:
        try:
            # Ruta rápida HLS-first sin cookies ni solver JS (evita throttling 17 KB/s y acelera ~2x).
            return _extract(None, skip_js=True)
        except Exception:
            try:
                return _extract(None, skip_js=False)
            except Exception as exc_no_ck:
                if _should_retry_with_cookies(str(exc_no_ck)):
                    with _cookiefile_context() as ck:
                        if ck is not None:
                            try:
                                return _extract(ck, skip_js=False)
                            except Exception:
                                raise exc_no_ck
                raise
    finally:
        gc.collect()


def _handle_search_youtube(req: dict[str, Any]) -> list[dict[str, Any]]:
    from swiftplay.models import MediaItem, format_duration
    from swiftplay.youtube_service import _pick_best_thumb, is_short_video

    query = str(req.get("query") or "").strip()
    limit = int(req.get("limit") or 16)
    block_shorts = bool(req.get("block_shorts", True))
    if not query:
        return []

    fetch_n = limit + 8 if block_shorts else limit
    try:
        with _cookiefile_context() as ck:
            opts = _base_ydl_opts(ck)
            opts["extract_flat"] = "in_playlist"
            opts["skip_download"] = True
            with _make_youtube_ydl(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{fetch_n}:{query}", download=False)

        entries = info.get("entries") or [] if isinstance(info, dict) else []
        results: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            vid = str(entry.get("id") or "")
            title = str(entry.get("title") or "").strip()
            if not vid or not title:
                continue
            dur_sec = int(entry.get("duration") or 0)
            url_str = str(entry.get("url") or entry.get("webpage_url") or "")
            is_live = bool(entry.get("is_live"))
            if block_shorts and is_short_video(
                dur_sec, url=url_str, title=title, is_live=is_live
            ):
                continue
            dur_str = entry.get("duration_string") or format_duration(dur_sec)
            author = str(
                entry.get("channel") or entry.get("uploader") or "YouTube"
            ).strip()
            thumb = _pick_best_thumb(entry.get("thumbnails"), vid)
            results.append(
                MediaItem(
                    video_id=vid,
                    title=title,
                    author=author,
                    duration=str(dur_str),
                    duration_sec=dur_sec,
                    thumb_url=thumb,
                    kind="video",
                ).to_dict()
            )
            if len(results) >= limit:
                break
        return results
    finally:
        gc.collect()


def _handle_download_media(
    req: dict[str, Any], emit_progress: Callable[[float, str], None]
) -> str:
    from swiftplay.youtube_service import QUALITY_HEIGHT_MAP

    video_id = str(req.get("video_id") or "")
    mode = str(req.get("mode") or "video")
    out_path = Path(str(req.get("output_dir") or ""))
    quality = str(req.get("quality") or "720p")

    out_path.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
    downloaded_file: list[Path] = []
    last_emit = [0.0]

    def _hook(d: dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if (now - last_emit[0]) < 0.2:
                return
            last_emit[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100.0) if total > 0 else 0.0
            emit_progress(pct, "Descargando...")
        elif status == "finished":
            fn = d.get("filename")
            if fn:
                downloaded_file.append(Path(fn))
            emit_progress(100.0, "Procesando archivo...")

    def _run_dl(cookiefile: str | None) -> Path:
        opts = _base_ydl_opts(cookiefile)
        opts["noplaylist"] = True
        opts["http_chunk_size"] = 10 * 1024 * 1024
        opts["outtmpl"] = str(out_path / "%(title).120s [%(id)s].%(ext)s")
        opts["progress_hooks"] = [_hook]

        if mode == "music":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "0",
                },
                {"key": "FFmpegMetadata"},
            ]
        else:
            opts["format"] = (
                f"bestvideo[height<={max_h}][vcodec!*=av01]+bestaudio/"
                f"bestvideo[height<={max_h}]+bestaudio/"
                f"best[height<={max_h}]/best"
            )
            opts["merge_output_format"] = "mp4"

        with _make_youtube_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))

    try:
        try:
            prepared = _run_dl(None)
        except Exception as exc_no_ck:
            if _should_retry_with_cookies(str(exc_no_ck)):
                with _cookiefile_context() as ck:
                    if ck is not None:
                        downloaded_file.clear()
                        try:
                            prepared = _run_dl(ck)
                        except Exception:
                            raise exc_no_ck
                    else:
                        raise
            else:
                raise

        if mode == "music":
            final = prepared.with_suffix(".m4a")
            if final.exists():
                return str(final)
        else:
            final = prepared.with_suffix(".mp4")
            if final.exists():
                return str(final)

        if downloaded_file and downloaded_file[-1].exists():
            return str(downloaded_file[-1])
        return str(prepared)
    finally:
        gc.collect()


def _handle_extract_chrome_cookies() -> str:
    from swiftplay.security import ALLOWED_COOKIE_DOMAINS, filter_youtube_netscape_cookies

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "cookiesfrombrowser": ("chrome",),
        "skip_download": True,
    }
    try:
        with _make_youtube_ydl(opts, load_extractors=False) as ydl:
            jar = ydl.cookiejar
            lines = ["# Netscape HTTP Cookie File"]
            for cookie in jar:
                domain = (cookie.domain or "").strip().lower()
                if domain in ALLOWED_COOKIE_DOMAINS:
                    flag = "TRUE" if domain.startswith(".") else "FALSE"
                    secure = "TRUE" if cookie.secure else "FALSE"
                    expires = str(int(cookie.expires or 0))
                    lines.append(
                        f"{cookie.domain}\t{flag}\t{cookie.path or '/'}\t{secure}\t{expires}\t{cookie.name}\t{cookie.value}"
                    )
        filtered = filter_youtube_netscape_cookies("\n".join(lines))
        if not filtered:
            raise RuntimeError(
                "Chrome bloqueó la lectura directa. Usa el botón 'Iniciar sesión con Google (Navegador)'."
            )
        return filtered
    finally:
        gc.collect()


def _handle_resolve_twitch(req: dict[str, Any]) -> dict[str, Any]:
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.twitch import TwitchStreamIE

    from swiftplay.twitch_service import _USER_AGENT
    from swiftplay.youtube_service import QUALITY_HEIGHT_MAP

    login = str(req.get("login") or "").strip().lstrip("#@").lower()
    audio_only = bool(req.get("audio_only", False))
    quality = str(req.get("quality") or "720p")
    max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
    fmt = "audio_only/bestaudio/worst" if audio_only else f"best[height<={max_h}]/best"

    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "no_color": True,
            "skip_download": True,
            "format": fmt,
        }
        ydl = YoutubeDL(opts, auto_init=False)
        ydl.add_info_extractor(TwitchStreamIE(ydl))
        with ydl:
            info = ydl.extract_info(f"https://www.twitch.tv/{login}", download=False)
        if not isinstance(info, dict) or not info.get("url"):
            raise RuntimeError(f"El canal '{login}' no está en directo en este momento.")
        return {
            "video_id": login,
            "primary_url": str(info["url"]),
            "audio_url": None,
            "http_headers": dict(info.get("http_headers") or {"User-Agent": _USER_AGENT}),
            "duration": 0.0,
            "title": str(info.get("title") or ""),
            "author": str(info.get("uploader") or login),
            "thumb_url": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-440x248.jpg",
            "video_enabled": not audio_only,
        }
    finally:
        gc.collect()


def _write_json_line(proto_out: Any, obj: dict[str, Any]) -> None:
    proto_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proto_out.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import locale
    import site

    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        site_dir = os.path.join(venv_env, "Lib", "site-packages")
        if os.path.isdir(site_dir):
            site.addsitedir(site_dir)
    set_process_app_user_model_id()

    proto_out = sys.stdout
    sys.stdout = sys.stderr

    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--oneshot", action="store_true")
    parser.add_argument("--idle-timeout", type=float, default=_IDLE_TIMEOUT_SEC)
    parser.add_argument("--max-requests", type=int, default=_MAX_WORKER_REQUESTS)
    args, _ = parser.parse_known_args(argv)

    state_lock = threading.Lock()
    last_activity = [time.monotonic()]
    in_request = [False]

    if not args.oneshot and args.idle_timeout > 0:
        def _watchdog() -> None:
            while True:
                time.sleep(1.0)
                with state_lock:
                    idle = (not in_request[0]) and (
                        (time.monotonic() - last_activity[0]) >= args.idle_timeout
                    )
                if idle:
                    os._exit(0)

        t = threading.Thread(target=_watchdog, daemon=True)
        t.start()

    requests_handled = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        raw = line.strip()
        if not raw:
            continue

        with state_lock:
            in_request[0] = True
            last_activity[0] = time.monotonic()

        try:
            req = json.loads(raw)
            cmd = str(req.get("cmd") or "")
            if cmd == "shutdown":
                _write_json_line(proto_out, {"type": "result", "ok": True, "data": None})
                break
            if cmd == "ping":
                _write_json_line(proto_out, {"type": "result", "ok": True, "data": "pong"})
            elif cmd == "resolve_youtube":
                _write_json_line(
                    proto_out,
                    {"type": "result", "ok": True, "data": _handle_resolve_youtube(req)},
                )
            elif cmd == "search_youtube":
                _write_json_line(
                    proto_out,
                    {"type": "result", "ok": True, "data": _handle_search_youtube(req)},
                )
            elif cmd == "download_media":
                path_str = _handle_download_media(
                    req,
                    lambda pct, msg: _write_json_line(
                        proto_out, {"type": "progress", "pct": pct, "msg": msg}
                    ),
                )
                _write_json_line(
                    proto_out, {"type": "result", "ok": True, "data": path_str}
                )
            elif cmd == "extract_chrome_cookies":
                _write_json_line(
                    proto_out,
                    {
                        "type": "result",
                        "ok": True,
                        "data": _handle_extract_chrome_cookies(),
                    },
                )
            elif cmd == "resolve_twitch":
                _write_json_line(
                    proto_out,
                    {"type": "result", "ok": True, "data": _handle_resolve_twitch(req)},
                )
            else:
                _write_json_line(
                    proto_out,
                    {"type": "result", "ok": False, "error": f"Comando desconocido: {cmd}"},
                )
        except Exception as exc:
            _write_json_line(
                proto_out, {"type": "result", "ok": False, "error": str(exc)}
            )
        finally:
            requests_handled += 1
            with state_lock:
                in_request[0] = False
                last_activity[0] = time.monotonic()

        if args.oneshot or requests_handled >= args.max_requests:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
