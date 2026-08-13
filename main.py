import socket
import threading
import webbrowser

from app import create_app
from app.network import get_lan_ip


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


def main() -> None:
    port = find_free_port()
    lan_ip = get_lan_ip()
    local_url = f"http://127.0.0.1:{port}"
    lan_url = f"http://{lan_ip}:{port}"

    app = create_app()
    app.config["LAN_URL"] = lan_url

    threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()

    print("Asset Management Tool is running.")
    print(f"  - On this computer:        {local_url}")
    print(f"  - From other computers on the same network: {lan_url}")
    print()
    print("Other computers just need a web browser pointed at the address above -")
    print("nothing to install on them. Keep this window open; closing it stops the app")
    print("for everyone using it.")
    print()
    print("If Windows Firewall prompts you, allow access on Private networks so")
    print("other computers on the same office/home network can reach this one.")
    print()
    print("Close this window (or press Ctrl+C) to stop the app.")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
