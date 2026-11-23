# SwiftPlay

Cliente nativo de escritorio ultraliviano para **YouTube**, **YouTube Music**, **Twitch** y **Discord** en Windows, diseñado como reemplazo rápido y de bajo consumo de RAM frente al navegador y la app oficial de Discord. Construido con **PySide6**, **`libmpv`** (`python-mpv`), **`yt-dlp`**, **`ytmusicapi`**, **`discord.py[voice]`** y **`davey`** (DAVE E2EE).

---

## Qué ofrece cada módulo

### YouTube
- **Feed de Inicio sin Shorts con scroll infinito:** carga un lote inicial ligero (`24` videos) y carga automáticamente más recomendaciones al desplazarte hasta un máximo de `120` elementos desde InnerTube (`FEwhat_to_watch`), descargando miniaturas fuera de pantalla (`1500 px`), filtrando `/shorts/`, `#shorts` y videos `<= 60s`, y liberando caché/RAM al pulsar recargar.
- **Vista de canales:** clic en el canal o avatar desde las tarjetas o el reproductor para explorar sus pestañas exclusivas de **Videos** y **Directos** (sin Shorts ni playlists) con scroll infinito propio.
- **Duración, directos y anti-vistos:** muestra duración o insignia roja **`EN VIVO`** en las miniaturas, excluye videos ya reproducidos o marcados como vistos, y al refrescar trae videos nuevos continuando el feed.
- **Reproductor GPU con `libmpv`:** velocidades `1x`–`2.5x`, calidades `360p`–`1080p`, **Modo Solo Audio** para ahorrar GPU/RAM, pantalla completa 100% con barra flotante y HUD de volumen, descarga local, *Ver más tarde*, historial con minuto exacto de reanudación, botón **`Ir al vivo`** / **`● EN VIVO`** clicable en directos de YouTube y botón **`Cerrar video` / `Cerrar directo`** en la barra superior.

### YouTube Music
- **Reproductor de audio independiente (lazy):** comparte barra inferior inteligente con YouTube sin cargar una instancia innecesaria cuando está en reposo.
- **Inicio infinito, búsqueda, cola y radio:** carga secciones iniciales personalizadas (*Volver a escuchar*) y amplía el inicio al hacer scroll hasta `120` elementos (con limpieza de caché al recargar), filtros *Canciones* / *Videos musicales*, lista *Mi música*, **Mini-barra flotante** y soporte para **teclas multimedia globales** de Windows (Play/Pausa, Siguiente, Anterior, Stop).

### Twitch *(solo invitado, sin chat)*
- **Directos en vivo sin sesión:** pestañas **Español**, **Global** y **Favoritos** locales mediante GQL + HLS (`m3u8`), buscador de canales o enlaces de Twitch y botón de sincronización del directo en el reproductor.

### Discord *(Bot puente aislado, `0 MB` en reposo)*
- **Voz E2EE (DAVE), arranque rápido y supresión IA:** audio en tiempo real mediante subproceso independiente (`discord_worker.py`) con conector IPv4 directo, apertura paralela de audio/voz sin bloqueos de warmup ni cuelgues por IPv6 o WDM-KS, modos `IA Máxima` (DeepFilterNet3 + Silero VAD v6), `IA`, `Estándar (WebRTC)` o `Desactivada`, auto-ganancia (AGC), VAD y control de volumen individual por participante (`0–200%`).
- **Tarjetas centradas, vista exclusiva Voz/Chat y CSS Grafito:** tema neutro Grafito Obsidiana, vista central exclusiva entre **Voz** (tarjetas centradas en filas balanceadas estilo Discord `1`, `2`, `2+1`, `2+2`, `3+2`, etc., con foto real y tinte dinámico) y **Chat** nativo agrupado (`_ChatMessagesView`) con auto-scroll al fondo, barra flotante (*responder*, *editar*, *copiar*, *pegar*), visor translúcido interno de imágenes, adjuntos por clic o *drag & drop*, popover de audio en la tarjeta fija inferior y paneles laterales colapsables (**Canales**, **Miembros**).
- **Presencia en tiempo real y DM persistente:** estados en vivo (*En línea*, *Ausente*, *No molestar*, *Desconectado*) en hilos DM, cabecera, canales de voz, tarjetas de llamada y lista de miembros; clic derecho para abrir DMs guardados localmente en SQLite bajo **`MENSAJES DIRECTOS`**.
- **Alertas selectivas, tarjeta fija y overlay movible:** campana por canal de texto para notificaciones de escritorio y parpadeo en barra de tareas, **9 sonidos personalizables** (mute/unmute, ensordecer, mensaje, entrada/salida propia y de otros) con estilos *Suave* / *Ultra-sutil* y volumen independiente en una sección colapsable de Ajustes, **overlay selector en 3 modos** (*Sin overlay*, *Overlay de estado*, *Overlay total* estilo Discord con posición libre editable desde Ajustes) y tarjeta fija de tu Bot bajo los recursos del sistema.

### Interfaz y sistema
- **Tema Grafito Oscuro / Plateado Mate** (`matte_silver`) con transiciones suaves ligeras (`QVariantAnimation`) en pestañas no-video y cambio Voz/Chat (sin afectar `libmpv` D3D11), bordes redondeados uniformes, texto claro legible en todos los botones y menús, atajos (`?` para ver guía, `Tab` rotando YouTube → YT Music → Twitch → Discord, `T` modo teatro, `F` fullscreen) y diálogos de **Ajustes** y **Atajos** cerrables al hacer clic fuera.
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
    ├── models.py                # MediaItem, ChannelInfo, ResolvedStream, FeedSection, AccountProfile
    ├── security.py              # Cifrado/descifrado Windows DPAPI para cookies y Discord Bot Token
    ├── storage.py               # SQLite local (Mi música, Ver más tarde, historial, Twitch favs, DMs Discord)
    ├── session_state.py         # Persistencia de último video/música/cola al cerrar o suspender
    ├── browser_auth.py          # Login Google/YouTube en 1 clic vía Chrome/Edge CDP temporal
    ├── youtube_service.py       # InnerTube (Home sin Shorts, scroll infinito, canales Videos/Directos) + YTMusic + caché streams
    ├── ytdlp_worker.py          # Cliente + subproceso aislado para yt-dlp (resolución/descarga con auto-cierre)
    ├── twitch_service.py        # Directos Twitch GQL + HLS sin chat ni sesión
    ├── discord_service.py       # Gestor IPC JSON en el proceso GUI hacia discord_worker.py
    ├── discord_worker.py        # Subproceso aislado: Gateway IPv4, conexión paralela, DAVE E2EE, presencia, audio sounddevice + IA
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

- **Voz en tiempo real (DAVE E2EE):** worker aislado que consume **0 MB en reposo** al desconectar el bot, con conector IPv4 rápido, arranque paralelo de audio/voz y resincronización automática al entrar al canal.
- **Supresión de ruido con IA (estilo Krisp):** `IA Máxima` (DeepFilterNet3 + Silero VAD v6), `IA`, `Estándar (WebRTC)` y `Desactivada`, respaldo WebRTC instantáneo durante el warmup en frío, ganancia de mic, auto-ganancia (AGC), VAD y volumen por persona.
- **Presencia, chat agrupado, avisos sin leer y DMs:** vista central exclusiva entre Voz (tarjetas centradas estilo Discord) y Chat nativo agrupado con auto-scroll al fondo, respuesta/edición/copia/adjuntos drag & drop + visor translúcido interno, estados en vivo (`En línea`, `Ausente`, `No molestar`, `Desconectado`), DMs persistentes, ocultación de canales y contador de no leídos.
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
