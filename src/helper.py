"""Jellyfin External Player — local helper service.

Listens on 127.0.0.1:54321 for play requests from the Tampermonkey userscript,
launches PotPlayer with the right seek position, and on PotPlayer exit reports
the final playback position back to Jellyfin so "Continue Watching" works.
"""
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
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
APP_VERSION = "0.3.5"
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
# PotPlayer IPC — query current playback position via Win32 SendMessage
# ---------------------------------------------------------------------------
# PotPlayer registers WM_USER + 0x64 (GetCurrentTime, returns ms) and
# WM_USER + 0x65 (GetTotalTime, returns ms) on its main window. These are
# reverse-engineered from third-party PotPlayer remote-control tools.
# Window classes seen in the wild: PotPlayer64, PotPlayerMini64, PotPlayer.

POTPLAYER_WINDOW_CLASSES = {
    "PotPlayer64",
    "PotPlayerMini64",
    "PotPlayer",
    "PotPlayerMini",
}
WM_USER = 0x0400
POT_GET_CURRENT_TIME_MS = WM_USER + 0x64
POT_GET_TOTAL_TIME_MS = WM_USER + 0x65

_user32 = ctypes.windll.user32
# WPARAM/LPARAM/LRESULT are pointer-sized on Windows (32-bit on x86, 64-bit
# on x64). ctypes.c_long is always 32-bit on Windows — using it would
# truncate wParam=0x64 to 0 on a 64-bit Python and PotPlayer would not
# recognize the IPC request. ctypes.c_ssize_t is platform-correct.
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.SendMessageW.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    ctypes.c_ssize_t,  # WPARAM
    ctypes.c_ssize_t,  # LPARAM
]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.restype = ctypes.c_int

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def find_potplayer_window() -> int | None:
    """Find the first visible PotPlayer top-level window."""
    found: list[int] = []

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, buf, 64)
        if buf.value in POTPLAYER_WINDOW_CLASSES:
            found.append(hwnd)
            return False  # stop enum
        return True

    _user32.EnumWindows(_EnumWindowsProc(cb), 0)
    return found[0] if found else None


def enum_all_windows_for_pid(target_pid: int) -> list[tuple[int, str, str, bool]]:
    """Return [(hwnd, class, title, visible)] for ALL top-level + child
    windows owned by the given PID. Used for debug logging when we can't
    find a responsive PotPlayer IPC window."""
    results: list[tuple[int, str, str, bool]] = []

    def collect(hwnd):
        wpid = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != target_pid:
            return
        cls = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, cls, 128)
        tit = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, tit, 256)
        results.append((hwnd, cls.value, tit.value, bool(_user32.IsWindowVisible(hwnd))))

    def top_cb(hwnd, _):
        collect(hwnd)
        # also enum children of this top-level window
        def child_cb(child_hwnd, _):
            collect(child_hwnd)
            return True
        _user32.EnumChildWindows(hwnd, _EnumWindowsProc(child_cb), 0)
        return True

    _user32.EnumWindows(_EnumWindowsProc(top_cb), 0)
    return results


def get_window_text(hwnd: int) -> str:
    """Read the title of a top-level window."""
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


# Different PotPlayer builds expose different IPC shapes; try several and use
# whichever returns a sensible value. Discovered through trial.
IPC_PROBES = [
    # (msg, wParam, label)
    (WM_USER + 0x64, 0x64, "WM_USER+0x64,w=0x64"),
    (WM_USER + 0x64, 0,    "WM_USER+0x64,w=0"),
    (WM_USER + 0x100, 0,   "WM_USER+0x100,w=0"),
    (WM_USER + 0x5000, 0,  "WM_USER+0x5000,w=0"),
    (WM_USER + 100, 0x5000, "WM_USER+100,w=0x5000"),
]


def probe_potplayer_position(hwnd: int) -> tuple[int | None, str]:
    """Try every known IPC shape. Return (position_ms, which_probe_worked)."""
    for msg, wparam, label in IPC_PROBES:
        try:
            v = _user32.SendMessageW(hwnd, msg, wparam, 0)
            if v and 0 < v < 24 * 3600 * 1000:  # plausible 0..24h in ms
                return int(v), label
        except OSError:
            continue
    return None, ""


