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
APP_VERSION = "0.2.1"
GITHUB_REPO = "shawnlu96/jellyfin-external-player"
UPDATE_CHECK_INTERVAL_SEC = 24 * 3600  # daily
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

        # Open a Jellyfin playback session BEFORE launching PotPlayer.
        # Without this, /Sessions/Playing/Stopped is rejected silently —
        # Jellyfin only records progress for sessions it has seen start.
        self._report_playing()

        # IMPORTANT: do NOT preflight-resolve the URL. Many object-storage
        # signed URLs (alipan OSS, S3 pre-signed, etc.) are single-use or
        # time-sensitive — consuming the redirect ourselves invalidates the
        # link before PotPlayer gets to it. Let PotPlayer follow any 302s
        # itself, fresh each time.
        #
        # We still HEAD the URL once for diagnostic logging (status + final
        # host) without consuming the body.
        self._log_url_diagnostic(url)

        # PotPlayer parses its own cmdline by splitting on spaces (not via
        # Windows CommandLineToArgvW), so options with spaces in values get
        # mangled even when properly quoted. Skip /title= entirely —
        # PotPlayer will auto-derive the window title from the URL filename
        # (which is what the user sees anyway). The title we built was only
        # cosmetic.
        log.info("PotPlayer URL: %s", url)
        args = [self.potplayer_path, url, f"/seek={seek_arg}"]
        log.info("launching PotPlayer: %s (seek %s)", title, seek_arg)
        self.process = subprocess.Popen(args)
        self.started_at = time.time()

    def _log_url_diagnostic(self, url: str) -> None:
        """HEAD-only probe to log what the URL responds with. Best-effort —
        if HEAD is refused (some object stores reject HEAD), just log and
        move on; PotPlayer will GET it itself."""
        try:
            resp = requests.head(
                url,
                allow_redirects=False,
                timeout=5,
                headers={"User-Agent": "JellyfinExternalPlayer/diagnostic"},
            )
            log.info(
                "url HEAD: status=%d content-type=%s location=%s",
                resp.status_code,
                resp.headers.get("Content-Type", "?"),
                resp.headers.get("Location", "")[:200],
            )
        except requests.RequestException as e:
            log.info("url HEAD failed (non-fatal): %s", e)

    def _resolve_redirect(self, url: str, depth: int = 0) -> str:
        """Follow Jellyfin's stream URL through redirects; return the final
        playable URL. Handles three cases:
          1. Direct video stream (200 + video/* content-type)  -> use as-is
          2. 302 redirect chain                                  -> follow
          3. Jellyfin .strm returned as application/oct-stream  -> peek body,
             extract the inner URL, recurse
        Logs every hop. Returns the original URL on failure."""
        if depth > 3:
            log.warning("preflight: recursion depth exceeded, abort")
            return url
        try:
            resp = requests.get(
                url,
                stream=True,
                allow_redirects=True,
                timeout=10,
                headers={"User-Agent": "JellyfinExternalPlayer/1.0"},
            )
            content_type = resp.headers.get("Content-Type", "")
            log.info(
                "preflight[%d] status=%d content-type=%s final-url=%s",
                depth, resp.status_code, content_type, resp.url[:200],
            )
            for h in resp.history:
                log.info("  redirect: %d -> %s", h.status_code, h.headers.get("Location", "")[:200])

            # Case 3: octet-stream — could be a .strm text file OR a binary
            # video stream served with wrong mime. Disambiguate by sniffing
            # the first 4KB:
            #   - decodable as UTF-8 + first line is an http(s) URL  -> strm
            #   - anything else (binary, MKV header, etc.)            -> real video
            if "octet-stream" in content_type or "oct-stream" in content_type:
                sample = b""
                for chunk in resp.iter_content(4096):
                    sample += chunk
                    if len(sample) >= 4096:
                        break
                resp.close()
                is_strm = False
                try:
                    text = sample.decode("utf-8")  # strict — binary will raise
                    first_line = text.strip().split("\n", 1)[0].strip()
                    if first_line.startswith(("http://", "https://")) and len(first_line) < 2000:
                        is_strm = True
                        log.info("  strm content detected, inner URL: %s", first_line[:200])
                        return self._resolve_redirect(first_line, depth + 1)
                except UnicodeDecodeError:
                    pass  # binary content — fall through
                # Binary or non-URL text: trust the redirected URL as-is
                log.info("  binary stream (not strm), using resolved URL")
                return resp.url

            resp.close()
            if 200 <= resp.status_code < 400:
                return resp.url
        except requests.RequestException as e:
            log.warning("preflight failed: %s — falling back to original URL", e)
        return url

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

    def _session_id(self) -> str:
        return self.payload.get("playSessionId") or f"ep-{APP_NAME}-{int(self.started_at or time.time())}"

    def _auth_headers(self) -> dict:
        token = self.payload["accessToken"]
        return {
            "X-Emby-Token": token,
            "X-Emby-Authorization": (
                f'MediaBrowser Client="{APP_DISPLAY}", '
                f'Device="External Player", DeviceId="{APP_NAME}", Version="{APP_VERSION}"'
            ),
            "Content-Type": "application/json",
        }

    def _report_playing(self) -> None:
        server = self.payload["serverAddress"].rstrip("/")
        body = {
            "ItemId": self.payload["itemId"],
            "MediaSourceId": self.payload.get("mediaSourceId") or self.payload["itemId"],
            "PlaySessionId": self._session_id(),
            "PositionTicks": self.start_position_ticks,
            "CanSeek": True,
            "PlayMethod": "DirectStream",
        }
        try:
            resp = requests.post(
                f"{server}/Sessions/Playing",
                headers=self._auth_headers(),
                json=body,
                timeout=10,
            )
            log.info("Playing reported: %s", resp.status_code)
        except requests.RequestException as e:
            log.warning("failed to report Playing: %s", e)

    def _report_stopped(self, position_ticks: int) -> None:
        server = self.payload["serverAddress"].rstrip("/")
        body = {
            "ItemId": self.payload["itemId"],
            "MediaSourceId": self.payload.get("mediaSourceId") or self.payload["itemId"],
            "PlaySessionId": self._session_id(),
            "PositionTicks": position_ticks,
        }
        try:
            resp = requests.post(
                f"{server}/Sessions/Playing/Stopped",
                headers=self._auth_headers(),
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

    def on_check_update(icon, item):
        check_update()
        icon.update_menu()

    def on_update_now(icon, item):
        perform_update()  # exits process on success

    def on_exit(icon, item):
        icon.stop()
        os._exit(0)

    def update_menu_text(item):
        if _update_state["available"]:
            return f"Update available: v{_update_state['version']} — click to install"
        return "Check for updates"

    menu = pystray.Menu(
        pystray.MenuItem(f"{APP_DISPLAY} v{APP_VERSION}", None, enabled=False),
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
        pystray.MenuItem(
            update_menu_text,
            lambda icon, item: on_update_now(icon, item) if _update_state["available"] else on_check_update(icon, item),
        ),
        pystray.MenuItem("Open log file", on_open_logs),
        pystray.MenuItem("Exit", on_exit),
    )
    return pystray.Icon(APP_NAME, image, APP_DISPLAY, menu)


# ---------------------------------------------------------------------------
# Self-update via GitHub Releases
# ---------------------------------------------------------------------------

_update_state: dict = {"available": False, "version": None, "url": None}


def _version_tuple(v: str) -> tuple:
    return tuple(int(p) for p in v.lstrip("v").split(".") if p.isdigit())


def check_update() -> None:
    """Query GitHub Releases API; if a newer JellyfinExternalPlayer.exe asset
    exists, populate _update_state so the tray menu can offer it."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        resp = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            log.warning("update check: github api %d", resp.status_code)
            return
        data = resp.json()
        latest = data.get("tag_name", "").lstrip("v")
        if not latest or _version_tuple(latest) <= _version_tuple(APP_VERSION):
            log.info("update check: up to date (current %s, latest %s)", APP_VERSION, latest or "?")
            return
        for asset in data.get("assets", []):
            if asset.get("name") == "JellyfinExternalPlayer.exe":
                _update_state["available"] = True
                _update_state["version"] = latest
                _update_state["url"] = asset.get("browser_download_url")
                log.info("update available: v%s (%s)", latest, _update_state["url"])
                return
        log.info("update check: latest %s but no EXE asset found", latest)
    except Exception as e:
        log.warning("update check failed: %s", e)


def update_check_loop() -> None:
    """Daemon: check on boot + every 24h."""
    while True:
        check_update()
        time.sleep(UPDATE_CHECK_INTERVAL_SEC)


def perform_update() -> None:
    """Download the new EXE next to the current one, write an updater batch
    that waits for us to exit, replaces the EXE, restarts, and self-deletes."""
    if not getattr(sys, "frozen", False):
        log.warning("perform_update: not running from frozen EXE — skip")
        return
    if not _update_state["available"]:
        log.info("perform_update: nothing to update")
        return
    current_exe = Path(sys.executable).resolve()
    work_dir = current_exe.parent
    new_exe = work_dir / f"_update_new_{_update_state['version']}.exe"
    bat_path = work_dir / "_update.bat"
    log.info("downloading update v%s -> %s", _update_state["version"], new_exe)
    try:
        with requests.get(_update_state["url"], stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(new_exe, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
    except Exception as e:
        log.error("download failed: %s", e)
        return
    bat_path.write_text(
        f'''@echo off
:wait
tasklist /FI "IMAGENAME eq {current_exe.name}" 2>NUL | find /I "{current_exe.name}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >nul
    goto wait
)
move /Y "{new_exe}" "{current_exe}" >nul
start "" "{current_exe}"
del "%~f0"
''',
        encoding="utf-8",
    )
    # Detach the batch so it survives our exit
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        cwd=str(work_dir),
        close_fds=True,
    )
    log.info("updater spawned; exiting to release file lock")
    os._exit(0)


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

    threading.Thread(target=update_check_loop, daemon=True).start()
    log.info("update checker started")

    tray = make_tray()
    tray.run()


if __name__ == "__main__":
    main()
