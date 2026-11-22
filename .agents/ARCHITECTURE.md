# SwiftPlay Technical Architecture (`.agents/ARCHITECTURE.md`)

Reference map for AI coding agents working in `c:\Users\benja\Desktop\trabajos\Swift`.

---

## 1. Runtime & Process Topology

SwiftPlay is engineered around strict **RAM minimization** and **crash/dependency isolation**:

1. **GUI Process (`pythonw launch_swiftplay.pyw` -> `swiftplay.main:main`)**
   - Single-instance lock via `QLocalServer("SwiftPlaySingleInstanceIPC")`. Re-launching focuses the existing window (`SHOW`); `--quit` triggers `quit_app()`.
   - Hosts PySide6 UI (`MainWindow`), `ThumbnailCache`, SQLite (`LocalLibrary`), direct lightweight HTTP calls (InnerTube JSON + Twitch GQL JSON), and two lazy `MediaPlayer` instances wrapping `libmpv-2.dll`.
   - Must NEVER import `yt_dlp`, `discord`, `davey`, `sounddevice`, `pywebrtc_audio`, `deepfilternet`, or `pysilero_vad`. Checked dynamically by `scripts/verify_swiftplay.py`.

2. **yt-dlp Subprocess (`swiftplay.ytdlp_worker` via `YtdlpWorkerClient`)**
   - Spawned on demand (`CREATE_NO_WINDOW`) over JSON-lines stdio when stream resolution fallback or downloads run.
   - Auto-terminates after `_IDLE_TIMEOUT_SEC = 45.0` or `_MAX_WORKER_REQUESTS = 15` requests to shed Python/extractor RAM.

3. **Discord Voice & Gateway Subprocess (`swiftplay.discord_worker` via `DiscordService`)**
   - Spawned only when the user clicks **Conectar Bot**; consumes **0 MB** beforehand and upon disconnect/kill.
   - Runs asyncio `discord.py[voice]` Gateway client, DAVE E2EE voice transport (`davey`), RTP/AEAD audio mixer (`VoiceAudioMixer`), `sounddevice` input/output audio streams (`AudioEngine`), noise suppression (`ai_max` DeepFilterNet3 + Silero VAD v6, `ai` DeepFilterNet3, `webrtc`, `off`), AGC, per-user volume (`0–200%`), guild/channel/member snapshot emission, and DM/channel chat IPC.

4. **Dual Lazy `libmpv` Players (`swiftplay.player:MediaPlayer`)**
   - `MainWindow.video_player`: bound to `VideoPlayerPanel.video_surface.winId()`, hardware/GPU-capable, unloads automatically after `20s` paused (`_video_idle_unload_timer`) or on `close_media_btn`.
   - `MainWindow.music_player`: headless audio-only (`wid=None`), unloads after `45s` paused (`_music_idle_unload_timer`) or on `close_media_btn`.
   - In "Modo A" (`auto_pause_other_player=True`), starting one deck pauses the other; `Tab` rotates pages: YouTube -> YT Music -> Twitch -> Discord.

---

## 2. Complete Module Inventory

### Root & Scripts
- `Actualizar_Ruta.bat`: Closes running SwiftPlay (`--quit`), runs `git pull --ff-only` when tree is clean, runs `uv sync --python 3.12`, runs `scripts/setup_binaries.py` & `scripts/install_windows_integration.py`, and launches SwiftPlay.
- `launch_swiftplay.pyw`: No-console entrypoint redirecting `None` stdio to `os.devnull`, writing fatal startup errors to `%LOCALAPPDATA%\SwiftPlay\crash.log` + Win32 `MessageBoxW`.
- `scripts/setup_binaries.py`: Downloads missing native DLL/executables (`libmpv-2.dll`, `ffmpeg.exe`, `ffprobe.exe`) into `bin/`.
- `scripts/install_windows_integration.py`: Creates `.lnk` shortcuts (Desktop + Start Menu) and optional Windows startup registry entry.
- `scripts/generate_icon.py`: Generates `assets/swiftplay.png` and `assets/swiftplay.ico`.
- `scripts/verify_swiftplay.py`: 6-stage automated integration + GUI + live-network + worker suite (`[1/6]`–`[6/6]`).

