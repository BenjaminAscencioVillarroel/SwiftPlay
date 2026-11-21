"""Registro ligero en texto plano del consumo de RAM de SwiftPlay."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading

from swiftplay.paths import RAM_LOG_PATH


class RamLogger:
    """Escribe muestras acotadas de consumo de RAM (GUI + Worker Discord) en ram_log.txt."""

    def __init__(
        self,
        log_path: Path = RAM_LOG_PATH,
        max_bytes: int = 512 * 1024,
    ) -> None:
        self._path = Path(log_path)
        self._max_bytes = max(8 * 1024, int(max_bytes))
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _append_line(self, line: str) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self._rotate_if_needed_locked()
            except OSError:
                pass

    def _rotate_if_needed_locked(self) -> None:
        try:
            if not self._path.exists():
                return
            if self._path.stat().st_size <= self._max_bytes:
                return
            lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= 2:
                return
            keep = lines[len(lines) // 2 :]
            self._path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except OSError:
            pass

    def log_session_start(self) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"=== [{ts}] INICIO DE SESIÓN SwiftPlay ==="
        self._append_line(line)
        return line

    def log_sample(
        self,
        event: str,
        *,
        gui_ws_mb: float,
        gui_priv_mb: float,
        worker_ws_mb: float = 0.0,
        worker_priv_mb: float = 0.0,
        page: str = "",
        mpv_state: str = "none",
        discord_state: str = "off",
    ) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_ws = float(gui_ws_mb) + float(worker_ws_mb)
        total_priv = float(gui_priv_mb) + float(worker_priv_mb)
        line = (
            f"[{ts}] {event} | "
            f"Total WS={total_ws:.1f} MB (Priv={total_priv:.1f} MB) | "
            f"GUI WS={float(gui_ws_mb):.1f} MB (Priv={float(gui_priv_mb):.1f} MB) | "
            f"Discord WS={float(worker_ws_mb):.1f} MB (Priv={float(worker_priv_mb):.1f} MB) | "
            f"page={page or '-'} | mpv={mpv_state or 'none'} | discord={discord_state or 'off'}"
        )
        self._append_line(line)
        return line
