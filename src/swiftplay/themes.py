"""Paleta unificada Grafito Oscuro y Plateado Mate de SwiftPlay + QSS."""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    key: str
    name: str
    base: str
    surface1: str
    surface2: str
    border: str
    border_bright: str
    text: str
    text2: str
    accent: str
    accent_hover: str
    accent_text: str


@dataclass(frozen=True)
class NotifyColorSpec:
    key: str
    name: str
    badge_bg: str
    border: str
    hover_border: str
    tint_rgba: str
    hover_tint_rgba: str
    badge_text: str


THEMES: dict[str, ThemePalette] = {
    "matte_silver": ThemePalette(
        key="matte_silver",
        name="Obsidian Graphite & Matte Silver",
        base="#0B0C0E",
        surface1="#121418",
        surface2="#1A1E26",
        border="#262B36",
        border_bright="#3B4252",
        text="#F1F3F6",
        text2="#9299A8",
        accent="#CFD5DF",
        accent_hover="#E4E8EE",
        accent_text="#0B0C0E",
    ),
}

NOTIFICATION_COLORS: dict[str, NotifyColorSpec] = {
    "purple": NotifyColorSpec(
        key="purple",
        name="Morado",
        badge_bg="#8B5CF6",
        border="#A78BFA",
        hover_border="#C4B5FD",
        tint_rgba="rgba(139, 92, 246, 40)",
        hover_tint_rgba="rgba(139, 92, 246, 62)",
        badge_text="#FFFFFF",
    ),
    "indigo": NotifyColorSpec(
        key="indigo",
        name="Índigo",
        badge_bg="#6366F1",
        border="#818CF8",
        hover_border="#A5B4FC",
        tint_rgba="rgba(99, 102, 241, 40)",
        hover_tint_rgba="rgba(99, 102, 241, 62)",
        badge_text="#FFFFFF",
    ),
    "blue": NotifyColorSpec(
        key="blue",
        name="Azul",
        badge_bg="#3B82F6",
        border="#60A5FA",
        hover_border="#93C5FD",
        tint_rgba="rgba(59, 130, 246, 36)",
        hover_tint_rgba="rgba(59, 130, 246, 56)",
        badge_text="#FFFFFF",
    ),
    "pink": NotifyColorSpec(
        key="pink",
        name="Rosa",
        badge_bg="#EC4899",
        border="#F472B6",
        hover_border="#F9A8D4",
        tint_rgba="rgba(236, 72, 153, 38)",
        hover_tint_rgba="rgba(236, 72, 153, 58)",
        badge_text="#FFFFFF",
    ),
    "emerald": NotifyColorSpec(
        key="emerald",
        name="Esmeralda",
        badge_bg="#10B981",
        border="#34D399",
        hover_border="#6EE7B7",
        tint_rgba="rgba(16, 185, 129, 36)",
        hover_tint_rgba="rgba(16, 185, 129, 56)",
        badge_text="#0B0C0E",
    ),
    "amber": NotifyColorSpec(
        key="amber",
        name="Ámbar",
        badge_bg="#F59E0B",
        border="#FBBF24",
        hover_border="#FDE68A",
        tint_rgba="rgba(245, 158, 11, 36)",
        hover_tint_rgba="rgba(245, 158, 11, 56)",
        badge_text="#0B0C0E",
    ),
    "silver": NotifyColorSpec(
        key="silver",
        name="Plateado",
        badge_bg="#CFD5DF",
        border="#E4E8EE",
        hover_border="#FFFFFF",
        tint_rgba="rgba(207, 213, 223, 32)",
        hover_tint_rgba="rgba(207, 213, 223, 50)",
        badge_text="#0B0C0E",
    ),
}


def get_notification_color(color_key: str = "purple") -> NotifyColorSpec:
    return NOTIFICATION_COLORS.get(
        str(color_key or "").strip().lower(), NOTIFICATION_COLORS["purple"]
    )


