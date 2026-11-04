"""The central server (`AssetManagementTool.exe serve`): the app for
everyone on the network, served by cheroot (a production WSGI server,
several requests at a time - not Flask's development server), with no
console window or browser needed: run at the server's start by the
scheduled task `install-task` registers, its messages in
<data_dir>\\logs\\server.log.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from logging.handlers import RotatingFileHandler

from . import paths, server_config

log = logging.getLogger(__name__)

APP_NAME = "Asset Management Tool"
TASK_NAME = "Asset Management Tool"
FIREWALL_RULE = "Asset Management Tool"
DRAIN_MAX_BYTES = 256 * 1024 * 1024


def log_dir() -> str:
    path = os.path.join(paths.get_app_data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def setup_logging(console: bool = True) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_am", False) for h in root.handlers):
        return
    handler = RotatingFileHandler(os.path.join(log_dir(), "server.log"), maxBytes=5 * 1024 * 1024,
                                  backupCount=10, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._am = True
    root.addHandler(handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        stream._am = True
        root.addHandler(stream)
    root.setLevel(logging.INFO)

    def thread_error(args):  # a background thread's error, into the log (no console under the task)
        if args.exc_type is not SystemExit:
            log.error("Error in thread %s", args.thread.name if args.thread else "?",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_error


def _drain_oversized(app):
    """An upload over MAX_CONTENT_LENGTH is read and dropped before Flask
    answers 413: cheroot would otherwise close the connection mid-upload
    and the browser shows "connection reset" instead of the error page."""
    limit = app.config["MAX_CONTENT_LENGTH"]

    def wrapped(environ, start_response):
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if limit and limit < length <= DRAIN_MAX_BYTES:
            while environ["wsgi.input"].read(64 * 1024):
                pass
        return app(environ, start_response)

    return wrapped


def make_server(config: dict, app):
    from cheroot import wsgi

    class Server(wsgi.Server):
        def error_log(self, msg="", level=logging.INFO, traceback=False):
            log.log(level, "web: %s", msg, exc_info=traceback)

    server = Server((config["host"], config["port"]), _drain_oversized(app), numthreads=config["threads"],
                    server_name=APP_NAME)
    if config["tls_cert"]:
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        server.ssl_adapter = BuiltinSSLAdapter(config["tls_cert"], config["tls_key"])
    return server


def create_server_app(config: dict):
    """The Flask app on the server's data folder. -> (app, error or '')."""
    paths.use_server_data_dir(config["data_dir"])
    from . import create_app

    app = create_app()
    app.config["SERVER_MODE"] = True
    # imports one at a time in a worker thread (app/jobs.py), not in the request
    app.config["IMPORT_JOBS_BACKGROUND"] = True
    # a CUCM scan the same way (one at a time, page waits) - Network Check
    # already scans in its own threads and the page polls
    app.config["CUCM_SCAN_BACKGROUND"] = True
    # Settings > Data & System shows the backups and "Back up now" (app/backup.py)
    app.config["SERVER_CONFIG"] = dict(config)
    scheme = "https" if config["tls_cert"] else "http"
    # over HTTPS the session cookie is never sent on a plain-HTTP request
    app.config["SESSION_COOKIE_SECURE"] = bool(config["tls_cert"])
    app.config["LAN_URL"] = f"{scheme}://{_server_name()}:{config['port']}"
    return app, app.config.get("DB_INIT_ERROR") or ""


def _server_name() -> str:
    import socket
    try:
        return socket.getfqdn() or socket.gethostname()
    except OSError:
        return "localhost"


