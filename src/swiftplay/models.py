"""Modelos de datos compartidos para SwiftPlay."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


def upgrade_thumb_url(url: str, target_px: int = 544) -> str:
    """Solicita mayor resolución en miniaturas de Google/YT Music sin afectar otros hosts."""
    if not url:
        return url
    low = url.lower()
    if "googleusercontent.com" in low or "ggpht.com" in low:
        updated = re.sub(r"=w\d+-h\d+", f"=w{target_px}-h{target_px}", url)
        if updated != url:
            return updated
        return re.sub(r"=s\d+", f"=s{target_px}", url)
    return url


def format_duration(seconds: int | float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_duration_text(text: str | None) -> int:
    if not text:
        return 0
    parts = [p.strip() for p in text.strip().split(":")]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


@dataclass(slots=True)
class MediaItem:
    video_id: str
    title: str
    author: str = ""
    duration: str = ""
    duration_sec: int = 0
    thumb_url: str = ""
    kind: Literal["video", "music", "twitch"] = "video"
    channel_avatar_url: str = ""
    resume_pos: float = 0.0
    published_text: str = ""
    is_live: bool = False
    game_name: str = ""
    viewer_count: int = 0
    channel_id: str = ""

    def normalized_thumb(self) -> str:
        if self.thumb_url:
            return self.thumb_url
        if self.kind == "twitch" and self.video_id:
            return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{self.video_id.lower()}-440x248.jpg"
        if self.video_id:
            return f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"
        return ""

    def to_dict(self) -> dict[str, object]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "author": self.author,
            "duration": self.duration,
            "duration_sec": int(self.duration_sec or 0),
            "thumb_url": self.thumb_url,
            "kind": self.kind,
            "channel_avatar_url": self.channel_avatar_url,
            "resume_pos": float(self.resume_pos or 0.0),
            "published_text": self.published_text,
            "is_live": bool(self.is_live),
            "game_name": self.game_name,
            "viewer_count": int(self.viewer_count or 0),
            "channel_id": self.channel_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MediaItem:
        if not isinstance(data, dict):
            return cls(video_id="", title="")
        raw_kind = str(data.get("kind") or "video")
        if raw_kind == "music":
            kind: Literal["video", "music", "twitch"] = "music"
        elif raw_kind == "twitch":
            kind = "twitch"
        else:
            kind = "video"
        try:
            dur_sec = int(data.get("duration_sec") or 0)
        except (TypeError, ValueError):
            dur_sec = 0
        try:
            res_pos = max(0.0, float(data.get("resume_pos") or 0.0))
        except (TypeError, ValueError):
            res_pos = 0.0
        try:
            viewers = max(0, int(data.get("viewer_count") or 0))
        except (TypeError, ValueError):
            viewers = 0
        return cls(
            video_id=str(data.get("video_id") or "").strip(),
            title=str(data.get("title") or "").strip(),
            author=str(data.get("author") or "").strip(),
            duration=str(data.get("duration") or "").strip(),
            duration_sec=dur_sec,
            thumb_url=str(data.get("thumb_url") or "").strip(),
            kind=kind,
            channel_avatar_url=str(data.get("channel_avatar_url") or "").strip(),
            resume_pos=res_pos,
            published_text=str(data.get("published_text") or "").strip(),
            is_live=bool(data.get("is_live", False)),
            game_name=str(data.get("game_name") or "").strip(),
            viewer_count=viewers,
            channel_id=str(data.get("channel_id") or "").strip(),
        )


@dataclass(slots=True)
class ChannelInfo:
    channel_id: str
    name: str = ""
    handle: str = ""
    subscribers: str = ""
    avatar_url: str = ""
    videos_params: str = "EgZ2aWRlb3PyBgQKAjoA"
    streams_params: str = "EgdzdHJlYW1z8gYECgJ6AA=="


@dataclass(slots=True)
class AccountProfile:
    name: str = ""
    handle: str = ""
    avatar_url: str = ""


@dataclass(slots=True)
class FeedSection:
    title: str
    items: list[MediaItem] = field(default_factory=list)


@dataclass(slots=True)
class ResolvedStream:
    video_id: str
    primary_url: str
    audio_url: str | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    duration: float = 0.0
    title: str = ""
    author: str = ""
    thumb_url: str = ""
    video_enabled: bool = True
    is_live: bool = False
    live_start_ts: float = 0.0
