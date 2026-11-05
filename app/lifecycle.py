"""The app has no console window (2026-11-05, as in AD Audit Log).

The app on a PC alone (no command): the pages have a Quit button, since there
is no window to close. Quitting refuses new requests, waits for the ones
running, then ends the process. One copy per Windows account and session: a
second start finds the first (a named mutex, its port in running.json) and
only opens the browser on it.

The central server has neither: `stop` ends it (server.stop_server).
"""

from __future__ import annotations

import json
import os
import threading
import time

from .paths import get_default_app_data_dir

QUIT_WAIT_SECONDS = 60
MUTEX_PREFIX = "Local\\AssetManagementTool-"
_ERROR_ALREADY_EXISTS = 183

_active = 0
_idle = threading.Condition()
_quitting = False
_mutex = None


def track_requests(app) -> None:
    """Count requests in progress. Registered before every other
    before_request, so each one counted is also uncounted (teardown runs for
    every request, whatever answered it)."""

    @app.before_request
    def _start():
        global _active
        with _idle:
            _active += 1
        if _quitting:
            return "Asset Management Tool is shutting down.", 503
        return None

    @app.teardown_request
    def _end(_exc):
        global _active
        with _idle:
            _active -= 1
            _idle.notify_all()


def quit_after_requests(exit_process=os._exit) -> None:
    """Called by the Quit button: in the background, once no other request is
    running (the Quit request itself included), give the server a moment to
    send the goodbye page and end the app."""
    global _quitting
    _quitting = True

    def run():
        deadline = time.monotonic() + QUIT_WAIT_SECONDS
        with _idle:
            while _active > 0 and time.monotonic() < deadline:
                _idle.wait(timeout=0.5)
        time.sleep(1)
        forget_running()
        exit_process(0)

    threading.Thread(target=run, name="am-quit", daemon=True).start()


def _running_path() -> str:
    return os.path.join(get_default_app_data_dir(), "running.json")


def _mutex_name() -> str:
    account = f"{os.environ.get('USERDOMAIN', '')}_{os.environ.get('USERNAME', '')}"
    return MUTEX_PREFIX + "".join(c if c.isalnum() or c in "-_." else "_" for c in account)


def claim_instance() -> bool:
    """True when this is the only copy of this Windows account in this
    session (it keeps the mutex until it ends); False when one already runs."""
    global _mutex
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _mutex_name())
    if handle and ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex = handle
    return True


def write_running(port: int) -> None:
    os.makedirs(os.path.dirname(_running_path()), exist_ok=True)
    with open(_running_path(), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": port}, f)


def running_port() -> int | None:
    try:
        with open(_running_path(), encoding="utf-8") as f:
            return int(json.load(f)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget_running() -> None:
    try:
        os.remove(_running_path())
    except OSError:
        pass
