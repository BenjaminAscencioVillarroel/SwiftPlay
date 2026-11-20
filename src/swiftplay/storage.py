"""Base de datos SQLite local para listas rápidas, historial de búsquedas y reproducción."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from swiftplay.models import MediaItem, format_duration
from swiftplay.paths import DB_PATH


class LocalLibrary:
    """Listas locales separadas: 'Mi música' (Música), 'Ver más tarde' (Video) e historial."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS my_music (
                    video_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    duration TEXT NOT NULL DEFAULT '',
                    duration_sec INTEGER NOT NULL DEFAULT 0,
                    thumb_url TEXT NOT NULL DEFAULT '',
                    added_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watch_later (
                    video_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    duration TEXT NOT NULL DEFAULT '',
                    duration_sec INTEGER NOT NULL DEFAULT 0,
                    thumb_url TEXT NOT NULL DEFAULT '',
                    channel_avatar_url TEXT NOT NULL DEFAULT '',
                    added_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_history (
                    mode TEXT NOT NULL,
                    query TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (mode, query)
                );
                CREATE TABLE IF NOT EXISTS play_history (
                    video_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    duration TEXT NOT NULL DEFAULT '',
                    duration_sec INTEGER NOT NULL DEFAULT 0,
                    thumb_url TEXT NOT NULL DEFAULT '',
                    channel_avatar_url TEXT NOT NULL DEFAULT '',
                    play_count INTEGER NOT NULL DEFAULT 1,
                    last_played_at REAL NOT NULL,
                    last_position REAL NOT NULL DEFAULT 0.0
                );
                """
            )
            # Migraciones seguras para bases de datos previas
            for sql in (
                "ALTER TABLE watch_later ADD COLUMN channel_avatar_url TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE play_history ADD COLUMN last_position REAL NOT NULL DEFAULT 0.0",
            ):
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- Mi música (Modo Música) ---

    def add_my_music(self, item: MediaItem) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO my_music (video_id, title, artist, duration, duration_sec, thumb_url, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title=excluded.title,
                    artist=excluded.artist,
                    duration=excluded.duration,
                    duration_sec=excluded.duration_sec,
                    thumb_url=excluded.thumb_url
                """,
                (
                    item.video_id,
                    item.title,
                    item.author,
                    item.duration,
                    int(item.duration_sec or 0),
                    item.normalized_thumb(),
                    time.time(),
                ),
            )

    def remove_my_music(self, video_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM my_music WHERE video_id = ?", (video_id,))

    def is_in_my_music(self, video_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM my_music WHERE video_id = ?", (video_id,)
            ).fetchone()
            return row is not None

    def list_my_music(self) -> list[MediaItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT video_id, title, artist, duration, duration_sec, thumb_url "
                "FROM my_music ORDER BY added_at DESC"
            ).fetchall()
            return [
                MediaItem(
                    video_id=r["video_id"],
                    title=r["title"],
                    author=r["artist"],
                    duration=r["duration"],
                    duration_sec=r["duration_sec"],
                    thumb_url=r["thumb_url"],
                    kind="music",
                )
                for r in rows
            ]

    def count_my_music(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM my_music").fetchone()
            return int(row[0]) if row else 0

    # --- Ver más tarde (Modo Video) ---

    def add_watch_later(self, item: MediaItem) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO watch_later (video_id, title, channel, duration, duration_sec, thumb_url, channel_avatar_url, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title=excluded.title,
                    channel=excluded.channel,
                    duration=excluded.duration,
                    duration_sec=excluded.duration_sec,
                    thumb_url=excluded.thumb_url,
                    channel_avatar_url=CASE
                        WHEN excluded.channel_avatar_url != '' THEN excluded.channel_avatar_url
                        ELSE watch_later.channel_avatar_url
                    END
                """,
                (
                    item.video_id,
                    item.title,
                    item.author,
                    item.duration,
                    int(item.duration_sec or 0),
                    item.normalized_thumb(),
                    item.channel_avatar_url or "",
                    time.time(),
                ),
            )

    def remove_watch_later(self, video_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM watch_later WHERE video_id = ?", (video_id,)
            )

    def is_in_watch_later(self, video_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM watch_later WHERE video_id = ?", (video_id,)
            ).fetchone()
            return row is not None

    def list_watch_later(self) -> list[MediaItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT video_id, title, channel, duration, duration_sec, thumb_url, channel_avatar_url "
                "FROM watch_later ORDER BY added_at DESC"
            ).fetchall()
            return [
                MediaItem(
                    video_id=r["video_id"],
                    title=r["title"],
                    author=r["channel"],
                    duration=r["duration"],
                    duration_sec=r["duration_sec"],
                    thumb_url=r["thumb_url"],
                    kind="video",
                    channel_avatar_url=r["channel_avatar_url"] or "",
                )
                for r in rows
            ]

    def count_watch_later(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM watch_later").fetchone()
            return int(row[0]) if row else 0

    # --- Historial corto de búsquedas (15 por modo) ---

    def record_search(self, mode: str, query: str) -> None:
        q = query.strip()
        if not q:
            return
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO search_history (mode, query, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(mode, query) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (mode, q, time.time()),
            )
            self._conn.execute(
                """
                DELETE FROM search_history
                WHERE mode = ? AND query NOT IN (
                    SELECT query FROM search_history
                    WHERE mode = ?
                    ORDER BY updated_at DESC
                    LIMIT 15
                )
                """,
                (mode, mode),
            )

    def get_search_history(self, mode: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT query FROM search_history WHERE mode = ? ORDER BY updated_at DESC LIMIT 15",
                (mode,),
            ).fetchall()
            return [r["query"] for r in rows]

    def clear_search_history(self, mode: str | None = None) -> None:
        with self._lock, self._conn:
            if mode:
                self._conn.execute("DELETE FROM search_history WHERE mode = ?", (mode,))
            else:
                self._conn.execute("DELETE FROM search_history")

    # --- Historial local de reproducción ("Volver a escuchar" / Historial con progreso) ---

    def record_play(self, item: MediaItem, position: float | None = None) -> None:
        if not item.video_id:
            return
        init_pos = max(0.0, float(position)) if position is not None else 0.0
        update_pos = position is not None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO play_history (
                    video_id, kind, title, author, duration, duration_sec,
                    thumb_url, channel_avatar_url, play_count, last_played_at, last_position
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    kind=excluded.kind,
                    title=excluded.title,
                    author=excluded.author,
                    duration=CASE
                        WHEN excluded.duration != '' THEN excluded.duration
                        ELSE play_history.duration
                    END,
                    duration_sec=CASE
                        WHEN excluded.duration_sec > 0 THEN excluded.duration_sec
                        ELSE play_history.duration_sec
                    END,
                    thumb_url=excluded.thumb_url,
                    channel_avatar_url=CASE
                        WHEN excluded.channel_avatar_url != '' THEN excluded.channel_avatar_url
                        ELSE play_history.channel_avatar_url
                    END,
                    play_count=play_history.play_count + 1,
                    last_played_at=excluded.last_played_at,
                    last_position=CASE
                        WHEN ? THEN excluded.last_position
                        ELSE play_history.last_position
                    END
                """,
                (
                    item.video_id,
                    item.kind,
                    item.title,
                    item.author,
                    item.duration,
                    int(item.duration_sec or 0),
                    item.normalized_thumb(),
                    item.channel_avatar_url or "",
                    time.time(),
                    init_pos,
                    1 if update_pos else 0,
                ),
            )
            self._conn.execute(
                """
                DELETE FROM play_history
                WHERE kind = ? AND video_id NOT IN (
                    SELECT video_id FROM play_history
                    WHERE kind = ?
                    ORDER BY last_played_at DESC
                    LIMIT 100
                )
                """,
                (item.kind, item.kind),
            )

    def update_play_progress(
        self, video_id: str, position: float, duration: float = 0.0
    ) -> None:
        if not video_id:
            return
        pos = max(0.0, float(position))
        dur = max(0.0, float(duration))
        if pos < 3.0 or (dur > 30.0 and pos >= dur - 5.0):
            pos = 0.0
        dur_int = int(dur) if dur > 0 else 0
        dur_str = format_duration(dur) if dur > 0 else ""
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE play_history
                SET last_position = ?,
                    last_played_at = ?,
                    duration_sec = CASE
                        WHEN ? > 0 AND duration_sec <= 0 THEN ?
                        ELSE duration_sec
                    END,
                    duration = CASE
                        WHEN ? != '' AND duration = '' THEN ?
                        ELSE duration
                    END
                WHERE video_id = ?
                """,
                (pos, time.time(), dur_int, dur_int, dur_str, dur_str, video_id),
            )

    def get_play_position(self, video_id: str) -> float:
        if not video_id:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT last_position FROM play_history WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            return float(row[0] or 0.0) if row else 0.0

    def list_video_history(self, limit: int = 50) -> list[MediaItem]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT video_id, kind, title, author, duration, duration_sec,
                       thumb_url, channel_avatar_url, last_position
                FROM play_history
                WHERE kind = 'video'
                ORDER BY last_played_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [
                MediaItem(
                    video_id=r["video_id"],
                    title=r["title"],
                    author=r["author"],
                    duration=r["duration"],
                    duration_sec=r["duration_sec"],
                    thumb_url=r["thumb_url"],
                    kind="video",
                    channel_avatar_url=r["channel_avatar_url"] or "",
                    resume_pos=float(r["last_position"] or 0.0),
                )
                for r in rows
            ]

    def count_video_history(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM play_history WHERE kind = 'video'"
            ).fetchone()
            return int(row[0]) if row else 0

    def remove_from_history(self, video_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM play_history WHERE video_id = ?", (video_id,)
            )

    def clear_video_history(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM play_history WHERE kind = 'video'")

    def list_music_history(self, limit: int = 80) -> list[MediaItem]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT video_id, kind, title, author, duration, duration_sec,
                       thumb_url, channel_avatar_url, last_position
                FROM play_history
                WHERE kind = 'music'
                ORDER BY last_played_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [
                MediaItem(
                    video_id=r["video_id"],
                    title=r["title"],
                    author=r["author"],
                    duration=r["duration"],
                    duration_sec=r["duration_sec"],
                    thumb_url=r["thumb_url"],
                    kind="music",
                    channel_avatar_url=r["channel_avatar_url"] or "",
                    resume_pos=float(r["last_position"] or 0.0),
                )
                for r in rows
            ]

    def count_music_history(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM play_history WHERE kind = 'music'"
            ).fetchone()
            return int(row[0]) if row else 0

    def clear_music_history(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM play_history WHERE kind = 'music'")

    def list_listen_again(self, kind: str = "music", limit: int = 8) -> list[MediaItem]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT video_id, kind, title, author, duration, duration_sec,
                       thumb_url, channel_avatar_url, last_position
                FROM play_history
                WHERE kind = ?
                ORDER BY last_played_at DESC, play_count DESC
                LIMIT ?
                """,
                (kind, max(1, int(limit))),
            ).fetchall()
            return [
                MediaItem(
                    video_id=r["video_id"],
                    title=r["title"],
                    author=r["author"],
                    duration=r["duration"],
                    duration_sec=r["duration_sec"],
                    thumb_url=r["thumb_url"],
                    kind=r["kind"],
                    channel_avatar_url=r["channel_avatar_url"] or "",
                    resume_pos=float(r["last_position"] or 0.0),
                )
                for r in rows
            ]