def get_theme(theme_key: str = "matte_silver") -> ThemePalette:
    return THEMES.get(theme_key, THEMES["matte_silver"])


def build_stylesheet(theme_key: str = "matte_silver", notify_color: str = "purple") -> str:
    p = get_theme(theme_key)
    nc = get_notification_color(notify_color)
    return f"""
    QWidget {{
        background-color: {p.base};
        color: {p.text};
        font-family: "Segoe UI", "Inter", sans-serif;
        font-size: 13px;
        selection-background-color: #2D3647;
        selection-color: {p.text};
    }}

    QLabel {{
        background-color: transparent;
    }}

    QTextBrowser {{
        selection-background-color: #3B4C6B;
        selection-color: #FFFFFF;
    }}

    QSlider {{
        background-color: transparent;
    }}

    QMainWindow, QDialog {{
        background-color: {p.base};
    }}

    QFrame#Sidebar {{
        background-color: {p.surface1};
        border-right: 1px solid rgba(255, 255, 255, 14);
    }}

    QScrollArea#SidebarScroll,
    QScrollArea#TransparentScroll,
    QWidget#SidebarViewport,
    QWidget#SidebarNavContainer,
    QWidget#TransparentBox {{
        background-color: transparent;
        border: none;
    }}

    QLabel#BrandTitle {{
        font-size: 16px;
        font-weight: 700;
        color: {p.text};
        letter-spacing: 0.4px;
    }}

    QLabel#SectionHeader {{
        color: {p.text2};
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.3px;
        padding: 8px 10px 2px 10px;
    }}

    QLabel#ViewTitle {{
        font-size: 21px;
        font-weight: 700;
        letter-spacing: -0.2px;
        color: {p.text};
    }}

    QLabel#MutedLabel {{
        color: {p.text2};
        font-size: 12px;
    }}

    QLabel#DeckTag {{
        background-color: rgba(255, 255, 255, 14);
        color: {p.accent};
        border: none;
        border-radius: 6px;
        padding: 2px 6px;
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.7px;
    }}

    QPushButton#NavBtn {{
        background-color: transparent;
        color: {p.text2};
        border: 1px solid transparent;
        border-radius: 10px;
        text-align: left;
        padding: 6px 12px;
        font-size: 13px;
        font-weight: 500;
    }}

    QPushButton#NavBtn:hover {{
        background-color: rgba(255, 255, 255, 9);
        color: {p.text};
        border: 1px solid transparent;
    }}

    QPushButton#NavBtn[unread="true"] {{
        background-color: {nc.tint_rgba};
        color: #FFFFFF;
        border: 1px solid {nc.border};
        border-radius: 10px;
        font-weight: 700;
    }}

    QPushButton#NavBtn[unread="true"]:hover {{
        background-color: {nc.hover_tint_rgba};
        border-color: {nc.hover_border};
    }}

    QPushButton#NavBtn[active="true"] {{
        background-color: #2D3647;
        color: {p.text};
        border: 1px solid {p.accent};
        border-radius: 10px;
        font-weight: 700;
    }}

    QPushButton#NavBtn[active="true"]:hover {{
        background-color: #384358;
        border-color: {p.accent_hover};
    }}

    QLabel#UnreadBadge {{
        background-color: {nc.badge_bg};
        color: {nc.badge_text};
        border: none;
        border-radius: 8px;
        padding: 1px 6px;
        min-width: 14px;
        font-size: 10px;
        font-weight: 700;
    }}

    QFrame#ResourceMonitorCard {{
        background-color: rgba(255, 255, 255, 8);
        border: 1px solid rgba(255, 255, 255, 20);
        border-radius: 10px;
    }}

    QFrame#TopBar {{
        background-color: {p.base};
        border-bottom: 1px solid rgba(255, 255, 255, 12);
    }}

    QLineEdit, QPlainTextEdit {{
        background-color: {p.surface1};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: 10px;
        padding: 7px 12px;
    }}

    QLineEdit:focus, QPlainTextEdit:focus {{
        border: 1px solid {p.accent};
        background-color: {p.surface2};
    }}

    QLineEdit#SearchInput {{
        background-color: {p.surface1};
        border: 1px solid rgba(255, 255, 255, 18);
        border-radius: 15px;
        padding: 7px 16px;
        min-height: 22px;
        font-size: 13px;
    }}

    QLineEdit#SearchInput:focus {{
        border: 1px solid {p.accent};
        background-color: {p.surface2};
    }}

    QPushButton#SearchSubmitBtn {{
        background-color: rgba(255, 255, 255, 14);
        color: {p.text};
        border: 1px solid rgba(255, 255, 255, 20);
        border-radius: 16px;
        padding: 0px;
    }}

    QPushButton#SearchSubmitBtn:hover {{
        background-color: rgba(255, 255, 255, 26);
        border-color: {p.border_bright};
    }}

    QPushButton#SearchSubmitBtn:pressed {{
        background-color: {p.surface1};
    }}

    QPushButton {{
        background-color: {p.surface2};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: 10px;
        padding: 6px 12px;
        font-weight: 500;
    }}

    QPushButton:hover {{
        border-color: {p.border_bright};
        background-color: #212630;
    }}

    QPushButton:pressed {{
        background-color: {p.surface1};
    }}

    QPushButton:checked {{
        background-color: #2D3647;
        color: {p.text};
        border: 1px solid {p.accent};
        font-weight: 700;
    }}

    QPushButton:checked:hover {{
        background-color: #384358;
        border-color: {p.accent_hover};
    }}

    QPushButton:disabled {{
        color: {p.text2};
        opacity: 0.55;
    }}

    QPushButton#AccentBtn {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #323B4D, stop:1 #252C3A);
        color: {p.text};
        border: 1px solid {p.accent};
        border-radius: 10px;
        padding: 7px 16px;
        font-weight: 700;
    }}

    QPushButton#AccentBtn:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3E4A60, stop:1 #2E3749);
        border-color: {p.accent_hover};
    }}

    QPushButton#AccentBtn:pressed {{
        background-color: #1F2531;
        border-color: {p.accent};
    }}

    QPushButton#PlayAccentBtn {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #EAEEF4, stop:1 #C2C9D4);
        color: {p.accent_text};
        border: 1px solid #F2F4F8;
        border-radius: 16px;
        padding: 0px;
        font-weight: 700;
    }}

    QPushButton#PlayAccentBtn:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F6F8FB, stop:1 #D3D9E3);
        border-color: #FFFFFF;
    }}

    QPushButton#PlayAccentBtn:pressed {{
        background-color: #AEB7C4;
        border-color: {p.accent};
    }}

    QPushButton#DangerBtn {{
        background-color: transparent;
        color: #F87171;
        border: 1px solid #7F1D1D;
        border-radius: 10px;
        padding: 7px 14px;
        font-weight: 600;
    }}

    QPushButton#DangerBtn:hover {{
        background-color: #7F1D1D;
        color: #FFFFFF;
    }}

    QPushButton#IconBtn {{
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 10px;
        padding: 5px;
    }}

    QPushButton#IconBtn:hover {{
        background-color: {p.surface2};
        border: 1px solid {p.border_bright};
    }}

    QPushButton#IconBtn[active="true"] {{
        background-color: #2D3647;
        border: 1px solid {p.accent};
        color: {p.text};
    }}

    QPushButton#IconBtn[active="true"]:hover {{
        background-color: #384358;
        border-color: {p.accent_hover};
    }}

    QPushButton#PillBtn {{
        background-color: {p.surface1};
        color: {p.text2};
        border: 1px solid {p.border};
        border-radius: 10px;
        min-height: 18px;
        padding: 4px 12px;
        font-size: 12px;
        font-weight: 500;
    }}

    QPushButton#PillBtn:hover {{
        color: {p.text};
        background-color: {p.surface2};
        border-color: {p.border_bright};
    }}

    QPushButton#PillBtn[active="true"] {{
        background-color: #2D3647;
        color: {p.text};
        border: 1px solid {p.accent};
        font-weight: 700;
    }}

    QPushButton#PillBtn[active="true"]:hover {{
        background-color: #384358;
        border-color: {p.accent_hover};
    }}

    QFrame#YtVideoTile, QFrame#YtMusicTile {{
        background-color: transparent;
        border: none;
    }}

    QFrame#MediaCard {{
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 10px;
    }}

    QFrame#MediaCard:hover {{
        background-color: rgba(255, 255, 255, 10);
        border-color: {p.border};
    }}

    QFrame#MusicRow {{
        background-color: transparent;
        border: none;
        border-radius: 8px;
    }}

    QPushButton#TileMoreBtn {{
        background-color: transparent;
        border: none;
        border-radius: 11px;
        padding: 2px;
    }}

    QPushButton#TileMoreBtn:hover {{
        background-color: rgba(255, 255, 255, 24);
    }}

    QPushButton#ShelfNavBtn {{
        background-color: rgba(255, 255, 255, 10);
        border: 1px solid {p.border};
        border-radius: 14px;
        padding: 0px;
    }}

    QPushButton#ShelfNavBtn:hover {{
        background-color: {p.surface2};
        border-color: {p.border_bright};
    }}

    QLabel#QueueTabActive {{
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 1.1px;
        padding: 6px 2px 8px 2px;
        border-bottom: 2px solid {p.accent};
        color: {p.text};
    }}

    QFrame#PlayerBar, QFrame#VideoControlsBar {{
        background-color: {p.surface1};
        border-top: 1px solid rgba(255, 255, 255, 16);
    }}

    QFrame#ContinueBanner {{
        background-color: rgba(255, 255, 255, 9);
        border: 1px solid rgba(255, 255, 255, 18);
        border-radius: 12px;
    }}

    QFrame#ContinueBanner:hover {{
        background-color: rgba(255, 255, 255, 14);
        border-color: {p.border_bright};
    }}

    QFrame#MiniClickArea {{
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 2px 6px;
    }}

    QFrame#MiniClickArea:hover {{
        background-color: {p.surface2};
        border: 1px solid {p.border};
    }}

    QFrame#MiniBarFrame {{
        background-color: {p.surface1};
        border: 1px solid {p.border_bright};
        border-radius: 12px;
    }}

    QFrame#FsOverlayFrame {{
        background-color: rgba(14, 16, 20, 212);
        border: 1px solid rgba(207, 213, 223, 62);
        border-radius: 14px;
    }}

    QFrame#VolumeHudFrame {{
        background-color: rgba(14, 16, 20, 218);
        border: 1px solid rgba(207, 213, 223, 72);
        border-radius: 12px;
    }}

    QProgressBar#HudProgress {{
        background-color: rgba(255, 255, 255, 26);
        border: none;
        border-radius: 3px;
    }}

    QProgressBar#HudProgress::chunk {{
        background-color: {p.accent};
        border-radius: 3px;
    }}

    QProgressBar#HistoryProgress {{
        background-color: rgba(255, 255, 255, 24);
        border: none;
        border-radius: 1px;
    }}

    QProgressBar#HistoryProgress::chunk {{
        background-color: {p.accent};
        border-radius: 1px;
    }}

    QFrame#ShortcutDialogFrame {{
        background-color: #1A1E26;
        border: 1px solid #262B36;
        border-radius: 12px;
    }}

    QLabel#ShortcutKey {{
        background-color: #121418;
        color: {p.accent};
        border: 1px solid #3B4252;
        border-radius: 6px;
        padding: 3px 9px;
        font-family: "Consolas", "Segoe UI", monospace;
        font-size: 12px;
        font-weight: 700;
    }}

    QFrame#AudioOnlyBanner {{
        background-color: {p.surface1};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}

    QComboBox {{
        background-color: {p.surface2};
        color: {p.text};
        border: 1px solid rgba(255, 255, 255, 18);
        border-radius: 10px;
        padding: 3px 10px;
        min-height: 22px;
    }}

    QComboBox:hover {{
        border-color: {p.border_bright};
    }}

    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}

    QComboBox QAbstractItemView {{
        background-color: {p.surface1};
        color: {p.text};
        border: 1px solid {p.border_bright};
        border-radius: 10px;
        padding: 4px;
        selection-background-color: #2D3647;
        selection-color: {p.text};
        outline: none;
    }}

    QCheckBox {{
        spacing: 8px;
        color: {p.text};
        background-color: transparent;
    }}

    QCheckBox::indicator {{
        width: 17px;
        height: 17px;
        border-radius: 5px;
        border: 1px solid {p.border_bright};
        background-color: {p.surface1};
    }}

    QCheckBox::indicator:hover {{
        border-color: {p.accent};
    }}

    QCheckBox::indicator:checked {{
        background-color: {p.accent};
        border-color: {p.accent_hover};
    }}

    QSlider::groove:horizontal {{
        height: 3px;
        background: rgba(255, 255, 255, 26);
        border-radius: 1px;
    }}

    QSlider::sub-page:horizontal {{
        background: {p.accent};
        border-radius: 1px;
    }}

    QSlider::handle:horizontal {{
        background: {p.accent};
        border: 1px solid {p.base};
        width: 9px;
        height: 9px;
        margin: -3px 0;
        border-radius: 5px;
    }}

    QSlider::handle:horizontal:hover {{
        background: #FFFFFF;
        border: 1px solid {p.accent};
        width: 13px;
        height: 13px;
        margin: -5px 0;
        border-radius: 7px;
    }}

    QScrollArea {{
        border: none;
        background-color: {p.base};
    }}

    QScrollArea#SidebarScroll,
    QScrollArea#SidebarScroll > QWidget > QWidget,
    QWidget#SidebarNavContainer {{
        background-color: transparent;
        border: none;
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 6px;
        margin: 2px 1px;
    }}

    QScrollBar::handle:vertical {{
        background: rgba(255, 255, 255, 26);
        border-radius: 3px;
        min-height: 28px;
    }}

    QScrollBar::handle:vertical:hover {{
        background: rgba(255, 255, 255, 58);
    }}

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        height: 0px;
        background: transparent;
    }}

    QTabWidget::pane {{
        border: 1px solid {p.border};
        border-radius: 12px;
        background-color: {p.base};
        padding: 8px;
    }}

    QTabBar::tab {{
        background-color: {p.surface1};
        color: {p.text2};
        border: 1px solid {p.border};
        border-radius: 10px;
        padding: 7px 14px;
        margin-right: 6px;
        margin-bottom: 4px;
    }}

    QTabBar::tab:hover {{
        color: {p.text};
        border-color: {p.border_bright};
    }}

    QTabBar::tab:selected {{
        background-color: #2D3647;
        color: {p.text};
        border: 1px solid {p.accent};
        font-weight: 700;
    }}

    QListWidget::item:selected {{
        background-color: #2D3647;
        color: {p.text};
        border: 1px solid {p.accent};
        border-radius: 7px;
        font-weight: 700;
    }}

    QMenu {{
        background-color: {p.surface1};
        color: {p.text};
        border: 1px solid {p.border_bright};
        border-radius: 10px;
        padding: 5px;
    }}

    QMenu::item {{
        padding: 6px 18px;
        border-radius: 7px;
    }}

    QMenu::item:selected {{
        background-color: #2D3647;
        color: {p.text};
        border: 1px solid rgba(207, 213, 223, 120);
        font-weight: 600;
    }}

    QToolTip {{
        background-color: {p.surface2};
        color: {p.text};
        border: 1px solid {p.border_bright};
        border-radius: 8px;
        padding: 4px 8px;
    }}

    QLabel#ToastLabel {{
        background-color: {p.surface2};
        color: {p.text};
        border: 1px solid {p.accent};
        border-radius: 10px;
        padding: 8px 14px;
        font-weight: 500;
    }}

    QLabel#LiveBadge, QPushButton#LiveBadge {{
        background-color: rgba(239, 68, 68, 32);
        color: #F87171;
        border: 1px solid rgba(239, 68, 68, 95);
        border-radius: 10px;
        padding: 3px 10px;
        font-size: 10.5px;
        font-weight: 700;
    }}

    QPushButton#LiveBadge:hover {{
        background-color: rgba(239, 68, 68, 56);
        color: #FCA5A5;
        border-color: #F87171;
    }}

    QFrame#DiscordVoiceHeader {{
        background-color: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}

    QFrame#DiscordSidebarCard {{
        background-color: {p.surface1};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}

    QFrame#DiscordHeaderBar {{
        background-color: {p.surface1};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}

    QFrame#DiscordVoiceArea {{
        background-color: {p.base};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}

    QFrame#DiscordParticipantCard {{
        background-color: {p.surface1};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}

    QFrame#DiscordChatCard {{
        background-color: {p.surface1};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}

    QFrame#DiscordChatHeader {{
        background-color: {p.surface2};
        border: none;
        border-bottom: 1px solid {p.border};
        border-top-left-radius: 11px;
        border-top-right-radius: 11px;
    }}

    QLineEdit#DiscordChatInput {{
        background-color: {p.base};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: 9px;
        padding: 7px 12px;
    }}

    QLineEdit#DiscordChatInput:focus {{
        border-color: {p.accent};
    }}

    QFrame#DiscordDeckCard {{
        background-color: {p.surface2};
        border: 1px solid {p.border_bright};
        border-radius: 14px;
    }}

    QFrame#DiscordDeckCard QLabel,
    QFrame#DiscordDeckCard QCheckBox,
    QFrame#DiscordDeckCard QSlider {{
        background-color: transparent;
    }}

    QFrame#DiscordSidebarMiniCard {{
        background-color: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}

    QFrame#DiscordChannelRow {{
        background-color: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 9px;
    }}

    QFrame#DiscordChannelRow:hover {{
        border-color: {p.border_bright};
    }}

    QFrame#DiscordChannelRow[clickable="true"]:hover {{
        background-color: #24272F;
        border-color: {p.border_bright};
    }}

    QFrame#DiscordChannelRow[active="true"] {{
        background-color: #16221B;
        border: 1px solid #2F6F52;
    }}

    QFrame#ScreenShareLiveStrip {{
        background-color: #1A161A;
        border: 1px solid #5B2E35;
        border-radius: 10px;
    }}

    QFrame#ScreenShareBanner {{
        background-color: {p.surface2};
        border: 1px solid {p.border_bright};
        border-radius: 10px;
    }}

    QFrame#ScreenShareViewerCard {{
        background-color: {p.surface1};
        border: 1px solid {p.border_bright};
        border-radius: 12px;
    }}
    """


def _hex_to_colorref(hex_color: str) -> int:
    c = hex_color.lstrip("#")
    r = int(c[0:2], 16)
    g = int(c[2:4], 16)
    b = int(c[4:6], 16)
    return (b << 16) | (g << 8) | r


def apply_windows_dark_titlebar(hwnd: int, bg_hex: str) -> None:
    """Aplica modo oscuro y color de barra de título en Windows 10/11."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        dwmapi = ctypes.windll.dwmapi
        dark_mode = ctypes.c_int(1)
        res = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            20,
            ctypes.byref(dark_mode),
            ctypes.sizeof(dark_mode),
        )
        if res != 0:
            dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                19,
                ctypes.byref(dark_mode),
                ctypes.sizeof(dark_mode),
            )
        colorref = ctypes.c_uint32(_hex_to_colorref(bg_hex))
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            35,
            ctypes.byref(colorref),
            ctypes.sizeof(colorref),
        )
    except Exception:
        pass
