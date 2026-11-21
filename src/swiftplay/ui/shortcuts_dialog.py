"""Modal minimalista de atajos de teclado (?) en tonos grafito y plata mate."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from swiftplay.config import AppConfig
from swiftplay.themes import ThemePalette
from swiftplay.ui.icons import render_icon

_SHORTCUT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "NAVEGACIÓN Y MODOS",
        (
            ("Tab", "Rotar al instante entre YouTube, YT Music, Twitch y Discord"),
            ("T", "Ocultar / mostrar barra lateral (Modo teatro)"),
            ("Ctrl + L", "Enfocar la barra de búsqueda"),
            ("?", "Mostrar / cerrar esta guía de atajos"),
        ),
    ),
    (
        "REPRODUCCIÓN",
        (
            ("Espacio  o  Clic en video", "Reproducir / Pausar el reproductor activo"),
            ("←  /  →", "Retroceder / Adelantar 5 segundos"),
            ("M", "Silenciar / Activar sonido"),
            ("Rueda del mouse", "Subir / Bajar volumen sobre el video (±5%)"),
        ),
    ),
    (
        "PANTALLA COMPLETA Y TECLAS GLOBALES",
        (
            ("F  o  Doble clic", "Activar / Salir de pantalla completa 100%"),
            ("Esc", "Salir de pantalla completa"),
            ("Play / Sig. / Ant.", "Teclas multimedia globales (incluso en bandeja)"),
        ),
    ),
)


class ShortcutsDialog(QDialog):
    """Ventana flotante minimalista (#1A1E26 y borde #262B36) con los atajos de SwiftPlay."""

    def __init__(
        self,
        palette: ThemePalette,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(540)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("ShortcutDialogFrame")
        outer.addWidget(card)

        root = QVBoxLayout(card)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        hdr = QHBoxLayout()
        title = QLabel("Atajos de teclado")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        hdr.addWidget(title)
        hdr.addStretch()

        close_btn = QPushButton()
        close_btn.setObjectName("IconBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.setIcon(render_icon("close", palette.text2, 15))
        close_btn.setToolTip("Cerrar [Esc]")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        hdr.addWidget(close_btn)
        root.addLayout(hdr)

        mute_key = (config.discord_hotkey_mute if config is not None else "Ctrl+Shift+M").strip()
        deaf_key = (config.discord_hotkey_deafen if config is not None else "Ctrl+Shift+D").strip()
        hang_key = (config.discord_hotkey_hangup if config is not None else "Ctrl+Shift+H").strip()
        prio_tag = (
            " (prioritario)"
            if (config is None or config.discord_hotkey_mute_priority)
            else ""
        )

        groups = list(_SHORTCUT_GROUPS) + [
            (
                "DISCORD (ATAJOS GLOBALES)",
                (
                    (
                        mute_key or "Desactivado",
                        f"Silenciar / Activar micrófono{prio_tag}",
                    ),
                    (deaf_key or "Desactivado", "Ensordecer / Activar audio Discord"),
                    (hang_key or "Desactivado", "Colgar / Salir del canal de voz"),
                ),
            )
        ]

        for group_title, rows in groups:
            g_lbl = QLabel(group_title)
            g_lbl.setObjectName("SectionHeader")
            g_lbl.setStyleSheet(
                "padding: 6px 0px 2px 0px; font-size: 10px; font-weight: 700; letter-spacing: 1.2px;"
            )
            root.addWidget(g_lbl)

            for key_text, desc in rows:
                r = QHBoxLayout()
                r.setContentsMargins(0, 2, 0, 2)
                r.setSpacing(12)

                desc_lbl = QLabel(desc)
                desc_lbl.setStyleSheet("font-size: 13px;")
                r.addWidget(desc_lbl, 1)

                key_lbl = QLabel(key_text)
                key_lbl.setObjectName("ShortcutKey")
                key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                r.addWidget(key_lbl)

                root.addLayout(r)

        footer = QLabel("Configura los atajos de Discord en Ajustes → Atajos Discord · Pulsa ? o Esc para cerrar")
        footer.setObjectName("MutedLabel")
        footer.setAlignment(Qt.AlignmentFlag.AlignRight)
        footer.setStyleSheet("padding-top: 6px; font-size: 11px;")
        root.addWidget(footer)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Question) or event.text() == "?":
            self.accept()
            return
        super().keyPressEvent(event)
