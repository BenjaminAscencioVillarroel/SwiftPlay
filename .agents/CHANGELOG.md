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

## 2026-11-23 — Fix Timeout de Llamada (Sesión Huérfana + Reintento Rápido), "Probar micrófono" Local y Estética Popover Audio
- **Files:** `src/swiftplay/discord_service.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/themes.py`, `src/swiftplay/ui/discord_panel.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - **Fix Timeout / Sesión Huérfana al Entrar en Voz (`discord_service.py`, `discord_worker.py`):** `disconnect_and_kill()` espera el cierre limpio del worker (`1.5s`) antes de cerrar sus pipes para que desconecte del canal sin dejar sesión fantasma; `on_ready()` precalienta DF3/Silero y limpia estados de voz huérfanos en el servidor; `_join_channel()` limpia cualquier sesión fantasma previa antes de conectar, ya no bloquea el connect con `AudioEngine.start()` ni con `await` previo, y ejecuta conexión rápida con reintento automático (`7s`/`9s` + `8s`/`10s`) guardando el estado/fase en `_connecting_vc` + `logs/discord_voice_diag.log`.
  - **Prueba Local de Micrófono / Loopback (`discord_worker.py`, `discord_service.py`, `discord_panel.py`):** Nuevo botón **"Probar micrófono"** en el popover de Audio que silencia y ensordece al bot en Discord (`muted=True, deafened=True`), enruta la captura procesada por el filtro/ganancia/VAD al dispositivo de salida en loopback local sin transmitir a Discord, y restaura automáticamente los estados previos al desactivar o al usar los botones normales.
  - **Estética Popover Audio (`themes.py`, `discord_panel.py`):** `audio_advanced_box` usa `TransparentBox` junto a reglas transparentes para `QFrame#DiscordDeckCard` (eliminando el rectángulo negro interno) y ensancha `audio_popup` a `404 px` evitando recortes de texto.
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

## 2026-11-23 — Fix Supresión IA (Re-warmup), Clic Canal → Voz, UDP Discovery sin Carrera y Diagnóstico de Fases
- **Files:** `src/swiftplay/discord_worker.py`, `src/swiftplay/ui/discord_panel.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - **Restauración de Supresión IA (`discord_worker.py`):** `_DeepFilterMicDsp` ahora expone `_ready` real tras completar buffers y Silero VAD, incorpora `start_warmup_async()` idempotente, reinicia `_failed` al reactivar `ai`/`ai_max`, y `MicProcessor.process_frame()` / `AudioEngine.configure()` auto-lanzan el warmup en segundo plano si estaba descargado o si se cambió de modo (`off -> ai`), eliminando el flag bloqueante `_warmup_started`.
  - **Redirección al Panel Voz desde Canal (`discord_panel.py`):** Al pulsar el canal de voz activo o conectando mientras se visualiza un chat o DM, `_build_voice_channel_row()` ahora cambia automáticamente la vista central al panel de Voz (`_set_pane_visible("voice", True)`).
  - **Conexión de Voz Sin Bloqueos (`discord_worker.py`):** `_SwiftVoiceConnectionState` corrige la carrera UDP en `discover_ip()` (registra el listener y reanuda `SocketReader` antes de enviar con reintentos cada `0.35 s`), acota reintentos/desconexión y registra tiempos exactos por fase (`op4`, `ws`, `udp`, `key`, `audio`) en `connect_diag` + `logs/discord_voice_diag.log`, desacoplando `AudioEngine.start()` y `kickstart_voice()` en `_finish_post_join()`.
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

## 2026-11-23 — Conexión Rápida Discord (IPv4 + Audio Paralelo + Desconexión Acotada) y Tarjetas de Voz Centradas Estilo Discord
- **Files:** `src/swiftplay/discord_service.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/ui/discord_panel.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - **Eliminación de Demora al Conectar Bot/Voz (`discord_service.py`, `discord_worker.py`, `discord_panel.py`):** Envío inmediato de `connect_bot` antes de `list_devices`, conector `aiohttp.TCPConnector(limit=0, family=socket.AF_INET)` en `discord.Client` para evitar cuelgues TCP SYN por IPv6 en Windows, enumeración/apertura/cierre de PortAudio fuera del loop asyncio (`asyncio.to_thread`), arranque paralelo de `AudioEngine.start()` y `channel.connect()`, filtro de dispositivos `WDM-KS` cuando existen WASAPI/MME, `reset()` sin bloqueo en `_DeepFilterMicDsp` durante warmup y fallback inmediato WebRTC en modo IA, timeout de `2.5 s` en `vc.disconnect(force=True)`, límite de `0.6 s` en espera DAVE de `kickstart_voice()` y diagnóstico de tiempos (`connect_diag`) visible en `info_pill`.
  - **Tarjetas de Participantes Centradas Estilo Discord (`discord_panel.py`):** Distribución centrada vertical y horizontalmente en filas balanceadas (`_compute_voice_row_sizes`: `1`, `2`, `2+1`, `2+2`, `3+2`, `3+3`, `4+3`, `4+4`, `3+3+3`) con tamaños adaptativos (`_compute_participant_card_size`), estiramiento interno centrado en `_ParticipantCard` y relayout sin recrear tarjetas ni ventanas fantasma.
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