def parse_position_from_title(title: str) -> int | None:
    """Extract HH:MM:SS or MM:SS from PotPlayer window title if present.

    PotPlayer default format: '00:12:34 / 00:24:01 - filename'."""
    import re
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", title)
    if m:
        h, mi, s = (int(x) for x in m.groups())
        return (h * 3600 + mi * 60 + s) * 1000
    m = re.search(r"\b(\d{1,3}):(\d{2})\b", title)
    if m:
        mi, s = (int(x) for x in m.groups())
        return (mi * 60 + s) * 1000
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
        # Last known playback position in 100ns ticks. Polled from PotPlayer
        # IPC; falls back to start_position_ticks until first poll succeeds.
        self.last_position_ticks: int = self.start_position_ticks

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
        # mangled even when properly quoted. Skip /title= entirely — the
        # URL filename is already a usable window title.
        log.info("PotPlayer URL: %s", url)
        args = [self.potplayer_path, url, f"/seek={seek_arg}"]

        # External subtitles: PotPlayer /sub=URL loads remote subtitle files.
        # Pass the first (highest-priority: IsDefault > IsForced > rest).
        # We don't append /sub for every subtitle to avoid bloating cmdline
        # past PotPlayer's limit; PotPlayer's right-click menu can switch
        # between server-side tracks if user wants others.
        subtitle_urls = self.payload.get("subtitleUrls") or []
        if subtitle_urls:
            sub_url = subtitle_urls[0]
            args.append(f"/sub={sub_url}")
            log.info("subtitle: %s", sub_url[:200])

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
        """Block until PotPlayer exits, then POST true playback position
        back to Jellyfin. A background poller queries PotPlayer's Win32 IPC
        every 2s; we use the latest observed value.

        If IPC never returned a position (PotPlayer failed to start, IPC
        protocol changed in a future build, etc.) we DO NOT fall back to
        elapsed-time estimation — estimation produces wrong values whenever
        the user seeks. We just skip the Stopped report and log a warning;
        Jellyfin's Continue Watching stays at whatever the Playing call
        set, which is accurate as a starting position."""
        assert self.process is not None
        self.ipc_succeeded = False
        poller = threading.Thread(target=self._poll_position, daemon=True)
        poller.start()

        self.process.wait()
        elapsed_seconds = int(time.time() - self.started_at)
        log.info("PotPlayer exited after %ds", elapsed_seconds)

        if not self.ipc_succeeded:
            log.warning(
                "no IPC position observed during playback — skipping Stopped "
                "report to avoid corrupting Jellyfin progress with a guess"
            )
            return

        log.info("final position from IPC: %d ticks", self.last_position_ticks)
        self._report_stopped(self.last_position_ticks)

    def _poll_position(self) -> None:
        """Background poller. Strategy:
        1. Wait 10s for PotPlayer to start playback (need real position values).
        2. EXHAUSTIVE probe: every PotPlayer-owned window × every wParam 0..255
           on a few candidate msg ids (0x460-0x4FF range). Log every non-zero.
        3. Pick (hwnd, msg, wparam) returning a plausible 0..24h ms as winner.
        4. Re-poll the winner every 2s until exit."""
        time.sleep(10)  # let PotPlayer initialize playback
        if not self.process or self.process.poll() is not None:
            return

        all_wins = enum_all_windows_for_pid(self.process.pid)
        log.info(
            "EXHAUSTIVE PROBE: enumerating %d windows for PotPlayer pid=%d",
            len(all_wins), self.process.pid,
        )
        # Skip windows that can't possibly host IPC
        skip_classes = {
            "tooltips_class32", "IME", "MSCTFIME UI", "ComboLBox", "Edit",
            "ComboBox", "SysTabControl32", "Static",
        }
        candidate_wins = [w for w in all_wins if w[1] not in skip_classes]
        log.info("probing %d candidate windows (skipped %d noise)",
                 len(candidate_wins), len(all_wins) - len(candidate_wins))

        candidate_msgs = [0x464, 0x465, 0x460, 0x461, 0x4C8, 0x500]
        winning: tuple[int, int, int] | None = None  # (hwnd, msg, wparam)

        for h, c, t, _ in candidate_wins:
            for msg in candidate_msgs:
                for w in range(256):
                    try:
                        v = _user32.SendMessageW(h, msg, w, 0)
                    except OSError:
                        continue
                    if v == 0:
                        continue
                    # Any non-zero response — log it
                    plausible_ms = 0 < v < 24 * 3600 * 1000
                    log.info(
                        "  RESPONSE: hwnd=0x%x class=%r msg=0x%x w=0x%x -> %d %s",
                        h, c, msg, w, v,
                        "[PLAUSIBLE ms!]" if plausible_ms else "",
                    )
                    if plausible_ms and winning is None:
                        winning = (h, msg, w)
                        log.info("  WINNER LOCKED: %s", winning)

        if winning is None:
            log.warning(
                "EXHAUSTIVE PROBE found no plausible IPC response — "
                "this PotPlayer build does not expose SendMessage IPC. "
                "Switching to mpv is the only path to accurate progress."
            )
            return

        # Re-poll winner every 2s
        hwnd, msg, wparam = winning
        while self.process and self.process.poll() is None:
            try:
                v = _user32.SendMessageW(hwnd, msg, wparam, 0)
                if 0 < v < 24 * 3600 * 1000:
                    self.last_position_ticks = int(v) * 10_000
                    self.ipc_succeeded = True
            except OSError:
                pass
            time.sleep(2)

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
