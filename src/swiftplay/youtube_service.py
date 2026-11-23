"""Servicios de búsqueda, feeds sin Shorts, perfiles, streams y descargas."""

from __future__ import annotations

import json
import locale
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from hashlib import sha1
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any, Callable, Literal

import requests
from requests.adapters import HTTPAdapter

from swiftplay.models import (
    AccountProfile,
    ChannelInfo,
    FeedSection,
    MediaItem,
    ResolvedStream,
    format_duration,
    parse_duration_text,
)
from swiftplay.security import (
    load_account_profile,
    load_cookies_decrypted,
    netscape_to_cookie_dict,
    save_account_profile,
)
from swiftplay.ytdlp_worker import YtdlpWorkerClient

QUALITY_HEIGHT_MAP: dict[str, int] = {
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
}

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def is_short_video(
    duration_sec: int,
    url: str = "",
    title: str = "",
    is_live: bool = False,
) -> bool:
    """Devuelve True si el video es un Short oficial (/shorts/, #shorts) o dura <= 60s."""
    u = (url or "").lower()
    t = (title or "").lower()
    if "/shorts/" in u or "#shorts" in t or "#short" in t:
        return True
    if not is_live and duration_sec > 0 and duration_sec <= 60:
        return True
    return False


def _pick_best_thumb(thumbnails: list[dict] | None, video_id: str = "") -> str:
    if thumbnails and isinstance(thumbnails, list):
        best = max(
            (t for t in thumbnails if isinstance(t, dict) and t.get("url")),
            key=lambda t: int(t.get("width", 0) or 0),
            default=None,
        )
        if best and best.get("url"):
            url = str(best["url"])
            if url.startswith("//"):
                return "https:" + url
            return url
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


_DATE_TOKENS = (
    "hace ",
    "emitido",
    "transmitido",
    "estrenado",
    "ago",
    "streamed",
    "premiered",
    "en vivo",
    "directo",
)


def _normalize_space(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split()).strip()


def _extract_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        if "simpleText" in obj:
            return str(obj["simpleText"]).strip()
        if "content" in obj:
            return str(obj["content"]).strip()
        runs = obj.get("runs")
        if isinstance(runs, list):
            return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict)).strip()
    return ""


def _extract_published_from_lockup_rows(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    candidates: list[str] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        parts = row.get("metadataParts") or []
        if not isinstance(parts, list):
            continue
        row_texts = [
            _normalize_space(_extract_text(p.get("text")))
            for p in parts
            if isinstance(p, dict)
        ]
        row_texts = [t for t in row_texts if t]
        search_slice = row_texts[1:] if idx == 0 else row_texts
        for t in search_slice:
            low = t.lower()
            if any(tok in low for tok in _DATE_TOKENS):
                return t
        if idx == 1 and len(row_texts) >= 2:
            candidates.append(row_texts[-1])
    return candidates[0] if candidates else ""


def _build_sapisid_hash(sapisid: str, origin: str) -> str:
    ts = str(int(time.time()))
    digest = sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _build_innertube_headers(origin: str) -> dict[str, str]:
    headers: dict[str, str] = {
        "user-agent": _DESKTOP_UA,
        "accept": "*/*",
        "content-type": "application/json",
        "origin": origin,
        "x-origin": origin,
        "referer": origin + "/",
    }
    raw = load_cookies_decrypted()
    if not raw:
        return headers
    cdict = netscape_to_cookie_dict(raw)
    if not cdict:
        return headers
    if "__Secure-3PAPISID" not in cdict and "SAPISID" in cdict:
        cdict["__Secure-3PAPISID"] = cdict["SAPISID"]
    cookie_hdr = "; ".join(f"{k}={v}" for k, v in cdict.items())
    headers["cookie"] = cookie_hdr
    sapisid = cdict.get("__Secure-3PAPISID") or cdict.get("SAPISID")
    if sapisid:
        headers["authorization"] = _build_sapisid_hash(sapisid, origin)
        headers["x-goog-authuser"] = "0"
    return headers


def _innertube_web_context() -> dict[str, Any]:
    return {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2." + time.strftime("%Y%m%d", time.gmtime()) + ".01.00",
                "hl": "es",
                "gl": "CL",
            }
        }
    }


def _find_channel_id_in_node(node: Any) -> str:
    """Extrae el primer browseId de canal YouTube ('UC...') desde cualquier subárbol InnerTube."""
    if isinstance(node, list):
        for el in node:
            found = _find_channel_id_in_node(el)
            if found:
                return found
        return ""
    if not isinstance(node, dict):
        return ""
    be = node.get("browseEndpoint")
    if isinstance(be, dict):
        bid = str(be.get("browseId") or "").strip()
        if bid.startswith("UC"):
            return bid
    for v in node.values():
        found = _find_channel_id_in_node(v)
        if found:
            return found
    return ""


def _extract_feed_continuation_token(root: Any) -> str | None:
    """Extrae el token de paginación del grid principal o appendContinuationItemsAction."""
    tokens: list[str] = []

    def _collect_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for el in items:
            if not isinstance(el, dict):
                continue
            cir = el.get("continuationItemRenderer")
            if isinstance(cir, dict):
                tok = (
                    cir.get("continuationEndpoint", {})
                    .get("continuationCommand", {})
                    .get("token")
                )
                if tok:
                    tokens.append(str(tok))

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for el in node:
                _walk(el)
            return
        if not isinstance(node, dict):
            return
        grid = node.get("richGridRenderer")
        if isinstance(grid, dict):
            _collect_items(grid.get("contents"))
        app_act = node.get("appendContinuationItemsAction")
        if isinstance(app_act, dict):
            _collect_items(app_act.get("continuationItems"))
        rel_act = node.get("reloadContinuationItemsCommand")
        if isinstance(rel_act, dict):
            _collect_items(rel_act.get("continuationItems"))
        for v in node.values():
            _walk(v)

    _walk(root)
    return tokens[-1] if tokens else None


