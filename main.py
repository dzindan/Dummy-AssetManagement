import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from app import create_app, lifecycle
from app.network import get_lan_ip

WAIT_FOR_RUNNING_SECONDS = 15


def find_free_port(preferred: int = 8737) -> int:
    for port in [preferred, *range(8738, 8760)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _no_console_streams() -> None:
    """No console: print() and the web server's own messages would go to a
    stream that isn't there."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _attach_console() -> None:
    """The exe has no console of its own: print into the command prompt it
    was started from."""
    if getattr(sys, "frozen", False):
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8")
            print()


def _detach_console() -> None:
    """The server stays running after its command prompt has moved on: it
    must not end on a Ctrl+C meant for something else, or with that window."""
    if getattr(sys, "frozen", False):
        import ctypes

        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
        ctypes.windll.kernel32.FreeConsole()


def _message(text: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "Asset Management Tool", 0x10)  # MB_ICONERROR


def _answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 503  # up, the database isn't: its pages say so
    except OSError:
        return False


def _open_running() -> bool:
    """Another copy of this account holds the mutex: open the browser on it.
    It may still be starting (no window shows meanwhile, so people start it
    again) - its port is waited for."""
    deadline = time.monotonic() + WAIT_FOR_RUNNING_SECONDS
    while time.monotonic() < deadline:
        port = lifecycle.running_port()
        if port and _answers(port):
            webbrowser.open(f"http://127.0.0.1:{port}")
            return True
        time.sleep(0.5)
    return False


def main() -> None:
    if not lifecycle.claim_instance():
        if not _open_running():
            _message("Asset Management Tool is running for this account but doesn't answer.\n\n"
                     "Wait a moment and open it again, or end AssetManagementTool.exe in Task Manager.")
        return
    port = find_free_port()
    lan_ip = get_lan_ip()
    local_url = f"http://127.0.0.1:{port}"
    lan_url = f"http://{lan_ip}:{port}"

    app = create_app()
    app.config["LAN_URL"] = lan_url
    app.config["QUIT_ENABLED"] = True  # no window to close: the pages' Quit button ends it

    lifecycle.write_running(port)
    threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except OSError as e:
        _message(f"Asset Management Tool can't start: {e}")
    finally:
        lifecycle.forget_running()


SERVER_HELP = """Central server (client/server, one database for everyone):
  AssetManagementTool.exe init-server --data-dir D:\\AMData --ad-group "CORP\\AM Users" [--ad-role Viewer] [--port 8737]
        saves %ProgramData%\\AssetManagementTool\\server.json; members of that AD group sign in
        with their Windows account
  AssetManagementTool.exe make-cert [--names amserver,amserver.corp.local,10.0.0.5]
        HTTPS with a self-signed certificate (copy the .cer it prints to the clients)
  AssetManagementTool.exe serve                 run the server, in the background with no window
  AssetManagementTool.exe stop                  stop it (elevated, if it runs as another account)
  AssetManagementTool.exe install-task --user DOMAIN\\svc-am
        run it at the server's start as that account, + firewall rule (elevated);
        a Windows box asks for the account's password
  AssetManagementTool.exe remove-task
Backups (the server also makes one every night, at server.json's backup_time):
  AssetManagementTool.exe backup                back up the server's data now (it may be running)
  AssetManagementTool.exe backup --desktop --to D:\\Temp
        back up this PC's own data (the app without a command), e.g. to move it to the server
  AssetManagementTool.exe list-backups
  AssetManagementTool.exe restore --from <backup zip>        (stop the server first)
Moving a PC's data onto the server (stop the server first):
  AssetManagementTool.exe import-desktop-data --from <backup/DATA zip or data folder> [--replace]
Without a command: the app on this PC alone, as before."""


def _print_reconcile(result: dict) -> bool:
    """Row counts before/after, table by table. -> True when they agree."""
    from app import backup

    rows = backup.reconcile(result["source"]["counts"], result["after"])
    print(backup.reconcile_text(rows))
    bad = [r for r in rows if r["state"] == "MISMATCH"]
    if bad:
        print(f"\n{len(bad)} table(s) don't match: {', '.join(r['table'] for r in bad)} - "
              "check them before people start using the server")
    else:
        print("\nEvery table came over complete (\"rebuilt\" ones are remade by the app itself).")
    if result["pre_backup"]:
        print(f"What was there before is in {result['pre_backup']['path']}")
    return not bad


def _backup_cli(args) -> int:
    from app import backup, paths, server_config

    if args.desktop:
        status = paths.get_app_data_dir_status()
        data_dir = status["path"]
        if status["is_fallback"]:
            print(f"Note: {status['configured_path']} can't be reached ({status['error']}) - "
                  f"backing up {data_dir} instead")
        dest = args.to or os.getcwd()
        try:
            result = backup.make_backup(data_dir, dest, "desktop", keep=365)
        except (backup.BackupError, OSError) as e:
            print(f"Backup failed: {e}")
            return 1
    else:
        try:
            config = server_config.load(args.config)
        except server_config.ConfigError as e:
            print(f"{e}\n(for this PC's own data: backup --desktop --to <folder>)")
            return 2
        if args.to:
            config = {**config, "backup_dir": args.to}
        try:
            result = backup.run_backup(config, "manual", by="command line")
        except Exception as e:  # noqa: BLE001
            print(f"Backup failed: {e}")
            return 1
    counts = result["counts"]["data.db"]
    print(f"Saved {result['path']} ({result['size'] / 1048576:.1f} MB): "
          f"{counts.get('asset_items', 0)} asset rows, {counts.get('cctv_items', 0)} CCTV rows, "
          f"{counts.get('accounts', 0)} accounts")
    print("It holds the bank's real data - keep it only where the data folder itself may be.")
    return 0


def _load_cli(args, kind: str) -> int:
    """restore / import-desktop-data: the server's data replaced by --from."""
    from app import backup, server_config

    try:
        config = server_config.load(args.config)
    except server_config.ConfigError as e:
        print(e)
        return 2
    if backup.server_running(config):
        print(f"The server is running (port {config['port']} answers) - stop it first:\n"
              f"  schtasks /End /TN \"Asset Management Tool\"")
        return 1
    source = os.path.abspath(args.source)
    if kind == "pre-import" and not args.replace and backup.has_data(config["data_dir"]):
        print(f"The server already has data in {config['data_dir']}. To replace it with {source} "
              "anyway (it is backed up first), add --replace.")
        return 1
    try:
        result = backup.load_into(config, source, kind)
    except (backup.BackupError, OSError) as e:
        print(f"Not done: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - e.g. the upgrade steps (init_db) on the data brought in
        print(f"The files are in, but opening them failed: {type(e).__name__}: {e}\n"
              f"What was there before is in the newest pre-restore / pre-import backup "
              f"(list-backups) - restore it with: restore --from <that zip>")
        return 1
    manifest = result["source"]["manifest"]
    print(f"Loaded {source}" + (f" (made {manifest.get('created', '?')} by version "
                                f"{manifest.get('version', '?')} on {manifest.get('host', '?')})" if manifest else ""))
    print(f"Files: {', '.join(result['source']['files'])}")
    if result.get("kept_settings"):
        print(f"Kept the server's own AD sign-in settings ({', '.join(result['kept_settings'])}). AD accounts "
              "made on the server before are gone with its old data - each is made again at its first "
              "sign-in, with the default role.")
    print()
    ok = _print_reconcile(result)
    print("\nStart the server: schtasks /Run /TN \"Asset Management Tool\"")
    return 0 if ok else 3


def _make_cert(config_file, names: str) -> int:
    from app import server_config

    try:
        made = server_config.make_self_signed(config_file, [n for n in names.split(",") if n.strip()] or None)
    except (server_config.ConfigError, OSError, ValueError) as e:
        print(f"Certificate not made: {e}")
        return 1
    print(f"HTTPS on: certificate for {', '.join(made['names'])}, valid until {made['not_after']}")
    # a new key after install-task: the task's account must still read it,
    # or the server can't start HTTPS at its next start
    from app import server, tls
    user = server.task_user()
    if user:
        print(f"  {user} (the server's task) may read the new key" if tls.grant_key(made["key"], user)
              else f"  Couldn't let {user} read {made['key']} - grant it: icacls \"{made['key']}\" /grant {user}:R")
    print(f"  SHA-256 {made['sha256']}")
    print("  For the clients' Trusted Root Certification Authorities (GPO, or on each PC as admin:")
    print(f"  certutil -addstore Root AssetManagementTool.cer):\n  {made['cer']}")
    print("Restart the server to use it: schtasks /End /TN \"Asset Management Tool\" & "
          "schtasks /Run /TN \"Asset Management Tool\"")
    return 0


def cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="AssetManagementTool", description=SERVER_HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="server.json to use (default %%ProgramData%%\\AssetManagementTool\\server.json)")
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init-server", help="save the server's settings")
    init.add_argument("--data-dir", required=True, help="a folder on a local disk of the server")
    init.add_argument("--port", type=int, default=8737)
    init.add_argument("--https", action="store_true", help="also make a self-signed certificate (make-cert)")
    init.add_argument("--ad-group", help="the AD group whose members may sign in, e.g. CORP\\AM Users")
    init.add_argument("--ad-role", help="the app role a new AD account starts with")
    cert = sub.add_parser("make-cert", help="HTTPS: make a self-signed certificate and use it")
    cert.add_argument("--names", default="", help="comma-separated names/IPs (default: this server's)")
    serve = sub.add_parser("serve", help="run the central server")
    serve.add_argument("--no-console", action="store_true", help="log to the file only (the scheduled task)")
    task = sub.add_parser("install-task", help="run the server at the server's start")
    task.add_argument("--user", required=True, help="the account it runs as, e.g. DOMAIN\\svc-am")
    sub.add_parser("remove-task", help="delete the task and the firewall rule")
    sub.add_parser("stop", help="stop the server")
    init.add_argument("--backup-dir", help="where the nightly backups go (default <data-dir>\\backups; "
                                           "better another disk or a share)")
    bak = sub.add_parser("backup", help="back up the data now")
    bak.add_argument("--desktop", action="store_true", help="this PC's own data, not the server's")
    bak.add_argument("--to", help="the folder for the zip (server: default backup_dir; --desktop: here)")
    sub.add_parser("list-backups", help="the server's backups, newest first")
    restore = sub.add_parser("restore", help="replace the server's data with a backup (server stopped)")
    restore.add_argument("--from", dest="source", required=True, help="the backup zip")
    move = sub.add_parser("import-desktop-data", help="move a PC's data onto the server (server stopped)")
    move.add_argument("--from", dest="source", required=True,
                      help="a backup zip, a DATA zip or a data folder (%%LOCALAPPDATA%%\\AssetManagementTool)")
    move.add_argument("--replace", action="store_true", help="even when the server already has data")
    for p in (serve, task, init, cert, sub.choices["remove-task"], sub.choices["stop"], bak, sub.choices["list-backups"], restore, move):
        p.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    _no_console_streams()
    if argv:
        _attach_console()
    args = parser.parse_args(argv)

    from app import server, server_config

    if args.command == "init-server":
        try:
            extra = {"backup_dir": args.backup_dir} if args.backup_dir is not None else {}
            if args.ad_group is not None:
                extra.update(ad_group=args.ad_group, ad_default_role=args.ad_role or "")
            path = server_config.write(args.data_dir, args.port, args.config, **extra)
        except server_config.ConfigError as e:
            print(e)
            return 2
        print(f"Saved {path}")
        if args.https and _make_cert(args.config, ""):
            return 1
        print("Next: AssetManagementTool.exe install-task --user DOMAIN\\svc-am  (or: serve)")
        return 0
    if args.command == "make-cert":
        return _make_cert(args.config, args.names)
    if args.command == "serve":
        if getattr(sys, "frozen", False):  # no window of its own, whatever started it
            try:
                config = server_config.load(args.config)
            except server_config.ConfigError as e:
                print(f"Can't start the server: {e}")
                return 2
            if not args.no_console:
                print(f"Starting in the background. Log: {server.log_path(config)}\n"
                      "To stop it: AssetManagementTool.exe stop")
            _detach_console()
            return server.serve(args.config, console=False)
        return server.serve(args.config, console=not args.no_console)
    if args.command == "stop":
        try:
            print(server.stop_server(args.config))
        except (server_config.ConfigError, RuntimeError) as e:
            print(f"Not done: {e}")
            return 1
        return 0
    if args.command == "install-task":
        try:
            password = server.ask_password(args.user)
            for line in server.install_task(args.user, password, args.config):
                print(line)
        except (server_config.ConfigError, RuntimeError) as e:
            print(f"Not done: {e}")
            return 1
        print("Start it now (or restart the server): schtasks /Run /TN \"Asset Management Tool\"")
        return 0
    if args.command == "remove-task":
        for line in server.remove_task():
            print(line)
        return 0
    if args.command == "backup":
        return _backup_cli(args)
    if args.command == "list-backups":
        from app import backup
        try:
            config = server_config.load(args.config)
        except server_config.ConfigError as e:
            print(e)
            return 2
        folder = config["backup_dir"] or backup.default_dir(config["data_dir"])
        found = backup.list_backups(folder)
        print(f"{folder}: {len(found)} backup(s)")
        for b in found:
            print(f"  {b['created']:%Y-%m-%d %H:%M}  {b['kind']:<11} {b['size'] / 1048576:7.1f} MB  {b['name']}")
        return 0
    if args.command == "restore":
        return _load_cli(args, "pre-restore")
    if args.command == "import-desktop-data":
        return _load_cli(args, "pre-import")
    main()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(cli(sys.argv[1:]))
