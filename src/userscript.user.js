// ==UserScript==
// @name         Jellyfin External Player (PotPlayer)
// @namespace    https://github.com/shawnlu96/jellyfin-external-player
// @version      0.1.6
// @description  Hand off Jellyfin web playback to PotPlayer; progress synced back on player close.
// @author       shawnlu96
// @match        *://*/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    https://raw.githubusercontent.com/shawnlu96/jellyfin-external-player/main/src/userscript.user.js
// @downloadURL  https://raw.githubusercontent.com/shawnlu96/jellyfin-external-player/main/src/userscript.user.js
// @supportURL   https://github.com/shawnlu96/jellyfin-external-player/issues
// @homepageURL  https://github.com/shawnlu96/jellyfin-external-player
// ==/UserScript==

(function () {
  'use strict';

  // -----------------------------------------------------------------------
  // 1. Strict Jellyfin fingerprint — meta tag OR localStorage credentials
  //    We DO NOT rely on window.ApiClient: Tampermonkey runs in a sandbox
  //    whose window often can't see page-level globals; also Jellyfin 10.10+
  //    no longer guarantees window.ApiClient is exposed.
  // -----------------------------------------------------------------------
  function hasJellyfinMeta() {
    const meta = document.querySelector('meta[name="application-name"]');
    return !!(meta && meta.content === 'Jellyfin');
  }

  /** Read the Jellyfin web credentials persisted by the SPA into localStorage.
   *  Returns the active server object (with AccessToken + UserId + ManualAddress)
   *  or null if not logged in / not Jellyfin. */
  function getCredentials() {
    try {
      const raw = localStorage.getItem('jellyfin_credentials');
      if (!raw) return null;
      const data = JSON.parse(raw);
      const servers = (data && data.Servers) || [];
      const valid = servers.filter(
        (s) => s && s.AccessToken && s.UserId && (s.ManualAddress || s.LocalAddress || s.RemoteAddress)
      );
      if (!valid.length) return null;
      // Prefer server matching ?serverId= in URL hash, else most recent (DateLastAccessed)
      const urlServerId = new URLSearchParams(
        (location.hash || '').split('?')[1] || ''
      ).get('serverId');
      const matched = urlServerId && valid.find((s) => s.Id === urlServerId);
      if (matched) return matched;
      // Fallback: most recently accessed
      valid.sort((a, b) => (b.DateLastAccessed || 0) - (a.DateLastAccessed || 0));
      return valid[0];
    } catch (e) {
      return null;
    }
  }

  function getServerAddress(creds) {
    return creds.ManualAddress || creds.LocalAddress || creds.RemoteAddress || '';
  }

  function isJellyfin() {
    return hasJellyfinMeta() || !!getCredentials();
  }

  // Defer until ApiClient is ready (Jellyfin SPA loads it async)
  function waitFor(cond, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const t0 = Date.now();
      const tick = () => {
        if (cond()) return resolve();
        if (Date.now() - t0 > timeoutMs) return reject(new Error('timeout'));
        setTimeout(tick, 500);
      };
      tick();
    });
  }

  const HELPER_URL = 'http://127.0.0.1:54321';

  // -----------------------------------------------------------------------
  // 2. Talk to local helper via GM_xmlhttpRequest (bypasses mixed-content)
  // -----------------------------------------------------------------------
  function helperPost(path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: HELPER_URL + path,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(body),
        timeout: 10000,
        onload: (r) => resolve({ status: r.status, body: r.responseText }),
        onerror: (e) => reject(e),
        ontimeout: () => reject(new Error('helper timeout')),
      });
    });
  }

  function helperGet(path) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url: HELPER_URL + path,
        timeout: 5000,
        onload: (r) => resolve({ status: r.status, body: r.responseText }),
        onerror: (e) => reject(e),
        ontimeout: () => reject(new Error('helper timeout')),
      });
    });
  }

  // -----------------------------------------------------------------------
  // 3. HTTP to Jellyfin server using stored credentials (via GM_xmlhttpRequest
  //    to bypass CORS / mixed-content from Tampermonkey sandbox).
  // -----------------------------------------------------------------------
  function jellyfinGet(serverAddress, accessToken, path) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url: serverAddress + path,
        headers: {
          'X-Emby-Token': accessToken,
          Accept: 'application/json',
        },
        timeout: 15000,
        onload: (r) => {
          if (r.status >= 200 && r.status < 300) {
            try {
              resolve(JSON.parse(r.responseText));
            } catch (e) {
              reject(new Error('invalid json from jellyfin'));
            }
          } else {
            reject(new Error(`jellyfin ${path} -> ${r.status}`));
          }
        },
        onerror: reject,
        ontimeout: () => reject(new Error('jellyfin timeout')),
      });
    });
  }

  /** GET that returns the raw response body as text (no JSON parse).
   *  Used for /Items/{id}/Download which returns .strm contents (a URL line). */
  function jellyfinGetText(serverAddress, accessToken, path) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url: serverAddress + path,
        headers: {
          'X-Emby-Token': accessToken,
        },
        timeout: 15000,
        onload: (r) => {
          if (r.status >= 200 && r.status < 300) {
            resolve(r.responseText || '');
          } else {
            reject(new Error(`jellyfin ${path} -> ${r.status}`));
          }
        },
        onerror: reject,
        ontimeout: () => reject(new Error('jellyfin timeout')),
      });
    });
  }

  function jellyfinPost(serverAddress, accessToken, path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: serverAddress + path,
        headers: {
          'X-Emby-Token': accessToken,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        data: JSON.stringify(body),
        timeout: 15000,
        onload: (r) => {
          if (r.status >= 200 && r.status < 300) {
            try {
              resolve(r.responseText ? JSON.parse(r.responseText) : {});
            } catch {
              resolve({});
            }
          } else {
            reject(new Error(`jellyfin ${path} -> ${r.status}: ${r.responseText.slice(0, 200)}`));
          }
        },
        onerror: reject,
        ontimeout: () => reject(new Error('jellyfin timeout')),
      });
    });
  }

  // -----------------------------------------------------------------------
  // 4. Build payload — uses ONLY localStorage credentials + HTTP API.
  // -----------------------------------------------------------------------
  async function buildPayload(itemId) {
    const creds = getCredentials();
    if (!creds) throw new Error('no credentials in localStorage — log in to Jellyfin first');
    const serverAddress = getServerAddress(creds);
    const accessToken = creds.AccessToken;
    const userId = creds.UserId;

    // Fetch item + playback info via HTTP API
    const item = await jellyfinGet(
      serverAddress,
      accessToken,
      `/Users/${userId}/Items/${itemId}`
    );
    const playbackInfo = await jellyfinPost(
      serverAddress,
      accessToken,
      `/Items/${itemId}/PlaybackInfo?UserId=${encodeURIComponent(userId)}`,
      {
        UserId: userId,
        MaxStreamingBitrate: 200_000_000,
        DeviceProfile: { MaxStreamingBitrate: 200_000_000 },
      }
    );
    const mediaSource = (playbackInfo.MediaSources || [])[0];
    if (!mediaSource) throw new Error('no MediaSource for item');

    // For .strm items, Jellyfin's /Videos/{id}/stream returns
    // application/oct-stream which PotPlayer refuses to play. Bypass this
    // entirely by calling /Items/{id}/Download which returns the .strm file
    // raw contents — the first line is the real media URL (typically a
    // direct CDN / object-storage signed link).
    let streamUrl = null;
    const path = item.Path || '';
    const isStrm = /\.strm$/i.test(path) || mediaSource.Container === 'strm';
    if (isStrm) {
      try {
        const strmText = await jellyfinGetText(
          serverAddress,
          accessToken,
          `/Items/${itemId}/Download?api_key=${encodeURIComponent(accessToken)}`
        );
        const firstLine = (strmText || '').split('\n')[0].trim();
        if (/^https?:\/\//i.test(firstLine)) {
          streamUrl = firstLine;
        }
      } catch (e) {
        console.warn('[jellyfin-external-player] strm download failed, falling back to /stream:', e);
      }
    }
    if (!streamUrl) {
      streamUrl =
        serverAddress +
        '/Videos/' +
        itemId +
        '/stream?Static=true&MediaSourceId=' +
        encodeURIComponent(mediaSource.Id) +
        '&api_key=' +
        encodeURIComponent(accessToken);
    }

    let title = item.Name || 'Jellyfin';
    if (item.SeriesName && item.ParentIndexNumber != null && item.IndexNumber != null) {
      title = `${item.SeriesName} S${String(item.ParentIndexNumber).padStart(2, '0')}E${String(item.IndexNumber).padStart(2, '0')} — ${item.Name}`;
    }

    return {
      serverAddress,
      accessToken,
      itemId,
      mediaSourceId: mediaSource.Id,
      playSessionId: playbackInfo.PlaySessionId,
      streamUrl,
      title,
      startPositionTicks: (item.UserData && item.UserData.PlaybackPositionTicks) || 0,
    };
  }

  // -----------------------------------------------------------------------
  // 4. UI — floating button (theme-agnostic; works on JellyFlix and any
  //         Jellyfin web version because it doesn't rely on detail-page DOM)
  // -----------------------------------------------------------------------
  const BUTTON_ID = 'jep-floating-btn';

  function getItemIdFromUrl() {
    // Jellyfin SPA URL: .../#/details?id=xxxx&serverId=yyyy
    const hash = location.hash || '';
    const m = hash.match(/[?&]id=([a-f0-9-]+)/i);
    return m ? m[1] : null;
  }

  function isDetailPage() {
    return /[#&/]details(\?|$)/.test(location.hash) && !!getItemIdFromUrl();
  }

  function ensureFloatingButton() {
    let btn = document.getElementById(BUTTON_ID);
    if (!btn) {
      btn = document.createElement('button');
      btn.id = BUTTON_ID;
      btn.type = 'button';
      btn.title = 'Play in PotPlayer';
      btn.innerHTML = '▶ PotPlayer';
      btn.style.cssText = [
        'position:fixed', 'right:24px', 'bottom:24px', 'z-index:99998',
        'padding:12px 20px', 'border-radius:28px', 'border:0',
        'background:linear-gradient(135deg,#00a4dc 0%,#0078b3 100%)',
        'color:#fff', 'font:600 14px/1 system-ui,-apple-system,sans-serif',
        'cursor:pointer', 'box-shadow:0 4px 16px rgba(0,164,220,0.45)',
        'transition:transform 0.15s, box-shadow 0.15s', 'display:none',
      ].join(';');
      btn.onmouseover = () => {
        btn.style.transform = 'scale(1.05)';
        btn.style.boxShadow = '0 6px 22px rgba(0,164,220,0.65)';
      };
      btn.onmouseout = () => {
        btn.style.transform = '';
        btn.style.boxShadow = '0 4px 16px rgba(0,164,220,0.45)';
      };
      btn.addEventListener('click', onClick);
      document.body.appendChild(btn);
    }
    btn.style.display = isDetailPage() ? '' : 'none';
  }

  let inFlight = false;

  function setButtonBusy(busy) {
    const btn = document.getElementById(BUTTON_ID);
    if (!btn) return;
    if (busy) {
      btn.dataset.original = btn.innerHTML;
      btn.innerHTML = '⏳ 启动中…';
      btn.style.opacity = '0.6';
      btn.style.cursor = 'wait';
      btn.disabled = true;
    } else {
      if (btn.dataset.original) btn.innerHTML = btn.dataset.original;
      btn.style.opacity = '';
      btn.style.cursor = 'pointer';
      btn.disabled = false;
    }
  }

  async function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    if (inFlight) return; // debounce — prevent double-launch
    const itemId = getItemIdFromUrl();
    if (!itemId) {
      toast('No item id detected on this page');
      return;
    }
    const creds = getCredentials();
    if (!creds || !creds.AccessToken || !creds.UserId) {
      toast('Not logged in to Jellyfin — please log in first');
      return;
    }
    if (!getServerAddress(creds)) {
      toast('Jellyfin server address missing from credentials');
      return;
    }
    inFlight = true;
    setButtonBusy(true);
    try {
      const payload = await buildPayload(itemId);
      const resp = await helperPost('/play', payload);
      if (resp.status === 200) {
        toast(`PotPlayer launched: ${payload.title.slice(0, 60)}`);
      } else {
        toast(`Helper error ${resp.status}: ${resp.body.slice(0, 100)}`);
      }
    } catch (err) {
      const msg = String(err && err.message ? err.message : err);
      if (msg.includes('timeout') || msg.includes('helper')) {
        toast('Local helper not running. Start Jellyfin External Player on your PC.');
      } else {
        toast('Error: ' + msg.slice(0, 120));
      }
    } finally {
      // 3s cooldown so user can't spam-launch while PotPlayer is opening
      setTimeout(() => {
        inFlight = false;
        setButtonBusy(false);
      }, 3000);
    }
  }

  function toast(msg) {
    let el = document.getElementById('external-player-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'external-player-toast';
      el.style.cssText =
        'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
        'background:#222;color:#fff;padding:12px 18px;border-radius:6px;' +
        'font:14px/1.4 system-ui;z-index:99999;max-width:80vw;' +
        'box-shadow:0 4px 16px rgba(0,0,0,0.4);transition:opacity 0.3s;';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => (el.style.opacity = '0'), 4000);
  }

  // -----------------------------------------------------------------------
  // 5. Boot
  // -----------------------------------------------------------------------
  (async function boot() {
    try {
      await waitFor(isJellyfin);
    } catch {
      return; // Not Jellyfin, silent exit
    }
    console.log('[jellyfin-external-player] Jellyfin detected, userscript active');

    // SPA route changes — re-evaluate visibility after each navigation
    ensureFloatingButton();
    window.addEventListener('hashchange', ensureFloatingButton);
    window.addEventListener('popstate', ensureFloatingButton);
    setInterval(ensureFloatingButton, 1000);

    // Optional: ping helper once to log status
    try {
      const r = await helperGet('/health');
      console.log('[jellyfin-external-player] helper health:', r.body);
    } catch (e) {
      console.warn(
        '[jellyfin-external-player] helper not reachable at ' + HELPER_URL +
          ' — install + start the helper from the release page.'
      );
    }
  })();
})();