## 2026-11-23 — Activación Inmediata de Voz Discord, Auto-Scroll/Presencia/CSS Grafito, Scroll Infinito (120), Canales YouTube y Transiciones Fluidas
- **Files:** `src/swiftplay/models.py`, `src/swiftplay/themes.py`, `src/swiftplay/discord_service.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/youtube_service.py`, `src/swiftplay/ui/widgets.py`, `src/swiftplay/ui/video_surface.py`, `src/swiftplay/ui/discord_panel.py`, `src/swiftplay/ui/main_window.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - **Discord Voice Instant Activation (`discord_worker.py`):** Eliminado el bloqueo de ~1.6 s del warmup de DeepFilterNet3/Silero VAD en `_mic_sender_loop()` usando `_DeepFilterMicDsp.is_ready` con fallback instantáneo WebRTC durante el arranque; apertura anticipada de `AudioEngine`, conexión previa de callbacks `on_opus`/`on_dave_change` vía `_wire_voice_client()`, reanudación forzada de `SocketReader`, tracking `OP 11` (`CLIENTS_CONNECT`) y resolución/passthrough DAVE para SSRCs ya presentes en sala, y envío de `OPUS_SILENCE` cifrado solo con capa de transporte sin DAVE `encrypt_opus`.
  - **Discord Auto-Scroll, CSS Grafito, Vista Exclusiva Voz/Chat y Presencia:** `_ChatMessagesView` mantiene el foco abajo al abrir o recibir/enviar mensajes mediante `rangeChanged`, `_stick_to_bottom` e inserción incremental `append_message()`; el panel central usa `center_stack` exclusivo (`Voz` o `Chat`), tema neutro Grafito Obsidiana sin tintes azulados e indicadores de presencia (`online`, `idle`, `dnd`, `offline`) en DMs, cabecera, canales de voz y miembros.
  - **Infinite Scroll (120) y Limpieza RAM (`main_window.py`, `youtube_service.py`, `widgets.py`):** Lotes iniciales ligeros en YouTube y YT Music con carga automática al avanzar (`MAX_FEED_ITEMS = 120`), descarga de miniaturas fuera de pantalla (`set_offscreen_unloaded`) y limpieza completa de caché/RAM al pulsar recargar.
  - **YouTube Channels (`PAGE_CHANNEL`, `youtube_service.py`, `video_surface.py`):** Vista de canal accesible desde tarjetas y reproductor con pestañas exclusivas `Videos` y `Directos` (sin Shorts ni playlists) y scroll infinito propio.
  - **Smooth Animations:** Transiciones `QVariantAnimation` ligeras en páginas no-video de `MainWindow` (`140 ms`, excluyendo siempre `PAGE_VIDEO_PLAYER`), `center_stack` de Discord (`130 ms`) y `_ImageLightboxOverlay` (`150 ms`) sin fugas de `QPropertyAnimation`.
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

## 2026-11-22 — Chat Nativo Agrupado Estilo Discord, Voz Clic-Directo, Fix Superposición Feed y Video Azul D3D11
- **Files:** `src/swiftplay/ui/icons.py`, `src/swiftplay/ui/discord_panel.py`, `src/swiftplay/ui/widgets.py`, `src/swiftplay/player.py`, `src/swiftplay/ui/video_surface.py`, `src/swiftplay/ui/main_window.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/storage.py`, `src/swiftplay/themes.py`, `scripts/verify_swiftplay.py`
- **Voice Click-to-Join:** `_VoiceChannelRowFrame` reemplaza el botón redundante `"Entrar"` en canales de voz; clic izquierdo directo sobre la fila conecta al canal (sin botones `"Salir"` / `"Entrar"` / `"Bloq."`), conservando `"En Voz"`, `"Conectando..."` o `"Bloq."` como etiquetas limpias.
- **Native Discord Chat UI & Grouping:** `_ChatMessagesView` / `_ChatMessageRow` / `_ChatHoverToolbar` reemplazan `QTextBrowser` HTML: avatar circular izquierdo, cabecera superior (nombre + `APP` + hora), agrupación de mensajes consecutivos del mismo autor (`_should_group_message`, <= 5 min sin `reply_to` con hora solo en hover), resaltado de fila y barra flotante única superior derecha (`reply`, `edit` propio del Bot, `copy`, `paste`), además de `created_ts` serializado desde `discord_worker.py` y `storage.py`.
- **Feed Card Spacing Fix:** `VideoGridCard.set_tile_width()` recalcula la altura real tras ajustar la miniatura (`max(h + 100, lay.sizeHint().height())`) y `ElidedLabel.paintEvent()` alinea el texto desde arriba (`y0 = 0`), evitando que título y avatar invadan el borde inferior de la miniatura.
- **Solid-Blue Video Fix:** `MediaPlayer` desactiva `d3d11_flip` y `gpu_dumb_mode` usando `hwdec="d3d11va-copy"`, agrega `refresh_video_output()` y `VideoSurface.force_native_redraw()` fuerza `WM_SIZE` / `RedrawWindow` sobre el HWND nativo y su ventana hija al abrir o volver al reproductor.

## 2026-11-22 — Lightbox In-Window de Chat, Responder/Copiar/Editar/Drag&Drop en Discord, Colgar junto a Ensordecer, Notis Siempre Activas Fuera de Chat y Cambio de Pestañas Instantáneo
- **Files:** `src/swiftplay/storage.py`, `src/swiftplay/themes.py`, `src/swiftplay/discord_service.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/ui/discord_panel.py`, `src/swiftplay/ui/main_window.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - **Discord Chat Media Lightbox & Safe Selection:** Set `chat_browser.setOpenLinks(False)` (preventing `QTextBrowser` document blanking when clicking image thumbnails), replaced `_ChatImagePreviewDialog` with the in-window dark translucent `_ImageLightboxOverlay` (scales oversized images automatically with `KeepAspectRatio`, closes on `Esc` or outside backdrop click), and removed all external browser/Chrome `Abrir` links.
  - **Selectable Chat Text, Context Menu, Reply & Edit + Drag-and-Drop:** Switched message rendering to clean `<div>` blocks tracking `_chat_msg_ranges`, added highlighted `QTextBrowser` selection CSS, right-click menu (`Copiar texto seleccionado`, `Copiar mensaje`, `Responder`, `Editar mensaje`, `Copiar imagen`, `Guardar imagen como...`) plus subtle inline message links, `discord_worker` support for `reply_to` (`discord.MessageReference`) and `edit_chat_message` / `on_raw_message_edit` on Bot own messages, and drag-and-drop file/image attachment support across the chat pane/input.
  - **Voice Hangup & Notification Accuracy:** Removed the `"Salir"` button from voice channel rows in favor of `MainWindow.discord_mini_hangup_btn` (`phone_off` icon placed to the right of `discord_mini_deaf_btn`). Fixed `discord_worker.on_message` (`elif` -> `if`) and GUI focus/pane guards (`_is_window_actively_focused`, `is_viewing_channel`, `is_viewing_dm`, `WindowActivate`) so DM/channel alerts and unread badges trigger whenever the chat pane is closed or SwiftPlay is unfocused.
  - **Instant Main Tab Switching:** Removed full-page `QGraphicsOpacityEffect` reveal transitions, stopped destroying hidden browse grids/lists on `_switch_page` by tracking dirty flags with batched SQLite ID-set lookups (`list_watch_later_ids`, `list_my_music_ids`, `list_twitch_favorite_ids`) and a 90s Twitch feed cache (`_twitch_feed_cache`), while releasing hidden browse pages cleanly on `free_memory_now()` and minimize-to-tray.
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

