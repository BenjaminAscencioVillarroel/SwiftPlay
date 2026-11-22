# SwiftPlay Engineering Changelog (`.agents/CHANGELOG.md`)

Chronological log of major milestones and design changes for AI context continuity.

---

## Format for New Entries
```markdown
## YYYY-MM-DD — <Short Summary> (<commit or PR if committed>)
- **Files:** ...
- **Changes / Decisions:** ...
- **Verified:** ...
```

---

## 2026-11-21 — Documentación IA, Monitor RAM Dual, Sonidos Discord y Overlay Total
- **Project Documentation & AI Workspace Context (`README.md`, `AGENTS.md`, `.agents/ARCHITECTURE.md`, `.agents/CHANGELOG.md`):**
  - Expanded user README with full module overview, DPAPI/`%LOCALAPPDATA%\SwiftPlay` privacy isolation, Discord bot setup, and updated repository tree.
  - Added `AGENTS.md` workspace guardrails (import isolation, DPAPI isolation, no Shorts, Win32 modal/ghost-window/QSS rules, PowerShell 5.1 protocol) plus `.agents/` deep architecture reference & changelog.
- **Native Resource Monitor & Compact Sidebar Fix (`themes.py`, `main_window.py`, `verify_swiftplay.py`):**
  - Switched `resource_card` to display live active physical RAM (Windows Working Set) for both the GUI (`RAM APP`) and total `GUI + Discord Worker` (`TOTAL (+DS)`), updating immediately on Discord worker RAM events while preserving `Priv` commit values in tooltip & `ram_log.txt`.
  - Wrapped the sidebar navigation section in a transparent `QScrollArea#SidebarScroll` (`SidebarNavContainer`), pinned `resource_card` and `discord_mini_card` with `Fixed` vertical `QSizePolicy`, `SetMinimumSize`, and `15px` label minimum heights so resource metrics never squash or clip at `860x560` non-fullscreen sizes.
- **Custom Discord Sounds & 3-Mode Overlay (`sound_effects.py`, `config.py`, `discord_overlay.py`, `discord_panel.py`, `settings_dialog.py`, `main_window.py`):**
  - Added pure-stdlib WAV PCM synthesizer & async `winmm.PlaySoundW` (`SND_MEMORY | SND_ASYNC`) playback (`sound_effects.py`, 0 MB extra deps — replacing `winsound` which rejects `SND_MEMORY | SND_ASYNC`) for 9 events (`mute_on`, `mute_off`, `deafen_on`, `deafen_off`, `message`, `self_join`, `self_leave`, `user_join`, `user_leave`) with selectable `soft` / `minimal` styles and per-event `0–100%` volume; suppresses incoming `message` / `user_join` / `user_leave` cues while deafened.
  - Replaced `SettingsDialog` overlay checkbox with a 3-mode selector (`off` / `status` / `full`) and added a collapsed-by-default `Sonidos de Discord` accordion containing master toggle, style selector, 9 individual sliders + ▶ test buttons, and reset.
  - Upgraded `DiscordStatusOverlay` for compact `"full"` mode (omits redundant standalone status box, uses `"Voz 1"` / `"Voz 2"` preview rows, limits display names to 10 chars, uses subtle pill backdrop with floating circular avatars, live green speaking rings from `speaking_updated`, and inline mute/deafen icons immediately right of each name).
- **Verified:** `verify_swiftplay.py` `[1/6]`–`[6/6]` passed (`Command exited with code 0`).

---

## 2026-11-21 — Discord UX Polish, Persistent DMs, Live Monitor & YouTube Feed Overhaul (`ef58026`)
- **Discord UI & Surfaces (`themes.py`, `discord_panel.py`):**
  - Removed 4 stray QSS lines after `QFrame#DiscordVoiceHeader` that aborted Qt QSS parsing for Discord cards; restored distinct card/background/border tones (`#DiscordSidebarCard`, `#DiscordVoiceArea`, `#DiscordChatCard`, `#DiscordChatHeader`, `#DiscordDeckCard`) and visible `center_splitter` divider handle.
  - Added collapsible `close_*` (`IconBtn` close) controls to Channels, Voice (`DiscordVoiceHeader`), Chat (`center_collapsed_card` with reopen buttons), Members, and Intents banner.
  - Added persistent hidden channels (`discord_hidden_channels`, `discord_hidden_channel_names` in `config.py`) via right-click context menu and Settings manager card (`settings_dialog.py`).
  - Added selective per-channel taskbar flash alerts (`flash_window_taskbar` without stealing focus), dual-icon (`both` `86x46`) top-left status overlay (`discord_overlay.py`), and profile-avatar card tinting.
- **Persistent DMs & Always-Visible Sidebar Bot Card (`storage.py`, `discord_worker.py`, `discord_service.py`, `discord_panel.py`, `main_window.py`):**
  - Added `discord_dm_threads` and `discord_dm_messages` SQLite tables in `LocalLibrary` plus collapsible `MENSAJES DIRECTOS` section in Discord's left column.
  - Centralized `exec_user_dm_menu` to allow right-click -> DM from voice participant cards, voice-channel member rows, and the members list.
  - Propagated `bot_avatar_url` from `discord_worker.py` and added `discord_mini_card` below `resource_card` in `MainWindow` with bot avatar, name, connection/voice state, Mute, and Deafen buttons usable from YouTube, Music, or Twitch.
- **Global QSS Normalization, Click-Outside Modals & Native Resource Monitor (`themes.py`, `windows_os.py`, `shortcuts_dialog.py`, `settings_dialog.py`, `video_overlays.py`, `mini_bar.py`, `main_window.py`):**
  - Fixed black-on-gray button contrast by standardizing `#F1F3F6` light text on `#AccentBtn`, `#PillBtn[active="true"]`, checked buttons, combos, and menus; fixed 90° border-radius clipping on `PillBtn` (`10px`), `PlayAccentBtn` (`16px` / `>=34x34`), `NavBtn[active="true"]` (removed `border-left: 3px`), and `QTabBar::tab`.
  - Added `SystemResourceMonitor` (`GetProcessTimes`/`GetSystemTimes` + `pdh.dll` GPU Engine) backing compact `resource_card` (`RAM`, `CPU`, `GPU`) below `Ajustes`.
  - Added `OutsideClickDismissFilter` + `MainWindow.nativeEvent` (`WM_SETCURSOR` + `HTERROR`) so `SettingsDialog` and `ShortcutsDialog` close when clicking outside without Windows modal beeps.
  - Added top-bar `close_media_btn` (`Cerrar video` / `Cerrar directo` / `Cerrar música`) next to the search bar to stop/unload active playback and trim memory.
- **YouTube Feed Fixes (`youtube_service.py`, `storage.py`, `widgets.py`, `main_window.py`):**
  - Fixed `lockupViewModel` duration & `EN VIVO` badge parsing via `thumbnailBottomOverlayViewModel.badges`.
  - Added SQLite `list_watched_video_ids()`, YouTube watched-bar (`startPercent >= 80`) filtering, 4-page InnerTube `richGridRenderer` continuation pagination (limit `36`), and unseen rotation when clicking Refresh.
- **Verified:** `verify_swiftplay.py` `[1/6]`–`[6/6]` passed (`Command exited with code 0`).
