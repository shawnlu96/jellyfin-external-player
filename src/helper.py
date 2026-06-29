"""Jellyfin External Player — local helper service.

Listens on 127.0.0.1:54321 for play requests from the Tampermonkey userscript,
launches PotPlayer with the right seek position, and on PotPlayer exit reports
the final playback position back to Jellyfin so "Continue Watching" works.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 54321
APP_NAME = "JellyfinExternalPlayer"
APP_DISPLAY = "Jellyfin External Player"
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "helper.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------------------
# PotPlayer discovery
# ---------------------------------------------------------------------------

def find_potplayer() -> str | None:
    """Locate PotPlayerMini64.exe via registry uninstall keys + common paths."""
    # Registry uninstall keys (preferred — survives custom install dirs)
    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in uninstall_roots:
        try:
            with winreg.OpenKey(hive, subkey) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as app:
                            display, _ = winreg.QueryValueEx(app, "DisplayName")
                            if "PotPlayer" not in display:
                                continue
                            try:
                                install_dir, _ = winreg.QueryValueEx(app, "InstallLocation")
                            except FileNotFoundError:
                                continue
                            for exe in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
                                path = Path(install_dir) / exe
                                if path.is_file():
                                    return str(path)
                    except OSError:
                        continue
        except OSError:
            continue

    # Common install paths fallback
    candidates = [
        r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
        r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


# ---------------------------------------------------------------------------
# PotPlayer registry — read last playback position on exit
# ---------------------------------------------------------------------------

POTPLAYER_REG_PATHS = [
    r"Software\DAUM\PotPlayerMini64\RecentFileList",
    r"Software\DAUM\PotPlayer64\RecentFileList",
    r"Software\DAUM\PotPlayerMini\RecentFileList",
]


def read_last_position(stream_url: str) -> int | None:
    """Read PotPlayer's last playback position for the given URL.

    PotPlayer writes RecentFileList\\Entry%d entries as REG_BINARY containing
    UTF-16LE path followed by metadata. Position is appended as the file's
    "last position" 8 bytes (100ns ticks, same unit as Jellyfin PositionTicks).
    """
    for reg_path in POTPLAYER_REG_PATHS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
                entries = []
                n_values = winreg.QueryInfoKey(key)[1]
                for i in range(n_values):
                    name, data, _ = winreg.EnumValue(key, i)
                    if not name.startswith("File"):
                        continue
                    entries.append((name, data))
                # Find entry whose path matches stream_url
                target = stream_url.split("?", 1)[0].lower()
                for name, data in entries:
                    if not isinstance(data, str):
                        continue
                    if target in data.lower() or stream_url.lower() in data.lower():
                        # Sibling key "Position{N}" may carry the ticks
                        suffix = name[len("File"):]
                        try:
                            pos_data, _ = winreg.QueryValueEx(key, f"Position{suffix}")
                            if isinstance(pos_data, int):
                                return int(pos_data)
                        except FileNotFoundError:
                            pass
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("read registry %s failed: %s", reg_path, e)
    return None


# ---------------------------------------------------------------------------
# Playback session
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, payload: dict, potplayer_path: str):
        self.payload = payload
        self.potplayer_path = potplayer_path
        self.process: subprocess.Popen | None = None
        self.started_at: float = 0.0
        self.start_position_ticks: int = int(payload.get("startPositionTicks") or 0)

    def launch(self) -> None:
        url = self.payload["streamUrl"]
        title = self.payload.get("title") or "Jellyfin"
        seek_seconds = self.start_position_ticks // 10_000_000
        seek_arg = time.strftime("%H:%M:%S", time.gmtime(seek_seconds))
        args = [self.potplayer_path, url, f"/seek={seek_arg}", f"/title={title}"]
        log.info("launching PotPlayer: %s (seek %s)", title, seek_arg)
        self.process = subprocess.Popen(args)
        self.started_at = time.time()

    def watch_and_report(self) -> None:
        """Block until PotPlayer exits, then POST progress back to Jellyfin."""
        assert self.process is not None
        self.process.wait()
        elapsed_seconds = int(time.time() - self.started_at)
        log.info("PotPlayer exited after %ds", elapsed_seconds)

        # Try to read precise position from registry; fall back to elapsed time
        position_ticks = read_last_position(self.payload["streamUrl"])
        if position_ticks is None:
            position_ticks = self.start_position_ticks + (elapsed_seconds * 10_000_000)
            log.info("registry position not found, estimated from elapsed: %d ticks", position_ticks)
        else:
            log.info("registry position: %d ticks", position_ticks)

        self._report_stopped(position_ticks)

    def _report_stopped(self, position_ticks: int) -> None:
        server = self.payload["serverAddress"].rstrip("/")
        token = self.payload["accessToken"]
        item_id = self.payload["itemId"]
        play_session_id = self.payload.get("playSessionId") or f"ep-{int(self.started_at)}"
        body = {
            "ItemId": item_id,
            "PositionTicks": position_ticks,
            "PlaySessionId": play_session_id,
            "MediaSourceId": self.payload.get("mediaSourceId") or item_id,
        }
        headers = {
            "X-Emby-Token": token,
            "X-Emby-Authorization": (
                f'MediaBrowser Client="{APP_DISPLAY}", '
                f'Device="External Player", DeviceId="{APP_NAME}", Version="1.0.0"'
            ),
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                f"{server}/Sessions/Playing/Stopped",
                headers=headers,
                json=body,
                timeout=10,
            )
            log.info("Stopped reported: %s %s", resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            log.error("failed to report Stopped: %s", e)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

app = Flask(__name__)
_potplayer_path: str | None = None


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    return jsonify({
        "status": "ok",
        "app": APP_DISPLAY,
        "version": "1.0.0",
        "potplayer": _potplayer_path,
    })


@app.route("/play", methods=["POST", "OPTIONS"])
def play():
    if request.method == "OPTIONS":
        return "", 204
    if not _potplayer_path:
        return jsonify({"error": "PotPlayer not found"}), 500
    try:
        payload = request.get_json(force=True)
    except Exception as e:
        return jsonify({"error": f"invalid json: {e}"}), 400
    required = ["streamUrl", "serverAddress", "accessToken", "itemId"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    session = Session(payload, _potplayer_path)
    session.launch()
    threading.Thread(target=session.watch_and_report, daemon=True).start()
    return jsonify({"status": "launched", "title": payload.get("title")})


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

def make_tray():
    """Build a pystray icon. Lazy import so flask-only invocations don't pay."""
    import pystray
    from PIL import Image

    # Load embedded icon (PyInstaller --add-data)
    if getattr(sys, "frozen", False):
        icon_path = Path(sys._MEIPASS) / "icon.ico"
    else:
        icon_path = Path(__file__).parent / "icon.ico"

    if icon_path.exists():
        image = Image.open(icon_path)
    else:
        # Fallback 64x64 solid color square
        image = Image.new("RGB", (64, 64), (52, 152, 219))

    def on_open_logs(icon, item):
        os.startfile(LOG_FILE)

    def on_toggle_startup(icon, item):
        toggle_startup()
        icon.update_menu()

    def on_exit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem(f"{APP_DISPLAY} (running)", None, enabled=False),
        pystray.MenuItem(f"Port: {LISTEN_PORT}", None, enabled=False),
        pystray.MenuItem(
            "PotPlayer: " + (_potplayer_path or "NOT FOUND"),
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Start with Windows",
            on_toggle_startup,
            checked=lambda item: is_startup_enabled(),
        ),
        pystray.MenuItem("Open log file", on_open_logs),
        pystray.MenuItem("Exit", on_exit),
    )
    return pystray.Icon(APP_NAME, image, APP_DISPLAY, menu)


