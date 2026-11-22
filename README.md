# SwiftPlay

Cliente nativo de escritorio ultraliviano para **YouTube**, **YouTube Music**, **Twitch** y **Discord** en Windows, diseñado como reemplazo rápido y de bajo consumo de RAM frente al navegador y la app oficial de Discord. Construido con **PySide6**, **`libmpv`** (`python-mpv`), **`yt-dlp`**, **`ytmusicapi`**, **`discord.py[voice]`** y **`davey`** (DAVE E2EE).

---

## Qué ofrece cada módulo

### YouTube
- **Feed de Inicio sin Shorts:** carga hasta 36 videos paginados desde InnerTube (`FEwhat_to_watch`), filtrando `/shorts/`, `#shorts` y videos `<= 60s`.
- **Duración, directos y anti-vistos:** muestra duración o insignia roja **`EN VIVO`** en las miniaturas, excluye videos ya reproducidos o marcados como vistos, y al refrescar trae videos nuevos continuando el feed.
- **Reproductor GPU con `libmpv`:** velocidades `1x`–`2.5x`, calidades `360p`–`1080p`, **Modo Solo Audio** para ahorrar GPU/RAM, pantalla completa 100% con barra flotante y HUD de volumen, descarga local, *Ver más tarde*, historial con minuto exacto de reanudación y botón **`Cerrar video` / `Cerrar directo`** en la barra superior.

### YouTube Music
- **Reproductor de audio independiente (lazy):** comparte barra inferior inteligente con YouTube sin cargar una instancia innecesaria cuando está en reposo.
- **Inicio, búsqueda, cola y radio infinita:** secciones personalizadas (*Volver a escuchar*), filtros *Canciones* / *Videos musicales*, lista *Mi música*, **Mini-barra flotante** y soporte para **teclas multimedia globales** de Windows (Play/Pausa, Siguiente, Anterior, Stop).

### Twitch *(solo invitado, sin chat)*
- **Directos en vivo sin sesión:** pestañas **Español**, **Global** y **Favoritos** locales mediante GQL + HLS (`m3u8`), buscador de canales o enlaces de Twitch y botón de sincronización del directo en el reproductor.

### Discord *(Bot puente aislado, `0 MB` en reposo)*
- **Voz E2EE (DAVE) y supresión IA:** audio en tiempo real mediante subproceso independiente (`discord_worker.py`), con modos `IA Máxima` (DeepFilterNet3 + Silero VAD v6), `IA`, `Estándar (WebRTC)` o `Desactivada`, auto-ganancia (AGC), VAD y control de volumen individual por participante (`0–200%`).
- **Tarjetas e interfaz modular:** tarjetas de voz con foto de perfil real y tinte dinámico según el avatar, divisor arrastrable entre Voz y Chat, paneles colapsables y reabribles (**Canales**, **Voz**, **Chat**, **Miembros**) y ocultación persistente de canales de voz y texto (clic derecho → *Ocultar este canal siempre*, gestionable en **Ajustes → Discord**).
- **Clic derecho → DM persistente:** menú contextual en tarjetas de voz, canales de voz o panel de miembros para abrir DMs y conservar historial local en SQLite bajo la sección colapsable **`MENSAJES DIRECTOS`**.
- **Alertas selectivas, tarjeta fija y overlay flotante:** campana por canal de texto para iluminar discretamente la barra de tareas, **9 sonidos personalizables** (mute/unmute, ensordecer, mensaje, entrada/salida propia y de otros) con estilos *Suave* / *Ultra-sutil* y volumen independiente en una sección colapsable de Ajustes, **overlay selector en 3 modos** (*Sin overlay*, *Overlay de estado*, *Overlay total* estilo Discord con participantes en voz, avatares y aro verde al hablar) con opacidad `10–100%`, y tarjeta fija de tu Bot bajo los recursos del sistema.

