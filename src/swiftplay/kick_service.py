"""Servicio ultraliviano de Kick (solo invitados, API pública + HLS directo sin chat)."""

from __future__ import annotations

import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from swiftplay.models import MediaItem, ResolvedStream
from swiftplay.twitch_service import (
    _USER_AGENT,
    _clean_text,
    _format_live_subtext,
    _parse_hls_master,
    _pick_hls_variant,
)

_KICK_WEB_LIVESTREAMS_URL = "https://web.kick.com/api/v1/livestreams"
_KICK_FEATURED_URL = "https://kick.com/stream/featured-livestreams"
_KICK_CHANNEL_V2_URL = "https://kick.com/api/v2/channels"
_KICK_SEARCH_URL = "https://kick.com/api/search"

_RESERVED_KICK_PATHS = {
    "api",
    "browse",
    "categories",
    "dashboard",
    "directory",
    "download",
    "following",
    "jobs",
    "login",
    "notifications",
    "privacy",
    "search",
    "settings",
    "signup",
    "subscriptions",
    "terms",
    "transactions",
}
_URL_SLUG_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?kick\.com/([A-Za-z0-9_-]{2,32})(?:[/?#].*)?$",
    re.IGNORECASE,
)


class _KickChannelOfflineError(RuntimeError):
    """Canal de Kick existente pero sin transmisión en directo."""


def extract_kick_slug(query: str) -> str | None:
    """Extrae el slug de canal si el usuario pegó un enlace directo o @usuario de Kick."""
    q = (query or "").strip()
    if q.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_-]{2,32}", q):
        slug = q[1:].lower()
        return None if slug in _RESERVED_KICK_PATHS else slug
    m = _URL_SLUG_RE.match(q)
    if not m:
        return None
    slug = m.group(1).lower()
    return None if slug in _RESERVED_KICK_PATHS else slug


def _thumb_from_any(raw_thumb: Any, channel_id: int | str = "") -> str:
    if isinstance(raw_thumb, dict):
        url = str(raw_thumb.get("src") or raw_thumb.get("url") or "").strip()
        if url:
            return url
    elif isinstance(raw_thumb, str) and raw_thumb.strip():
        return raw_thumb.strip()
    if channel_id:
        return f"https://web.kick.com/api/v1/channels/{channel_id}/thumbnail.jpeg?height=720"
    return ""


def _v1_livestream_to_item(row: dict[str, Any]) -> MediaItem | None:
    ch = row.get("channel") if isinstance(row.get("channel"), dict) else {}
    slug = str(ch.get("slug") or row.get("slug") or "").strip().lower()
    if not slug:
        return None
    author = _clean_text(ch.get("username")) or slug
    avatar = str(ch.get("profile_pic") or "").strip()
    title = _clean_text(row.get("title") or row.get("session_title")) or f"Directo de {author}"
    viewers = int(row.get("viewer_count") or 0)
    cat = row.get("category") if isinstance(row.get("category"), dict) else {}
    game = _clean_text(cat.get("name"))
    thumb = _thumb_from_any(row.get("thumbnail"), ch.get("id") or "")
    return MediaItem(
        video_id=slug,
        title=title,
        author=author,
        duration="EN VIVO",
        duration_sec=0,
        thumb_url=thumb,
        kind="kick",
        channel_avatar_url=avatar,
        published_text=_format_live_subtext(game, viewers),
        is_live=True,
        game_name=game,
        viewer_count=viewers,
    )


def _featured_to_item(row: dict[str, Any]) -> MediaItem | None:
    ch = row.get("channel") if isinstance(row.get("channel"), dict) else {}
    slug = str(ch.get("slug") or "").strip().lower()
    if not slug:
        return None
    user = ch.get("user") if isinstance(ch.get("user"), dict) else {}
    author = _clean_text(user.get("username") or ch.get("slug")) or slug
    avatar = str(user.get("profile_pic") or user.get("profilepic") or "").strip()
    title = _clean_text(row.get("session_title")) or f"Directo de {author}"
    viewers = int(row.get("viewer_count") or row.get("viewers") or 0)
    cats = row.get("categories") if isinstance(row.get("categories"), list) else []
    game = _clean_text((cats[0] or {}).get("name")) if cats and isinstance(cats[0], dict) else ""
    thumb = _thumb_from_any(row.get("thumbnail"), row.get("channel_id") or ch.get("id") or "")
    return MediaItem(
        video_id=slug,
        title=title,
        author=author,
        duration="EN VIVO",
        duration_sec=0,
        thumb_url=thumb,
        kind="kick",
        channel_avatar_url=avatar,
        published_text=_format_live_subtext(game, viewers),
        is_live=True,
        game_name=game,
        viewer_count=viewers,
    )


