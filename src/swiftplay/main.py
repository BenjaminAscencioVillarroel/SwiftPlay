"""Punto de entrada de SwiftPlay con bloqueo de instancia única (QLocalServer)."""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QIcon, QPixmapCache
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from swiftplay.paths import APP_NAME, ICON_ICO_PATH, IS_ISOLATED_PROFILE, PROFILE_NAME
from swiftplay.ui.main_window import MainWindow
from swiftplay.windows_os import (
    allow_set_foreground_window,
    set_process_app_user_model_id,
)

IPC_SERVER_NAME = (
    f"SwiftPlaySingleInstanceIPC_{PROFILE_NAME}"
    if IS_ISOLATED_PROFILE
    else "SwiftPlaySingleInstanceIPC"
)


def _parse_initial_page(argv: list[str]) -> int | None:
    raw = ""
    for i, arg in enumerate(argv):
        s = str(arg or "").strip()
        if s.startswith("--page="):
            raw = s.split("=", 1)[1].strip().lower()
            break
        if s == "--page" and i + 1 < len(argv):
            raw = str(argv[i + 1] or "").strip().lower()
            break
    if raw == "watchparty":
        return MainWindow.PAGE_WATCHPARTY
    return None


def _send_ipc_command(cmd: bytes = b"SHOW") -> bool:
    sock = QLocalSocket()
    sock.connectToServer(IPC_SERVER_NAME)
    if sock.waitForConnected(600):
        if cmd == b"SHOW":
            allow_set_foreground_window()
        sock.write(cmd)
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main() -> int:
    if "--quit" in sys.argv[1:]:
        _app = QCoreApplication(sys.argv)
        _send_ipc_command(b"QUIT")
        return 0

    set_process_app_user_model_id()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(bool(IS_ISOLATED_PROFILE))
    QPixmapCache.setCacheLimit(4096)
    if ICON_ICO_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_ICO_PATH)))

    if _send_ipc_command(b"SHOW"):
        return 0

    QLocalServer.removeServer(IPC_SERVER_NAME)
    ipc_server = QLocalServer()
    ipc_server.listen(IPC_SERVER_NAME)

    win = MainWindow(initial_page=_parse_initial_page(sys.argv[1:]))
    app.commitDataRequest.connect(lambda _mgr: win.persist_session_state(force=True))
    app.aboutToQuit.connect(lambda: win.persist_session_state(force=True))

    def _on_new_connection() -> None:
        conn = ipc_server.nextPendingConnection()
        cmd = b"SHOW"
        if conn is not None:
            conn.waitForReadyRead(200)
            raw = bytes(conn.readAll()).strip()
            if raw:
                cmd = raw
            conn.close()
            conn.deleteLater()
        if cmd == b"QUIT":
            win.quit_app()
        else:
            win.restore_window()

    ipc_server.newConnection.connect(_on_new_connection)
    win.show()
    exit_code = app.exec()
    ipc_server.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