def _is_badge_live(badge_vm: dict[str, Any]) -> bool:
    txt = str(badge_vm.get("text") or "").strip().upper()
    style = str(badge_vm.get("badgeStyle") or "").upper()
    if "LIVE" in style or txt in ("LIVE", "EN VIVO", "EN DIRECTO", "DIRECTO"):
        return True
    sources = (badge_vm.get("icon") or {}).get("sources") or []
    for src in sources:
        if not isinstance(src, dict):
            continue
        img_name = str(
            (src.get("clientResource") or {}).get("imageName") or ""
        ).upper()
        if "LIVE" in img_name:
            return True
    return False


def _extract_innertube_videos(
    root: Any,
    limit: int = 20,
    block_shorts: bool = True,
    exclude_ids: set[str] | None = None,
    exclude_watched: bool = False,
) -> list[MediaItem]:
    """Recorre respuesta InnerTube WEB extrayendo videoRenderer y lockupViewModel sin Shorts."""
    results: list[MediaItem] = []
    seen: set[str] = set()
    skip_ids = exclude_ids or set()

    def _walk(node: Any) -> None:
        if len(results) >= limit:
            return
        if isinstance(node, list):
            for el in node:
                if len(results) >= limit:
                    break
                _walk(el)
            return
        if not isinstance(node, dict):
            return

        # Saltar estanterías de Shorts completas
        if block_shorts:
            if any(
                k in node
                for k in (
                    "reelShelfRenderer",
                    "reelItemRenderer",
                    "shortsLockupViewModel",
                )
            ):
                return
            if "richShelfRenderer" in node and isinstance(node["richShelfRenderer"], dict):
                shelf_title = _extract_text(node["richShelfRenderer"].get("title")).lower()
                if "shorts" in shelf_title:
                    return

        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            vr = node["videoRenderer"]
            vid = str(vr.get("videoId") or "")
            if vid and vid not in seen and vid not in skip_ids:
                overlays = vr.get("thumbnailOverlays") or []
                watched_pct = 0
                ov_dur = ""
                ov_live = False
                for ov in overlays:
                    if not isinstance(ov, dict):
                        continue
                    rep = ov.get("thumbnailOverlayResumePlaybackRenderer")
                    if isinstance(rep, dict):
                        watched_pct = max(
                            watched_pct, int(rep.get("percentDurationWatched") or 0)
                        )
                    tsr = ov.get("thumbnailOverlayTimeStatusRenderer")
                    if isinstance(tsr, dict):
                        ts_txt = _extract_text(tsr.get("text"))
                        ts_style = str(tsr.get("style") or "").upper()
                        if "LIVE" in ts_style or ts_txt.upper() in (
                            "LIVE",
                            "EN VIVO",
                            "EN DIRECTO",
                        ):
                            ov_live = True
                        elif ":" in ts_txt and not ov_dur:
                            ov_dur = ts_txt
                if exclude_watched and watched_pct >= 80:
                    return

                title = _extract_text(vr.get("title"))
                author = _extract_text(
                    vr.get("ownerText")
                    or vr.get("longBylineText")
                    or vr.get("shortBylineText")
                )
                dur_str = _extract_text(vr.get("lengthText")) or ov_dur
                dur_sec = parse_duration_text(dur_str)
                nav_url = (
                    vr.get("navigationEndpoint", {})
                    .get("commandMetadata", {})
                    .get("webCommandMetadata", {})
                    .get("url", "")
                )
                badges_blob = json.dumps(vr.get("badges") or []).upper()
                is_live = not dur_str and (
                    ov_live
                    or "LIVE" in badges_blob
                    or "EN VIVO" in badges_blob
                    or "EN DIRECTO" in badges_blob
                )
                if title and not (
                    block_shorts
                    and is_short_video(dur_sec, url=str(nav_url), title=title, is_live=is_live)
                ):
                    seen.add(vid)
                    thumb = _pick_best_thumb(
                        vr.get("thumbnail", {}).get("thumbnails"), vid
                    )
                    avatar = _pick_best_thumb(
                        vr.get("channelThumbnailSupportedRenderers", {})
                        .get("channelThumbnailWithLinkRenderer", {})
                        .get("thumbnail", {})
                        .get("thumbnails")
                        or vr.get("avatar", {})
                        .get("decoratedAvatarViewModel", {})
                        .get("avatar", {})
                        .get("avatarViewModel", {})
                        .get("image", {})
                        .get("sources")
                    )
                    published = _normalize_space(_extract_text(vr.get("publishedTimeText")))
                    chan_id = (
                        _find_channel_id_in_node(vr.get("channelThumbnailSupportedRenderers"))
                        or _find_channel_id_in_node(vr.get("ownerText"))
                        or _find_channel_id_in_node(vr.get("longBylineText"))
                        or _find_channel_id_in_node(vr.get("shortBylineText"))
                    )
                    results.append(
                        MediaItem(
                            video_id=vid,
                            title=title,
                            author=_normalize_space(author) or "YouTube",
                            duration="EN VIVO" if is_live else dur_str,
                            duration_sec=dur_sec,
                            thumb_url=thumb,
                            kind="video",
                            is_live=is_live,
                            channel_avatar_url=avatar,
                            published_text=published,
                            channel_id=chan_id,
                        )
                    )
            return

        if "lockupViewModel" in node and isinstance(node["lockupViewModel"], dict):
            lm = node["lockupViewModel"]
            ctype = str(lm.get("contentType") or "")
            vid = str(lm.get("contentId") or "")
            nav_url = str(
                lm.get("rendererContext", {})
                .get("commandContext", {})
                .get("onTap", {})
                .get("innertubeCommand", {})
                .get("commandMetadata", {})
                .get("webCommandMetadata", {})
                .get("url", "")
            )
            if (
                vid
                and len(vid) == 11
                and vid not in seen
                and vid not in skip_ids
                and ctype in ("LOCKUP_CONTENT_TYPE_VIDEO", "")
                and "/shorts/" not in nav_url.lower()
            ):
                meta_vm = lm.get("metadata", {}).get("lockupMetadataViewModel", {})
                title = _extract_text(meta_vm.get("title"))
                rows = (
                    meta_vm.get("metadata", {})
                    .get("contentMetadataViewModel", {})
                    .get("metadataRows", [])
                )
                author = ""
                if rows and isinstance(rows[0], dict):
                    parts = rows[0].get("metadataParts", [])
                    if parts and isinstance(parts[0], dict):
                        author = _extract_text(parts[0].get("text"))

                # Extraer duración, estado EN VIVO y barra de progreso visto desde overlays actuales de YT
                dur_str = ""
                is_live = False
                watched_pct = 0
                cimg = lm.get("contentImage", {}).get("thumbnailViewModel", {})
                for ov in cimg.get("overlays") or []:
                    if not isinstance(ov, dict):
                        continue
                    pb_vm = ov.get("thumbnailOverlayProgressBarViewModel")
                    if isinstance(pb_vm, dict):
                        watched_pct = max(
                            watched_pct, int(pb_vm.get("startPercent") or 0)
                        )
                    bottom_ov = ov.get("thumbnailBottomOverlayViewModel")
                    if isinstance(bottom_ov, dict):
                        pb_nested = (
                            bottom_ov.get("progressBar", {})
                            .get("thumbnailOverlayProgressBarViewModel", {})
                        )
                        if isinstance(pb_nested, dict):
                            watched_pct = max(
                                watched_pct, int(pb_nested.get("startPercent") or 0)
                            )
                    badge_lists = [
                        (ov.get("thumbnailOverlayBadgeViewModel") or {}).get(
                            "thumbnailBadges"
                        ),
                        (bottom_ov or {}).get("badges")
                        if isinstance(bottom_ov, dict)
                        else None,
                    ]
                    for b_list in badge_lists:
                        for b in b_list or []:
                            tb = (
                                b.get("thumbnailBadgeViewModel", {})
                                if isinstance(b, dict)
                                else {}
                            )
                            if not isinstance(tb, dict):
                                continue
                            txt = str(tb.get("text") or "").strip()
                            if _is_badge_live(tb):
                                is_live = True
                            elif ":" in txt and not dur_str:
                                dur_str = txt

                if exclude_watched and watched_pct >= 80:
                    return

                dur_sec = parse_duration_text(dur_str)
                if title and not (
                    block_shorts
                    and is_short_video(dur_sec, url=nav_url, title=title, is_live=is_live)
                ):
                    seen.add(vid)
                    thumb = _pick_best_thumb(cimg.get("image", {}).get("sources"), vid)
                    avatar = _pick_best_thumb(
                        meta_vm.get("image", {})
                        .get("decoratedAvatarViewModel", {})
                        .get("avatar", {})
                        .get("avatarViewModel", {})
                        .get("image", {})
                        .get("sources")
                    )
                    published = _extract_published_from_lockup_rows(rows)
                    chan_id = _find_channel_id_in_node(meta_vm)
                    results.append(
                        MediaItem(
                            video_id=vid,
                            title=title,
                            author=_normalize_space(author) or "YouTube",
                            duration="EN VIVO" if is_live else dur_str,
                            duration_sec=dur_sec,
                            thumb_url=thumb,
                            kind="video",
                            is_live=is_live,
                            channel_avatar_url=avatar,
                            published_text=published,
                            channel_id=chan_id,
                        )
                    )
            return

        for v in node.values():
            if len(results) >= limit:
                break
            _walk(v)

    _walk(root)
    return results