---

## 2026-11-22 — Barra Inferior Contextual, Sidebar Instantáneo, Popover de Audio Discord, Overlay Movible, Marca SwiftPlay en Notis y Adjuntos en Chat
- **Files:** `src/swiftplay/config.py`, `src/swiftplay/thumb_cache.py`, `src/swiftplay/discord_service.py`, `src/swiftplay/discord_worker.py`, `src/swiftplay/ui/icons.py`, `src/swiftplay/ui/discord_notification.py`, `src/swiftplay/ui/discord_overlay.py`, `src/swiftplay/ui/settings_dialog.py`, `src/swiftplay/ui/discord_panel.py`, `src/swiftplay/ui/main_window.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - Prioritized context-appropriate deck in `MainWindow._get_bottom_bar_deck()` so video/Twitch pages show the loaded video deck even while background music plays, and saved `prev_idx` on `PAGE_VIDEO_PLAYER` transitions in `_switch_page` / `_on_video_panel_back()` so "Volver" returns to the exact previous view rather than getting stuck on History.
  - Replaced laggy sidebar width animation with instant toggle in `toggle_sidebar()` while keeping `_BottomBarContainer` animation intact.
  - Updated `DiscordMessageToast` branding (`APP_NAME` `"SwiftPlay"` + `swiftplay.png` icon).
  - Removed the inline `DiscordDeckCard` from `DiscordVoicePanel`, moving device/volume/VAD/AI-suppression/Colgar controls into a floating `audio_popup` opened via a new settings gear icon (`discord_mini_audio_btn`) left of the mic button on `discord_mini_card`, accompanied by the live `Mic %` VU meter bar.
  - Added free drag-and-drop overlay positioning (`DiscordStatusOverlay.set_edit_mode`, companion `_OverlayEditBar`, multi-monitor clamping) persisted via `AppConfig.discord_overlay_x` / `discord_overlay_y` and launched from `SettingsDialog` (`Mover posición` / `Restablecer posición`).
  - Added file & image attachment support in Discord chat (`discord_worker` `files=`, `can_attach`, extended attachment serialization; `ThumbnailCache(..., crop=False)` fit mode; inline chat image thumbnails, `_ChatImagePreviewDialog`, Save As download, paperclip picker, and `Ctrl+V` clipboard image/file paste under `%LOCALAPPDATA%\SwiftPlay\tmp_uploads`).
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

---

## 2026-11-22 — Live YT Elapsed Time & Refresh, DXGI VideoSurface Redraw, Smooth Animations & Instant Discord Chat Switch
- **Files:** `src/swiftplay/models.py`, `src/swiftplay/ytdlp_worker.py`, `src/swiftplay/youtube_service.py`, `src/swiftplay/storage.py`, `src/swiftplay/ui/widgets.py`, `src/swiftplay/ui/video_surface.py`, `src/swiftplay/ui/video_overlays.py`, `src/swiftplay/ui/discord_panel.py`, `src/swiftplay/ui/main_window.py`, `scripts/verify_swiftplay.py`
- **Changes / Decisions:**
  - Extracted `release_timestamp` (`live_start_ts`) in `ytdlp_worker.py` -> `ResolvedStream.live_start_ts` so long YouTube Live streams display full elapsed duration (e.g. `5:00:00`) via `_live_display_offset` while keeping seek bars aligned to `libmpv`'s playable DVR window; refreshed stream `title`/`author`/`thumb_url`/`is_live` metadata in `_on_video_stream_resolved` and `LocalLibrary.update_play_metadata`.
  - Fixed solid-color `VideoSurface` after returning from the bottom bar by removing Qt raster `setAutoFillBackground(True)` / `WA_OpaquePaintEvent` in favor of `WA_NoSystemBackground` + `WA_PaintOnScreen` + `paintEngine() -> None` + `force_native_redraw()`.
  - Fixed 1–8 frame squashed YouTube grid transitions on minimize by enforcing `VideoGridCard.setMinimumHeight(h + 76)` and pre-rendering target grid pages with `hint_width` inside `stack.setUpdatesEnabled(False)` before switching page index.
  - Added fixed-size clipping containers `_SidebarContainer` and `_BottomBarContainer` driven by `QVariantAnimation`, shared `LoadingShimmerBar` on feed/search pages, and a single reusable `QVariantAnimation` + transient `QGraphicsOpacityEffect` reveal for non-video pages only.
  - Added in-place Discord channel/DM selection updates (`_sync_chat_selection_in_place`), LRU chat history cache, stale `_on_chat_history_loaded` response guard, and `_format_chat_placeholder` consistency across DMs and channels.
- **Verified:** `py_compile` + `scripts/verify_swiftplay.py` `[1/6]`–`[6/6]`.

---

## 2026-11-22 — Agrupación de Procesos Windows, Vivo en Directos YT, No Leídos Discord y Selección Unificada
- **Unified Windows Process Grouping & Job Lifecycle (`windows_os.py`, `discord_service.py`, `discord_worker.py`, `ytdlp_worker.py`):**
  - Added `resolve_worker_python_and_env()` so isolated workers (`discord_worker`, `ytdlp_worker`) launch directly with the same `pythonw.exe` image as the GUI (eliminating separate `python.exe` background entries and duplicate `.venv` stub processes) while loading `.venv` site-packages cleanly via `PYTHONPATH` + `site.addsitedir`.
  - Bound spawned worker processes to a Win32 Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` via `assign_process_to_child_job`) so closing or terminating the main SwiftPlay task cleans up all workers automatically.
