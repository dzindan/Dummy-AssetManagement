"""Backups of the central server's data (P6) and moving data in (P7).

A backup is one zip - AssetManagementTool_BACKUP_<date>_<time>_<kind>.zip:

    data.db, logs.db   snapshots taken with SQLite's online backup API, so
                       they're consistent even while people are using the
                       app (a plain file copy of a WAL database can miss the
                       newest rows, or catch a half-written page)
    settings.json      the portable settings (CUCM connection...)
    handovers/         the generated hand-over forms
    exports/diff_reports/   the saved Asset Diff Reports (Import > Diff Reports)
    uploads/disposal_*      the lists uploaded to Disposal Check ("original")
    backup.json        when, which version, and every table's row count

It is the same layout as the AssetManagementTool_DATA_*.zip copies made
for a new PC (data.db, logs.db, settings.json at the top), so `restore` and
`import-desktop-data` take either - or a desktop data folder.

Never in a backup: local_settings.json (the server's own secret key), the
rest of exports/ (one-off downloads) and of uploads/ (workbooks waiting
for an import to be confirmed).

The server makes one every night at server.json's backup_time (Nightly
below, started by `serve`), keeps the newest backup_keep of each kind, and
records the outcome in <data_dir>\\backup_status.json for Settings > Data &
System. Restoring replaces the live files, so it is only done with the
server stopped (CLI `restore`), after a "pre-restore" backup of what is
being replaced.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import zipfile

log = logging.getLogger(__name__)

PREFIX = "AssetManagementTool_BACKUP_"
KINDS = ("nightly", "manual", "pre-restore", "pre-import", "desktop")
NAME_RE = re.compile(r"^AssetManagementTool_BACKUP_(\d{4}-\d{2}-\d{2}_\d{6})_([a-z-]+)\.zip$")
DB_FILES = ("data.db", "logs.db")
FILES = ("settings.json",)
# (folder, file name prefix): files the database points to, kept with it
KEPT_FOLDERS = (("handovers", ""), ("exports/diff_reports", ""), ("uploads", "disposal_"))
MANIFEST = "backup.json"
STATUS_FILE = "backup_status.json"
CATCH_UP_AFTER = dt.timedelta(hours=24)  # no backup that recent at start -> one soon
CATCH_UP_DELAY = 120  # seconds after the start, so it doesn't slow the start
_WAKE_EVERY = 300  # the nightly wait re-checks the clock (sleep, clock changes)
LEFTOVER_AFTER = 6 * 3600  # a work folder / .part this old is from a killed run
LIST_FOR_SECONDS = 60  # Settings' list of backups, cached (the folder may be a slow share)

# Tables a move/restore must bring over row for row. The others are made
# again by the app itself on start (seeded roles/permissions, the
# "unmapped"/"unresolved" lists rebuilt from the data), so their counts can
# change on the way and that is fine.
REBUILT_TABLES = frozenset({"permissions", "roles", "role_permissions", "device_unmapped", "status_unmapped",
                            "model_unmapped", "branch_unresolved", "sqlite_sequence"})

_lock = threading.Lock()  # one backup at a time (nightly vs "Back up now")


class BackupError(Exception):
    pass


def _kept(rel: str) -> bool:
    """rel ("handovers/x.docx") is one of KEPT_FOLDERS' files."""
    folder, _, name = rel.rpartition("/")
    return bool(name) and any(folder == f or (not p and folder.startswith(f + "/"))
                              for f, p in KEPT_FOLDERS if name.startswith(p))


def _kept_files(base: str):
    """The KEPT_FOLDERS files under base, as "folder/name" paths."""
    for folder, _prefix in KEPT_FOLDERS:
        top = os.path.join(base, *folder.split("/"))
        for root, _dirs, files in os.walk(top):
            for file in files:
                rel = os.path.relpath(os.path.join(root, file), base).replace(os.sep, "/")
                if _kept(rel):
                    yield rel


# --- taking a backup ----------------------------------------------------------------

def default_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "backups")


