"""The central server's own settings (client/server mode, 2026-11-02):
one server runs the app for everyone (`AssetManagementTool.exe serve`),
the database lives on that server only, the other PCs just open its
address in a browser.

server.json - kept outside the data folder, at
%ProgramData%\\AssetManagementTool\\server.json (or the path in the
AM_SERVER_CONFIG environment variable / `--config`), written by
`AssetManagementTool.exe init-server`:

    {"data_dir": "D:\\\\AMData", "host": "0.0.0.0", "port": 8737,
     "threads": 8, "tls_cert": "", "tls_key": "",
     "backup_dir": "", "backup_time": "02:00", "backup_keep": 14}

data_dir must be a folder on a local disk of the server: SQLite's live
files are never shared over the network.
"""

from __future__ import annotations

import os
import re

from .paths import is_network_path, read_json, write_json_atomic

CONFIG_ENV = "AM_SERVER_CONFIG"
DEFAULT_PORT = 8737
DEFAULT_THREADS = 8
DEFAULT_BACKUP_TIME = "02:00"
DEFAULT_BACKUP_KEEP = 14
DEFAULTS = {"data_dir": "", "host": "0.0.0.0", "port": DEFAULT_PORT, "threads": DEFAULT_THREADS,
            "tls_cert": "", "tls_key": "",
            # nightly backup (app/backup.py): "" = <data_dir>\backups; better
            # another disk or a share, so a dead disk doesn't take both
            "backup_dir": "", "backup_time": DEFAULT_BACKUP_TIME, "backup_keep": DEFAULT_BACKUP_KEEP}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_LOCAL_DIR_RE = re.compile(r"^[A-Za-z]:\\")


class ConfigError(ValueError):
    pass


def default_config_path() -> str:
    base = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return os.path.join(base, "AssetManagementTool", "server.json")


def config_path(explicit: str | None = None) -> str:
    return explicit or os.environ.get(CONFIG_ENV) or default_config_path()


def validate(config: dict) -> dict:
    """Defaults filled in, values checked -> the config to serve with."""
    out = {**DEFAULTS, **{k: v for k, v in (config or {}).items() if k in DEFAULTS}}
    data_dir = str(out["data_dir"] or "").strip().strip('"')
    if not data_dir:
        raise ConfigError("server.json has no data_dir - run: AssetManagementTool.exe init-server --data-dir D:\\AMData")
    if not _LOCAL_DIR_RE.match(data_dir) or is_network_path(data_dir):
        raise ConfigError(f"data_dir must be a folder on a local disk of the server (e.g. D:\\AMData), "
                          f"not {data_dir} - the live database can't be on a network share")
    out["data_dir"] = os.path.normpath(data_dir)
    try:
        out["port"] = int(out["port"])
        out["threads"] = int(out["threads"])
    except (TypeError, ValueError):
        raise ConfigError("port and threads must be numbers") from None
    if not 1 <= out["port"] <= 65535:
        raise ConfigError(f"Invalid port: {out['port']}")
    if not 1 <= out["threads"] <= 64:
        raise ConfigError(f"threads must be 1-64, not {out['threads']}")
    out["host"] = str(out["host"] or "0.0.0.0").strip()
    out["tls_cert"] = str(out["tls_cert"] or "").strip()
    out["tls_key"] = str(out["tls_key"] or "").strip()
    if bool(out["tls_cert"]) != bool(out["tls_key"]):
        raise ConfigError("tls_cert and tls_key go together (both set, or both empty)")
    for key in ("tls_cert", "tls_key"):
        if out[key] and not os.path.isfile(out[key]):
            raise ConfigError(f"{key} not found: {out[key]}")
    backup_dir = str(out["backup_dir"] or "").strip().strip('"')
    if backup_dir and not (_LOCAL_DIR_RE.match(backup_dir) or backup_dir.startswith("\\\\")):
        raise ConfigError(f"backup_dir must be a full path (D:\\AMBackup or \\\\server\\share\\AMBackup), not {backup_dir}")
    out["backup_dir"] = os.path.normpath(backup_dir) if backup_dir else ""
    time_match = _TIME_RE.match(str(out["backup_time"] or "").strip())
    if not time_match:
        raise ConfigError(f"backup_time must be HH:MM (24-hour), not {out['backup_time']}")
    out["backup_time"] = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
    try:
        out["backup_keep"] = int(out["backup_keep"])
    except (TypeError, ValueError):
        raise ConfigError("backup_keep must be a number") from None
    if not 1 <= out["backup_keep"] <= 365:
        raise ConfigError(f"backup_keep must be 1-365, not {out['backup_keep']}")
    return out


def load(explicit: str | None = None) -> dict:
    path = config_path(explicit)
    if not os.path.exists(path):
        raise ConfigError(f"No server config at {path} - run: AssetManagementTool.exe init-server "
                          "--data-dir D:\\AMData")
    return validate(read_json(path))


def write(data_dir: str, port: int = DEFAULT_PORT, explicit: str | None = None, **extra) -> str:
    """init-server: check and save server.json. -> its path."""
    path = config_path(explicit)
    current = read_json(path) if os.path.exists(path) else {}
    config = validate({**current, **extra, "data_dir": data_dir, "port": port})
    os.makedirs(config["data_dir"], exist_ok=True)
    write_json_atomic(path, config)
    return path


def tls_dir(explicit: str | None = None) -> str:
    """Where make-cert puts the certificate: next to server.json."""
    return os.path.join(os.path.dirname(config_path(explicit)), "tls")


def make_self_signed(explicit: str | None = None, names: list[str] | None = None) -> dict:
    """make-cert: a self-signed certificate (app/tls.py), switched on in
    server.json. -> what tls.make_cert made."""
    from . import tls

    path = config_path(explicit)
    if not os.path.exists(path):
        raise ConfigError(f"No server config at {path} - run init-server first")
    current = read_json(path)
    made = tls.make_cert(tls_dir(explicit), names)
    write_json_atomic(path, validate({**current, "tls_cert": made["cert"], "tls_key": made["key"]}))
    return made
