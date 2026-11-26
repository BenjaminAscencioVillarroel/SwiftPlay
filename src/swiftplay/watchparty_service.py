"""Servicio WebSocket ultraliviano de WatchParty (presencia en vivo y control cruzado)."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from typing import Any, Literal
from urllib.parse import urlencode, urlparse, urlunparse

from PySide6.QtCore import QObject, Signal

VALID_WP_STATUSES: tuple[str, ...] = ("active", "idle", "offline")


def normalize_relay_ws_url(raw: str) -> str:
    """Normaliza la URL del Worker de Cloudflare a endpoint WebSocket `/ws`."""
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if txt.startswith("https://"):
        txt = "wss://" + txt[len("https://") :]
    elif txt.startswith("http://"):
        txt = "ws://" + txt[len("http://") :]
    elif "://" not in txt:
        txt = "wss://" + txt

    parsed = urlparse(txt)
    scheme = (parsed.scheme or "wss").lower()
    if scheme not in ("ws", "wss"):
        scheme = "wss"
    path = parsed.path or ""
    if not path or path == "/":
        path = "/ws"
    elif path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def build_relay_connect_url(relay_url: str, code: str, secret: str) -> str:
    base = normalize_relay_ws_url(relay_url)
    if not base:
        return ""
    q = urlencode({"code": str(code).strip().upper(), "secret": str(secret).strip()})
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{q}"


def compute_live_position(state: dict[str, Any] | None, now: float | None = None) -> float:
    """Estima la posición actual de un deck según `pos`, `paused`, `speed` y `updated_at`."""
    if not isinstance(state, dict):
        return 0.0
    try:
        base_pos = max(0.0, float(state.get("pos") or 0.0))
    except (TypeError, ValueError):
        base_pos = 0.0
    if bool(state.get("is_live")) or state.get("kind") == "twitch":
        return 0.0
    if bool(state.get("paused")):
        return base_pos
    try:
        updated_at = float(state.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    if updated_at <= 0.0:
        return base_pos
    try:
        speed = max(0.5, min(3.0, float(state.get("speed") or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0
    cur_now = float(time.time() if now is None else now)
    elapsed = max(0.0, min(86400.0, cur_now - updated_at))
    est = base_pos + elapsed * speed
    try:
        dur = max(0.0, float(state.get("duration_sec") or 0.0))
    except (TypeError, ValueError):
        dur = 0.0
    if dur > 0.0:
        est = min(dur, est)
    return round(est, 2)


def _presence_deck_fingerprint(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Construye una huella estable de presencia omitiendo `updated_at` y `pos` en curso."""
    if not isinstance(state, dict):
        return None
    paused = bool(state.get("paused"))
    fp: dict[str, Any] = {
        "video_id": str(state.get("video_id") or ""),
        "title": str(state.get("title") or ""),
        "author": str(state.get("author") or ""),
        "kind": str(state.get("kind") or "video"),
        "is_live": bool(state.get("is_live")),
        "paused": paused,
        "duration_sec": int(state.get("duration_sec") or 0),
        "speed": float(state.get("speed") or 1.0),
    }
    if paused:
        try:
            fp["pos"] = round(max(0.0, float(state.get("pos") or 0.0)), 1)
        except (TypeError, ValueError):
            fp["pos"] = 0.0
    if "queue" in state:
        fp["queue"] = [
            str(q.get("video_id") or "")
            for q in (state.get("queue") or [])
            if isinstance(q, dict)
        ]
        fp["queue_index"] = int(state.get("queue_index", 0) or 0)
    return fp


