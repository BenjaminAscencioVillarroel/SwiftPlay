"""Estado ligero persistente de la última sesión de reproducción (Video + Música)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from swiftplay.models import MediaItem
from swiftplay.paths import SESSION_STATE_PATH

_MAX_PERSISTED_QUEUE = 15


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
        return max(0.0, v)
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int = -1) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class SessionState:
    active_deck: Literal["video", "music"] = "video"
    video_item: MediaItem | None = None
    video_pos: float = 0.0
    video_dur: float = 0.0
    music_item: MediaItem | None = None
    music_pos: float = 0.0
    music_dur: float = 0.0
    music_queue: list[MediaItem] = field(default_factory=list)
    queue_index: int = -1


def load_session_state(path: Path = SESSION_STATE_PATH) -> SessionState:
    if not path.exists():
        return SessionState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return SessionState()
    except Exception:
        return SessionState()

    deck_raw = str(raw.get("active_deck") or "video")
    active_deck: Literal["video", "music"] = "music" if deck_raw == "music" else "video"

    v_item: MediaItem | None = None
    if isinstance(raw.get("video_item"), dict):
        parsed_v = MediaItem.from_dict(raw["video_item"])
        if parsed_v.video_id:
            v_item = parsed_v

    m_item: MediaItem | None = None
    if isinstance(raw.get("music_item"), dict):
        parsed_m = MediaItem.from_dict(raw["music_item"])
        if parsed_m.video_id:
            m_item = parsed_m

    queue: list[MediaItem] = []
    for entry in raw.get("music_queue") or []:
        if isinstance(entry, dict):
            parsed_q = MediaItem.from_dict(entry)
            if parsed_q.video_id:
                queue.append(parsed_q)

    q_idx = _safe_int(raw.get("queue_index"), -1)
    if not queue or q_idx < 0 or q_idx >= len(queue):
        q_idx = 0 if (queue and q_idx >= len(queue)) else (-1 if not queue else max(-1, q_idx))

    return SessionState(
        active_deck=active_deck,
        video_item=v_item,
        video_pos=_safe_float(raw.get("video_pos"), 0.0) if v_item is not None else 0.0,
        video_dur=_safe_float(raw.get("video_dur"), 0.0) if v_item is not None else 0.0,
        music_item=m_item,
        music_pos=_safe_float(raw.get("music_pos"), 0.0) if m_item is not None else 0.0,
        music_dur=_safe_float(raw.get("music_dur"), 0.0) if m_item is not None else 0.0,
        music_queue=queue[:_MAX_PERSISTED_QUEUE],
        queue_index=min(q_idx, len(queue[:_MAX_PERSISTED_QUEUE]) - 1) if queue else -1,
    )


def save_session_state(state: SessionState, path: Path = SESSION_STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    valid_queue = [it for it in state.music_queue if it.video_id]
    q_idx = state.queue_index
    if len(valid_queue) > _MAX_PERSISTED_QUEUE:
        if 0 <= q_idx < len(valid_queue):
            start = max(0, q_idx - 4)
            end = start + _MAX_PERSISTED_QUEUE
            if end > len(valid_queue):
                start = max(0, len(valid_queue) - _MAX_PERSISTED_QUEUE)
                end = len(valid_queue)
            valid_queue = valid_queue[start:end]
            q_idx -= start
        else:
            valid_queue = valid_queue[:_MAX_PERSISTED_QUEUE]
            q_idx = -1
    elif not valid_queue or q_idx < 0 or q_idx >= len(valid_queue):
        q_idx = -1 if not valid_queue else max(0, min(q_idx, len(valid_queue) - 1))

    payload = {
        "active_deck": "music" if state.active_deck == "music" else "video",
        "video_item": (
            state.video_item.to_dict()
            if state.video_item is not None and state.video_item.video_id
            else None
        ),
        "video_pos": round(max(0.0, float(state.video_pos or 0.0)), 2)
        if state.video_item is not None
        else 0.0,
        "video_dur": round(max(0.0, float(state.video_dur or 0.0)), 2)
        if state.video_item is not None
        else 0.0,
        "music_item": (
            state.music_item.to_dict()
            if state.music_item is not None and state.music_item.video_id
            else None
        ),
        "music_pos": round(max(0.0, float(state.music_pos or 0.0)), 2)
        if state.music_item is not None
        else 0.0,
        "music_dur": round(max(0.0, float(state.music_dur or 0.0)), 2)
        if state.music_item is not None
        else 0.0,
        "music_queue": [it.to_dict() for it in valid_queue],
        "queue_index": q_idx,
    }

    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)