def serve(config_file: str | None = None, console: bool = True) -> int:
    try:
        config = server_config.load(config_file)
    except server_config.ConfigError as e:
        print(f"Can't start the server: {e}")
        return 2
    paths.use_server_data_dir(config["data_dir"])
    setup_logging(console)
    app, error = create_server_app(config)
    if error:
        log.error("The database can't be opened: %s - every page shows the error until this is fixed", error)
    from .version import APP_VERSION
    log.info("%s %s serving %s on %s (data: %s)", APP_NAME, APP_VERSION, app.config["LAN_URL"],
             f"{config['host']}:{config['port']}", config["data_dir"])
    if config["tls_cert"]:
        try:
            from . import tls
            info = tls.cert_info(config["tls_cert"])
            log.info("HTTPS certificate for %s, valid until %s, SHA-256 %s", ", ".join(info["names"]),
                     info["not_after"], info["sha256"])
        except Exception as e:  # noqa: BLE001 - cheroot reports a really broken file itself
            log.warning("Can't read the certificate %s: %s", config["tls_cert"], e)
    else:
        log.warning("Plain HTTP: sign-ins and data cross the network unencrypted - "
                    "run `AssetManagementTool.exe make-cert` for HTTPS")
    server = make_server(config, app)
    from . import backup
    nightly = backup.Nightly(config)
    nightly.start()

    def stop(*_args):
        log.info("Stopping")
        nightly.stop.set()
        threading.Thread(target=server.stop, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, stop)
    try:
        server.start()  # blocks until stop()
    except OSError as e:
        log.error("Can't listen on %s:%s - %s (is another copy running? is the port free?)",
                  config["host"], config["port"], e)
        return 1
    log.info("Stopped")
    return 0


# --- the scheduled task that runs it at the server's start ------------------------

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _exe_command(config_file: str | None) -> list[str]:
    import sys
    if paths.is_frozen():
        cmd = [sys.executable, "serve", "--no-console"]
    else:
        main = os.path.join(paths.get_bundle_dir(), "main.py")
        cmd = [sys.executable, main, "serve", "--no-console"]
    return cmd + (["--config", server_config.config_path(config_file)] if config_file else [])


def install_task(user: str, password: str, config_file: str | None = None) -> list[str]:
    """Register the task (at system start, as `user` - a domain service
    account: Network Check / CUCM scan reach other PCs as it) and the
    firewall rule for the port. Run elevated. -> what was done."""
    config = server_config.load(config_file)
    cmd = subprocess.list2cmdline(_exe_command(config_file))
    done = []
    args = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONSTART",
            "/TR", cmd, "/RU", user]
    if password:
        args += ["/RP", password]
    proc = subprocess.run(args, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"schtasks failed ({proc.returncode})")
    # the task's default 72 h time limit would stop the server after 3 days
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) "
                    f"-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries "
                    f"-DontStopIfGoingOnBatteries; Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $s"],
                   capture_output=True, text=True, creationflags=_NO_WINDOW)
    done.append(f"Task '{TASK_NAME}' runs at the server's start as {user}: {cmd}")
    if config["tls_key"]:
        from . import tls
        done.append(f"{user} may read the HTTPS key" if tls.grant_key(config["tls_key"], user)
                    else f"Couldn't let {user} read {config['tls_key']} - grant it read access (icacls)")
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
                   capture_output=True, creationflags=_NO_WINDOW)
    proc = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in",
                           "action=allow", "protocol=TCP", f"localport={config['port']}", "profile=domain,private"],
                          capture_output=True, text=True, creationflags=_NO_WINDOW)
    done.append(f"Firewall: TCP {config['port']} allowed in (domain / private networks)" if proc.returncode == 0
                else f"Firewall rule not added: {(proc.stdout or proc.stderr).strip()}")
    return done


def remove_task() -> list[str]:
    done = []
    for args, what in ((["schtasks", "/Delete", "/F", "/TN", TASK_NAME], f"Task '{TASK_NAME}' deleted"),
                       (["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
                        "Firewall rule deleted")):
        proc = subprocess.run(args, capture_output=True, text=True, creationflags=_NO_WINDOW)
        done.append(what if proc.returncode == 0 else f"{what.split()[0]}: {(proc.stderr or proc.stdout).strip()}")
    return done
