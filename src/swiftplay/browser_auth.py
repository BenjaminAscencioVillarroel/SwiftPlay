"""Inicio de sesión con Google en 1 clic mediante perfil temporal de Chrome/Edge (CDP)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from websockets.sync.client import connect as ws_connect

from swiftplay.security import ALLOWED_COOKIE_DOMAINS, filter_youtube_netscape_cookies

_LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin"
    "?service=youtube&uilel=3&passive=true"
    "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin"
    "%3Faction_handle_signin%3Dtrue%26app%3Ddesktop"
    "%26next%3Dhttps%253A%252F%252Fwww.youtube.com%252F"
)


def find_chromium_browser() -> Path | None:
    """Busca Chrome o Microsoft Edge instalado en Windows."""
    candidates: list[Path] = []
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")

    candidates.extend(
        [
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
    )
    for exe in ("chrome", "msedge"):
        found = shutil.which(exe)
        if found:
            candidates.append(Path(found))

    for p in candidates:
        if p.exists():
            return p
    return None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _fetch_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=2.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cdp_call(ws, msg_id: int, method: str, params: dict | None = None) -> dict:
    payload = {"id": msg_id, "method": method, "params": params or {}}
    ws.send(json.dumps(payload))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        raw = ws.recv(timeout=5.0)
        data = json.loads(raw)
        if data.get("id") == msg_id:
            return data.get("result") or {}
    return {}


def _cookies_to_netscape(cookies: list[dict]) -> str:
    lines: list[str] = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = str(c.get("domain") or "").strip().lower()
        if domain not in ALLOWED_COOKIE_DOMAINS:
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not name or not value:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expires", 0)
        expires = str(max(0, int(exp))) if isinstance(exp, (int, float)) and exp > 0 else "0"
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return filter_youtube_netscape_cookies("\n".join(lines))


def _has_youtube_auth(cookies: list[dict]) -> bool:
    yt_names = {
        str(c.get("name") or "")
        for c in cookies
        if str(c.get("domain") or "").strip().lower() in ALLOWED_COOKIE_DOMAINS
    }
    has_sapisid = ("SAPISID" in yt_names) or ("__Secure-3PAPISID" in yt_names)
    has_sid = ("SID" in yt_names) or ("__Secure-3PSID" in yt_names)
    return has_sapisid and has_sid


def _page_is_on_youtube(targets: list[dict]) -> dict | None:
    for t in targets:
        if t.get("type") != "page":
            continue
        url = str(t.get("url") or "").lower()
        if (
            url.startswith("https://www.youtube.com")
            or url.startswith("https://music.youtube.com")
        ) and "accounts.google.com" not in url and "/signin" not in url:
            return t
    return None


def login_with_browser(
    status_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    timeout_sec: int = 300,
) -> str:
    """
    Abre una ventana temporal limpia de Chrome/Edge en Google Sign-In para YouTube.
    Al completar el login, captura solo cookies de YouTube via CDP y destruye el perfil temporal.
    """
    browser_exe = find_chromium_browser()
    if browser_exe is None:
        raise RuntimeError(
            "No se encontró Google Chrome ni Microsoft Edge instalado en el equipo."
        )

    port = _pick_free_port()
    profile_dir = tempfile.mkdtemp(prefix="swiftplay_login_")
    proc: subprocess.Popen | None = None
    msg_id = 0

    def _emit(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    try:
        _emit(f"Abriendo ventana segura de inicio de sesión ({browser_exe.name})...")
        args = [
            str(browser_exe),
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--window-size=520,700",
            f"--app={_LOGIN_URL}",
        ]
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Esperar a que el puerto CDP responda
        cdp_ready = False
        wait_deadline = time.monotonic() + 15.0
        while time.monotonic() < wait_deadline:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Inicio de sesión cancelado.")
            if proc.poll() is not None:
                raise RuntimeError("El navegador se cerró antes de iniciar.")
            try:
                _fetch_json(f"http://127.0.0.1:{port}/json/version")
                cdp_ready = True
                break
            except Exception:
                time.sleep(0.35)

        if not cdp_ready:
            raise RuntimeError("No se pudo conectar con la ventana de inicio de sesión.")

        _emit("Inicia sesión con tu cuenta de Google en la ventana que se abrió...")
        login_deadline = time.monotonic() + timeout_sec

        while time.monotonic() < login_deadline:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Inicio de sesión cancelado.")
            if proc.poll() is not None:
                raise RuntimeError(
                    "Se cerró la ventana antes de completar el inicio de sesión."
                )

            try:
                targets = _fetch_json(f"http://127.0.0.1:{port}/json")
                if not isinstance(targets, list):
                    targets = []
            except Exception:
                time.sleep(0.7)
                continue

            yt_target = _page_is_on_youtube(targets)
            if yt_target and yt_target.get("webSocketDebuggerUrl"):
                ws_url = str(yt_target["webSocketDebuggerUrl"])
                try:
                    with ws_connect(
                        ws_url,
                        open_timeout=5,
                        close_timeout=2,
                        max_size=16 * 1024 * 1024,
                    ) as ws:
                        msg_id += 1
                        res = _cdp_call(ws, msg_id, "Network.getAllCookies")
                        cookies = res.get("cookies") or []
                        if not cookies:
                            msg_id += 1
                            res = _cdp_call(ws, msg_id, "Storage.getCookies")
                            cookies = res.get("cookies") or []

                        if _has_youtube_auth(cookies):
                            _emit("Sincronizando sesión de YouTube Music...")
                            msg_id += 1
                            _cdp_call(
                                ws,
                                msg_id,
                                "Page.navigate",
                                {"url": "https://music.youtube.com/"},
                            )
                            for _ in range(4):
                                if cancel_event and cancel_event.is_set():
                                    raise RuntimeError("Inicio de sesión cancelado.")
                                time.sleep(0.5)

                            msg_id += 1
                            res2 = _cdp_call(ws, msg_id, "Network.getAllCookies")
                            final_cookies = res2.get("cookies") or cookies
                            netscape = _cookies_to_netscape(final_cookies)
                            if netscape:
                                return netscape
                except RuntimeError:
                    raise
                except Exception:
                    pass

            time.sleep(1.0)

        raise RuntimeError("Tiempo de espera agotado para iniciar sesión.")
    finally:
        try:
            ver = _fetch_json(f"http://127.0.0.1:{port}/json/version")
            b_ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
            if b_ws:
                with ws_connect(b_ws, open_timeout=2, close_timeout=1) as ws:
                    _cdp_call(ws, 9999, "Browser.close")
        except Exception:
            pass
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2.5)
            except Exception:
                proc.terminate()
        shutil.rmtree(profile_dir, ignore_errors=True)
