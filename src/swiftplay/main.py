"""Punto de entrada de SwiftPlay con bloqueo de instancia única (QLocalServer)."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from swiftplay.paths import APP_NAME, ICON_ICO_PATH
from swiftplay.ui.main_window import MainWindow
from swiftplay.windows_os import (
    allow_set_foreground_window,
    set_process_app_user_model_id,
)

IPC_SERVER_NAME = "SwiftPlaySingleInstanceIPC"


def _notify_existing_instance() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(IPC_SERVER_NAME)
    if sock.waitForConnected(600):
        allow_set_foreground_window()
        sock.write(b"SHOW")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main() -> int:
    set_process_app_user_model_id()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    if ICON_ICO_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_ICO_PATH)))

    if _notify_existing_instance():
        return 0

    QLocalServer.removeServer(IPC_SERVER_NAME)
    ipc_server = QLocalServer()
    ipc_server.listen(IPC_SERVER_NAME)

    win = MainWindow()

    def _on_new_connection() -> None:
        conn = ipc_server.nextPendingConnection()
        if conn is not None:
            conn.waitForReadyRead(200)
            conn.close()
            conn.deleteLater()
        win.restore_window()

    ipc_server.newConnection.connect(_on_new_connection)
    win.show()
    exit_code = app.exec()
    ipc_server.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