### Core (`src/swiftplay/`)
- `main.py`: Single-instance IPC (`QLocalServer`), PassThrough High-DPI, `SetCurrentProcessExplicitAppUserModelID`, clean save on shutdown.
- `paths.py`: Resolves `APP_ROOT`, `BIN_DIR`, `ASSETS_DIR`, `LOGS_DIR`, and `%LOCALAPPDATA%\SwiftPlay\*`.
- `models.py`: Dataclasses (`MediaItem`, `ResolvedStream`, `FeedSection`, `AccountProfile`) and duration formatting/parsing helpers.
- `config.py`: `AppConfig` dataclass persisted atomically to `%LOCALAPPDATA%\SwiftPlay\config.json`:
  - Core playback: `theme` (`"matte_silver"`), `audio_normalization`, `auto_pause_other_player`, `video_quality`, `video_speed`, `volume`, `video_volume`, `music_volume`, `video_audio_only`, `autoplay_music`, `minimize_to_tray`, `start_with_windows`, `music_download_dir`, `video_download_dir`, `mini_bar_always_on_top`.
  - Discord audio & hotkeys: `discord_input_device`, `discord_output_device`, `discord_output_volume`, `discord_mic_gain`, `discord_vad_threshold`, `discord_mic_agc`, `discord_noise_suppression` (`"off"|"webrtc"|"ai"|"ai_max"`), `discord_hotkey_mute`, `discord_hotkey_deafen`, `discord_hotkey_hangup`, `discord_hotkey_mute_priority`.
  - Discord UI & notifications: `discord_show_locked_channels`, `discord_overlay_enabled`, `discord_overlay_mode` (`"off"|"status"|"full"`), `discord_overlay_opacity` (`10–100`), `discord_notify_chat_enabled`, `discord_notify_color` (`"purple"|"indigo"|"blue"|"pink"|"emerald"|"amber"|"silver"`, default `"purple"`), `discord_notify_channels`, `discord_hidden_channels`, `discord_hidden_channel_names`, `discord_sounds_enabled`, `discord_sound_pack` (`"soft"|"minimal"`), `discord_sound_volumes` (per-event `0–100` dict for 9 events).
- `security.py`: Windows DPAPI (`CryptProtectData`/`CryptUnprotectData`) encryption/decryption for YouTube cookies (`session.dpapi`, domain-filtered) and Discord Bot Token (`discord_token.dpapi`), plus `account_profile.json`.
- `storage.py`: Thread-safe `LocalLibrary` SQLite (WAL mode) at `%LOCALAPPDATA%\SwiftPlay\library.db`:
  - `my_music`: saved YT Music tracks (`video_id PK`, `title`, `artist`, `duration`, `duration_sec`, `thumb_url`, `added_at`).
  - `watch_later`: saved YouTube videos.
  - `search_history`: last 15 searches per mode (`video`, `music`, `twitch`).
  - `play_history`: up to 100 entries per `kind` with `last_position` resume timestamp (`list_watched_video_ids()` powers home feed watched filtering).
  - `twitch_favorites`: starred Twitch channel logins.
  - `discord_dm_threads`: persistent DM list (`user_id PK`, `user_name`, `avatar_url`, `last_preview`, `updated_at`).
  - `discord_dm_messages`: up to 100 cached messages per `user_id` (`msg_key PK`, `user_id`, `author_id`, `author_name`, `is_self`, `is_bot`, `content`, `timestamp`, `date_label`, `created_at`).
- `session_state.py`: Saves/restores active video item+position and music item+queue (`<= 15`) in `session_state.json`.
- `browser_auth.py`: Launches temporary Chrome/Edge profile with CDP websocket to capture Google/YouTube login cookies cleanly into DPAPI.
- `youtube_service.py`:
  - InnerTube `WEB` client (`_extract_innertube_videos`, `_extract_feed_continuation_token`): parses both legacy `videoRenderer` and current `lockupViewModel` (extracting durations & `LIVE` badges from `thumbnailBottomOverlayViewModel.badges`, `thumbnailOverlayBadgeViewModel.thumbnailBadges`, and detecting watched bars `startPercent >= 80`).
  - `get_video_home_feed(limit=36, ...)`: paginates up to 4 InnerTube `browse` pages, excludes `watched_ids` + `recent_shown_ids`, persists `_feed_continuation` so clicking Refresh fetches fresh unseen recommendations.
  - Lazy `YTMusic` (dual auth + unauthenticated search client), radio queue generation, and LRU `OrderedDict` resolved-stream cache (`_stream_cache`) + in-flight deduplication (`_inflight_streams`).
- `twitch_service.py`: Anonymous Twitch GQL + Usher HLS (`m3u8`) resolver (`get_top_streams` in ES/ALL, search, live stream metadata, `extract_channel_login`, `format_viewers`) with zero chat/login overhead.
- `media_keys.py`: `GlobalMediaKeyFilter` (`QAbstractNativeEventFilter`) handling Win32 `WM_HOTKEY` and optional `WH_KEYBOARD_LL` low-level hook for priority Discord mute hotkey inside games/fullscreen apps.
- `windows_os.py`:
  - Startup registry integration, `.lnk` AppUserModelID properties, unified `pythonw.exe` worker resolution (`resolve_worker_python_and_env`), Win32 Job Object lifecycle binding (`assign_process_to_child_job` with `KILL_ON_JOB_CLOSE`), `get_process_memory_mb`, `trim_process_memory`, `bring_window_to_front`, `flash_window_taskbar` (`FLASHW_TRAY | FLASHW_TIMERNOFG`, non-stealing!).
  - `SystemResourceMonitor` + `ResourceSnapshot`: live Win32 `GetProcessTimes`/`GetSystemTimes` CPU% and native `pdh.dll` `\GPU Engine(*)\Utilization Percentage` GPU% (24-byte `_PDH_FMT_COUNTERVALUE_ITEM_W` on 64-bit).
  - `is_win32_disabled_parent_click`: detects `WM_SETCURSOR` (`0x0020`) + `HTERROR` (`0xFFFE`) mouse clicks on disabled parent `MainWindow` during modal `exec()`.
