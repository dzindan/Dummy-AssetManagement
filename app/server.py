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
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

APP_NAME = "Asset Management Tool"
TASK_NAME = "Asset Management Tool"
FIREWALL_RULE = "Asset Management Tool"
DRAIN_MAX_BYTES = 256 * 1024 * 1024


def log_dir() -> str:
    path = os.path.join(paths.get_app_data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def log_path(config: dict) -> str:
    return os.path.join(config["data_dir"], "logs", "server.log")


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
    else:
        from .auth import ad_sign_in_on, apply_server_ad
        note = apply_server_ad(config)
        if note:
            log.info(note)
        if not ad_sign_in_on():
            log.warning("AD sign-in is off: only the app's own accounts can sign in - "
                        "set it in Users & Roles > AD sign-in")
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
    _write_pid(config)
    try:
        server.start()  # blocks until stop()
    except OSError as e:
        log.error("Can't listen on %s:%s - %s (is another copy running? is the port free?)",
                  config["host"], config["port"], e)
        return 1
    finally:
        _forget_pid(config)
    log.info("Stopped")
    return 0


# --- stopping it: there is no window to close -------------------------------------

def _pid_path(config: dict) -> str:
    return os.path.join(config["data_dir"], "server.pid")


def _write_pid(config: dict) -> None:
    with open(_pid_path(config), "w", encoding="ascii") as f:
        f.write(str(os.getpid()))


def _forget_pid(config: dict) -> None:
    try:
        with open(_pid_path(config), encoding="ascii") as f:
            ours = f.read().strip() == str(os.getpid())
        if ours:  # a second copy that couldn't start must not forget the running one
            os.remove(_pid_path(config))
    except OSError:
        pass


def _is_server_process(pid: str) -> bool:
    """The pid file can be old and the number reused by something else."""
    if not pid.isdigit():
        return False
    proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                          creationflags=_NO_WINDOW)
    first = (proc.stdout or "").split(",")[0].lower()
    return "assetmanagementtool" in first or "python" in first


def stop_server(config_file: str | None = None) -> str:
    """`stop`: end the server, whether the task or `serve` started it. The
    database is safe (SQLite); a request in progress is cut. -> what happened."""
    config = server_config.load(config_file)
    try:
        with open(_pid_path(config), encoding="ascii") as f:
            pid = f.read().strip()
    except OSError:
        return "The server isn't running."
    if not _is_server_process(pid):
        os.remove(_pid_path(config))
        return "The server isn't running."
    proc = subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, text=True,
                          creationflags=_NO_WINDOW)
    if proc.returncode:
        raise RuntimeError(f"{(proc.stderr or proc.stdout).strip()} - run this from an elevated command prompt")
    os.remove(_pid_path(config))
    return "The server is stopped."


def ask_password(user: str) -> str:
    """The service account's password. The exe has no console to type it in,
    so it's Windows' own box (a console when run from source)."""
    if not paths.is_frozen():
        import getpass
        return getpass.getpass(f"Password of {user}: ")
    proc = subprocess.run(["powershell", "-NoProfile", "-Command",
                           "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                           "$c = Get-Credential -UserName $env:AM_TASK_USER "
                           "-Message 'The account the Asset Management server runs as'; "
                           "if ($c) { [Console]::Out.Write($c.GetNetworkCredential().Password) }"],
                          capture_output=True, encoding="utf-8", creationflags=_NO_WINDOW,
                          env={**os.environ, "AM_TASK_USER": user})
    if not proc.stdout:
        raise RuntimeError("no password given")
    return proc.stdout


# --- the scheduled task that runs it at the server's start ------------------------


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
    done.append(f"Task '{TASK_NAME}' runs at the server's start as {user}: {cmd}")
    # The task's default 72 h time limit would stop the server after 3 days.
    # Changing a task stored with a password takes the account + password
    # again (else "The user name or password is incorrect") - the password
    # goes in by environment, never on a command line.
    credentials = "-User $env:AM_TASK_USER -Password $env:AM_TASK_PASSWORD " if password else ""
    proc = subprocess.run(["powershell", "-NoProfile", "-Command",
                           "$ErrorActionPreference = 'Stop'; "
                           "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) "
                           "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries "
                           f"-DontStopIfGoingOnBatteries; Set-ScheduledTask -TaskName '{TASK_NAME}' {credentials}"
                           "-Settings $s | Out-Null"],
                          capture_output=True, text=True, creationflags=_NO_WINDOW,
                          env={**os.environ, "AM_TASK_USER": user, "AM_TASK_PASSWORD": password or ""})
    done.append("No time limit on the task (it would stop the server after 3 days), restarted if it fails"
                if proc.returncode == 0 else
                "WARNING: the task keeps Windows' 3-day time limit - the server would stop after 3 days. "
                "Task Scheduler > the task > Settings: untick \"Stop the task if it runs longer than\". "
                f"({(proc.stderr or proc.stdout).strip()[:300]})")
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


def task_user() -> str:
    """The account the scheduled task runs the server as ('' when there's
    no task) - from its XML, so a non-English Windows reads the same."""
    import re
    proc = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], capture_output=True, text=True,
                          creationflags=_NO_WINDOW)
    if proc.returncode:
        return ""
    m = re.search(r"<UserId>\s*([^<]+?)\s*</UserId>", proc.stdout or "")
    return m.group(1) if m else ""


def remove_task() -> list[str]:
    done = []
    for args, what in ((["schtasks", "/Delete", "/F", "/TN", TASK_NAME], f"Task '{TASK_NAME}' deleted"),
                       (["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
                        "Firewall rule deleted")):
        proc = subprocess.run(args, capture_output=True, text=True, creationflags=_NO_WINDOW)
        done.append(what if proc.returncode == 0 else f"{what.split()[0]}: {(proc.stderr or proc.stdout).strip()}")
    return done