def _has_large_live_pos_jump(
    prev: dict[str, Any] | None,
    curr: dict[str, Any] | None,
) -> bool:
    """Detecta saltos grandes de posición (>= 4 s) sobre el mismo medio en reproducción."""
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return False
    if (
        str(prev.get("video_id") or "") != str(curr.get("video_id") or "")
        or str(prev.get("kind") or "") != str(curr.get("kind") or "")
    ):
        return False
    if bool(prev.get("paused")) or bool(curr.get("paused")):
        return False
    if (
        bool(prev.get("is_live"))
        or bool(curr.get("is_live"))
        or prev.get("kind") == "twitch"
        or curr.get("kind") == "twitch"
    ):
        return False
    now_ts = time.time()
    return abs(
        compute_live_position(prev, now=now_ts) - compute_live_position(curr, now=now_ts)
    ) >= 4.0


def compute_presence_status(
    video_state: dict[str, Any] | None,
    music_state: dict[str, Any] | None,
    os_idle_sec: float,
    discord_speaking_recent: bool,
    afk_minutes: int = 8,
) -> Literal["active", "idle"]:
    """Calcula `active` vs `idle` (sin nada abierto, o >= 8 min AFK pausado sin voz)."""
    has_video = isinstance(video_state, dict) and bool(
        video_state.get("video_id") or video_state.get("title")
    )
    has_music = isinstance(music_state, dict) and bool(
        music_state.get("video_id") or music_state.get("title")
    )
    if not has_video and not has_music:
        return "idle"

    vid_playing = has_video and not bool(video_state.get("paused"))  # type: ignore[union-attr]
    mus_playing = has_music and not bool(music_state.get("paused"))  # type: ignore[union-attr]
    if vid_playing or mus_playing:
        return "active"

    if discord_speaking_recent:
        return "active"

    threshold = max(60.0, float(max(1, int(afk_minutes))) * 60.0)
    if float(os_idle_sec or 0.0) >= threshold:
        return "idle"
    return "active"


