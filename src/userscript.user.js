// ==UserScript==
// @name         Jellyfin External Player (PotPlayer)
// @namespace    https://github.com/shawnlu96/jellyfin-external-player
// @version      0.1.0
// @description  Hand off Jellyfin web playback to PotPlayer; progress synced back on player close.
// @author       shawnlu96
// @match        *://*/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    https://github.com/shawnlu96/jellyfin-external-player/releases/latest/download/jellyfin-external-player.user.js
// @downloadURL  https://github.com/shawnlu96/jellyfin-external-player/releases/latest/download/jellyfin-external-player.user.js
// @supportURL   https://github.com/shawnlu96/jellyfin-external-player/issues
// @homepageURL  https://github.com/shawnlu96/jellyfin-external-player
// ==/UserScript==

(function () {
  'use strict';

  // -----------------------------------------------------------------------
  // 1. Strict Jellyfin fingerprint — meta tag + ApiClient shape
  // -----------------------------------------------------------------------
  function isJellyfin() {
    const meta = document.querySelector('meta[name="application-name"]');
    if (meta && meta.content === 'Jellyfin') return true;
    const c = window.ApiClient;
    return !!(
      c &&
      typeof c.serverAddress === 'function' &&
      typeof c.getCurrentUserId === 'function' &&
      typeof c.accessToken === 'function' &&
      typeof c.getJSON === 'function'
    );
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
  // 3. Build payload from Jellyfin ApiClient for current item
  // -----------------------------------------------------------------------
  async function buildPayload(itemId) {
    const c = window.ApiClient;
    const serverAddress = c.serverAddress();
    const accessToken = c.accessToken();
    const userId = c.getCurrentUserId();

    // Fetch item + playback info
    const item = await c.getItem(userId, itemId);
    const playbackInfo = await c.getPlaybackInfo(itemId, {
      UserId: userId,
      MaxStreamingBitrate: 200_000_000,
    });
    const mediaSource = (playbackInfo.MediaSources || [])[0];
    if (!mediaSource) throw new Error('no MediaSource for item');

    // Build direct-stream URL (Jellyfin serves transcoded/remuxed if needed)
    const streamUrl =
      serverAddress +
      '/Videos/' +
      itemId +
      '/stream?Static=true&MediaSourceId=' +
      encodeURIComponent(mediaSource.Id) +
      '&api_key=' +
      encodeURIComponent(accessToken);

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
      startPositionTicks: item.UserData?.PlaybackPositionTicks || 0,
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

  async function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    const itemId = getItemIdFromUrl();
    if (!itemId) {
      toast('No item id detected on this page');
      return;
    }
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
