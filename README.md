# SwiftPlay

Cliente nativo ultraliviano para **YouTube** y **YouTube Music** en Windows, construido con PySide6, `libmpv`, `yt-dlp` y `ytmusicapi`.

## Requisitos

- **Windows 10 / 11 (64-bit)**
- **[uv](https://docs.astral.sh/uv/)** (descarga e instala Python 3.12 automáticamente). Si aún no lo tienes, instálalo en PowerShell con:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **Google Chrome o Microsoft Edge** *(opcional)*: necesario solo si quieres vincular tu cuenta de Google/YouTube en 1 clic desde Ajustes.

---

## Instalación rápida (recomendada)

1. Clona el repositorio y entra en la carpeta:
   ```powershell
   git clone https://github.com/BenjaminAscencioVillarroel/SwiftPlay.git
   cd SwiftPlay
   ```
2. Haz doble clic en **`Actualizar_Ruta.bat`** (o ejecútalo desde la terminal).

   Este script realiza automáticamente todo lo necesario:
   - Crea el entorno virtual e instala las dependencias definidas en `pyproject.toml` / `uv.lock` (`uv sync --python 3.12`).
   - Descarga los binarios nativos faltantes (`libmpv-2.dll`, `ffmpeg.exe` y `ffprobe.exe`) dentro de `bin/`.
   - Crea accesos directos en el **Escritorio** y en el **Menú Inicio**.
   - Habilita el **inicio automático con Windows** *(puedes desactivarlo cuando quieras desde Ajustes dentro de la app)*.
   - Abre **SwiftPlay**.

> **¿Moviste la carpeta del proyecto a otra ruta?** Vuelve a ejecutar `Actualizar_Ruta.bat` para regenerar el entorno y actualizar los accesos directos.

---

## Instalación y ejecución manual (sin accesos directos ni inicio automático)

Si prefieres probar o ejecutar la aplicación sin crear accesos directos ni registrar inicio con Windows:

```powershell
uv sync --python 3.12
uv run python scripts/setup_binaries.py
uv run pythonw launch_swiftplay.pyw
```

---

## Privacidad y almacenamiento de datos locales

El código fuente del repositorio está completamente separado de los datos del usuario:

- **Sesión, perfil, ajustes e historial:** se guardan fuera del proyecto en `%LOCALAPPDATA%\SwiftPlay`:
  - `session.dpapi` — cookies de YouTube cifradas con **Windows DPAPI** (vinculadas únicamente al usuario local de Windows).
  - `account_profile.json` — nombre, handle y URL de avatar de la cuenta vinculada.
  - `config.json` — preferencias locales de la aplicación.
  - `library.db` — base de datos SQLite local (*Mi música*, *Ver más tarde*, historial de búsquedas y progreso de reproducción).
- **Miniaturas y buffer de reproducción:** se mantienen únicamente en memoria RAM.
- **Descargas:** por defecto se guardan fuera del repositorio, en `~/Music/SwiftPlay` y `~/Videos/SwiftPlay` (configurable en Ajustes).

---

## Verificación opcional

Para ejecutar la suite de verificación automatizada (seguridad DPAPI, almacenamiento SQLite, extracción y smoke test de interfaz):

```powershell
uv run python scripts/verify_swiftplay.py
```
