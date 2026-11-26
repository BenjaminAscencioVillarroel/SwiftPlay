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
    surface_hover: str = "#212630"
    active_bg: str = "#2D3647"
    active_hover: str = "#384358"
    accent_grad_top: str = "#323B4D"
    accent_grad_bot: str = "#252C3A"
    accent_hover_top: str = "#3E4A60"
    accent_hover_bot: str = "#2E3749"
    accent_pressed: str = "#1F2531"
    play_grad_top: str = "#EAEEF4"
    play_grad_bot: str = "#C2C9D4"
    play_hover_top: str = "#F6F8FB"
    play_hover_bot: str = "#D3D9E3"
    play_border: str = "#F2F4F8"
    play_pressed: str = "#AEB7C4"
    row_hover: str = "#24272F"


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
        name="Grafito Plata",
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
        surface_hover="#212630",
        active_bg="#2D3647",
        active_hover="#384358",
        accent_grad_top="#323B4D",
        accent_grad_bot="#252C3A",
        accent_hover_top="#3E4A60",
        accent_hover_bot="#2E3749",
        accent_pressed="#1F2531",
        play_grad_top="#EAEEF4",
        play_grad_bot="#C2C9D4",
        play_hover_top="#F6F8FB",
        play_hover_bot="#D3D9E3",
        play_border="#F2F4F8",
        play_pressed="#AEB7C4",
        row_hover="#24272F",
    ),
    "rose_sakura": ThemePalette(
        key="rose_sakura",
        name="Rosa Sakura",
        base="#110A0E",
        surface1="#1B1016",
        surface2="#271720",
        border="#3D2131",
        border_bright="#64354F",
        text="#FDF2F7",
        text2="#C49BB0",
        accent="#F472B6",
        accent_hover="#F9A8D4",
        accent_text="#1A0B13",
        surface_hover="#331E2A",
        active_bg="#4D223A",
        active_hover="#632B4A",
        accent_grad_top="#5E2947",
        accent_grad_bot="#401B30",
        accent_hover_top="#753459",
        accent_hover_bot="#52233D",
        accent_pressed="#331526",
        play_grad_top="#FBCFE8",
        play_grad_bot="#F472B6",
        play_hover_top="#FCE7F3",
        play_hover_bot="#F9A8D4",
        play_border="#FDF2F7",
        play_pressed="#DB2777",
        row_hover="#361E2C",
    ),
    "amethyst_velvet": ThemePalette(
        key="amethyst_velvet",
        name="Amatista",
        base="#0D0A14",
        surface1="#151021",
        surface2="#201831",
        border="#33254F",
        border_bright="#523B7D",
        text="#F5F2FF",
        text2="#A99BC7",
        accent="#A78BFA",
        accent_hover="#C4B5FD",
        accent_text="#120B1F",
        surface_hover="#2B2042",
        active_bg="#3E2A66",
        active_hover="#4E3580",
        accent_grad_top="#4A327A",
        accent_grad_bot="#322154",
        accent_hover_top="#5B3E94",
        accent_hover_bot="#3F2A69",
        accent_pressed="#281A45",
        play_grad_top="#DDD6FE",
        play_grad_bot="#A78BFA",
        play_hover_top="#EDE9FE",
        play_hover_bot="#C4B5FD",
        play_border="#F5F3FF",
        play_pressed="#8B5CF6",
        row_hover="#291E40",
    ),
    "sapphire_ocean": ThemePalette(
        key="sapphire_ocean",
        name="Zafiro Azul",
        base="#080D15",
        surface1="#0E1726",
        surface2="#162338",
        border="#213452",
        border_bright="#355482",
        text="#EFF6FF",
        text2="#8FA8C9",
        accent="#60A5FA",
        accent_hover="#93C5FD",
        accent_text="#081120",
        surface_hover="#1E2F4A",
        active_bg="#233F6B",
        active_hover="#2D5087",
        accent_grad_top="#294A7D",
        accent_grad_bot="#1B3257",
        accent_hover_top="#355E9E",
        accent_hover_bot="#23406E",
        accent_pressed="#162845",
        play_grad_top="#BFDBFE",
        play_grad_bot="#60A5FA",
        play_hover_top="#DBEAFE",
        play_hover_bot="#93C5FD",
        play_border="#EFF6FF",
        play_pressed="#3B82F6",
        row_hover="#1C2C45",
    ),
    "emerald_forest": ThemePalette(
        key="emerald_forest",
        name="Esmeralda",
        base="#07100D",
        surface1="#0D1B16",
        surface2="#142921",
        border="#1E3D31",
        border_bright="#2F6350",
        text="#ECFDF5",
        text2="#89B5A3",
        accent="#34D399",
        accent_hover="#6EE7B7",
        accent_text="#061A13",
        surface_hover="#1B372C",
        active_bg="#1B4D3B",
        active_hover="#23634C",
        accent_grad_top="#205C46",
        accent_grad_bot="#153E2F",
        accent_hover_top="#2A7559",
        accent_hover_bot="#1B4F3C",
        accent_pressed="#103024",
        play_grad_top="#A7F3D0",
        play_grad_bot="#34D399",
        play_hover_top="#D1FAE5",
        play_hover_bot="#6EE7B7",
        play_border="#ECFDF5",
        play_pressed="#10B981",
        row_hover="#183329",
    ),
    "crimson_ruby": ThemePalette(
        key="crimson_ruby",
        name="Rubí Carmesí",
        base="#12090B",
        surface1="#1D0F12",
        surface2="#2B161B",
        border="#422027",
        border_bright="#6B313C",
        text="#FFF1F2",
        text2="#C7969E",
        accent="#FB7185",
        accent_hover="#FDA4AF",
        accent_text="#1C080B",
        surface_hover="#381D23",
        active_bg="#57212B",
        active_hover="#6E2936",
        accent_grad_top="#692734",
        accent_grad_bot="#471922",
        accent_hover_top="#823141",
        accent_hover_bot="#59202B",
        accent_pressed="#38131A",
        play_grad_top="#FECDD3",
        play_grad_bot="#FB7185",
        play_hover_top="#FFE4E6",
        play_hover_bot="#FDA4AF",
        play_border="#FFF1F2",
        play_pressed="#F43F5E",
        row_hover="#361B21",
    ),
    "amber_gold": ThemePalette(
        key="amber_gold",
        name="Ámbar Dorado",
        base="#110D07",
        surface1="#1B150B",
        surface2="#281F11",
        border="#403018",
        border_bright="#664D26",
        text="#FFFBEB",
        text2="#C4B08A",
        accent="#FBBF24",
        accent_hover="#FDE68A",
        accent_text="#1B1305",
        surface_hover="#352916",
        active_bg="#523B17",
        active_hover="#694C1E",
        accent_grad_top="#63471C",
        accent_grad_bot="#422F11",
        accent_hover_top="#7A5823",
        accent_hover_bot="#543C16",
        accent_pressed="#33240C",
        play_grad_top="#FDE68A",
        play_grad_bot="#F59E0B",
        play_hover_top="#FEF3C7",
        play_hover_bot="#FBBF24",
        play_border="#FFFBEB",
        play_pressed="#D97706",
        row_hover="#332613",
    ),
    "arctic_cyan": ThemePalette(
        key="arctic_cyan",
        name="Cian Ártico",
        base="#070F12",
        surface1="#0C1A20",
        surface2="#132730",
        border="#1D3C4A",
        border_bright="#2E5F73",
        text="#ECFEFF",
        text2="#8AB5C4",
        accent="#22D3EE",
        accent_hover="#67E8F9",
        accent_text="#06181E",
        surface_hover="#1A3541",
        active_bg="#1A4A5C",
        active_hover="#225E75",
        accent_grad_top="#20596E",
        accent_grad_bot="#143B4A",
        accent_hover_top="#28708A",
        accent_hover_bot="#1B4D61",
        accent_pressed="#102E3A",
        play_grad_top="#A5F3FC",
        play_grad_bot="#22D3EE",
        play_hover_top="#CFFAFE",
        play_hover_bot="#67E8F9",
        play_border="#ECFEFF",
        play_pressed="#06B6D4",
        row_hover="#18323D",
    ),
    "sunset_coral": ThemePalette(
        key="sunset_coral",
        name="Atardecer Coral",
        base="#120B08",
        surface1="#1D120D",
        surface2="#2C1B13",
        border="#452619",
        border_bright="#6E3E29",
        text="#FFF7ED",
        text2="#C9A18D",
        accent="#FB923C",
        accent_hover="#FDBA74",
        accent_text="#1C0E06",
        surface_hover="#3A241A",
        active_bg="#59301D",
        active_hover="#703C24",
        accent_grad_top="#693822",
        accent_grad_bot="#472415",
        accent_hover_top="#82462B",
        accent_hover_bot="#592E1B",
        accent_pressed="#381C10",
        play_grad_top="#FED7AA",
        play_grad_bot="#FB923C",
        play_hover_top="#FFEDD5",
        play_hover_bot="#FDBA74",
        play_border="#FFF7ED",
        play_pressed="#EA580C",
        row_hover="#382218",
    ),
    "indigo_nebula": ThemePalette(
        key="indigo_nebula",
        name="Índigo Nébula",
        base="#090B15",
        surface1="#101324",
        surface2="#191E38",
        border="#252D52",
        border_bright="#3E4B85",
        text="#EEF2FF",
        text2="#959EC9",
        accent="#818CF8",
        accent_hover="#A5B4FC",
        accent_text="#0B0E21",
        surface_hover="#22294A",
        active_bg="#2C366B",
        active_hover="#384587",
        accent_grad_top="#34407D",
        accent_grad_bot="#222A54",
        accent_hover_top="#43529E",
        accent_hover_bot="#2C376E",
        accent_pressed="#1A2142",
        play_grad_top="#C7D2FE",
        play_grad_bot="#818CF8",
        play_hover_top="#E0E7FF",
        play_hover_bot="#A5B4FC",
        play_border="#EEF2FF",
        play_pressed="#6366F1",
        row_hover="#202747",
    ),
    "matcha_sage": ThemePalette(
        key="matcha_sage",
        name="Matcha Salvia",
        base="#0B0F09",
        surface1="#131A10",
        surface2="#1D2718",
        border="#2C3B24",
        border_bright="#465E39",
        text="#F7FEE7",
        text2="#A3B895",
        accent="#A3E635",
        accent_hover="#BEF264",
        accent_text="#111A07",
        surface_hover="#273420",
        active_bg="#344D26",
        active_hover="#426330",
        accent_grad_top="#3D5C2D",
        accent_grad_bot="#293E1D",
        accent_hover_top="#4D7339",
        accent_hover_bot="#344F25",
        accent_pressed="#1F3016",
        play_grad_top="#D9F99D",
        play_grad_bot="#A3E635",
        play_hover_top="#ECFCCB",
        play_hover_bot="#BEF264",
        play_border="#F7FEE7",
        play_pressed="#84CC16",
        row_hover="#25331E",
    ),
    "magenta_orchid": ThemePalette(
        key="magenta_orchid",
        name="Orquídea Fucsia",
        base="#110913",
        surface1="#1B0F1F",
        surface2="#28162E",
        border="#3E2047",
        border_bright="#643573",
        text="#FDF4FF",
        text2="#BF9BC9",
        accent="#E879F9",
        accent_hover="#F0ABFC",
        accent_text="#1B0721",
        surface_hover="#351E3D",
        active_bg="#4F225E",
        active_hover="#652C78",
        accent_grad_top="#5E2970",
        accent_grad_bot="#3F1B4C",
        accent_hover_top="#75348C",
        accent_hover_bot="#512361",
        accent_pressed="#30143A",
        play_grad_top="#F5D0FE",
        play_grad_bot="#E879F9",
        play_hover_top="#FAE8FF",
        play_hover_bot="#F0ABFC",
        play_border="#FDF4FF",
        play_pressed="#D946EF",
        row_hover="#331C3B",
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
    return THEMES.get(str(theme_key or "").strip().lower(), THEMES["matte_silver"])


def _hex_to_rgba(hex_color: str, alpha: int) -> str:
    c = hex_color.lstrip("#")
    r = int(c[0:2], 16)
    g = int(c[2:4], 16)
    b = int(c[4:6], 16)
    return f"rgba({r}, {g}, {b}, {max(0, min(255, int(alpha)))})"


def build_stylesheet(theme_key: str = "matte_silver", notify_color: str = "purple") -> str:
    p = get_theme(theme_key)
    nc = get_notification_color(notify_color)
    fs_bg_rgba = _hex_to_rgba(p.surface1, 212)
    hud_bg_rgba = _hex_to_rgba(p.surface1, 218)
    fs_border_rgba = _hex_to_rgba(p.accent, 62)
    hud_border_rgba = _hex_to_rgba(p.accent, 72)
    menu_sel_border = _hex_to_rgba(p.accent, 120)
    return f"""
    QWidget {{
        background-color: {p.base};
        color: {p.text};
        font-family: "Segoe UI", "Inter", sans-serif;
        font-size: 13px;
        selection-background-color: {p.active_bg};
        selection-color: {p.text};
    }}

    QLabel {{
        background-color: transparent;
    }}

    QTextBrowser {{
        selection-background-color: {p.active_hover};
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
        background-color: {p.active_bg};
        color: {p.text};
        border: 1px solid {p.accent};
        border-radius: 10px;
        font-weight: 700;
    }}

    QPushButton#NavBtn[active="true"]:hover {{
        background-color: {p.active_hover};
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
        background-color: {p.surface_hover};
    }}

    QPushButton:pressed {{
        background-color: {p.surface1};
    }}

    QPushButton:checked {{
        background-color: {p.active_bg};
        color: {p.text};
        border: 1px solid {p.accent};
        font-weight: 700;
    }}

    QPushButton:checked:hover {{
        background-color: {p.active_hover};
        border-color: {p.accent_hover};
    }}

    QPushButton:disabled {{
        color: {p.text2};
        opacity: 0.55;
    }}

    QPushButton#AccentBtn {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.accent_grad_top}, stop:1 {p.accent_grad_bot});
        color: {p.text};
        border: 1px solid {p.accent};
        border-radius: 10px;
        padding: 7px 16px;
        font-weight: 700;
    }}

    QPushButton#AccentBtn:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.accent_hover_top}, stop:1 {p.accent_hover_bot});
        border-color: {p.accent_hover};
    }}

    QPushButton#AccentBtn:pressed {{
        background-color: {p.accent_pressed};
        border-color: {p.accent};
    }}

    QPushButton#PlayAccentBtn {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.play_grad_top}, stop:1 {p.play_grad_bot});
        color: {p.accent_text};
        border: 1px solid {p.play_border};
        border-radius: 16px;
        padding: 0px;
        font-weight: 700;
    }}

    QPushButton#PlayAccentBtn:hover {{
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.play_hover_top}, stop:1 {p.play_hover_bot});
        border-color: #FFFFFF;
    }}

    QPushButton#PlayAccentBtn:pressed {{
        background-color: {p.play_pressed};
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
        background-color: {p.active_bg};
        border: 1px solid {p.accent};
        color: {p.text};
    }}

    QPushButton#IconBtn[active="true"]:hover {{
        background-color: {p.active_hover};
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
        background-color: {p.active_bg};
        color: {p.text};
        border: 1px solid {p.accent};
        font-weight: 700;
    }}

    QPushButton#PillBtn[active="true"]:hover {{
        background-color: {p.active_hover};
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
        background-color: {fs_bg_rgba};
        border: 1px solid {fs_border_rgba};
        border-radius: 14px;
    }}

    QFrame#VolumeHudFrame {{
        background-color: {hud_bg_rgba};
        border: 1px solid {hud_border_rgba};
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
        background-color: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}

    QLabel#ShortcutKey {{
        background-color: {p.surface1};
        color: {p.accent};
        border: 1px solid {p.border_bright};
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
        selection-background-color: {p.active_bg};
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
        background-color: {p.active_bg};
        color: {p.text};
        border: 1px solid {p.accent};
        font-weight: 700;
    }}

    QListWidget::item:selected {{
        background-color: {p.active_bg};
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
        background-color: {p.active_bg};
        color: {p.text};
        border: 1px solid {menu_sel_border};
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
        background-color: {p.row_hover};
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

    QLabel#ScreenShareLiveBadge, QPushButton#ScreenShareLiveBadge {{
        background-color: #DC2626;
        color: #FFFFFF;
        border: none;
        border-radius: 6px;
        padding: 1px 6px;
        font-size: 10px;
        font-weight: 700;
    }}

    QPushButton#ScreenShareLiveBadge:hover {{
        background-color: #EF4444;
        color: #FFFFFF;
    }}

    QPushButton#ScreenShareCardWatchBtn {{
        background-color: #2A1B20;
        color: #F1F3F6;
        border: 1px solid #991B1B;
        border-radius: 8px;
        padding: 3px 9px;
        font-size: 11px;
        font-weight: 600;
    }}

    QPushButton#ScreenShareCardWatchBtn:hover {{
        background-color: #B91C1C;
        color: #FFFFFF;
        border-color: #EF4444;
    }}

    QFrame#WatchPartyFriendCard {{
        background-color: {p.surface1};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}

    QFrame#WatchPartyFriendCard:hover {{
        border-color: {p.border_bright};
    }}

    QFrame#WatchPartySubCard {{
        background-color: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}

    QFrame#WatchPartySubCard:hover {{
        border-color: {p.border_bright};
    }}

    QFrame#WatchPartyActiveBar {{
        background-color: {p.surface2};
        border: 1px solid {p.accent};
        border-radius: 10px;
    }}

    QFrame#WatchPartyEyeBadge {{
        background-color: {p.surface2};
        border: 1px solid {p.accent};
        border-radius: 10px;
    }}

    QFrame#WatchPartySidebarMiniCard {{
        background-color: {p.surface2};
        border: 1px solid {p.accent};
        border-radius: 10px;
    }}

    QLabel#WatchPartyCodePill {{
        background-color: {p.surface2};
        color: {p.accent};
        border: 1px solid {p.border_bright};
        border-radius: 8px;
        padding: 4px 10px;
        font-size: 12px;
        font-weight: 700;
    }}

    QLabel#WatchPartyUnreadBadge {{
        background-color: {p.accent};
        color: {p.accent_text};
        border: none;
        border-radius: 9px;
        padding: 0px 5px;
        font-size: 10px;
        font-weight: 700;
    }}

    QFrame#WatchPartyMsgSelf {{
        background-color: {p.active_bg};
        border: 1px solid {p.accent};
        border-radius: 10px;
    }}

    QFrame#WatchPartyMsgPeer {{
        background-color: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 10px;
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
