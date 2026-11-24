@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\pythonw.exe" (
    echo [Error] No se encontro .venv\Scripts\pythonw.exe.
    echo Ejecuta Actualizar_Ruta.bat primero para preparar el entorno.
    pause
    exit /b 1
)

echo Abriendo instancia aislada de prueba para WatchParty ^(--profile test^)...
start "" ".venv\Scripts\pythonw.exe" "launch_swiftplay.pyw" --profile test --page watchparty
exit /b 0
