# Jellyfin External Player

Hand off Jellyfin web playback to PotPlayer on Windows, with playback progress synced back to Jellyfin on player close.

- ✅ Native PotPlayer playback quality (HDR, surround audio, hardware decoding)
- ✅ "Continue Watching" works — progress reported to Jellyfin when PotPlayer closes
- ✅ Per-item resume — opens at the position Jellyfin last knew about
- ✅ Universal Jellyfin detection — works on LAN, custom domain, reverse proxy, sub-path
- ✅ Zero config — auto-detects PotPlayer install path
- ✅ Single-file EXE — no Python install needed

## How it works

```
Jellyfin web (Chrome + Tampermonkey)
    │  POST 127.0.0.1:54321/play
    ▼
JellyfinExternalPlayer.exe (system tray)
    │  spawn PotPlayer with /seek=resume_position
    ▼
PotPlayer (you watch)
    │  you close PotPlayer
    ▼
helper reads last position + POST {jellyfin}/Sessions/Playing/Stopped
    ▼
Jellyfin Continue Watching updated ✓
```

## Install

### 1. Local helper

1. Download `JellyfinExternalPlayer.exe` from the [latest release](../../releases/latest).
2. Drop it anywhere (e.g. `%LOCALAPPDATA%\JellyfinExternalPlayer\`).
3. Double-click — a tray icon appears.
4. Right-click tray → **Start with Windows** to auto-launch on login.

### 2. Userscript

1. Install [Tampermonkey](https://www.tampermonkey.net/) in Chrome / Edge / Firefox.
2. Open `jellyfin-external-player.user.js` from the [latest release](../../releases/latest) — Tampermonkey will offer to install it.
3. Open your Jellyfin web UI — open any movie/episode detail page; a **PotPlayer** button shows up next to "Play".

## Requirements

- Windows 10 / 11
- [PotPlayer](https://potplayer.daum.net/) (auto-detected via registry + common install paths)
- Chrome / Edge / Firefox + [Tampermonkey](https://www.tampermonkey.net/)
- Jellyfin 10.8+

## Build from source

Requires Python 3.12.

```bash
git clone https://github.com/<you>/jellyfin-external-player
cd jellyfin-external-player
pip install -r requirements.txt
python src/helper.py        # run from source
build.bat                   # produce dist\JellyfinExternalPlayer.exe
```

Or push a `v*` tag — GitHub Actions builds the EXE and attaches it to a release automatically.

## How progress is reported

PotPlayer doesn't expose a real-time progress API, so this helper takes the **close-and-sync** approach:

1. On exit, read PotPlayer's `HKCU\Software\DAUM\PotPlayerMini64\RecentFileList\Position{N}` for the file we launched.
2. POST `{jellyfin}/Sessions/Playing/Stopped` with `PositionTicks` so Jellyfin records where you stopped.

**Edge case**: if you don't close PotPlayer cleanly (power loss, crash, kill from Task Manager), the position falls back to "launch time + elapsed seconds" — accurate enough for typical sessions.

To always sync progress when switching to another device (phone, iPad), **close PotPlayer before switching**.

## Troubleshooting

- **No PotPlayer button on Jellyfin pages** → check Tampermonkey is enabled + open browser DevTools console; look for `[jellyfin-external-player] Jellyfin detected, userscript active`.
- **"Local helper not running" toast** → start `JellyfinExternalPlayer.exe`; check the tray icon.
- **PotPlayer doesn't launch** → tray menu shows the detected PotPlayer path. If "NOT FOUND", install PotPlayer first.
- **Progress not synced** → open `%LOCALAPPDATA%\JellyfinExternalPlayer\helper.log`; look for `Stopped reported: 204`.

## License

MIT