def table_counts(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        conn.close()


def snapshot(src: str, dest: str) -> None:
    """A consistent copy of a live SQLite file (its -wal included), checked."""
    if not os.path.isfile(src):
        raise BackupError(f"{src} not found")
    try:
        try:
            _copy_db(src, dest, read_only=True)
        except sqlite3.OperationalError as e:
            # a read-only connection can't roll back a hot journal (a crash
            # mid-write) or open a WAL file without its -shm - a normal one can
            log.info("Read-only copy of %s failed (%s) - copying with a normal connection", src, e)
            if os.path.exists(dest):
                os.remove(dest)
            _copy_db(src, dest, read_only=False)
    except sqlite3.DatabaseError as e:  # not a database, damaged, locked for 30 s...
        raise BackupError(f"{src} can't be copied: {e}") from e


def _copy_db(src: str, dest: str, read_only: bool) -> None:
    if read_only and not os.path.abspath(src).startswith("\\\\"):
        # read-only: a source PC's data folder is only read, never written
        # to (nor checkpointed) by taking a copy of it. Not for a UNC path:
        # SQLite refuses a file://server/... URI.
        source = sqlite3.connect(pathlib.Path(os.path.abspath(src)).as_uri() + "?mode=ro", uri=True, timeout=30)
    else:
        source = sqlite3.connect(src, timeout=30)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            # a self-contained file: no -wal beside it to forget
            target.execute("PRAGMA journal_mode = DELETE")
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if result != "ok":
        raise BackupError(f"{os.path.basename(src)} failed the integrity check: {result}")


def backup_name(kind: str, now: dt.datetime) -> str:
    if kind not in KINDS:
        raise ValueError(kind)
    return f"{PREFIX}{now.strftime('%Y-%m-%d_%H%M%S')}_{kind}.zip"


def make_backup(data_dir: str, dest_dir: str, kind: str = "manual", keep: int = 14,
                now: dt.datetime | None = None) -> dict:
    """Zip a backup of data_dir into dest_dir. -> {"path", "size", "counts"}."""
    from .version import APP_VERSION

    with _lock:
        now = now or dt.datetime.now()
        os.makedirs(dest_dir, exist_ok=True)
        _clear_leftovers(data_dir, dest_dir)
        final = os.path.join(dest_dir, backup_name(kind, now))
        if os.path.exists(final):
            raise BackupError(f"{final} already exists")
        part = final + ".part"
        # the snapshots are made on the server's own disk, even when the
        # zips go to a share
        work = tempfile.mkdtemp(prefix=".backup-", dir=data_dir)
        try:
            counts = {}
            for name in DB_FILES:
                src = os.path.join(data_dir, name)
                if name == "data.db" or os.path.isfile(src):
                    snapshot(src, os.path.join(work, name))
                    counts[name] = table_counts(os.path.join(work, name))
            manifest = {"app": "Asset Management Tool", "version": APP_VERSION, "kind": kind,
                        "created": now.isoformat(timespec="seconds"), "host": socket.gethostname(),
                        "data_dir": data_dir, "counts": counts}
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                for name in counts:
                    z.write(os.path.join(work, name), name)
                for name in FILES:
                    path = os.path.join(data_dir, name)
                    if os.path.isfile(path):
                        z.write(path, name)
                for rel in _kept_files(data_dir):
                    z.write(os.path.join(data_dir, *rel.split("/")), rel)
                z.writestr(MANIFEST, json.dumps(manifest, indent=1))
            os.replace(part, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
        prune(dest_dir, kind, keep)
        _list_cache.pop(dest_dir, None)
        return {"path": final, "size": os.path.getsize(final), "counts": counts}


def _clear_leftovers(data_dir: str, dest_dir: str) -> None:
    """Work folders / half-written zips of a run that was killed (the
    server stopped mid-backup). Only old ones: another process (`backup`
    on the command line while the server runs) may be mid-way now."""
    limit = time.time() - LEFTOVER_AFTER
    for folder, test in ((data_dir, lambda n: n.startswith((".backup-", ".restore-"))),
                         (dest_dir, lambda n: n.startswith(PREFIX) and n.endswith(".zip.part"))):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            try:
                if not test(name) or os.path.getmtime(path) > limit:
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                log.info("Removed the leftover %s", path)
            except OSError:
                pass


_list_cache: dict[str, tuple[float, list[dict]]] = {}


def list_backups_cached(dest_dir: str) -> list[dict]:
    """list_backups for every Settings page view: an unreachable share can
    take many seconds to answer, so it's asked at most once a minute (and
    again right after a backup)."""
    hit = _list_cache.get(dest_dir)
    if hit and time.monotonic() - hit[0] < LIST_FOR_SECONDS:
        return hit[1]
    found = list_backups(dest_dir)
    _list_cache[dest_dir] = (time.monotonic(), found)
    return found


def list_backups(dest_dir: str) -> list[dict]:
    """Newest first."""
    out = []
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return out
    for name in names:
        m = NAME_RE.match(name)
        if not m:
            continue
        path = os.path.join(dest_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "kind": m.group(2), "size": size,
                    "created": dt.datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S")})
    out.sort(key=lambda b: b["created"], reverse=True)
    return out


def prune(dest_dir: str, kind: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` backups of this kind. -> deleted names."""
    if keep < 1:
        return []
    deleted = []
    for old in [b for b in list_backups(dest_dir) if b["kind"] == kind][keep:]:
        try:
            os.remove(old["path"])
            deleted.append(old["name"])
        except OSError as e:
            log.warning("Couldn't delete the old backup %s: %s", old["path"], e)
    return deleted


# --- outcome, for Settings and the catch-up at start --------------------------------

def status_path(data_dir: str) -> str:
    return os.path.join(data_dir, STATUS_FILE)


def read_status(data_dir: str) -> dict:
    from .paths import read_json
    return read_json(status_path(data_dir))


def _write_status(data_dir: str, **changes) -> None:
    from .paths import write_json_atomic
    try:
        write_json_atomic(status_path(data_dir), {**read_status(data_dir), **changes})
    except OSError as e:
        log.warning("Couldn't save %s: %s", status_path(data_dir), e)


def run_backup(config: dict, kind: str, by: str = "") -> dict:
    """make_backup with server.json's settings, logged and recorded."""
    data_dir = config["data_dir"]
    dest = config.get("backup_dir") or default_dir(data_dir)
    started = dt.datetime.now()
    try:
        result = make_backup(data_dir, dest, kind, config.get("backup_keep", 14), started)
    except Exception as e:
        _write_status(data_dir, last_error=f"{type(e).__name__}: {e}",
                      last_error_at=started.isoformat(timespec="seconds"), last_error_kind=kind)
        log.error("Backup (%s) failed: %s", kind, e, exc_info=not isinstance(e, (BackupError, OSError)))
        raise
    _write_status(data_dir, last_ok_at=started.isoformat(timespec="seconds"), last_ok_kind=kind,
                  last_file=result["path"], last_size=result["size"], last_by=by,
                  last_error="", last_error_at="")
    log.info("Backup (%s%s) saved: %s (%.1f MB, %.1fs)", kind, f", by {by}" if by else "", result["path"],
             result["size"] / 1048576, (dt.datetime.now() - started).total_seconds())
    return result


def last_ok(data_dir: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(read_status(data_dir).get("last_ok_at") or "")
    except ValueError:
        return None


# --- every night --------------------------------------------------------------------

def next_run(now: dt.datetime, at: str) -> dt.datetime:
    hour, minute = (int(x) for x in at.split(":"))
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return run if run > now else run + dt.timedelta(days=1)


class Nightly(threading.Thread):
    """Started by `serve`: a backup every night at backup_time, and one
    shortly after the start when the newest is over a day old (the server
    was off at that time, or this is its first start)."""

    def __init__(self, config: dict, stop: threading.Event | None = None, clock=dt.datetime.now):
        super().__init__(name="nightly-backup", daemon=True)
        self.config = config
        self.stop = stop or threading.Event()
        self.clock = clock

    def _wait_until(self, when: dt.datetime) -> bool:
        """False when stopped first."""
        while True:
            left = (when - self.clock()).total_seconds()
            if left <= 0:
                return True
            if self.stop.wait(min(left, _WAKE_EVERY)):
                return False

    def _backup(self) -> None:
        try:
            run_backup(self.config, "nightly")
        except Exception:  # noqa: BLE001 - logged and in backup_status.json; tried again next night
            pass

    def run(self) -> None:
        log.info("Nightly backup at %s into %s (keeping %s)", self.config["backup_time"],
                 self.config.get("backup_dir") or default_dir(self.config["data_dir"]),
                 self.config.get("backup_keep", 14))
        newest = last_ok(self.config["data_dir"])
        if (newest is None or self.clock() - newest > CATCH_UP_AFTER) and \
                os.path.isfile(os.path.join(self.config["data_dir"], "data.db")):
            if not self._wait_until(self.clock() + dt.timedelta(seconds=CATCH_UP_DELAY)):
                return
            self._backup()
        while True:
            if not self._wait_until(next_run(self.clock(), self.config["backup_time"])):
                return
            self._backup()


# --- bringing data in: restore (P6) and import-desktop-data (P7) --------------------

def server_running(config: dict) -> bool:
    host = config.get("host") or "0.0.0.0"
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((host, config["port"]), timeout=1):
            return True
    except OSError:
        return False


def _safe_member(name: str) -> str | None:
    """The data path a zip entry goes to, or None to skip it (anything
    else in the zip, or a path trying to leave the folder)."""
    name = name.replace("\\", "/")
    if name.endswith("/") or name.startswith("/") or ":" in name:
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if len(parts) == 1 and parts[0] in DB_FILES + FILES + (MANIFEST,):
        return name
    if len(parts) > 1 and _kept(name):
        return name
    return None


def _copy_rel(src_base: str, dest_base: str, rel: str) -> None:
    dest = os.path.join(dest_base, *rel.split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(os.path.join(src_base, *rel.split("/")), dest)


def read_source(source: str, work: str) -> dict:
    """A backup / DATA zip, or a data folder, unpacked into `work` and
    checked. -> {"counts", "manifest", "files"}."""
    if os.path.isdir(source):
        if not os.path.isfile(os.path.join(source, "data.db")):
            hint = (" - it has the old app.db: open it once with the current version first"
                    if os.path.isfile(os.path.join(source, "app.db")) else "")
            raise BackupError(f"No data.db in {source}{hint}")
        for name in DB_FILES:
            if os.path.isfile(os.path.join(source, name)):
                snapshot(os.path.join(source, name), os.path.join(work, name))
        for name in FILES:
            if os.path.isfile(os.path.join(source, name)):
                shutil.copy2(os.path.join(source, name), os.path.join(work, name))
        for rel in _kept_files(source):
            _copy_rel(source, work, rel)
    elif zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                target = _safe_member(info.filename)
                if target is None:
                    continue
                path = os.path.join(work, *target.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with z.open(info) as src, open(path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not os.path.isfile(os.path.join(work, "data.db")):
            raise BackupError(f"{source} has no data.db at the top - not a backup of this app")
        for name in DB_FILES:
            path = os.path.join(work, name)
            if os.path.isfile(path):
                checked = path + ".checked"
                snapshot(path, checked)  # integrity check + a plain (non-WAL) file
                os.replace(checked, path)
    else:
        raise BackupError(f"{source} is neither a folder nor a zip file")
    counts = {name: table_counts(os.path.join(work, name)) for name in DB_FILES
              if os.path.isfile(os.path.join(work, name))}
    missing = {"asset_items", "accounts"} - set(counts["data.db"])
    if missing:
        raise BackupError(f"data.db in {source} isn't this app's database (no {', '.join(sorted(missing))} table)")
    manifest = {}
    if os.path.isfile(os.path.join(work, MANIFEST)):
        try:
            with open(os.path.join(work, MANIFEST), encoding="utf-8") as f:
                manifest = json.load(f) or {}
        except (OSError, ValueError):
            manifest = {}
    files = sorted(n for n in DB_FILES + FILES if os.path.exists(os.path.join(work, n)))
    for folder, prefix in KEPT_FOLDERS:
        n = sum(1 for rel in _kept_files(work) if rel.startswith(folder + "/"))
        if n:
            files.append(f"{folder}/{prefix}* ({n})" if prefix else f"{folder}/ ({n})")
    return {"counts": counts, "manifest": manifest, "files": files}


def has_data(data_dir: str) -> bool:
    """data_dir's data.db holds any asset, CCTV row or hand-over."""
    path = os.path.join(data_dir, "data.db")
    if not os.path.isfile(path):
        return False
    counts = table_counts(path)
    return any(counts.get(t, 0) for t in ("asset_items", "cctv_items", "handover_records", "import_batches"))


def load_into(config: dict, source: str, kind: str) -> dict:
    """Replace the server's data with `source` (server stopped): a backup
    of what's there first, then the files swapped, then the app's own
    upgrade steps run on them. -> {"source", "before", "after", "pre_backup"}."""
    data_dir = config["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".restore-", dir=data_dir)
    try:
        src = read_source(source, work)
        kept = _keep_server_settings(data_dir, work) if kind == "pre-import" else []
        pre = None
        if os.path.isfile(os.path.join(data_dir, "data.db")):
            pre = run_backup(config, kind)
        swapped = []
        try:
            for name in DB_FILES:
                new = os.path.join(work, name)
                if not os.path.isfile(new):
                    continue
                live = os.path.join(data_dir, name)
                for suffix in ("-wal", "-shm", "-journal"):
                    if os.path.exists(live + suffix):
                        held_rows = suffix != "-shm" and os.path.getsize(live + suffix) > 0
                        os.remove(live + suffix)  # the old file's - its rows are in the pre-backup
                        if held_rows:
                            swapped.append(name + suffix)
                os.replace(new, live)
                swapped.append(name)
            for name in FILES:
                if os.path.isfile(os.path.join(work, name)):
                    os.replace(os.path.join(work, name), os.path.join(data_dir, name))
                    swapped.append(name)
            for rel in list(_kept_files(work)):  # added to what's there (same name: replaced)
                _copy_rel(work, data_dir, rel)
        except OSError as e:
            if not swapped:
                raise BackupError(f"nothing was replaced - {e} (is something else using the data folder?)") from e
            back = f' - put back what was there: restore --from "{pre["path"]}"' if pre else " - run it again"
            raise BackupError(f"stopped partway, the data folder is half replaced "
                              f"({', '.join(swapped)} done; {e}){back}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)

    from . import db, paths
    paths.use_server_data_dir(data_dir)
    db.init_db()  # this version's columns/fix-ups on the data brought in
    after = {name: table_counts(os.path.join(data_dir, name)) for name in DB_FILES
             if os.path.isfile(os.path.join(data_dir, name))}
    return {"source": src, "after": after, "pre_backup": pre, "kept_settings": kept}


# The server's own, set on the server (Users & Roles > AD sign-in) - a PC's
# settings.json moved in never has them right for the server.
SERVER_SETTING_KEYS = ("ad_enabled", "ad_domain", "ad_group", "ad_default_role")


def _keep_server_settings(data_dir: str, work: str) -> list[str]:
    """Moving a PC's data in: its settings.json, but with the server's AD
    sign-in settings. -> the keys kept."""
    from .paths import read_json, write_json_atomic

    current = read_json(os.path.join(data_dir, "settings.json"))
    kept = [k for k in SERVER_SETTING_KEYS if k in current]
    if kept and os.path.isfile(os.path.join(work, "settings.json")):
        incoming = read_json(os.path.join(work, "settings.json"))
        write_json_atomic(os.path.join(work, "settings.json"), {**incoming, **{k: current[k] for k in kept}})
        return kept
    return []  # nothing kept, or no settings.json comes in (the server's stays as it is)


def reconcile(before: dict, after: dict) -> list[dict]:
    """Row counts table by table. A table outside REBUILT_TABLES must
    match; logs.db may only grow (the start writes to it)."""
    rows = []
    for db_name in DB_FILES:
        b, a = before.get(db_name, {}), after.get(db_name, {})
        for table in sorted(set(b) | set(a)):
            n_before, n_after = b.get(table), a.get(table)
            if n_before == n_after:
                state = "ok"
            elif table in REBUILT_TABLES or n_before is None:
                state = "rebuilt"
            elif db_name == "logs.db" and n_after is not None and n_after > n_before:
                state = "ok"
            else:
                state = "MISMATCH"
            rows.append({"db": db_name, "table": table, "before": n_before, "after": n_after, "state": state})
    return rows


def reconcile_text(rows: list[dict]) -> str:
    width = max([len(r["table"]) for r in rows] + [5])
    lines = [f"{'file':<9} {'table':<{width}} {'source':>9} {'server':>9}  "]
    for r in rows:
        fmt = lambda n: "-" if n is None else str(n)  # noqa: E731
        lines.append(f"{r['db']:<9} {r['table']:<{width}} {fmt(r['before']):>9} {fmt(r['after']):>9}  "
                     f"{'' if r['state'] == 'ok' else r['state']}")
    return "\n".join(lines)