# ---------------------------------------------------------------------------
# Autostart via HKCU Run key
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def toggle_startup() -> None:
    exe = sys.executable if getattr(sys, "frozen", False) else f'pythonw "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if is_startup_enabled():
            winreg.DeleteValue(key, APP_NAME)
            log.info("startup disabled")
        else:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
            log.info("startup enabled: %s", exe)


# ---------------------------------------------------------------------------
# Single-instance guard via port bind
# ---------------------------------------------------------------------------

def ensure_single_instance() -> None:
    """Exit early if another helper is already listening on LISTEN_PORT."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LISTEN_HOST, LISTEN_PORT))
        s.close()
    except OSError:
        log.error("port %d busy — another instance is running. exit.", LISTEN_PORT)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_flask():
    # use_reloader=False — pystray hates fork
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True, use_reloader=False)


def main():
    global _potplayer_path
    ensure_single_instance()
    _potplayer_path = find_potplayer()
    log.info("PotPlayer: %s", _potplayer_path or "NOT FOUND")
    if not _potplayer_path:
        log.warning("PotPlayer not detected. Install from https://potplayer.daum.net/")

    threading.Thread(target=run_flask, daemon=True).start()
    log.info("HTTP server on http://%s:%d", LISTEN_HOST, LISTEN_PORT)

    tray = make_tray()
    tray.run()


if __name__ == "__main__":
    main()