### Interfaz y sistema
- **Tema Grafito Oscuro / Plateado Mate** (`matte_silver`) con bordes redondeados uniformes, texto claro legible en todos los botones y menús, atajos (`?` para ver guía, `Tab` rotando YouTube → YT Music → Twitch → Discord, `T` modo teatro, `F` fullscreen) y diálogos de **Ajustes** y **Atajos** cerrables al hacer clic fuera.
- **Monitor de recursos nativo:** lectura en vivo de **RAM física de la app (`RAM APP`)**, **RAM física total (`TOTAL +DS` con Discord)**, **CPU%** y **GPU%** en la barra lateral izquierda (siempre visible incluso en ventanas compactas), más liberación manual de memoria en reposo desde **Ajustes → Sistema**.

---

## Estructura del proyecto

```text
SwiftPlay/
├── Actualizar_Ruta.bat          # Instalador/actualizador 1-clic en Windows (pull + uv sync + binarios + accesos)
├── launch_swiftplay.pyw         # Lanzador sin consola con captura de errores de arranque (crash.log)
├── pyproject.toml / uv.lock     # Dependencias y configuración del paquete (Python >= 3.12)
├── README.md                    # Documentación general para usuarios
├── AGENTS.md                    # Reglas permanentes de contexto para agentes de IA
├── .agents/
│   ├── ARCHITECTURE.md          # Mapa técnico profundo para IA (procesos, SQLite, IPC, InnerTube, DAVE)
│   └── CHANGELOG.md             # Registro histórico de cambios arquitectónicos
├── assets/
│   ├── swiftplay.ico / .png     # Icono oficial generado
├── scripts/
│   ├── setup_binaries.py        # Descarga automática de libmpv-2.dll, ffmpeg.exe y ffprobe.exe en bin/
│   ├── install_windows_integration.py # Accesos directos (.lnk) y registro de inicio automático en Windows
│   ├── generate_icon.py         # Generación de assets/swiftplay.{png,ico}
│   └── verify_swiftplay.py      # Suite completa automatizada ([1/6] a [6/6])
└── src/swiftplay/
    ├── main.py                  # Arranque PySide6 + bloqueo de instancia única (QLocalServer)
    ├── paths.py                 # Rutas centrales (APP_ROOT y %LOCALAPPDATA%\SwiftPlay)
    ├── config.py                # AppConfig tipado (config.json)
    ├── models.py                # MediaItem, ResolvedStream, FeedSection, AccountProfile
    ├── security.py              # Cifrado/descifrado Windows DPAPI para cookies y Discord Bot Token
    ├── storage.py               # SQLite local (Mi música, Ver más tarde, historial, Twitch favs, DMs Discord)
    ├── session_state.py         # Persistencia de último video/música/cola al cerrar o suspender
    ├── browser_auth.py          # Login Google/YouTube en 1 clic vía Chrome/Edge CDP temporal
    ├── youtube_service.py       # InnerTube (Home sin Shorts, búsqueda, perfil) + YTMusic + caché streams
    ├── ytdlp_worker.py          # Cliente + subproceso aislado para yt-dlp (resolución/descarga con auto-cierre)
    ├── twitch_service.py        # Directos Twitch GQL + HLS sin chat ni sesión
    ├── discord_service.py       # Gestor IPC JSON en el proceso GUI hacia discord_worker.py
    ├── discord_worker.py        # Subproceso aislado: Gateway, DAVE E2EE, audio sounddevice + IA krisp-like
    ├── player.py                # Wrapper dual lazy sobre libmpv (Video GPU y Música Solo-Audio)
    ├── thumb_cache.py           # Caché LRU acotada en RAM para miniaturas y avatares circulares
    ├── sound_effects.py         # Síntesis WAV PCM en memoria + winmm.PlaySoundW (0 MB extra) para Discord
    ├── themes.py                # Paleta matte_silver, QSS completo y barra de título oscura DWM
    ├── media_keys.py            # Captura global de teclas multimedia y hotkeys prioritarias Discord en Windows
    ├── windows_os.py            # RAM/CPU/GPU nativo (GetProcessTimes + PDH), trim de RAM, inicio y taskbar
    ├── ram_logger.py            # Registro periódico en logs/ram_log.txt
    └── ui/
        ├── main_window.py       # Ventana principal, barra lateral, barra inferior, monitor y eventos globales
        ├── discord_panel.py     # Vista completa de Discord Voz, Chat, Miembros, Canales y DMs
        ├── discord_overlay.py   # Overlay flotante transparente always-on-top (estado y participantes en voz)
        ├── video_surface.py     # Reproductor embebido de video/directo + controles
        ├── video_overlays.py    # Barra fullscreen auto-ocultable, HUD de volumen y feedback
        ├── music_player_panel.py# Reproductor de música + cola "A continuación"
        ├── mini_bar.py          # Mini-barra flotante al minimizar
        ├── widgets.py           # Tarjetas responsivas, miniaturas redondeadas, toasts y banners
        ├── settings_dialog.py   # Ajustes por pestañas (incluyendo ocultación de canales y hotkeys)
        ├── shortcuts_dialog.py  # Modal rápido de atajos [?] con cierre al clic fuera
        └── icons.py             # Iconos SVG cacheados en memoria
```

