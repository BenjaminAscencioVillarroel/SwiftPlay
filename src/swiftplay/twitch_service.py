"""Servicio ultraliviano de Twitch (solo invitados, directos GQL + HLS Usher sin chat)."""

from __future__ import annotations

import gc
import random
import re
import urllib.parse
from typing import Any

import requests

from swiftplay.models import MediaItem, ResolvedStream
from swiftplay.youtube_service import QUALITY_HEIGHT_MAP

_TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
_TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_TOKEN_PERSISTED_SHA256 = "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RESERVED_PATHS = {
    "directory",
    "downloads",
    "jobs",
    "login",
    "p",
    "search",
    "settings",
    "signup",
    "subscriptions",
    "turbo",
    "videos",
    "wallet",
}
_URL_LOGIN_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{2,25})(?:[/?#].*)?$",
    re.IGNORECASE,
)


class _ChannelOfflineError(RuntimeError):
    """Canal existente pero sin transmisión en directo."""


def format_viewers(count: int) -> str:
    """Formatea espectadores en texto compacto legible (ej. 90.6 K, 1.2 M)."""
    c = max(0, int(count or 0))
    if c >= 1_000_000:
        num = f"{c / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{num} M"
    if c >= 1_000:
        num = f"{c / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{num} K"
    return str(c)


def extract_channel_login(query: str) -> str | None:
    """Extrae el login del canal si el usuario pegó un enlace directo o @usuario de Twitch."""
    q = (query or "").strip()
    if q.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{2,25}", q):
        login = q[1:].lower()
        return None if login in _RESERVED_PATHS else login
    m = _URL_LOGIN_RE.match(q)
    if not m:
        return None
    login = m.group(1).lower()
    if login in _RESERVED_PATHS:
        return None
    return login


def _clean_text(text: Any) -> str:
    return " ".join(str(text or "").replace("\xa0", " ").split()).strip()


def _format_live_subtext(game: str, viewers: int) -> str:
    v_str = format_viewers(viewers)
    if game:
        return f"{game}  ·  {v_str} espectadores"
    return f"{v_str} espectadores"


def _parse_hls_master(m3u8_text: str) -> list[dict[str, Any]]:
    """Extrae variantes (resolución, fps, bandwidth, audio_only, url) del master .m3u8."""
    variants: list[dict[str, Any]] = []
    media_names: dict[str, str] = {}
    lines = m3u8_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-MEDIA:"):
            gid_m = re.search(r'GROUP-ID="([^"]+)"', line)
            name_m = re.search(r'NAME="([^"]+)"', line)
            if gid_m and name_m:
                media_names[gid_m.group(1)] = name_m.group(1)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            url = ""
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    url = nxt
                    break
                j += 1
            if url:
                res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                bw_m = re.search(r"BANDWIDTH=(\d+)", line)
                fps_m = re.search(r"FRAME-RATE=([0-9.]+)", line)
                vid_m = re.search(r'VIDEO="([^"]+)"', line)
                group = vid_m.group(1) if vid_m else ""
                name = media_names.get(group, group)
                height = int(res_m.group(2)) if res_m else 0
                bw = int(bw_m.group(1)) if bw_m else 0
                fps = float(fps_m.group(1)) if fps_m else 0.0
                is_audio = (
                    group.lower() == "audio_only"
                    or name.lower() == "audio_only"
                    or (height == 0 and "audio" in name.lower())
                )
                variants.append(
                    {
                        "name": name,
                        "height": height,
                        "fps": fps,
                        "bandwidth": bw,
                        "is_audio_only": is_audio,
                        "url": url,
                    }
                )
                i = j
        i += 1
    return variants


def _pick_hls_variant(
    variants: list[dict[str, Any]], audio_only: bool, quality: str
) -> dict[str, Any] | None:
    if not variants:
        return None
    if audio_only:
        audio_vars = [v for v in variants if v["is_audio_only"]]
        if audio_vars:
            return audio_vars[0]
        return min(variants, key=lambda v: (v["height"] or 9999, v["bandwidth"]))

    video_vars = [v for v in variants if not v["is_audio_only"]]
    pool = video_vars or variants
    max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
    within = [v for v in pool if 0 < v["height"] <= max_h]
    if within:
        return max(within, key=lambda v: (v["height"], v["fps"], v["bandwidth"]))
    return min(pool, key=lambda v: (v["height"] or 9999, v["bandwidth"]))


def _node_to_twitch_item(
    login: str,
    display_name: str,
    avatar_url: str,
    stream: dict[str, Any] | None,
) -> MediaItem | None:
    clean_login = (login or "").strip().lower()
    if not clean_login:
        return None
    author = _clean_text(display_name) or clean_login
    avatar = str(avatar_url or "").strip()

    if isinstance(stream, dict) and (stream.get("previewImageURL") or stream.get("viewersCount") is not None or stream.get("title")):
        viewers = int(stream.get("viewersCount") or 0)
        game = _clean_text((stream.get("game") or {}).get("displayName"))
        title = _clean_text(stream.get("title")) or f"Directo de {author}"
        thumb = str(
            stream.get("previewImageURL")
            or f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{clean_login}-440x248.jpg"
        )
        return MediaItem(
            video_id=clean_login,
            title=title,
            author=author,
            duration="EN VIVO",
            duration_sec=0,
            thumb_url=thumb,
            kind="twitch",
            channel_avatar_url=avatar,
            published_text=_format_live_subtext(game, viewers),
            is_live=True,
            game_name=game,
            viewer_count=viewers,
        )

    return MediaItem(
        video_id=clean_login,
        title=f"{author} (Sin conexión)",
        author=author,
        duration="OFFLINE",
        duration_sec=0,
        thumb_url=avatar,
        kind="twitch",
        channel_avatar_url=avatar,
        published_text="Fuera de línea",
        is_live=False,
        game_name="",
        viewer_count=0,
    )


class TwitchService:
    """Cliente público ultraliviano de Twitch (Directos + Favoritos locales, sin chat)."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._gql_headers = {
            "Client-ID": _TWITCH_CLIENT_ID,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }

    def _post_gql(self, payload: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
        resp = self._session.post(
            _TWITCH_GQL_URL,
            headers=self._gql_headers,
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def get_top_streams(self, limit: int = 18, language: str = "ES") -> list[MediaItem]:
        """Obtiene directos populares en vivo filtrados por idioma o global."""
        lim = max(1, min(30, int(limit)))
        if language and language.upper() != "ALL":
            query = """
            query($first: Int, $langs: [Language!]) {
              streams(first: $first, options: {sort: VIEWER_COUNT, languages: $langs}) {
                edges {
                  node {
                    title
                    viewersCount
                    previewImageURL(width: 440, height: 248)
                    game { displayName }
                    broadcaster {
                      login
                      displayName
                      profileImageURL(width: 70)
                    }
                  }
                }
              }
            }
            """
            variables: dict[str, Any] = {"first": lim, "langs": [language.upper()]}
        else:
            query = """
            query($first: Int) {
              streams(first: $first, options: {sort: VIEWER_COUNT}) {
                edges {
                  node {
                    title
                    viewersCount
                    previewImageURL(width: 440, height: 248)
                    game { displayName }
                    broadcaster {
                      login
                      displayName
                      profileImageURL(width: 70)
                    }
                  }
                }
              }
            }
            """
            variables = {"first": lim}

        data = self._post_gql({"query": query, "variables": variables})
        edges = ((data.get("data") or {}).get("streams") or {}).get("edges") or []
        items: list[MediaItem] = []
        seen: set[str] = set()
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            bc = node.get("broadcaster") or {}
            login = str(bc.get("login") or "").strip().lower()
            if not login or login in seen:
                continue
            seen.add(login)
            it = _node_to_twitch_item(
                login=login,
                display_name=str(bc.get("displayName") or ""),
                avatar_url=str(bc.get("profileImageURL") or ""),
                stream=node,
            )
            if it is not None:
                items.append(it)
            if len(items) >= lim:
                break
        return items

    def get_channels_status(self, logins: list[str]) -> list[MediaItem]:
        """Consulta en lote el estado en vivo de una lista de logins (Favoritos)."""
        clean_logins: list[str] = []
        seen: set[str] = set()
        for raw in logins:
            lg = (raw or "").strip().lstrip("#@").lower()
            if lg and lg not in seen:
                seen.add(lg)
                clean_logins.append(lg)
        if not clean_logins:
            return []

        query = """
        query($logins: [String!]) {
          users(logins: $logins) {
            login
            displayName
            profileImageURL(width: 70)
            stream {
              title
              viewersCount
              previewImageURL(width: 440, height: 248)
              game { displayName }
            }
          }
        }
        """
        items: list[MediaItem] = []
        for idx in range(0, len(clean_logins), 30):
            chunk = clean_logins[idx : idx + 30]
            data = self._post_gql({"query": query, "variables": {"logins": chunk}})
            users = (data.get("data") or {}).get("users") or []
            for user in users:
                if not isinstance(user, dict):
                    continue
                it = _node_to_twitch_item(
                    login=str(user.get("login") or ""),
                    display_name=str(user.get("displayName") or ""),
                    avatar_url=str(user.get("profileImageURL") or ""),
                    stream=user.get("stream"),
                )
                if it is not None:
                    items.append(it)

        items.sort(key=lambda x: (0 if x.is_live else 1, -int(x.viewer_count or 0)))
        return items

    def search_channels(self, query: str, limit: int = 16) -> list[MediaItem]:
        """Busca canales de Twitch (acepta también enlace directo twitch.tv/canal)."""
        q = (query or "").strip()
        if not q:
            return []

        url_login = extract_channel_login(q)
        if url_login:
            direct = self.get_channels_status([url_login])
            if direct:
                return direct

        gql_query = """
        query($q: String!) {
          searchFor(userQuery: $q, platform: "web") {
            channels {
              items {
                login
                displayName
                profileImageURL(width: 70)
                stream {
                  title
                  viewersCount
                  previewImageURL(width: 440, height: 248)
                  game { displayName }
                }
              }
            }
          }
        }
        """
        data = self._post_gql({"query": gql_query, "variables": {"q": q}})
        raw_items = (
            (((data.get("data") or {}).get("searchFor") or {}).get("channels") or {}).get("items")
            or []
        )
        results: list[MediaItem] = []
        seen: set[str] = set()
        for ch in raw_items:
            if not isinstance(ch, dict):
                continue
            login = str(ch.get("login") or "").strip().lower()
            if not login or login in seen:
                continue
            seen.add(login)
            it = _node_to_twitch_item(
                login=login,
                display_name=str(ch.get("displayName") or ""),
                avatar_url=str(ch.get("profileImageURL") or ""),
                stream=ch.get("stream"),
            )
            if it is not None:
                results.append(it)
            if len(results) >= limit:
                break

        results.sort(key=lambda x: (0 if x.is_live else 1, -int(x.viewer_count or 0)))
        return results

    def resolve_live_stream(
        self,
        channel_login: str,
        audio_only: bool = False,
        quality: str = "720p",
    ) -> ResolvedStream:
        """Resuelve la variante HLS en directo vía GQL + Usher, con respaldo yt-dlp."""
        login = (channel_login or "").strip().lstrip("#@").lower()
        if not login:
            raise RuntimeError("Nombre de canal de Twitch inválido.")

        try:
            return self._resolve_via_usher(login, audio_only=audio_only, quality=quality)
        except _ChannelOfflineError:
            raise
        except Exception as fast_err:
            try:
                return self._resolve_via_ytdlp(login, audio_only=audio_only, quality=quality)
            except Exception:
                raise fast_err

    def _resolve_via_usher(
        self, login: str, audio_only: bool, quality: str
    ) -> ResolvedStream:
        tok: dict[str, Any] = {}
        for player_type in ("embed", "site"):
            payload = {
                "operationName": "PlaybackAccessToken",
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": _TOKEN_PERSISTED_SHA256,
                    }
                },
                "variables": {
                    "isLive": True,
                    "login": login,
                    "isVod": False,
                    "vodID": "",
                    "playerType": player_type,
                },
            }
            data = self._post_gql(payload, timeout=8)
            candidate = (data.get("data") or {}).get("streamPlaybackAccessToken")
            if isinstance(candidate, dict) and candidate.get("value") and candidate.get("signature"):
                tok = candidate
                break

        if not tok:
            raise RuntimeError("No se pudo obtener el token de reproducción de Twitch.")

        params = urllib.parse.urlencode(
            {
                "allow_source": "true",
                "allow_audio_only": "true",
                "fast_bread": "true",
                "p": str(random.randint(100000, 9999999)),
                "play_session_id": f"sp{random.randint(10000000, 99999999)}",
                "player_backend": "mediaplayer",
                "playlist_include_framerate": "true",
                "sig": str(tok["signature"]),
                "supported_codecs": "avc1",
                "token": str(tok["value"]),
            }
        )
        usher_url = f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?{params}"
        r = self._session.get(
            usher_url,
            headers={"User-Agent": _USER_AGENT, "Referer": "https://www.twitch.tv/"},
            timeout=8,
        )
        if r.status_code == 404 or not r.text.strip():
            raise _ChannelOfflineError(f"El canal '{login}' no está en directo en este momento.")
        r.raise_for_status()

        variants = _parse_hls_master(r.text)
        chosen = _pick_hls_variant(variants, audio_only=audio_only, quality=quality)
        target_url = chosen["url"] if chosen else usher_url
        return ResolvedStream(
            video_id=login,
            primary_url=target_url,
            audio_url=None,
            http_headers={"User-Agent": _USER_AGENT},
            duration=0.0,
            title="",
            author=login,
            thumb_url=f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-440x248.jpg",
            video_enabled=not audio_only,
        )

    def _resolve_via_ytdlp(
        self, login: str, audio_only: bool, quality: str
    ) -> ResolvedStream:
        try:
            from yt_dlp import YoutubeDL
            from yt_dlp.extractor.twitch import TwitchStreamIE

            max_h = QUALITY_HEIGHT_MAP.get(quality, 720)
            fmt = (
                "audio_only/bestaudio/worst"
                if audio_only
                else f"best[height<={max_h}]/best"
            )
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
                raise _ChannelOfflineError(f"El canal '{login}' no está en directo en este momento.")
            return ResolvedStream(
                video_id=login,
                primary_url=str(info["url"]),
                audio_url=None,
                http_headers=dict(info.get("http_headers") or {"User-Agent": _USER_AGENT}),
                duration=0.0,
                title=str(info.get("title") or ""),
                author=str(info.get("uploader") or login),
                thumb_url=f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-440x248.jpg",
                video_enabled=not audio_only,
            )
        finally:
            gc.collect()