class YouTubeService:
    """Capa de acceso ligero a YouTube, YouTube Music, feeds y perfil sin navegador."""

    def __init__(self) -> None:
        self._ytm_lock = threading.Lock()
        self._ytmusic: Any = None
        self._ytmusic_search: Any = None
        self._ytmusic_authenticated = False
        self._http_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._http_sessions: list[requests.Session] = []
        self._worker = YtdlpWorkerClient()
        self._feed_lock = threading.Lock()
        self._feed_continuation: str | None = None
        self._feed_used_fallback: bool = False
        self._fallback_seed_idx: int = 0
        self._music_home_pool: list[FeedSection] = []
        self._music_guest_seed_idx: int = 0
        self._stream_lock = threading.Lock()
        self._stream_cache: OrderedDict[tuple[str, bool, str], tuple[float, ResolvedStream]] = OrderedDict()
        self._inflight_streams: dict[
            tuple[str, bool, str],
            tuple[threading.Event, list[ResolvedStream | None], list[Exception | None]],
        ] = {}
        self.reload_auth()

    @property
    def worker_pid(self) -> int | None:
        return self._worker.worker_pid

    def _get_session(self) -> requests.Session:
        sess = getattr(self._http_local, "session", None)
        if sess is None:
            sess = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self._http_local.session = sess
            with self._sessions_lock:
                self._http_sessions.append(sess)
        return sess

    def close_worker(self) -> None:
        self._worker.stop_idle()

    def close(self) -> None:
        self._worker.reset_for_auth()
        self._worker.close()
        with self._sessions_lock:
            sessions = list(self._http_sessions)
            self._http_sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass
        with self._stream_lock:
            self._stream_cache.clear()
        with self._feed_lock:
            self._feed_continuation = None
            self._feed_used_fallback = False
        with self._ytm_lock:
            self._ytmusic = None
            self._ytmusic_search = None
            self._music_home_pool.clear()

    def reload_auth(self) -> None:
        """Recarga estado de autenticación DPAPI y limpia cachés sin instanciar YTMusic hasta usarse."""
        with self._ytm_lock:
            self._ytmusic = None
            self._ytmusic_search = None
        with self._stream_lock:
            self._stream_cache.clear()
        with self._feed_lock:
            self._feed_continuation = None
            self._feed_used_fallback = False
        with self._ytm_lock:
            self._music_home_pool.clear()
            self._music_guest_seed_idx = 0
        self._worker.reset_for_auth()

        raw = load_cookies_decrypted()
        try:
            if raw:
                headers = _build_innertube_headers("https://music.youtube.com")
                self._ytmusic_authenticated = (
                    "authorization" in headers and "cookie" in headers
                )
            else:
                self._ytmusic_authenticated = False
        finally:
            try:
                locale.setlocale(locale.LC_NUMERIC, "C")
            except locale.Error:
                pass

    def _get_ytmusic(self) -> Any:
        """Inicializa YTMusic bajo demanda para no gastar RAM si solo se usa Video/Twitch."""
        with self._ytm_lock:
            if self._ytmusic is not None:
                return self._ytmusic
            from ytmusicapi import YTMusic

            raw = load_cookies_decrypted()
            try:
                if raw:
                    headers = _build_innertube_headers("https://music.youtube.com")
                    if "authorization" in headers and "cookie" in headers:
                        try:
                            self._ytmusic = YTMusic(auth=headers, language="es")
                            self._ytmusic_authenticated = True
                            return self._ytmusic
                        except Exception:
                            pass
                self._ytmusic = YTMusic(language="es")
                self._ytmusic_authenticated = False
            except Exception:
                self._ytmusic = YTMusic()
                self._ytmusic_authenticated = False
            finally:
                try:
                    locale.setlocale(locale.LC_NUMERIC, "C")
                except locale.Error:
                    pass
            return self._ytmusic

    def _get_search_ytmusic(self) -> Any:
        """Cliente YTMusic en idioma base para evitar filtros vacíos en ytmusicapi."""
        with self._ytm_lock:
            if self._ytmusic_search is not None:
                return self._ytmusic_search
            from ytmusicapi import YTMusic

            raw = load_cookies_decrypted()
            try:
                if raw:
                    headers = _build_innertube_headers("https://music.youtube.com")
                    if "authorization" in headers and "cookie" in headers:
                        try:
                            self._ytmusic_search = YTMusic(auth=headers)
                            return self._ytmusic_search
                        except Exception:
                            pass
                self._ytmusic_search = YTMusic()
            except Exception:
                self._ytmusic_search = None
            finally:
                try:
                    locale.setlocale(locale.LC_NUMERIC, "C")
                except locale.Error:
                    pass
            if self._ytmusic_search is not None:
                return self._ytmusic_search
        return self._get_ytmusic()

    def is_authenticated(self) -> bool:
        return self._ytmusic_authenticated

    def fetch_account_profile(self) -> AccountProfile | None:
        """Obtiene nombre, @handle y foto del usuario autenticado y lo guarda localmente."""
        headers = _build_innertube_headers("https://www.youtube.com")
        if "authorization" not in headers:
            return load_account_profile()

        if self._ytmusic is not None and self._ytmusic_authenticated:
            try:
                info = self._ytmusic.get_account_info()
                name = str(info.get("accountName") or "").strip()
                handle = str(info.get("channelHandle") or "").strip()
                photo = str(info.get("accountPhotoUrl") or "").strip()
                if name or photo:
                    prof = AccountProfile(name=name, handle=handle, avatar_url=photo)
                    save_account_profile(prof)
                    return prof
            except Exception:
                pass

        try:
            with self._get_session().post(
                "https://www.youtube.com/youtubei/v1/account/account_menu?prettyPrint=false",
                headers=headers,
                json=_innertube_web_context(),
                timeout=8,
            ) as resp:
                if resp.ok:
                    data = resp.json()
                    header = (
                        data.get("actions", [{}])[0]
                        .get("openPopupAction", {})
                        .get("popup", {})
                        .get("multiPageMenuRenderer", {})
                        .get("header", {})
                        .get("activeAccountHeaderRenderer", {})
                    )
                    name = _extract_text(header.get("accountName"))
                    handle = _extract_text(header.get("channelHandle"))
                    photo = _pick_best_thumb(
                        header.get("accountPhoto", {}).get("thumbnails")
                    )
                    if name or photo:
                        prof = AccountProfile(name=name, handle=handle, avatar_url=photo)
                        save_account_profile(prof)
                        return prof
        except Exception:
            pass
        return load_account_profile()

    # ------------------------------------------------------------------
    # Feeds de Inicio (Video sin Shorts & Música en 2-3 secciones)
    # ------------------------------------------------------------------

    @property
    def has_more_video_feed(self) -> bool:
        with self._feed_lock:
            return bool(self._feed_continuation or self._feed_used_fallback)

    def _fetch_browse_page(
        self,
        headers: dict[str, str],
        continuation: str | None = None,
        browse_id: str = "FEwhat_to_watch",
        params: str | None = None,
    ) -> dict[str, Any] | None:
        payload = _innertube_web_context()
        if continuation:
            payload["continuation"] = continuation
        else:
            payload["browseId"] = browse_id
            if params:
                payload["params"] = params
        try:
            with self._get_session().post(
                "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=10,
            ) as resp:
                if resp.ok:
                    data = resp.json()
                    return data if isinstance(data, dict) else None
        except Exception:
            pass
        return None

    def get_video_home_feed(
        self,
        limit: int = 36,
        fallback_queries: list[str] | None = None,
        watched_ids: set[str] | None = None,
        recent_shown_ids: set[str] | None = None,
        refresh: bool = False,
        append: bool = False,
    ) -> list[MediaItem]:
        """Feed de YouTube sin Shorts, paginado incrementalmente y renovado al refrescar."""
        headers = _build_innertube_headers("https://www.youtube.com")
        hard_skip = {str(x) for x in (watched_ids or ()) if x}
        soft_skip = {str(x) for x in (recent_shown_ids or ()) if x}
        collected: list[MediaItem] = []
        seen_ids: set[str] = set()

        with self._feed_lock:
            token = self._feed_continuation if (refresh or append) else None
            used_fallback = self._feed_used_fallback

        if append and not token and not used_fallback:
            return []

        if not (append and used_fallback):
            latest_token: str | None = None
            max_pages = 2 if append else 4
            for page_idx in range(max_pages):
                if len(collected) >= limit:
                    break
                used_token = token
                if append and not used_token:
                    break
                data = self._fetch_browse_page(headers, continuation=used_token)
                if data is None and used_token is not None and page_idx == 0 and not append:
                    token = None
                    data = self._fetch_browse_page(headers, continuation=None)
                    used_token = None
                if data is None:
                    break

                batch = _extract_innertube_videos(
                    data,
                    limit=limit - len(collected),
                    block_shorts=True,
                    exclude_ids=hard_skip | soft_skip | seen_ids,
                    exclude_watched=True,
                )
                if not batch and used_token is not None and page_idx == 0 and not append:
                    data = self._fetch_browse_page(headers, continuation=None)
                    if data is None:
                        break
                    batch = _extract_innertube_videos(
                        data,
                        limit=limit - len(collected),
                        block_shorts=True,
                        exclude_ids=hard_skip | soft_skip | seen_ids,
                        exclude_watched=True,
                    )

                for item in batch:
                    if item.video_id and item.video_id not in seen_ids:
                        seen_ids.add(item.video_id)
                        collected.append(item)

                next_tok = _extract_feed_continuation_token(data)
                if next_tok:
                    latest_token = next_tok
                token = next_tok
                if not token:
                    break

            if not append and len(collected) < min(8, limit) and soft_skip:
                fresh_data = self._fetch_browse_page(headers, continuation=None)
                if fresh_data is not None:
                    extra = _extract_innertube_videos(
                        fresh_data,
                        limit=limit - len(collected),
                        block_shorts=True,
                        exclude_ids=hard_skip | seen_ids,
                        exclude_watched=True,
                    )
                    for item in extra:
                        if item.video_id and item.video_id not in seen_ids:
                            seen_ids.add(item.video_id)
                            collected.append(item)
                    if not latest_token:
                        latest_token = _extract_feed_continuation_token(fresh_data)

            with self._feed_lock:
                if append:
                    self._feed_continuation = token
                elif latest_token is not None:
                    self._feed_continuation = latest_token
                if collected:
                    self._feed_used_fallback = False

            if collected:
                return collected[:limit]
            if append and token is None:
                return []

        # Respaldo liviano si no hay sesión o FEwhat_to_watch vino vacío
        defaults = [
            "documentales tecnologia ciencia",
            "programacion ia desarrollo",
            "conciertos musica en vivo",
            "naturaleza historia 4k",
            "astronomia espacio documental",
            "arquitectura ingenieria moderna",
        ]
        pool = [q.strip() for q in (fallback_queries or []) if q and q.strip()]
        for d in defaults:
            if d not in pool:
                pool.append(d)
        with self._feed_lock:
            if refresh or append:
                self._fallback_seed_idx = (self._fallback_seed_idx + 1) % len(pool)
            seed = pool[self._fallback_seed_idx % len(pool)]
            self._feed_used_fallback = True
        raw_items = self.search_videos(
            seed, limit=min(50, limit + len(hard_skip) + 8), block_shorts=True
        )
        fresh_items = [
            it
            for it in raw_items
            if it.video_id not in hard_skip and it.video_id not in soft_skip
        ]
        if not append and len(fresh_items) < min(8, limit):
            fresh_ids = {it.video_id for it in fresh_items}
            fresh_items.extend(
                it
                for it in raw_items
                if it.video_id not in hard_skip and it.video_id not in fresh_ids
            )
        return fresh_items[:limit]

    def get_music_home_sections(
        self, local_listen_again: list[MediaItem]
    ) -> list[FeedSection]:
        """Obtiene 2-3 secciones livianas iniciales para YT Music y guarda el resto en pool para scroll."""
        sections: list[FeedSection] = []
        pool_sections: list[FeedSection] = []

        if self._ytmusic_authenticated:
            ytm = self._get_ytmusic()
            if self._ytmusic_authenticated:
                try:
                    home = ytm.get_home(limit=12)
                    for shelf in home or []:
                        if not isinstance(shelf, dict):
                            continue
                        title = str(shelf.get("title") or "").strip()
                        contents = shelf.get("contents") or []
                        tracks = self._parse_ytm_items(contents, limit=12)
                        if title and tracks:
                            sec = FeedSection(title=title, items=tracks)
                            pool_sections.append(sec)
                            if len(sections) < 3:
                                sections.append(sec)
                except Exception:
                    pass

                # Garantizar sección "Volver a escuchar" si get_home no la incluyó
                has_listen_again = any(
                    any(k in s.title.lower() for k in ("volver a escuchar", "listen again"))
                    for s in sections
                )
                if not has_listen_again:
                    try:
                        hist = ytm.get_history()
                        hist_tracks = self._parse_ytm_items(hist or [], limit=12)
                        if hist_tracks:
                            la_sec = FeedSection(title="Volver a escuchar", items=hist_tracks)
                            sections.insert(0, la_sec)
                            sections = sections[:3]
                            pool_sections.insert(0, la_sec)
                    except Exception:
                        pass

        with self._ytm_lock:
            self._music_home_pool = pool_sections
            self._music_guest_seed_idx = 0

        if not sections:
            if local_listen_again:
                sections.append(
                    FeedSection(
                        title="Volver a escuchar (Local)",
                        items=local_listen_again[:12],
                    )
                )
                try:
                    picks = self.get_music_radio_queue(
                        local_listen_again[0].video_id, limit=12
                    )
                    if picks:
                        sections.append(
                            FeedSection(title="Selección rápida", items=picks[:12])
                        )
                except Exception:
                    pass
                if len(local_listen_again) > 1:
                    try:
                        seen_ids = {it.video_id for s in sections for it in s.items}
                        recs = [
                            t
                            for t in self.get_music_radio_queue(
                                local_listen_again[1].video_id, limit=16
                            )
                            if t.video_id not in seen_ids
                        ]
                        if recs:
                            sections.append(
                                FeedSection(title="Recomendados para ti", items=recs[:12])
                            )
                    except Exception:
                        pass
            else:
                try:
                    top = self.search_music("éxitos musicales", limit=12)
                    if top:
                        sections.append(
                            FeedSection(title="Selección rápida", items=top[:12])
                        )
                except Exception:
                    pass
                try:
                    chill = self.search_music("lofi chill beats", limit=12)
                    if chill:
                        sections.append(
                            FeedSection(title="Para escuchar de fondo", items=chill[:12])
                        )
                except Exception:
                    pass

        return sections[:3]

    def get_music_home_more_sections(
        self,
        local_listen_again: list[MediaItem],
        *,
        exclude_titles: set[str] | None = None,
        exclude_ids: set[str] | None = None,
        batch_size: int = 3,
    ) -> list[FeedSection]:
        """Carga más estanterías de YT Music al hacer scroll infinito sin sobrecargar el arranque."""
        seen_titles = {str(t).strip().lower() for t in (exclude_titles or ()) if t}
        seen_ids = {str(x).strip() for x in (exclude_ids or ()) if x}
        out: list[FeedSection] = []

        with self._ytm_lock:
            pool_copy = list(self._music_home_pool)

        for sec in pool_copy:
            if len(out) >= batch_size:
                break
            tkey = sec.title.strip().lower()
            if tkey in seen_titles:
                continue
            unique_items = [it for it in sec.items if it.video_id and it.video_id not in seen_ids]
            if unique_items:
                seen_titles.add(tkey)
                for it in unique_items:
                    seen_ids.add(it.video_id)
                out.append(FeedSection(title=sec.title, items=unique_items[:12]))

        if out:
            return out

        guest_themes: list[tuple[str, str]] = [
            ("Ritmos electrónicos", "electronic synthwave chill"),
            ("Rock y Alternativo", "indie alternative rock hits"),
            ("Concentración y Jazz", "jazz instrumental study music"),
            ("Clásicos del recuerdo", "classic greatest hits"),
            ("Pop y Urbano", "latin urban pop music"),
            ("Bandas sonoras", "epic soundtrack orchestral"),
        ]
        # Primero probar con semillas locales recientes si quedan
        for local_it in local_listen_again[2:]:
            if len(out) >= batch_size:
                break
            title_cand = f"Similar a {local_it.title[:28]}"
            if title_cand.lower() in seen_titles:
                continue
            try:
                recs = self.get_music_radio_queue(
                    local_it.video_id, limit=14, exclude_ids=seen_ids
                )
            except Exception:
                recs = []
            if recs:
                seen_titles.add(title_cand.lower())
                for it in recs:
                    seen_ids.add(it.video_id)
                out.append(FeedSection(title=title_cand, items=recs[:12]))

        while len(out) < batch_size:
            with self._ytm_lock:
                idx = self._music_guest_seed_idx
                if idx >= len(guest_themes):
                    break
                self._music_guest_seed_idx = idx + 1
            sec_title, query = guest_themes[idx]
            if sec_title.lower() in seen_titles:
                continue
            try:
                found = [
                    it
                    for it in self.search_music(query, limit=16)
                    if it.video_id and it.video_id not in seen_ids
                ]
            except Exception:
                found = []
            if found:
                seen_titles.add(sec_title.lower())
                for it in found:
                    seen_ids.add(it.video_id)
                out.append(FeedSection(title=sec_title, items=found[:12]))

        return out[:batch_size]

    def resolve_channel_id_for_video(self, video_id: str) -> str:
        """Resuelve el ID UC... de canal a partir de un videoId cuando no está cacheado."""
        vid = str(video_id or "").strip()
        if not vid:
            return ""
        headers = _build_innertube_headers("https://www.youtube.com")
        payload = _innertube_web_context()
        payload["videoId"] = vid
        try:
            with self._get_session().post(
                "https://www.youtube.com/youtubei/v1/next?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=8,
            ) as resp:
                if resp.ok:
                    data = resp.json()
                    return _find_channel_id_in_node(data)
        except Exception:
            pass
        return ""

    def get_channel_page(
        self,
        channel_id: str,
        tab: Literal["videos", "streams"] = "videos",
        continuation: str | None = None,
        limit: int = 24,
        params: str | None = None,
        fallback_name: str = "",
        fallback_avatar: str = "",
    ) -> tuple[ChannelInfo, list[MediaItem], str | None]:
        """Obtiene cabecera y lista de Videos o Directos (sin Shorts) de un canal YouTube."""
        cid = str(channel_id or "").strip()
        default_params = (
            "EgdzdHJlYW1z8gYECgJ6AA==" if tab == "streams" else "EgZ2aWRlb3PyBgQKAjoA"
        )
        headers = _build_innertube_headers("https://www.youtube.com")
        data = self._fetch_browse_page(
            headers,
            continuation=continuation,
            browse_id=cid,
            params=None if continuation else (params or default_params),
        )
        if data is None:
            raise RuntimeError("No se pudo cargar la información del canal.")

        name = fallback_name.strip()
        handle = ""
        subscribers = ""
        avatar_url = fallback_avatar.strip()
        videos_params = "EgZ2aWRlb3PyBgQKAjoA"
        streams_params = "EgdzdHJlYW1z8gYECgJ6AA=="

        if not continuation:
            hdr = data.get("header") or {}
            ph = (
                hdr.get("pageHeaderRenderer", {})
                .get("content", {})
                .get("pageHeaderViewModel", {})
            )
            if isinstance(ph, dict) and ph:
                name = _extract_text(ph.get("title")) or name
                rows = (
                    ph.get("metadata", {})
                    .get("contentMetadataViewModel", {})
                    .get("metadataRows")
                    or []
                )
                for row in rows:
                    for part in (row.get("metadataParts") or []) if isinstance(row, dict) else ():
                        txt = _normalize_space(_extract_text((part or {}).get("text")))
                        if not txt:
                            continue
                        if txt.startswith("@") and not handle:
                            handle = txt
                        elif "suscriptor" in txt.lower() or "subscriber" in txt.lower():
                            subscribers = txt
                av_sources = (
                    ph.get("image", {})
                    .get("decoratedAvatarViewModel", {})
                    .get("avatar", {})
                    .get("avatarViewModel", {})
                    .get("image", {})
                    .get("sources")
                )
                found_av = _pick_best_thumb(av_sources)
                if found_av:
                    avatar_url = found_av

            meta_renderer = (data.get("metadata") or {}).get("channelMetadataRenderer", {})
            if isinstance(meta_renderer, dict):
                if not name:
                    name = str(meta_renderer.get("title") or "").strip()
                if not avatar_url:
                    avatar_url = _pick_best_thumb(
                        (meta_renderer.get("avatar") or {}).get("thumbnails")
                    )

            tabs = (
                data.get("contents", {})
                .get("twoColumnBrowseResultsRenderer", {})
                .get("tabs")
                or []
            )
            for t in tabs:
                tr = (t or {}).get("tabRenderer") if isinstance(t, dict) else None
                if not isinstance(tr, dict):
                    continue
                be = (tr.get("endpoint") or {}).get("browseEndpoint") or {}
                t_params = str(be.get("params") or "").strip()
                if not t_params:
                    continue
                t_title = str(tr.get("title") or "").strip().lower()
                if any(k in t_title for k in ("vídeo", "video")):
                    videos_params = t_params
                elif any(k in t_title for k in ("directo", "live", "en vivo")):
                    streams_params = t_params

        items = _extract_innertube_videos(data, limit=limit, block_shorts=True)
        for it in items:
            if cid and not it.channel_id:
                it.channel_id = cid
            if name and (not it.author or it.author == "YouTube"):
                it.author = name
            if avatar_url and not it.channel_avatar_url:
                it.channel_avatar_url = avatar_url

        next_tok = _extract_feed_continuation_token(data)
        info = ChannelInfo(
            channel_id=cid,
            name=name or cid,
            handle=handle,
            subscribers=subscribers,
            avatar_url=avatar_url,
            videos_params=videos_params,
            streams_params=streams_params,
        )
        return info, items, next_tok

    def _parse_ytm_items(self, raw_items: list[dict], limit: int = 8) -> list[MediaItem]:
        tracks: list[MediaItem] = []
        seen: set[str] = set()
        for row in raw_items:
            if not isinstance(row, dict):
                continue
            vid = row.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            artists = ", ".join(
                a.get("name", "")
                for a in (row.get("artists") or [])
                if isinstance(a, dict) and a.get("name")
            )
            dur_str = str(row.get("duration") or row.get("length") or "")
            dur_sec = int(row.get("duration_seconds") or 0) or parse_duration_text(dur_str)
            thumb = _pick_best_thumb(row.get("thumbnails") or row.get("thumbnail"), str(vid))
            tracks.append(
                MediaItem(
                    video_id=str(vid),
                    title=title,
                    author=artists or "YouTube Music",
                    duration=dur_str or format_duration(dur_sec),
                    duration_sec=dur_sec,
                    thumb_url=thumb,
                    kind="music",
                )
            )
            if len(tracks) >= limit:
                break
        return tracks

    # ------------------------------------------------------------------
    # Búsqueda (Video sin Shorts & Música)
    # ------------------------------------------------------------------

    def search_videos(
        self, query: str, limit: int = 16, block_shorts: bool = True
    ) -> list[MediaItem]:
        q = query.strip()
        if not q:
            return []

        # Camino rápido via InnerTube (incluye avatar del canal y filtra Shorts)
        try:
            headers = _build_innertube_headers("https://www.youtube.com")
            payload = _innertube_web_context()
            payload["query"] = q
            with self._get_session().post(
                "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=10,
            ) as resp:
                if resp.ok:
                    items = _extract_innertube_videos(
                        resp.json(), limit=limit, block_shorts=block_shorts
                    )
                    if items:
                        return items
        except Exception:
            pass

        # Respaldo aislado con yt-dlp en worker
        data = self._worker.request(
            {
                "cmd": "search_youtube",
                "query": q,
                "limit": limit,
                "block_shorts": block_shorts,
            },
            timeout=35.0,
        )
        return [MediaItem.from_dict(d) for d in (data or [])]

    def search_music(
        self, query: str, search_filter: str = "songs", limit: int = 20
    ) -> list[MediaItem]:
        q = query.strip()
        if not q:
            return []
        raw: list[dict] = []
        if search_filter:
            raw = self._get_search_ytmusic().search(q, filter=search_filter, limit=limit)
        if not raw:
            raw = self._get_ytmusic().search(q, limit=limit)
        return self._parse_ytm_items(raw, limit=limit)

    def get_music_radio_queue(
        self,
        video_id: str,
        limit: int = 25,
        exclude_ids: set[str] | None = None,
        fallback_seeds: list[str] | None = None,
        artist_hint: str = "",
    ) -> list[MediaItem]:
        """Obtiene pistas de radio evitando duplicados y usando semillas/artista de respaldo."""
        excluded: set[str] = set(exclude_ids or ())
        if video_id:
            excluded.add(video_id)
        results: list[MediaItem] = []
        ytm = self._get_ytmusic()

        def _collect_from_seed(seed_vid: str) -> None:
            if not seed_vid or len(results) >= limit:
                return
            data: dict[str, Any] = {}
            for use_radio in (True, False):
                try:
                    data = ytm.get_watch_playlist(
                        videoId=seed_vid,
                        limit=max(25, limit + 10),
                        radio=use_radio,
                    )
                    if isinstance(data, dict) and data.get("tracks"):
                        break
                except Exception:
                    data = {}
            tracks = data.get("tracks") or [] if isinstance(data, dict) else []
            parsed = self._parse_ytm_items(tracks, limit=max(30, limit + 15))
            for t in parsed:
                if t.video_id and t.video_id not in excluded:
                    excluded.add(t.video_id)
                    results.append(t)
                    if len(results) >= limit:
                        break

        _collect_from_seed(video_id)

        if len(results) < min(8, limit) and fallback_seeds:
            tried = {video_id}
            fb_count = 0
            for fb in fallback_seeds:
                if fb and fb not in tried:
                    tried.add(fb)
                    _collect_from_seed(fb)
                    fb_count += 1
                    if fb_count >= 2 or len(results) >= limit:
                        break

        if len(results) < 3 and artist_hint and artist_hint.strip():
            try:
                extra = self.search_music(
                    artist_hint.strip(), search_filter="songs", limit=limit
                )
                for t in extra:
                    if t.video_id and t.video_id not in excluded:
                        excluded.add(t.video_id)
                        results.append(t)
                        if len(results) >= limit:
                            break
            except Exception:
                pass

        return results[:limit]

    # ------------------------------------------------------------------
    # Streams y Descargas (aislados en worker yt-dlp)
    # ------------------------------------------------------------------

    _STREAM_CACHE_TTL = 1500.0
    _STREAM_CACHE_MAX = 6

    def _stream_key(
        self, video_id: str, audio_only: bool, quality: str
    ) -> tuple[str, bool, str]:
        return (video_id, bool(audio_only), "audio" if audio_only else quality)

    def _prune_expired_streams_locked(self, now: float) -> None:
        expired = [
            k
            for k, (ts, _st) in self._stream_cache.items()
            if (now - ts) >= self._STREAM_CACHE_TTL
        ]
        for k in expired:
            self._stream_cache.pop(k, None)

    def invalidate_stream_cache(self, video_id: str) -> None:
        if not video_id:
            return
        with self._stream_lock:
            keys = [k for k in self._stream_cache if k[0] == video_id]
            for k in keys:
                self._stream_cache.pop(k, None)

    def has_cached_stream(
        self, video_id: str, audio_only: bool, quality: str = "720p"
    ) -> bool:
        if not video_id:
            return False
        key = self._stream_key(video_id, audio_only, quality)
        with self._stream_lock:
            entry = self._stream_cache.get(key)
            if entry is None:
                return False
            if (time.monotonic() - entry[0]) < self._STREAM_CACHE_TTL:
                return True
            self._stream_cache.pop(key, None)
            return False

    def prefetch_stream(
        self, video_id: str, audio_only: bool, quality: str = "720p"
    ) -> None:
        if not video_id:
            return
        try:
            self.resolve_stream(video_id, audio_only=audio_only, quality=quality, use_cache=True)
        except Exception:
            pass

    def resolve_stream(
        self,
        video_id: str,
        audio_only: bool,
        quality: str = "720p",
        use_cache: bool = True,
    ) -> ResolvedStream:
        key = self._stream_key(video_id, audio_only, quality)
        inflight_entry: tuple[
            threading.Event, list[ResolvedStream | None], list[Exception | None]
        ] | None = None
        is_owner = False

        with self._stream_lock:
            if use_cache:
                cached = self._stream_cache.get(key)
                if cached is not None:
                    ts, stream = cached
                    if (time.monotonic() - ts) < self._STREAM_CACHE_TTL:
                        self._stream_cache.move_to_end(key)
                        return stream
                    self._stream_cache.pop(key, None)

            existing_inflight = self._inflight_streams.get(key)
            if existing_inflight is not None:
                inflight_entry = existing_inflight
            else:
                inflight_entry = (threading.Event(), [None], [None])
                self._inflight_streams[key] = inflight_entry
                is_owner = True

        if not is_owner:
            ev, res_box, err_box = inflight_entry
            ev.wait(timeout=20.0)
            if res_box[0] is not None:
                return res_box[0]
            if err_box[0] is not None:
                raise err_box[0]
            raise RuntimeError("Tiempo de espera agotado al resolver el stream.")

        ev, res_box, err_box = inflight_entry
        try:
            data = self._worker.request(
                {
                    "cmd": "resolve_youtube",
                    "video_id": video_id,
                    "audio_only": audio_only,
                    "quality": quality,
                },
                timeout=40.0,
            )
            if not isinstance(data, dict) or not data.get("primary_url"):
                raise RuntimeError("yt-dlp no devolvió una URL reproducible.")

            resolved = ResolvedStream(
                video_id=str(data.get("video_id") or video_id),
                primary_url=str(data["primary_url"]),
                audio_url=data.get("audio_url"),
                http_headers=dict(data.get("http_headers") or {}),
                duration=float(data.get("duration") or 0.0),
                title=str(data.get("title") or ""),
                author=str(data.get("author") or ""),
                thumb_url=str(data.get("thumb_url") or ""),
                video_enabled=bool(data.get("video_enabled", not audio_only)),
                is_live=bool(data.get("is_live", False)),
                live_start_ts=float(data.get("live_start_ts") or 0.0),
            )
            res_box[0] = resolved
            if not resolved.is_live:
                now = time.monotonic()
                with self._stream_lock:
                    self._prune_expired_streams_locked(now)
                    self._stream_cache[key] = (now, resolved)
                    self._stream_cache.move_to_end(key)
                    while len(self._stream_cache) > self._STREAM_CACHE_MAX:
                        self._stream_cache.popitem(last=False)
            return resolved
        except Exception as exc:
            err_box[0] = exc
            raise
        finally:
            with self._stream_lock:
                if self._inflight_streams.get(key) is inflight_entry:
                    self._inflight_streams.pop(key, None)
            ev.set()

    def download_media(
        self,
        video_id: str,
        mode: Literal["music", "video"],
        output_dir: str,
        quality: str = "720p",
        progress_cb: Callable[[float, str], None] | None = None,
    ) -> Path:
        final_path = self._worker.run_download(
            {
                "cmd": "download_media",
                "video_id": video_id,
                "mode": mode,
                "output_dir": output_dir,
                "quality": quality,
            },
            progress_cb=progress_cb,
        )
        return Path(final_path)

    def try_extract_chrome_cookies(self) -> str:
        """Intenta leer cookies de Chrome localmente y filtra solo dominios YouTube."""
        res = self._worker.request({"cmd": "extract_chrome_cookies"}, timeout=30.0)
        filtered = str(res or "")
        if not filtered:
            raise RuntimeError(
                "Chrome bloqueó la lectura directa. Usa el botón 'Iniciar sesión con Google (Navegador)'."
            )
        return filtered

    def update_yt_dlp(self) -> tuple[bool, str]:
        """Actualiza yt-dlp dentro del .venv usando uv."""
        try:
            old_ver = pkg_version("yt-dlp")
        except PackageNotFoundError:
            old_ver = "desconocida"
        cmd = [
            "uv",
            "pip",
            "install",
            "-U",
            "--python",
            sys.executable,
            "yt-dlp[default]",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            return True, f"Motor yt-dlp actualizado (versión previa en memoria: {old_ver}).\n{out.strip()}"
        return False, f"No se pudo actualizar yt-dlp:\n{out.strip()}"