class WatchPartyService(QObject):
    """Cliente WebSocket asíncrono aislado en hilo daemon para WatchParty en tiempo real."""

    connection_changed = Signal(str, str)  # (state: disconnected|connecting|connected|error, detail)
    friends_updated = Signal(list)  # list[dict]
    party_state_received = Signal(dict)
    party_members_updated = Signal(str, str, list, bool)  # host_code, deck, members, allow_guest
    party_notice = Signal(str)
    lookup_result = Signal(dict)
    direct_message_received = Signal(dict)
    direct_message_ack = Signal(dict)
    server_dm_support_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt = threading.Event()
        self._cmd_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._reconnect_evt: asyncio.Event | None = None

        self._conn_state: str = "disconnected"
        self._conn_detail: str = ""
        self._server_supports_dm: bool | None = None

        self._enabled: bool = True
        self._relay_url: str = ""
        self._code: str = ""
        self._secret: str = ""
        self._display_name: str = ""
        self._avatar_url: str = ""
        self._friend_codes: list[str] = []

        self._local_status: Literal["active", "idle"] = "idle"
        self._share_enabled: bool = True
        self._allow_guest_control: bool = True
        self._video_state: dict[str, Any] | None = None
        self._music_state: dict[str, Any] | None = None
        self._last_presence_fp: str = ""

        self._friends_by_code: dict[str, dict[str, Any]] = {}
        self._active_parties: dict[str, dict[str, Any]] = {}  # deck -> {host_code, members, allow_guest_control}

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._conn_state == "connected"

    @property
    def connection_state(self) -> tuple[str, str]:
        with self._lock:
            return self._conn_state, self._conn_detail

    @property
    def server_supports_dm(self) -> bool | None:
        with self._lock:
            return self._server_supports_dm

    @property
    def friends_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._friends_by_code.values()]

    def active_party_for(self, deck: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._active_parties.get(deck)
            return dict(entry) if entry else None

    def configure_and_start(
        self,
        *,
        enabled: bool,
        relay_url: str,
        code: str,
        secret: str,
        display_name: str,
        avatar_url: str = "",
        friend_codes: list[str] | None = None,
        share_enabled: bool = True,
        allow_guest_control: bool = True,
    ) -> None:
        norm_url = normalize_relay_ws_url(relay_url)
        clean_code = str(code or "").strip().upper()
        clean_secret = str(secret or "").strip()
        clean_friends = [str(c).strip().upper() for c in (friend_codes or []) if str(c).strip()]

        with self._lock:
            needs_reconnect = (
                norm_url != self._relay_url
                or clean_code != self._code
                or clean_secret != self._secret
                or bool(enabled) != self._enabled
            )
            self._enabled = bool(enabled)
            self._relay_url = norm_url
            self._code = clean_code
            self._secret = clean_secret
            self._display_name = str(display_name or "").strip() or clean_code
            self._avatar_url = str(avatar_url or "").strip()
            self._friend_codes = clean_friends
            self._share_enabled = bool(share_enabled)
            self._allow_guest_control = bool(allow_guest_control)

            if not self._enabled or not self._relay_url or not self._code or len(self._secret) < 16:
                should_stop = True
            else:
                should_stop = False

        if should_stop:
            self.stop()
            detail = "Configura la URL del servidor WatchParty" if self._enabled and not norm_url else ""
            self._set_connection_state("disconnected", detail)
            return

        with self._lock:
            running = self._thread is not None and self._thread.is_alive()

        if not running:
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._run_thread,
                name="SwiftPlayWatchParty",
                daemon=True,
            )
            self._thread.start()
        elif needs_reconnect:
            self._trigger_reconnect()
        else:
            self._enqueue(
                {
                    "type": "presence",
                    "name": self._display_name,
                    "avatar_url": self._avatar_url,
                    "status": self._local_status,
                    "share_enabled": self._share_enabled,
                    "allow_guest_control": self._allow_guest_control,
                }
            )
            self._enqueue({"type": "set_friends", "friends": list(clean_friends)})

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            loop = self._loop
            q = self._cmd_queue
            th = self._thread
            self._active_parties.clear()
        if loop is not None and not loop.is_closed():
            try:
                if q is not None:
                    loop.call_soon_threadsafe(q.put_nowait, {"_cmd": "stop"})
            except Exception:
                pass
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.5)
        with self._lock:
            self._thread = None
            self._loop = None
            self._cmd_queue = None
            self._reconnect_evt = None

    def set_friends(self, codes: list[str]) -> None:
        clean = [str(c).strip().upper() for c in (codes or []) if str(c).strip()]
        with self._lock:
            self._friend_codes = clean
            known_set = set(clean)
            for k in list(self._friends_by_code.keys()):
                if k not in known_set:
                    del self._friends_by_code[k]
        self._enqueue({"type": "set_friends", "friends": clean})

    def lookup_friend(self, code: str) -> None:
        clean = str(code or "").strip().upper()
        if not clean:
            return
        self._enqueue({"type": "lookup", "code": clean})

    def send_direct_message(self, to_code: str, text: str, client_id: str = "") -> str:
        """Envía un mensaje directo 1-a-1 a un amigo de WatchParty."""
        to = str(to_code or "").strip().upper()
        body = str(text or "").replace("\r\n", "\n").strip()[:1000]
        if not to or not body:
            return ""
        cid = str(client_id or "").strip()[:64] or f"wp_{int(time.time() * 1000)}_{secrets.token_hex(3)}"
        self._enqueue(
            {
                "type": "direct_message",
                "to": to,
                "text": body,
                "client_id": cid,
            }
        )
        return cid

    def _compute_presence_fp_locked(self) -> str:
        vid = self._video_state if self._share_enabled else None
        mus = self._music_state if self._share_enabled else None
        norm = {
            "name": self._display_name,
            "avatar_url": self._avatar_url,
            "status": self._local_status,
            "share_enabled": self._share_enabled,
            "allow_guest_control": self._allow_guest_control,
            "video": _presence_deck_fingerprint(vid),
            "music": _presence_deck_fingerprint(mus),
        }
        return json.dumps(norm, sort_keys=True, ensure_ascii=False)

    def record_local_deck_state(
        self,
        deck: Literal["video", "music"],
        state: dict[str, Any] | None,
        *,
        status: Literal["active", "idle"] | None = None,
    ) -> None:
        """Sincroniza en memoria el snapshot local tras enviar `party_sync` como anfitrión."""
        with self._lock:
            if status is not None:
                self._local_status = "idle" if status == "idle" else "active"
            copy_state = dict(state) if isinstance(state, dict) else None
            if deck == "music":
                self._music_state = copy_state
            else:
                self._video_state = copy_state
            self._last_presence_fp = self._compute_presence_fp_locked()

    def update_local_presence(
        self,
        *,
        status: Literal["active", "idle"],
        share_enabled: bool,
        allow_guest_control: bool,
        video_state: dict[str, Any] | None,
        music_state: dict[str, Any] | None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        force: bool = False,
    ) -> None:
        with self._lock:
            prev_vid = self._video_state if self._share_enabled else None
            prev_mus = self._music_state if self._share_enabled else None
            if display_name is not None:
                self._display_name = str(display_name).strip() or self._code
            if avatar_url is not None:
                self._avatar_url = str(avatar_url).strip()
            self._local_status = "idle" if status == "idle" else "active"
            self._share_enabled = bool(share_enabled)
            self._allow_guest_control = bool(allow_guest_control)
            self._video_state = dict(video_state) if isinstance(video_state, dict) else None
            self._music_state = dict(music_state) if isinstance(music_state, dict) else None

            new_vid = self._video_state if self._share_enabled else None
            new_mus = self._music_state if self._share_enabled else None
            fp = self._compute_presence_fp_locked()
            pos_jumped = _has_large_live_pos_jump(prev_vid, new_vid) or _has_large_live_pos_jump(
                prev_mus, new_mus
            )
            if not force and not pos_jumped and fp == self._last_presence_fp:
                return
            self._last_presence_fp = fp

            payload = {
                "type": "presence",
                "name": self._display_name,
                "avatar_url": self._avatar_url,
                "status": self._local_status,
                "share_enabled": self._share_enabled,
                "allow_guest_control": self._allow_guest_control,
                "video": new_vid,
                "music": new_mus,
            }
        self._enqueue(payload)

    def join_party(self, host_code: str, deck: Literal["video", "music"]) -> None:
        hc = str(host_code or "").strip().upper()
        dk = "music" if deck == "music" else "video"
        if not hc:
            return
        self._enqueue({"type": "join_party", "host_code": hc, "deck": dk})

    def leave_party(self, deck: Literal["video", "music"]) -> None:
        dk = "music" if deck == "music" else "video"
        with self._lock:
            self._active_parties.pop(dk, None)
        self._enqueue({"type": "leave_party", "deck": dk})

    def send_party_sync(
        self,
        host_code: str,
        deck: Literal["video", "music"],
        action: str,
        state: dict[str, Any] | None,
    ) -> None:
        hc = str(host_code or "").strip().upper()
        dk = "music" if deck == "music" else "video"
        if not hc:
            return
        self._enqueue(
            {
                "type": "party_sync",
                "host_code": hc,
                "deck": dk,
                "action": str(action or "sync"),
                "state": dict(state) if isinstance(state, dict) else None,
            }
        )

    def request_refresh(self) -> None:
        """Pide snapshot fresco de amigos o fuerza reconexión si el socket estaba caído/dormido."""
        with self._lock:
            enabled = self._enabled
            relay_url = self._relay_url
            code = self._code
            secret = self._secret
            running = self._thread is not None and self._thread.is_alive()
            connected = self._conn_state == "connected"
        if not enabled or not relay_url or not code or len(secret) < 16:
            return
        if not running:
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._run_thread,
                name="SwiftPlayWatchParty",
                daemon=True,
            )
            self._thread.start()
            return
        if connected:
            self._enqueue({"type": "refresh_friends"})
        else:
            self._trigger_reconnect()

    def force_reconnect(self) -> None:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
        if running:
            self._trigger_reconnect()

    def _trigger_reconnect(self) -> None:
        with self._lock:
            loop = self._loop
            ev = self._reconnect_evt
        if loop is not None and not loop.is_closed() and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:
                pass

    def _enqueue(self, payload: dict[str, Any]) -> None:
        with self._lock:
            loop = self._loop
            q = self._cmd_queue
        if loop is None or loop.is_closed() or q is None:
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, payload)
        except Exception:
            pass

    def _set_connection_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            changed = (state != self._conn_state) or (detail != self._conn_detail)
            self._conn_state = state
            self._conn_detail = detail
        if changed:
            self.connection_changed.emit(state, detail)

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            loop.run_until_complete(self._async_main())
        except Exception:
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass
            with self._lock:
                self._loop = None
                self._cmd_queue = None
                self._reconnect_evt = None

    async def _async_main(self) -> None:
        import websockets.asyncio.client as ws_client

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        reconnect_evt = asyncio.Event()
        with self._lock:
            self._cmd_queue = queue
            self._reconnect_evt = reconnect_evt

        backoff = 2.0
        while not self._stop_evt.is_set():
            reconnect_evt.clear()
            with self._lock:
                enabled = self._enabled
                relay_url = self._relay_url
                code = self._code
                secret = self._secret
            if not enabled or not relay_url or not code or len(secret) < 16:
                with self._lock:
                    self._server_supports_dm = None
                self._set_connection_state("disconnected", "")
                break

            with self._lock:
                self._server_supports_dm = None
            ws_url = build_relay_connect_url(relay_url, code, secret)
            self._set_connection_state("connecting", "")
            try:
                async with ws_client.connect(
                    ws_url,
                    open_timeout=10,
                    close_timeout=2,
                    ping_interval=None,
                    max_size=512 * 1024,
                ) as ws:
                    backoff = 2.0
                    # Conservar mensajes directos pendientes si hubo reconexión rápida y descartar presencias viejas
                    kept_dms: list[dict[str, Any]] = []
                    while not queue.empty():
                        try:
                            item = queue.get_nowait()
                            if item.get("_cmd") == "stop":
                                return
                            if item.get("type") == "direct_message":
                                kept_dms.append(item)
                        except asyncio.QueueEmpty:
                            break
                    for dm_cmd in kept_dms[-20:]:
                        queue.put_nowait(dm_cmd)

                    with self._lock:
                        hello_msg = {
                            "type": "hello",
                            "name": self._display_name or self._code,
                            "avatar_url": self._avatar_url,
                            "friends": list(self._friend_codes),
                            "status": self._local_status,
                            "share_enabled": self._share_enabled,
                            "allow_guest_control": self._allow_guest_control,
                            "video": self._video_state if self._share_enabled else None,
                            "music": self._music_state if self._share_enabled else None,
                        }
                        self._last_presence_fp = ""
                    await ws.send(json.dumps(hello_msg, ensure_ascii=False))
                    self._set_connection_state("connected", "")

                    # Re-join active rooms cleanly if a connection dropped temporarily
                    with self._lock:
                        rejoin_pairs = [
                            (dk, str(info.get("host_code") or ""))
                            for dk, info in self._active_parties.items()
                            if info.get("host_code") and info.get("host_code") != self._code
                        ]
                    for dk, hc in rejoin_pairs:
                        await ws.send(
                            json.dumps({"type": "join_party", "host_code": hc, "deck": dk})
                        )

                    await self._pump_socket(ws, queue, reconnect_evt)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._stop_evt.is_set():
                    break
                err_txt = str(exc)
                self._set_connection_state("error", err_txt[:120])
                if "4001" in err_txt or "Replaced by newer connection" in err_txt:
                    backoff = max(backoff, 10.0)

            if self._stop_evt.is_set():
                break
            try:
                await asyncio.wait_for(reconnect_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(20.0, backoff * 1.5)

        self._set_connection_state("disconnected", "")

    async def _pump_socket(
        self,
        ws: Any,
        queue: asyncio.Queue[dict[str, Any]],
        reconnect_evt: asyncio.Event,
    ) -> None:
        last_activity_mono = [time.monotonic()]
        last_wall = [time.time()]
        last_mono = [time.monotonic()]

        async def _reader() -> None:
            async for raw in ws:
                if self._stop_evt.is_set():
                    break
                last_activity_mono[0] = time.monotonic()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if isinstance(msg, dict):
                    self._handle_incoming_message(msg)

        async def _writer() -> None:
            while not self._stop_evt.is_set() and not reconnect_evt.is_set():
                cmd = await queue.get()
                if cmd.get("_cmd") == "stop":
                    await ws.close()
                    return
                await ws.send(json.dumps(cmd, ensure_ascii=False))

        async def _heartbeat() -> None:
            while not self._stop_evt.is_set() and not reconnect_evt.is_set():
                await asyncio.sleep(20)
                if self._stop_evt.is_set() or reconnect_evt.is_set():
                    return
                now_wall = time.time()
                now_mono = time.monotonic()
                sleep_drift = (now_wall - last_wall[0]) - (now_mono - last_mono[0])
                last_wall[0] = now_wall
                last_mono[0] = now_mono
                if abs(sleep_drift) > 20.0:
                    raise ConnectionError("wake_from_sleep")
                if (now_mono - last_activity_mono[0]) > 45.0:
                    raise TimeoutError("heartbeat_timeout")
                await ws.send(json.dumps({"type": "ping"}))

        async def _reconnect_waiter() -> None:
            await reconnect_evt.wait()
            await ws.close()

        tasks = [
            asyncio.create_task(_reader()),
            asyncio.create_task(_writer()),
            asyncio.create_task(_heartbeat()),
            asyncio.create_task(_reconnect_waiter()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            first_exc: BaseException | None = None
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError) and first_exc is None:
                    first_exc = exc
            if first_exc is not None:
                raise first_exc
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    def _resolve_host_name_locked(
        self,
        host_code: str,
        members: list[dict[str, Any]],
        deck: str,
        hint: str = "",
    ) -> str:
        for m in members:
            if isinstance(m, dict) and str(m.get("code") or "").upper() == host_code:
                nm = str(m.get("name") or "").strip()
                if nm and nm.upper() != host_code:
                    return nm
        clean_hint = str(hint or "").strip()
        if clean_hint and clean_hint.upper() != host_code:
            return clean_hint
        prev_entry = self._active_parties.get(deck) or {}
        if str(prev_entry.get("host_code") or "").upper() == host_code:
            prev_hn = str(prev_entry.get("host_name") or "").strip()
            if prev_hn and prev_hn.upper() != host_code:
                return prev_hn
        friend = self._friends_by_code.get(host_code)
        if friend:
            fn = str(friend.get("name") or "").strip()
            if fn and fn.upper() != host_code:
                return fn
        if host_code and host_code == self._code and self._display_name:
            return self._display_name
        return clean_hint or host_code

    def _handle_incoming_message(self, msg: dict[str, Any]) -> None:
        mtype = str(msg.get("type") or "").strip()
        if mtype == "pong":
            return
        if mtype == "welcome":
            supported = bool(msg.get("dm_supported", False))
            with self._lock:
                self._server_supports_dm = supported
            self.server_dm_support_changed.emit(supported)
            return
        if mtype == "friends_snapshot":
            friends = msg.get("friends") or []
            if isinstance(friends, list):
                with self._lock:
                    self._friends_by_code = {
                        str(f.get("code") or "").upper(): dict(f)
                        for f in friends
                        if isinstance(f, dict) and f.get("code")
                    }
                self.friends_updated.emit(self.friends_snapshot)
            return
        if mtype == "friend_state":
            friend = msg.get("friend")
            if isinstance(friend, dict) and friend.get("code"):
                fc = str(friend["code"]).upper()
                with self._lock:
                    self._friends_by_code[fc] = dict(friend)
                self.friends_updated.emit(self.friends_snapshot)
            return
        if mtype == "lookup_result":
            self.lookup_result.emit(dict(msg))
            return
        if mtype == "party_members":
            hc = str(msg.get("host_code") or "").upper()
            dk = "music" if msg.get("deck") == "music" else "video"
            members = list(msg.get("members") or [])
            allow_guest = bool(msg.get("allow_guest_control", True))
            with self._lock:
                host_name = self._resolve_host_name_locked(hc, members, dk)
                if members:
                    self._active_parties[dk] = {
                        "host_code": hc,
                        "host_name": host_name,
                        "members": members,
                        "allow_guest_control": allow_guest,
                    }
                elif self._active_parties.get(dk, {}).get("host_code") == hc:
                    self._active_parties.pop(dk, None)
            self.party_members_updated.emit(hc, dk, members, allow_guest)
            return
        if mtype == "party_state":
            hc = str(msg.get("host_code") or "").upper()
            dk = "music" if msg.get("deck") == "music" else "video"
            members = list(msg.get("members") or [])
            allow_guest = bool(msg.get("allow_guest_control", True))
            raw_hn = str(msg.get("host_name") or "").strip()
            with self._lock:
                host_name = self._resolve_host_name_locked(hc, members, dk, raw_hn)
                self._active_parties[dk] = {
                    "host_code": hc,
                    "host_name": host_name,
                    "members": members,
                    "allow_guest_control": allow_guest,
                }
            self.party_state_received.emit(dict(msg))
            return
        if mtype == "party_notice":
            txt = str(msg.get("message") or "").strip()
            if txt:
                self.party_notice.emit(txt)
            return
        if mtype == "dm_message":
            with self._lock:
                dm_changed = self._server_supports_dm is not True
                self._server_supports_dm = True
            if dm_changed:
                self.server_dm_support_changed.emit(True)
            raw_msg = msg.get("message")
            if isinstance(raw_msg, dict):
                from_code = str(raw_msg.get("from_code") or "").strip().upper()
                text = str(raw_msg.get("text") or "").strip()
                if from_code and text:
                    try:
                        ts = float(raw_msg.get("ts") or time.time())
                    except (TypeError, ValueError):
                        ts = time.time()
                    self.direct_message_received.emit(
                        {
                            "id": str(raw_msg.get("id") or "").strip(),
                            "from_code": from_code,
                            "from_name": str(raw_msg.get("from_name") or from_code).strip(),
                            "from_avatar_url": str(raw_msg.get("from_avatar_url") or "").strip(),
                            "to_code": str(raw_msg.get("to_code") or "").strip().upper(),
                            "text": text,
                            "ts": ts,
                        }
                    )
            return
        if mtype == "dm_ack":
            with self._lock:
                dm_changed = self._server_supports_dm is not True
                self._server_supports_dm = True
            if dm_changed:
                self.server_dm_support_changed.emit(True)
            try:
                ts = float(msg.get("ts") or time.time())
            except (TypeError, ValueError):
                ts = time.time()
            self.direct_message_ack.emit(
                {
                    "client_id": str(msg.get("client_id") or "").strip(),
                    "to_code": str(msg.get("to_code") or "").strip().upper(),
                    "ok": bool(msg.get("ok", True)),
                    "delivered": bool(msg.get("delivered", False)),
                    "queued": bool(msg.get("queued", False)),
                    "error": str(msg.get("error") or "").strip(),
                    "ts": ts,
                }
            )
