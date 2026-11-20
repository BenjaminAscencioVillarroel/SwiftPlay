"""Servicios de búsqueda, feeds sin Shorts, perfiles, streams y descargas."""

from __future__ import annotations

import contextlib
import gc
import json
import locale
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import OrderedDict
from hashlib import sha1
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

import requests
from ytmusicapi import YTMusic

from swiftplay.models import (
    AccountProfile,
    FeedSection,
    MediaItem,
    ResolvedStream,
    format_duration,
    parse_duration_text,
)
from swiftplay.paths import BIN_DIR
from swiftplay.security import (
    filter_youtube_netscape_cookies,
    load_account_profile,
    load_cookies_decrypted,
    netscape_to_cookie_dict,
    netscape_to_cookie_header,
    save_account_profile,
)

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


def _extract_innertube_videos(
    root: Any, limit: int = 20, block_shorts: bool = True
) -> list[MediaItem]:
    """Recorre respuesta InnerTube WEB extrayendo videoRenderer y lockupViewModel sin Shorts."""
    results: list[MediaItem] = []
    seen: set[str] = set()

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
            if vid and vid not in seen:
                title = _extract_text(vr.get("title"))
                author = _extract_text(
                    vr.get("ownerText")
                    or vr.get("longBylineText")
                    or vr.get("shortBylineText")
                )
                dur_str = _extract_text(vr.get("lengthText"))
                dur_sec = parse_duration_text(dur_str)
                nav_url = (
                    vr.get("navigationEndpoint", {})
                    .get("commandMetadata", {})
                    .get("webCommandMetadata", {})
                    .get("url", "")
                )
                is_live = not dur_str and (
                    "LIVE" in json.dumps(vr.get("badges") or []).upper()
                    or "EN VIVO" in json.dumps(vr.get("badges") or []).upper()
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
                    results.append(
                        MediaItem(
                            video_id=vid,
                            title=title,
                            author=author or "YouTube",
                            duration=dur_str or ("EN VIVO" if is_live else ""),
                            duration_sec=dur_sec,
                            thumb_url=thumb,
                            kind="video",
                            channel_avatar_url=avatar,
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

                # Extraer duración desde overlays de thumbnailViewModel
                dur_str = ""
                is_live = False
                cimg = lm.get("contentImage", {}).get("thumbnailViewModel", {})
                for ov in cimg.get("overlays") or []:
                    if not isinstance(ov, dict):
                        continue
                    badge_vm = ov.get("thumbnailOverlayBadgeViewModel", {})
                    for b in badge_vm.get("thumbnailBadges") or []:
                        tb = b.get("thumbnailBadgeViewModel", {}) if isinstance(b, dict) else {}
                        txt = str(tb.get("text") or "").strip()
                        style = str(tb.get("badgeStyle") or "").upper()
                        if "LIVE" in style or txt.upper() in ("LIVE", "EN VIVO"):
                            is_live = True
                        elif ":" in txt:
                            dur_str = txt

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
                    results.append(
                        MediaItem(
                            video_id=vid,
                            title=title,
                            author=author or "YouTube",
                            duration=dur_str or ("EN VIVO" if is_live else ""),
                            duration_sec=dur_sec,
                            thumb_url=thumb,
                            kind="video",
                            channel_avatar_url=avatar,
                        )
                    )
            return

        for v in node.values():
            if len(results) >= limit:
                break
            _walk(v)

    _walk(root)
    return results


@contextlib.contextmanager
def _cookiefile_context() -> Iterator[str | None]:
    """
    Si hay sesión DPAPI guardada, escribe un archivo temporal efímero con cookies
    exclusivas de YouTube y lo borra de inmediato al salir del bloque.
    """
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


def _base_ydl_opts(cookiefile: str | None = None) -> dict:
    opts: dict = {
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
    """Carga YoutubeDL con solo extractores de YouTube para ahorrar ~40 MB de RAM."""
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


class YouTubeService:
    """Capa de acceso ligero a YouTube, YouTube Music, feeds y perfil sin navegador."""

    def __init__(self) -> None:
        self._ytmusic: YTMusic
        self._ytmusic_search: YTMusic | None = None
        self._ytmusic_authenticated = False
        self._stream_lock = threading.Lock()
        self._stream_cache: OrderedDict[tuple[str, bool, str], tuple[float, ResolvedStream]] = OrderedDict()
        self._inflight_streams: dict[
            tuple[str, bool, str],
            tuple[threading.Event, list[ResolvedStream | None], list[Exception | None]],
        ] = {}
        self.reload_auth()

    def reload_auth(self) -> None:
        """Recarga YTMusic con cookies DPAPI si existen, protegiendo LC_NUMERIC='C'."""
        self._ytmusic_search = None
        with self._stream_lock:
            self._stream_cache.clear()
        raw = load_cookies_decrypted()
        try:
            if raw:
                headers = _build_innertube_headers("https://music.youtube.com")
                if "authorization" in headers and "cookie" in headers:
                    try:
                        self._ytmusic = YTMusic(auth=headers, language="es")
                        self._ytmusic_authenticated = True
                        return
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

    def _get_search_ytmusic(self) -> YTMusic:
        """Cliente YTMusic en idioma base para evitar filtros vacíos en ytmusicapi."""
        if self._ytmusic_search is not None:
            return self._ytmusic_search
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
            self._ytmusic_search = self._ytmusic
        finally:
            try:
                locale.setlocale(locale.LC_NUMERIC, "C")
            except locale.Error:
                pass
        return self._ytmusic_search

    def is_authenticated(self) -> bool:
        return self._ytmusic_authenticated

    def fetch_account_profile(self) -> AccountProfile | None:
        """Obtiene nombre, @handle y foto del usuario autenticado y lo guarda localmente."""
        headers = _build_innertube_headers("https://www.youtube.com")
        if "authorization" not in headers:
            return load_account_profile()

        if self._ytmusic_authenticated:
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
            resp = requests.post(
                "https://www.youtube.com/youtubei/v1/account/account_menu?prettyPrint=false",
                headers=headers,
                json=_innertube_web_context(),
                timeout=8,
            )
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

    def get_video_home_feed(
        self, limit: int = 18, fallback_queries: list[str] | None = None
    ) -> list[MediaItem]:
        """Feed liviano de YouTube sin Shorts (bloquea /shorts/ y videos <= 60s)."""
        headers = _build_innertube_headers("https://www.youtube.com")
        payload = _innertube_web_context()
        payload["browseId"] = "FEwhat_to_watch"
        try:
            resp = requests.post(
                "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if resp.ok:
                items = _extract_innertube_videos(
                    resp.json(), limit=limit, block_shorts=True
                )
                if items:
                    return items
        except Exception:
            pass

        # Respaldo liviano si no hay sesión o FEwhat_to_watch vino vacío
        queries = [q for q in (fallback_queries or []) if q.strip()]
        seed = queries[0] if queries else "documentales tecnologia musica en vivo"
        return self.search_videos(seed, limit=limit, block_shorts=True)

    def get_music_home_sections(
        self, local_listen_again: list[MediaItem]
    ) -> list[FeedSection]:
        """Obtiene 2-3 secciones livianas para YT Music ('Volver a escuchar', etc.)."""
        sections: list[FeedSection] = []

        if self._ytmusic_authenticated:
            try:
                home = self._ytmusic.get_home(limit=5)
                for shelf in home or []:
                    if not isinstance(shelf, dict):
                        continue
                    title = str(shelf.get("title") or "").strip()
                    contents = shelf.get("contents") or []
                    tracks = self._parse_ytm_items(contents, limit=12)
                    if title and tracks:
                        sections.append(FeedSection(title=title, items=tracks))
                    if len(sections) >= 3:
                        break
            except Exception:
                pass

            # Garantizar sección "Volver a escuchar" si get_home no la incluyó
            has_listen_again = any(
                any(k in s.title.lower() for k in ("volver a escuchar", "listen again"))
                for s in sections
            )
            if not has_listen_again:
                try:
                    hist = self._ytmusic.get_history()
                    hist_tracks = self._parse_ytm_items(hist or [], limit=12)
                    if hist_tracks:
                        sections.insert(
                            0, FeedSection(title="Volver a escuchar", items=hist_tracks)
                        )
                        sections = sections[:3]
                except Exception:
                    pass

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
            resp = requests.post(
                "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if resp.ok:
                items = _extract_innertube_videos(
                    resp.json(), limit=limit, block_shorts=block_shorts
                )
                if items:
                    return items
        except Exception:
            pass

        # Respaldo con yt-dlp (solo extractores YouTube)
        fetch_n = limit + 8 if block_shorts else limit
        with _cookiefile_context() as ck:
            opts = _base_ydl_opts(ck)
            opts["extract_flat"] = "in_playlist"
            opts["skip_download"] = True
            with _make_youtube_ydl(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{fetch_n}:{q}", download=False)

        entries = info.get("entries") or [] if isinstance(info, dict) else []
        results: list[MediaItem] = []
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
                )
            )
            if len(results) >= limit:
                break
        return results

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
            raw = self._ytmusic.search(q, limit=limit)
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

        def _collect_from_seed(seed_vid: str) -> None:
            if not seed_vid or len(results) >= limit:
                return
            data: dict[str, Any] = {}
            for use_radio in (True, False):
                try:
                    data = self._ytmusic.get_watch_playlist(
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
    # Streams y Descargas
    # ------------------------------------------------------------------

    _STREAM_CACHE_TTL = 1500.0
    _STREAM_CACHE_MAX = 20

    def _stream_key(
        self, video_id: str, audio_only: bool, quality: str
    ) -> tuple[str, bool, str]:
        return (video_id, bool(audio_only), "audio" if audio_only else quality)

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
        url = f"https://www.youtube.com/watch?v={video_id}"
        max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
        if audio_only:
            fmt = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"
        else:
            fmt = (
                f"bestvideo[height<={max_h}][vcodec!*=av01]+bestaudio/"
                f"bestvideo[height<={max_h}]+bestaudio/"
                f"best[height<={max_h}]/best"
            )

        try:
            with _cookiefile_context() as ck:
                opts = _base_ydl_opts(ck)
                opts["format"] = fmt
                opts["noplaylist"] = True
                opts["skip_download"] = True
                opts["noprogress"] = True
                opts["no_color"] = True
                opts["extractor_args"] = {"youtube": {"player_skip": ["configs"]}}
                with _make_youtube_ydl(opts) as ydl:
                    info = ydl.extract_info(url, download=False)

            if not isinstance(info, dict):
                raise RuntimeError("No se pudo obtener la información del stream.")

            requested = info.get("requested_formats")
            primary_url: str = ""
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

            resolved = ResolvedStream(
                video_id=video_id,
                primary_url=primary_url,
                audio_url=audio_url,
                http_headers=headers,
                duration=float(info.get("duration") or 0.0),
                title=str(info.get("title") or ""),
                author=str(info.get("channel") or info.get("uploader") or ""),
                thumb_url=_pick_best_thumb(info.get("thumbnails"), video_id),
                video_enabled=not audio_only,
            )
            res_box[0] = resolved
            with self._stream_lock:
                self._stream_cache[key] = (time.monotonic(), resolved)
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
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        url = f"https://www.youtube.com/watch?v={video_id}"
        max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
        downloaded_file: list[Path] = []

        def _hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                pct = (done / total * 100.0) if total > 0 else 0.0
                if progress_cb:
                    progress_cb(pct, "Descargando...")
            elif status == "finished":
                fn = d.get("filename")
                if fn:
                    downloaded_file.append(Path(fn))
                if progress_cb:
                    progress_cb(100.0, "Procesando archivo...")

        try:
            with _cookiefile_context() as ck:
                opts = _base_ydl_opts(ck)
                opts["noplaylist"] = True
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
                    prepared = Path(ydl.prepare_filename(info))

            if mode == "music":
                final = prepared.with_suffix(".m4a")
                if final.exists():
                    return final
            else:
                final = prepared.with_suffix(".mp4")
                if final.exists():
                    return final

            if downloaded_file and downloaded_file[-1].exists():
                return downloaded_file[-1]
            return prepared
        finally:
            gc.collect()

    def try_extract_chrome_cookies(self) -> str:
        """Intenta leer cookies de Chrome localmente y filtra solo dominios YouTube."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "cookiesfrombrowser": ("chrome",),
            "skip_download": True,
        }
        with _make_youtube_ydl(opts, load_extractors=False) as ydl:
            jar = ydl.cookiejar
            lines = ["# Netscape HTTP Cookie File"]
            for cookie in jar:
                domain = (cookie.domain or "").strip().lower()
                if domain in {
                    ".youtube.com",
                    "youtube.com",
                    "www.youtube.com",
                    "music.youtube.com",
                    ".youtu.be",
                    "youtu.be",
                }:
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