def _channel_v2_to_item(data: dict[str, Any], fallback_slug: str = "") -> MediaItem | None:
    slug = str(data.get("slug") or fallback_slug or "").strip().lower()
    if not slug:
        return None
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    author = _clean_text(user.get("username") or slug) or slug
    avatar = str(user.get("profile_pic") or user.get("profilePic") or "").strip()
    ls = data.get("livestream")
    pb = str(data.get("playback_url") or "").strip()
    is_live = isinstance(ls, dict) and ls.get("is_live") is not False and pb.startswith("http")
    if is_live and isinstance(ls, dict):
        viewers = int(ls.get("viewer_count") or 0)
        cats = ls.get("categories") if isinstance(ls.get("categories"), list) else []
        game = _clean_text((cats[0] or {}).get("name")) if cats and isinstance(cats[0], dict) else ""
        title = _clean_text(ls.get("session_title")) or f"Directo de {author}"
        thumb = _thumb_from_any(ls.get("thumbnail"), data.get("id") or "")
        return MediaItem(
            video_id=slug,
            title=title,
            author=author,
            duration="EN VIVO",
            duration_sec=0,
            thumb_url=thumb,
            kind="kick",
            channel_avatar_url=avatar,
            published_text=_format_live_subtext(game, viewers),
            is_live=True,
            game_name=game,
            viewer_count=viewers,
        )
    return MediaItem(
        video_id=slug,
        title=f"{author} (Sin conexión)",
        author=author,
        duration="OFFLINE",
        duration_sec=0,
        thumb_url=avatar,
        kind="kick",
        channel_avatar_url=avatar,
        published_text="Fuera de línea",
        is_live=False,
        game_name="",
        viewer_count=0,
    )


