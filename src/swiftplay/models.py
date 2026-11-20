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
    kind: Literal["video", "music"] = "video"
    channel_avatar_url: str = ""
    resume_pos: float = 0.0

    def normalized_thumb(self) -> str:
        if self.thumb_url:
            return self.thumb_url
        if self.video_id:
            return f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"
        return ""


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
