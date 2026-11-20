"""Caché LRU acotado de miniaturas y avatares circulares en RAM."""

from __future__ import annotations

import urllib.request
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap


class _ThumbSignals(QObject):
    loaded = Signal(str, QImage)


class _ThumbFetchTask(QRunnable):
    def __init__(
        self,
        key: str,
        url: str,
        target_size: QSize,
        circular: bool = False,
        radius: int = 0,
    ) -> None:
        super().__init__()
        self.key = key
        self.url = url
        self.target_size = target_size
        self.circular = circular
        self.radius = max(0, int(radius))
        self.signals = _ThumbSignals()

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self.url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
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

                self.signals.loaded.emit(self.key, scaled)
        except Exception:
            pass


class ThumbnailCache(QObject):
    """Mantiene como máximo `max_items` miniaturas/avatares ya escalados en RAM."""

    ready = Signal(str, QPixmap)

    def __init__(self, max_items: int = 45, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._max_items = max_items
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._inflight: set[str] = set()
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

        task = _ThumbFetchTask(key, url, size, circular=circular, radius=radius)
        task.signals.loaded.connect(self._on_loaded)
        self._pool.start(task)
        return None

    def _on_loaded(self, key: str, img: QImage) -> None:
        self._inflight.discard(key)
        if img.isNull():
            return
        pix = QPixmap.fromImage(img)
        self._cache[key] = pix
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_items:
            self._cache.popitem(last=False)
        url = key.rsplit("|", 2)[0]
        self.ready.emit(url, pix)

    def clear(self) -> None:
        self._cache.clear()

    def trim(self, keep: int = 12) -> None:
        """Reduce la caché LRU en memoria conservando solo las más recientes."""
        limit = max(0, int(keep))
        while len(self._cache) > limit:
            self._cache.popitem(last=False)
