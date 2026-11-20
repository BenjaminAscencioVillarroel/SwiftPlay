@echo off
setlocal
cd /d "%~dp0"

echo Actualizando codigo, entorno, binarios y accesos directos de SwiftPlay en:
echo %CD%
echo.

if exist ".venv\Scripts\python.exe" (
    echo Cerrando instancia abierta de SwiftPlay ^(si existe^)...
    ".venv\Scripts\python.exe" "launch_swiftplay.pyw" --quit >nul 2>&1
    timeout /t 1 /nobreak >nul
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and $_.CommandLine -like '*launch_swiftplay.pyw*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

if exist ".git" (
    where git >nul 2>&1
    if not errorlevel 1 (
        set "DIRTY_TREE="
        for /f "delims=" %%i in ('git status --porcelain 2^>nul') do set "DIRTY_TREE=1"
        if defined DIRTY_TREE (
            echo [Aviso] Hay cambios locales sin guardar; omitiendo git pull.
        ) else (
            echo Descargando ultimas actualizaciones desde GitHub...
            git pull --ff-only
            if errorlevel 1 echo [Aviso] No se pudo completar git pull; continuando con la version local...
        )
        echo.
    )
)

set "UV_CMD="
where uv >nul 2>&1
if not errorlevel 1 set "UV_CMD=uv"
if not defined UV_CMD if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_CMD=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_CMD if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_CMD=%USERPROFILE%\.cargo\bin\uv.exe"

if defined UV_CMD goto :have_uv

if not exist ".venv\Scripts\python.exe" (
    echo [Error] No se encontro 'uv' ni un entorno virtual existente ^(.venv^).
    echo Instala uv ejecutando en PowerShell:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo Luego vuelve a ejecutar este archivo.
    pause
    exit /b 1
)
echo [Aviso] No se encontro 'uv'; usando el entorno virtual local existente...
goto :check_bins

:have_uv
"%UV_CMD%" sync --python 3.12
if errorlevel 1 (
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo [Error] No se pudo crear el entorno virtual con uv sync.
        pause
        exit /b 1
    )
    echo [Aviso] uv sync no pudo completarse; continuando con el entorno local existente...
)

:check_bins
set "NEED_BINS=0"
if not exist "bin\libmpv-2.dll" set "NEED_BINS=1"
if not exist "bin\ffmpeg.exe" set "NEED_BINS=1"
if not exist "bin\ffprobe.exe" set "NEED_BINS=1"

if "%NEED_BINS%"=="1" (
    echo.
    echo Descargando binarios nativos faltantes ^(libmpv, ffmpeg, ffprobe^)...
    ".venv\Scripts\python.exe" "scripts\setup_binaries.py"
    if errorlevel 1 (
        echo.
        echo [Error] No se pudieron descargar los binarios necesarios en bin\.
        pause
        exit /b 1
    )
)

echo.
".venv\Scripts\python.exe" "scripts\install_windows_integration.py"
if errorlevel 1 (
    echo.
    echo [Error] No se pudo actualizar el acceso directo ni el inicio de Windows.
    pause
    exit /b 1
)

echo.
echo Listo! Entorno, codigo y accesos directos actualizados. Abriendo SwiftPlay...
start "" ".venv\Scripts\pythonw.exe" "launch_swiftplay.pyw"
timeout /t 2 >nul
exit /b 0