---

## Requisitos

- **Windows 10 / 11 (64-bit)**
- **[uv](https://docs.astral.sh/uv/)** (descarga e instala Python 3.12 automáticamente). Si aún no lo tienes, instálalo en PowerShell con:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **Google Chrome o Microsoft Edge** *(opcional)*: necesario solo si quieres vincular tu cuenta de Google/YouTube en 1 clic desde Ajustes.
- **Bot Token de Discord** *(opcional)*: necesario solo si quieres usar la pestaña **Discord - Voz**.

---

## Instalación rápida (recomendada)

1. Clona el repositorio y entra en la carpeta:
   ```powershell
   git clone https://github.com/BenjaminAscencioVillarroel/SwiftPlay.git
   cd SwiftPlay
   ```
2. Haz doble clic en **`Actualizar_Ruta.bat`** (o ejecútalo desde la terminal).

   Este script realiza automáticamente todo lo necesario:
   - Cierra SwiftPlay si estaba abierto en segundo plano y descarga actualizaciones (`git pull --ff-only`) si la carpeta no tiene cambios locales pendientes.
   - Crea el entorno virtual e instala las dependencias definidas en `pyproject.toml` / `uv.lock` (`uv sync --python 3.12`).
   - Descarga los binarios nativos faltantes (`libmpv-2.dll`, `ffmpeg.exe` y `ffprobe.exe`) dentro de `bin/`.
   - Crea accesos directos en el **Escritorio** y en el **Menú Inicio**.
   - Habilita el **inicio automático con Windows** *(puedes desactivarlo cuando quieras desde Ajustes dentro de la app)*.
   - Abre **SwiftPlay**.

> **¿Cómo actualizar en el futuro o tras mover la carpeta?** Haz doble clic nuevamente en **`Actualizar_Ruta.bat`** para bajar los últimos cambios de GitHub, regenerar el entorno/accesos directos y relanzar SwiftPlay en un paso.

---

## Instalación y ejecución manual (sin accesos directos ni inicio automático)

Si prefieres probar o ejecutar la aplicación sin crear accesos directos ni registrar inicio con Windows:

```powershell
uv sync --python 3.12
uv run python scripts/setup_binaries.py
uv run pythonw launch_swiftplay.pyw
```

---

## Discord

### Qué incluye

- **Voz en tiempo real (DAVE E2EE):** worker aislado que consume **0 MB en reposo** al desconectar el bot, con reconexión y resincronización automática de audio al entrar al canal.
- **Supresión de ruido con IA (estilo Krisp):** `IA Máxima` (DeepFilterNet3 + Silero VAD v6), `IA`, `Estándar (WebRTC)` y `Desactivada`, además de ganancia de mic, auto-ganancia (AGC), VAD y volumen por persona.
- **Estados, chat, DMs y privacidad:** paneles colapsables, lista de miembros por canal/servidor, clic derecho para abrir DMs guardados localmente en `MENSAJES DIRECTOS`, ocultación persistente de canales y campana de avisos por canal.
- **Overlay en 3 modos, sonidos, tarjeta fija y atajos:** mini-tarjeta fija del bot bajo los recursos del sistema, **9 sonidos personalizables** con sección colapsable en Ajustes, **overlay superior izquierdo en 3 modos** (*Sin overlay*, *Overlay de estado*, *Overlay total* compacto con participantes, nombres acotados, aro verde al hablar e íconos al lado derecho) con opacidad ajustable `10–100%`, y atajos globales (`Ctrl+Shift+M` con prioridad, `Ctrl+Shift+D`, `Ctrl+Shift+H`).

### Paso a paso para agregar el Bot

1. Entra a [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → pestaña **Bot** → **Reset Token** y copia el **Token**.
2. *(Opcional para ver estados y chat)* En esa misma pestaña **Bot**, activa **Presence Intent**, **Server Members Intent** y **Message Content Intent**, y pulsa **Save Changes**.
3. Ve a **OAuth2 → URL Generator**, marca scope **`bot`** y los permisos **`View Channels`**, **`Connect`**, **`Speak`**, **`Use Voice Activity`**, **`Send Messages`** y **`Read Message History`**. Abre la URL generada y autoriza el bot en tu servidor.
4. En SwiftPlay, abre **Ajustes** (abajo a la izquierda) → **Discord (DPAPI)** → pega el Token y pulsa **Cifrar y guardar token**.
5. Entra a **Discord** (o pulsa `Tab`), haz clic en **Conectar Bot**, elige servidor y canal de voz. Al desconectar el bot vuelve a `0 MB`.

---

## Privacidad y almacenamiento de datos locales

El código fuente del repositorio está completamente separado de los datos del usuario:

- **Sesión, perfil, ajustes e historial:** se guardan fuera del proyecto en `%LOCALAPPDATA%\SwiftPlay`:
  - `session.dpapi` — cookies de YouTube cifradas con **Windows DPAPI** (vinculadas únicamente al usuario local de Windows).
  - `discord_token.dpapi` — Bot Token de Discord cifrado con **Windows DPAPI**.
  - `account_profile.json` — nombre, handle y URL de avatar de la cuenta vinculada.
  - `config.json` — preferencias locales de la aplicación (calidad, audio, atajos Discord, modo/opacidad de overlay, sonidos Discord, canales notificados y canales ocultos).
  - `session_state.json` — última sesión activa (video/música y cola al cerrar o suspender).
  - `library.db` — base de datos SQLite local (*Mi música*, *Ver más tarde*, historial de búsquedas, progreso de reproducción, favoritos de Twitch e historial/hilos de DMs de Discord).
- **Registro de consumo de RAM:** se guarda en `logs/ram_log.txt` dentro de la carpeta del proyecto (accesible desde **Ajustes → Sistema → Abrir log de RAM**).
- **Miniaturas y buffer de reproducción:** se mantienen únicamente en memoria RAM (`ThumbnailCache` acotado).
- **Descargas:** por defecto se guardan fuera del repositorio, en `~/Music/SwiftPlay` y `~/Videos/SwiftPlay` (configurable en Ajustes).

---

## Verificación opcional

Para ejecutar la suite de verificación automatizada (seguridad DPAPI, almacenamiento SQLite, extracción en vivo, smoke test de interfaz/libmpv, integración Windows y puente/worker de Discord):

```powershell
uv run python scripts/verify_swiftplay.py
```