- **YouTube Live Mode & Clickable "Ir al vivo" Sync (`models.py`, `ytdlp_worker.py`, `youtube_service.py`, `player.py`, `video_surface.py`, `video_overlays.py`, `main_window.py`):**
  - Added `ResolvedStream.is_live`, avoided stale stream cache reuse for live streams, upgraded `live_badge` (`● EN VIVO`) into a clickable button alongside an explicit `Ir al vivo` pill in both embedded and fullscreen bars, and wired `_reload_current_live_stream()` for YouTube Live & Twitch.
  - Preserved DVR timeline seeking, duration/position labels, `speed_combo`, bottom-bar `bb_seek`, and arrow-key seek on YouTube Live (`allow_seek=True`, hiding timeline strictly on Twitch).
- **Discord Unread Badges, Desktop Toast, Purple Palette, Stable DM Order & Unified Selection (`themes.py`, `config.py`, `storage.py`, `discord_notification.py`, `discord_worker.py`, `discord_panel.py`, `settings_dialog.py`, `main_window.py`):**
  - Added frameless bottom-right `DiscordMessageToast` (`350x108`, non-focus-stealing, accented header badge + `×`, 48x48 circular sender avatar, author, preview, click-to-open target DM/channel) for DMs and bell-enabled text channels.
  - Changed default unread/notification color from blue to purple (`#8B5CF6`, `NOTIFICATION_COLORS` 7-swatch picker + test button in `SettingsDialog`).
  - Tracked unread counts per bell-enabled text channel and DM thread (`_unread_channels`, `_unread_dms`), added highlight + numeric `UnreadBadge` pills + section counters + main sidebar `Discord (N)` indicator, and fixed `is_viewing_channel()` / `showEvent()` / `_sync_center_panes()` auto-clear.
  - Standardized `#2D3647` + `#CFD5DF` silver-bordered filled active style (`PillBtn[active="true"]`) across `NavBtn[active="true"]`, `IconBtn[active="true"]`, checked buttons, `QTabBar::tab:selected`, `QListWidget::item:selected`, and `QMenu::item:selected`; moved transparent container styling onto scoped `#SidebarViewport`, `#TransparentScroll`, and `#TransparentBox` selectors so descendant `NavBtn` backgrounds are never overridden.
  - Enforced strictly one active chat selection across text channels and DMs, and added `touch_timestamp` control on DM thread persistence so opening an older DM preserves list order until a message is sent or received.
- **Targeted RAM Optimization (`windows_os.py`, `sound_effects.py`, `player.py`, `discord_worker.py`, `main_window.py`):**
  - Removed eager `warmup_async()` on Discord bot `on_ready()`, moved DSP warmup strictly to voice join (`_join_channel`), released DSP buffers on voice leave (`release_dsp_idle()`), and added periodic idle/voice working-set trimming (`trim_process_memory`).
  - Capped `QPixmapCache` to `2048` KB, `ThumbnailCache` to `32` items, `synthesize_sound_wav` LRU to `24`, trimmed `libmpv` demuxer caches (`4MiB`/`512KiB` video & YT Live DVR, `2MiB`/`128KiB` Twitch, `2MiB`/`256KiB` audio), and scheduled post-stream cold-page trim.

---

## 2026-11-21 — Documentación IA, Monitor RAM Dual, Sonidos Discord y Overlay Total (`b5e3645`)
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