class KickService:
    """Cliente público ultraliviano de Kick (Directos + Favoritos locales, sin chat)."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: list[requests.Session] = []
        self._headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://kick.com/",
        }

    def _get_session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self._local.session = sess
            with self._sessions_lock:
                self._sessions.append(sess)
        return sess

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass

    def _get_json(self, url: str, params: dict[str, Any] | None = None, timeout: int = 10) -> Any:
        with self._get_session().get(
            url,
            params=params,
            headers=self._headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    def get_channel_info(self, slug: str) -> dict[str, Any] | None:
        clean = str(slug or "").strip().lstrip("#@").lower()
        if not clean:
            return None
        data = self._get_json(f"{_KICK_CHANNEL_V2_URL}/{urllib.parse.quote(clean)}", timeout=8)
        return data if isinstance(data, dict) else None

    def get_top_streams(self, limit: int = 18, language: str = "ES") -> list[MediaItem]:
        """Obtiene directos populares en vivo de Kick filtrados por idioma o global."""
        lim = max(1, min(30, int(limit)))
        lang_up = str(language or "ES").strip().upper()
        params: dict[str, Any] = {"limit": lim, "sort": "viewer_count_desc"}
        if lang_up != "ALL":
            params["language"] = lang_up.lower()

        items: list[MediaItem] = []
        seen: set[str] = set()
        try:
            data = self._get_json(_KICK_WEB_LIVESTREAMS_URL, params=params, timeout=10)
            rows = ((data or {}).get("data") or {}).get("livestreams") if isinstance(data, dict) else []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                it = _v1_livestream_to_item(row)
                if it is None or it.video_id in seen:
                    continue
                seen.add(it.video_id)
                items.append(it)
                if len(items) >= lim:
                    break
        except Exception:
            items = []

        if not items:
            fb_lang = "es" if lang_up == "ES" else "en"
            fb_data = self._get_json(f"{_KICK_FEATURED_URL}/{fb_lang}", timeout=10)
            fb_rows = (fb_data or {}).get("data") if isinstance(fb_data, dict) else []
            for row in fb_rows or []:
                if not isinstance(row, dict):
                    continue
                it = _featured_to_item(row)
                if it is None or it.video_id in seen:
                    continue
                seen.add(it.video_id)
                items.append(it)
                if len(items) >= lim:
                    break
        return items

    def get_channels_status(self, slugs: list[str]) -> list[MediaItem]:
        """Consulta en lote el estado en vivo de una lista de slugs (Favoritos)."""
        clean_slugs: list[str] = []
        seen: set[str] = set()
        for raw in slugs:
            lg = str(raw or "").strip().lstrip("#@").lower()
            if lg and lg not in seen:
                seen.add(lg)
                clean_slugs.append(lg)
        if not clean_slugs:
            return []

        batch = clean_slugs[:30]
        items: list[MediaItem] = []
        with ThreadPoolExecutor(max_workers=min(6, len(batch))) as pool:
            infos = list(pool.map(lambda s: self._safe_channel_item(s), batch))
        for it in infos:
            if it is not None:
                items.append(it)
        items.sort(key=lambda x: (0 if x.is_live else 1, -int(x.viewer_count or 0)))
        return items

    def _safe_channel_item(self, slug: str) -> MediaItem | None:
        try:
            data = self.get_channel_info(slug)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return _channel_v2_to_item(data, fallback_slug=slug)

    def search_channels(self, query: str, limit: int = 16) -> list[MediaItem]:
        """Busca canales en Kick (acepta enlace directo kick.com/<canal> o búsqueda libre)."""
        q = (query or "").strip()
        if not q:
            return []

        url_slug = extract_kick_slug(q)
        if url_slug:
            direct = self.get_channels_status([url_slug])
            if direct:
                return direct

        data = self._get_json(_KICK_SEARCH_URL, params={"searched_word": q}, timeout=10)
        channels = (data or {}).get("channels") if isinstance(data, dict) else []
        if not isinstance(channels, list):
            return []

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            slug = str(ch.get("slug") or "").strip().lower()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            candidates.append(ch)
            if len(candidates) >= limit:
                break

        live_slugs = [
            str(c.get("slug") or "").strip().lower()
            for c in candidates
            if bool(c.get("isLive")) and str(c.get("playback_url") or "").startswith("http")
        ][:8]

        enriched: dict[str, MediaItem] = {}
        if live_slugs:
            with ThreadPoolExecutor(max_workers=min(6, len(live_slugs))) as pool:
                for it in pool.map(self._safe_channel_item, live_slugs):
                    if it is not None:
                        enriched[it.video_id] = it

        results: list[MediaItem] = []
        for ch in candidates:
            slug = str(ch.get("slug") or "").strip().lower()
            if slug in enriched:
                results.append(enriched[slug])
                continue
            user = ch.get("user") if isinstance(ch.get("user"), dict) else {}
            author = _clean_text(user.get("username") or slug) or slug
            avatar = str(user.get("profilePic") or user.get("profile_pic") or "").strip()
            is_live = bool(ch.get("isLive")) and str(ch.get("playback_url") or "").startswith("http")
            ch_id = ch.get("id") or ""
            if is_live:
                results.append(
                    MediaItem(
                        video_id=slug,
                        title=f"Directo de {author}",
                        author=author,
                        duration="EN VIVO",
                        duration_sec=0,
                        thumb_url=_thumb_from_any(None, ch_id),
                        kind="kick",
                        channel_avatar_url=avatar,
                        published_text="En vivo en Kick",
                        is_live=True,
                    )
                )
            else:
                results.append(
                    MediaItem(
                        video_id=slug,
                        title=f"{author} (Sin conexión)",
                        author=author,
                        duration="OFFLINE",
                        duration_sec=0,
                        thumb_url=avatar,
                        kind="kick",
                        channel_avatar_url=avatar,
                        published_text="Fuera de línea",
                        is_live=False,
                    )
                )

        results.sort(key=lambda x: (0 if x.is_live else 1, -int(x.viewer_count or 0)))
        return results

    def resolve_live_stream(
        self,
        channel_slug: str,
        audio_only: bool = False,
        quality: str = "720p",
    ) -> ResolvedStream:
        """Resuelve el stream HLS (.m3u8) en directo de un canal de Kick."""
        slug = str(channel_slug or "").strip().lstrip("#@").lower()
        if not slug:
            raise RuntimeError("Nombre de canal de Kick inválido.")

        info = self.get_channel_info(slug)
        if not info:
            raise _KickChannelOfflineError(f"No se encontró el canal '{slug}' en Kick.")

        ls = info.get("livestream")
        playback_url = str(info.get("playback_url") or "").strip()
        if not isinstance(ls, dict) or ls.get("is_live") is False or not playback_url.startswith("http"):
            raise _KickChannelOfflineError(
                f"El canal '{slug}' no está en directo en Kick en este momento."
            )

        hls_headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://kick.com/",
            "Origin": "https://kick.com",
        }
        with self._get_session().get(playback_url, headers=hls_headers, timeout=8) as resp:
            if resp.status_code == 404 or not resp.text.strip():
                raise _KickChannelOfflineError(
                    f"El canal '{slug}' no está en directo en Kick en este momento."
                )
            resp.raise_for_status()
            m3u8_text = resp.text

        variants = _parse_hls_master(m3u8_text)
        chosen = _pick_hls_variant(variants, audio_only=audio_only, quality=quality)
        target_url = str(chosen["url"]) if chosen and chosen.get("url") else playback_url
        if not target_url.startswith("http"):
            target_url = urllib.parse.urljoin(playback_url, target_url)

        item = _channel_v2_to_item(info, fallback_slug=slug)
        return ResolvedStream(
            video_id=slug,
            primary_url=target_url,
            audio_url=None,
            http_headers=hls_headers,
            duration=0.0,
            title=item.title if item else "",
            author=item.author if item else slug,
            thumb_url=item.thumb_url if item else "",
            video_enabled=not audio_only,
            is_live=True,
        )
