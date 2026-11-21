# SwiftPlay

Cliente nativo ultraliviano para **YouTube**, **YouTube Music**, **Twitch** y **Discord** en Windows, construido con PySide6, `libmpv`, `yt-dlp`, `ytmusicapi`, `discord.py[voice]` y `davey`.

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
- **Estados, chat y DMs:** lista desplegable de miembros conectados con su actividad, canales de texto y mensajes directos enviados como tu Bot oficial.
- **Atajos y navegación:** `Tab` rota entre YouTube, YT Music, Twitch y Discord; atajos globales en **Ajustes → Atajos Discord** (`Ctrl+Shift+M` con prioridad máxima para silenciar micrófono, `Ctrl+Shift+D` ensordecer, `Ctrl+Shift+H` colgar).

### Paso a paso para agregar el Bot

1. Entra a [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → pestaña **Bot** → **Reset Token** y copia el **Token**.
2. *(Opcional para ver estados y chat)* En esa misma pestaña **Bot**, activa **Presence Intent**, **Server Members Intent** y **Message Content Intent**, y pulsa **Save Changes**.
3. Ve a **OAuth2 → URL Generator**, marca scope **`bot`** y los permisos **`View Channels`**, **`Connect`**, **`Speak`**, **`Use Voice Activity`**, **`Send Messages`** y **`Read Message History`**. Abre la URL generada y autoriza el bot en tu servidor.
4. En SwiftPlay, abre **Ajustes** (abajo a la izquierda) → **Discord (DPAPI)** → pega el Token y pulsa **Cifrar y guardar token**.
5. Entra a **Discord - Voz** (o pulsa `Tab`), haz clic en **Conectar Bot**, elige servidor y canal de voz. Al desconectar el bot vuelve a `0 MB`.

---

## Privacidad y almacenamiento de datos locales

El código fuente del repositorio está completamente separado de los datos del usuario:

- **Sesión, perfil, ajustes e historial:** se guardan fuera del proyecto en `%LOCALAPPDATA%\SwiftPlay`:
  - `session.dpapi` — cookies de YouTube cifradas con **Windows DPAPI** (vinculadas únicamente al usuario local de Windows).
  - `discord_token.dpapi` — Bot Token de Discord cifrado con **Windows DPAPI**.
  - `account_profile.json` — nombre, handle y URL de avatar de la cuenta vinculada.
  - `config.json` — preferencias locales de la aplicación.
  - `library.db` — base de datos SQLite local (*Mi música*, *Ver más tarde*, historial de búsquedas y progreso de reproducción).
- **Registro de consumo de RAM:** se guarda en `logs/ram_log.txt` dentro de la carpeta del proyecto (accesible desde **Ajustes → Sistema → Abrir log de RAM**).
- **Miniaturas y buffer de reproducción:** se mantienen únicamente en memoria RAM.
- **Descargas:** por defecto se guardan fuera del repositorio, en `~/Music/SwiftPlay` y `~/Videos/SwiftPlay` (configurable en Ajustes).

---

## Verificación opcional

Para ejecutar la suite de verificación automatizada (seguridad DPAPI, almacenamiento SQLite, extracción, smoke test de interfaz y puente/worker de Discord):

```powershell
uv run python scripts/verify_swiftplay.py
```
