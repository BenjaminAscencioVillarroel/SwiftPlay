"""Caché LRU acotado de miniaturas y avatares circulares en RAM."""

from __future__ import annotations

import threading
from collections import OrderedDict

import requests
from requests.adapters import HTTPAdapter
from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap

_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_http_local = threading.local()


def _get_thumb_session() -> requests.Session:
    sess = getattr(_http_local, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _http_local.session = sess
    return sess


class _ThumbSignals(QObject):
    loaded = Signal(str, QImage, int)
    failed = Signal(str)


class _ThumbFetchTask(QRunnable):
    def __init__(
        self,
        key: str,
        url: str,
        target_size: QSize,
        generation: int = 0,
        is_current_fn=None,
        circular: bool = False,
        radius: int = 0,
    ) -> None:
        super().__init__()
        self.key = key
        self.url = url
        self.target_size = target_size
        self.generation = int(generation)
        self.is_current_fn = is_current_fn
        self.circular = circular
        self.radius = max(0, int(radius))
        self.signals = _ThumbSignals()

    def _still_current(self) -> bool:
        if self.is_current_fn is None:
            return True
        try:
            return bool(self.is_current_fn(self.generation))
        except Exception:
            return False

    def run(self) -> None:
        emitted_loaded = False
        try:
            if not self._still_current():
                return
            sess = _get_thumb_session()
            with sess.get(
                self.url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=32768):
                    if not self._still_current():
                        return
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        return
                    chunks.append(chunk)
                data = b"".join(chunks)

            if not data or not self._still_current():
                return

            img = QImage()
            if img.loadFromData(data):
                scaled = img.scaled(
                    self.target_size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                if (
                    scaled.width() > self.target_size.width()
                    or scaled.height() > self.target_size.height()
                ):
                    x = max(0, (scaled.width() - self.target_size.width()) // 2)
                    y = max(0, (scaled.height() - self.target_size.height()) // 2)
                    scaled = scaled.copy(
                        x, y, self.target_size.width(), self.target_size.height()
                    )

                if self.circular:
                    w = self.target_size.width()
                    h = self.target_size.height()
                    circ = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
                    circ.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(circ)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    path = QPainterPath()
                    path.addEllipse(0.0, 0.0, float(w), float(h))
                    painter.setClipPath(path)
                    painter.drawImage(0, 0, scaled)
                    painter.end()
                    scaled = circ
                elif self.radius > 0:
                    w = self.target_size.width()
                    h = self.target_size.height()
                    rounded = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
                    rounded.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(rounded)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    path = QPainterPath()
                    path.addRoundedRect(
                        0.0, 0.0, float(w), float(h), float(self.radius), float(self.radius)
                    )
                    painter.setClipPath(path)
                    painter.drawImage(0, 0, scaled)
                    painter.end()
                    scaled = rounded

                if not scaled.isNull() and self._still_current():
                    emitted_loaded = True
                    self.signals.loaded.emit(self.key, scaled, self.generation)
        except Exception:
            pass
        finally:
            if not emitted_loaded:
                try:
                    self.signals.failed.emit(self.key)
                except RuntimeError:
                    pass


class ThumbnailCache(QObject):
    """Mantiene como máximo `max_items` miniaturas/avatares ya escalados en RAM."""

    ready = Signal(str, QPixmap)

    def __init__(self, max_items: int = 32, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._max_items = max(1, int(max_items))
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._inflight: set[str] = set()
        self._pending_signals: set[_ThumbSignals] = set()
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._pool.setExpiryTimeout(10000)

    def _make_key(
        self, url: str, size: QSize, circular: bool, radius: int = 0
    ) -> str:
        if circular:
            mode = "c"
        elif radius > 0:
            mode = f"r{radius}"
        else:
            mode = "r"
        return f"{url}|{size.width()}x{size.height()}|{mode}"

    def _is_generation_current(self, gen: int) -> bool:
        return gen == self._generation

    def get_or_fetch(
        self,
        url: str,
        size: QSize,
        circular: bool = False,
        radius: int = 0,
    ) -> QPixmap | None:
        if not url:
            return None
        key = self._make_key(url, size, circular, radius=radius)
        pix = self._cache.get(key)
        if pix is not None:
            self._cache.move_to_end(key)
            return pix

        if key in self._inflight:
            return None
        self._inflight.add(key)

        gen = self._generation
        task = _ThumbFetchTask(
            key,
            url,
            size,
            generation=gen,
            is_current_fn=self._is_generation_current,
            circular=circular,
            radius=radius,
        )
        sig = task.signals
        self._pending_signals.add(sig)
        sig.loaded.connect(lambda k, img, g, s=sig: self._on_loaded(k, img, g, s))
        sig.failed.connect(lambda k, s=sig: self._on_failed(k, s))
        self._pool.start(task)
        return None

    def _release_signal(self, sig: _ThumbSignals | None) -> None:
        if sig is not None:
            self._pending_signals.discard(sig)
            try:
                sig.deleteLater()
            except RuntimeError:
                pass

    def _on_loaded(
        self,
        key: str,
        img: QImage,
        gen: int | None = None,
        sig: _ThumbSignals | None = None,
    ) -> None:
        self._inflight.discard(key)
        self._release_signal(sig)
        if gen is not None and gen != self._generation:
            return
        if img.isNull():
            return
        pix = QPixmap.fromImage(img)
        self._cache[key] = pix
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_items:
            self._cache.popitem(last=False)
        url = key.rsplit("|", 2)[0]
        self.ready.emit(url, pix)

    def _on_failed(self, key: str, sig: _ThumbSignals | None = None) -> None:
        self._inflight.discard(key)
        self._release_signal(sig)

    def cancel_pending(self) -> None:
        self._generation += 1
        self._pool.clear()
        self._inflight.clear()

    def clear(self) -> None:
        self._cache.clear()

    def trim(self, keep: int = 12) -> None:
        """Reduce la caché LRU en memoria conservando solo las más recientes."""
        limit = max(0, int(keep))
        while len(self._cache) > limit:
            self._cache.popitem(last=False)

    def shutdown(self) -> None:
        self.cancel_pending()
        self.clear()
        self._pool.waitForDone(1000)
        for sig in list(self._pending_signals):
            self._release_signal(sig)