- `thumb_cache.py`: `ThumbnailCache` (`QThreadPool` + `QPixmap` LRU cache supporting rounded corners and circular avatars).
- `sound_effects.py`: Pure-stdlib in-memory 16-bit PCM WAV synthesizer + Win32 `winmm.PlaySoundW` (`SND_MEMORY | SND_ASYNC`) player (`SoundEffectPlayer`, `SOUND_EVENTS`, `SOUND_PACKS`, `synthesize_sound_wav`) supporting `"soft"` and `"minimal"` packs and per-event volume (`0–100`).
- `ram_logger.py`: Writes memory checkpoints to `logs/ram_log.txt`.
- `themes.py`: `THEMES["matte_silver"]`, `NOTIFICATION_COLORS` (7 curated notification accent presets, defaulting to `"purple"`), `build_stylesheet(theme_key, notify_color=...)`, and DWM dark titlebar helper.

### UI (`src/swiftplay/ui/`)
- `main_window.py`: Orchestrates scrollable sidebar navigation (`SidebarScroll` + `SidebarNavContainer`), pinned non-compressible bottom cards (`resource_card` showing active physical `RAM APP` + `TOTAL (+DS)` Working Set + `CPU%`/`GPU%` and always-visible `discord_mini_card`), top search bar (with contextual `close_media_btn`), stacked pages (`0..9`), smart bottom bar, tray icon, global hotkeys, outside-modal `nativeEvent`, and clean teardown (`_cleanup_before_exit`).
- `discord_panel.py`: Discord workspace with collapsible `left_card` (Server selector, **CANALES DE VOZ**, **CANALES DE TEXTO** with selective notification bells & unread `UnreadBadge` counters, **MENSAJES DIRECTOS** with unread `UnreadBadge` counters & header badge), collapsible `voice_pane` (`_ParticipantCard` grid with profile-derived color tinting), draggable splitter handle, collapsible `chat_pane` (`QTextBrowser` + input), bottom audio deck, and toggleable right `members_card` (`Canal`/`Servidor` scope). Right-click context menus allow hiding any channel permanently (`discord_hidden_channels`) or starting persistent DMs (`exec_user_dm_menu`).
- `discord_overlay.py`: Frameless, click-through (`WindowTransparentForInput`), non-focus-stealing (`WindowDoesNotAcceptFocus`), top-left always-on-top overlay supporting 3 modes (`off`, `status`: `46x46`/`86x46` mute/deafen square, `full`: compact Discord-style participant list without redundant status square, names capped at 10 chars via `format_overlay_name`, subtle translucent pill backdrop, circular avatars, live `#4ADE80` speaking rings, inline mute/deafen icons right of the name, and `"Voz 1"` / `"Voz 2"` preview rows) with configurable opacity (`10–100%`).
- `discord_notification.py`: `DiscordMessageToast` frameless bottom-right desktop notification card matching Discord toast layout in SwiftPlay dark-graphite palette with selected notification color accent, circular 48x48 sender avatar, non-focus-stealing display, and click-to-open chat routing.
- `video_surface.py` & `video_overlays.py`: Embedded video/live controls (`34x34` circular `PlayAccentBtn`, quality, speed, `Solo Audio`, watch later/favorite, clickable `● EN VIVO` badge + `Ir al vivo` live-edge sync pill for YouTube Live & Twitch) and fullscreen floating overlay + volume/action HUDs.
- `music_player_panel.py` & `mini_bar.py`: Music stage with cover art / radio toggle and floating compact mini-bar.
- `widgets.py`: `RoundedThumbWidget` (hover zoom, crossfade, red `EN VIVO` badge, resume bar), `VideoGridCard`, `VideoCardWidget`, `MusicRowWidget`, `MusicShelfWidget`, `ContinueWatchingBanner`, `ResponsiveGridWidget`, `AnimatedToastWidget`.
- `settings_dialog.py`: 5-tab settings window (Playback/Downloads, YouTube Account DPAPI, System/RAM/yt-dlp update, Discord DPAPI / 3-mode overlay selector / collapsible 9-sound accordion / hidden channels manager, Global Discord Hotkeys).
- `shortcuts_dialog.py`: Frameless shortcuts cheat-sheet (`ShortcutsDialog`) + shared `OutsideClickDismissFilter`.
- `icons.py`: Cached programmatic SVG vector icon renderer (`render_icon`).
