# AGENTS.md — SwiftPlay Workspace Guide

SwiftPlay is a lightweight native Windows desktop client (PySide6 + dual lazy `libmpv`) for **YouTube**, **YouTube Music**, **Twitch** (guest live streams without chat), and **Discord** (isolated E2EE DAVE voice + text + persistent DMs).

Read [.agents/ARCHITECTURE.md](.agents/ARCHITECTURE.md) for deep module internals, IPC contracts, and database schemas. When completing notable architecture or feature changes, add a dated entry to [.agents/CHANGELOG.md](.agents/CHANGELOG.md) and update documentation if behavior/paths changed.

## Non-Negotiable Architectural Invariants

1. **Process & Memory Isolation (Strict Import Boundary):**
   - Heavy libraries (`yt_dlp`, `discord`, `davey`, `sounddevice`, `pywebrtc-audio`, `deepfilternet-slim`, `pysilero-vad`) MUST NEVER be imported in the PySide6 GUI process.
   - `ytdlp_worker.py` runs isolated via `YtdlpWorkerClient` and auto-terminates after idle timeout (`45s`) or request cycles (`15`).
   - `discord_worker.py` runs isolated via `DiscordService` JSON-lines IPC (`stdin`/`stdout`), using **0 MB** when disconnected.
   - Dual `MediaPlayer` instances in `player.py` initialize `libmpv` lazily (`lazy=True`) and unload cleanly when closed or idle.

2. **User Secrets & Local Data Isolation:**
   - NEVER store credentials, cookies, tokens, logs, `.db`, `.dpapi`, or `config.json` inside the repository root or commit them.
   - All user state lives outside the repo in `%LOCALAPPDATA%\SwiftPlay` (see `src/swiftplay/paths.py`): `session.dpapi`, `discord_token.dpapi` (Windows DPAPI), `config.json`, `library.db`, `session_state.json`, `account_profile.json`.

3. **No Shorts in YouTube Feeds:**
   - YouTube home feed and searches enforce `block_shorts=True` (`is_short_video` excludes `/shorts/`, `#shorts`, and non-live videos `<= 60s`). Live streams (`is_live=True`) bypass the duration check.

## Critical Windows, Qt6 & Shell Guardrails

- **Windows PowerShell 5.1 Only:** NEVER chain shell commands with `&&` (fails in PowerShell 5.1). Always use `; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE };`. Run project commands with `.\.venv\Scripts\python.exe` or `uv run`.
- **QSS Syntax & `border-radius` Clipping:**
  - Qt aborts parsing the remainder of a QSS stylesheet on a single stray `}` or syntax error. Always keep balanced `{` / `}` in `src/swiftplay/themes.py`.
  - Qt Fusion style drops `border-radius` to sharp 90° corners if asymmetric borders (`border-left: 3px`) or oversized radii (`border-radius * 2 > min(width, height)`) are used. Keep uniform borders and `border-radius` strictly `<= min_side / 2` (e.g., `PillBtn` `10px`, `PlayAccentBtn` `16px` with `34x34`+ buttons).
  - Never revert dark-surface buttons to dark `#0B0C0E` text on gray backgrounds; keep light `#F1F3F6` (`p.text`) everywhere except the icon-only silver `PlayAccentBtn`.
- **No Qt Ghost Top-Level Windows:**
  - When clearing widgets from layouts (`_clear_layout` in `discord_panel.py` / `main_window.py`), ALWAYS `hide()` + `setParent(None)` + `deleteLater()`. Simply removing or reparenting to `None` before hiding flashes orphan frameless windows on Windows.
- **Modal Dialogs (`SettingsDialog`, `ShortcutsDialog`):**
  - Keep `exec()` and `QApplication.activeModalWidget()` compatibility (required by `scripts/verify_swiftplay.py` leak checks).
  - Outside-click dismissal uses `OutsideClickDismissFilter` (with popup/child-modal/OAuth login guards) plus `MainWindow.nativeEvent` handling `WM_SETCURSOR` (`0x0020`) with `HTERROR` (`0xFFFE`).
- **Async Memory Audio (`sound_effects.py`):**
  - Never use CPython `winsound.PlaySound` with `SND_MEMORY | SND_ASYNC` (raises `RuntimeError: Cannot play asynchronously from memory`). Always use `ctypes.windll.winmm.PlaySoundW` (`0x0004 | 0x0001 | 0x0002`) holding a live `ctypes` buffer reference under lock.

## Quick Codebase Map

- **Entry & Infra:** `launch_swiftplay.pyw`, `src/swiftplay/main.py`, `src/swiftplay/paths.py`, `src/swiftplay/config.py`, `src/swiftplay/models.py`, `src/swiftplay/security.py`, `src/swiftplay/storage.py`, `src/swiftplay/windows_os.py`
- **Services & Workers:** `src/swiftplay/youtube_service.py`, `src/swiftplay/ytdlp_worker.py`, `src/swiftplay/twitch_service.py`, `src/swiftplay/discord_service.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/player.py`, `src/swiftplay/browser_auth.py`, `src/swiftplay/media_keys.py`, `src/swiftplay/sound_effects.py`
- **UI & Theme:** `src/swiftplay/themes.py`, `src/swiftplay/thumb_cache.py`, `src/swiftplay/ui/main_window.py`, `src/swiftplay/ui/discord_panel.py`, `src/swiftplay/ui/discord_overlay.py`, `src/swiftplay/ui/video_surface.py`, `src/swiftplay/ui/video_overlays.py`, `src/swiftplay/ui/music_player_panel.py`, `src/swiftplay/ui/mini_bar.py`, `src/swiftplay/ui/widgets.py`, `src/swiftplay/ui/settings_dialog.py`, `src/swiftplay/ui/shortcuts_dialog.py`, `src/swiftplay/ui/icons.py`

## Verification Protocol

After Python edits, always compile and run the full automated verification suite (`[1/6]`–`[6/6]`):

```powershell
.\.venv\Scripts\python.exe -m py_compile (Get-ChildItem src\swiftplay\*.py, src\swiftplay\ui\*.py).FullName scripts\verify_swiftplay.py; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; .\.venv\Scripts\python.exe scripts\verify_swiftplay.py
```

Wait for explicit user confirmation before running `git commit` or `git push`.
