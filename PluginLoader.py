"""
PluginLoader.py  –  Star's Pengu-compatible plugin engine.

How it works:
  Pengu is a DLL that hooks CEF at the C++ level and registers a custom
  https://plugins/ URL scheme, then injects a preload script into V8.
  We can't do that from Python — but the League client exposes a Chrome
  DevTools Protocol (CDP) WebSocket when --remote-debugging-port is set.

  Star uses that CDP socket to:
    1. Find the main League client page target (https://riot:*/index.html)
    2. Call Page.addScriptToEvaluateOnNewDocument — runs before any page JS,
       same timing as Pengu's OnContextCreated hook.
    3. Inject a Pengu-compatible preload: builds window.Pengu.plugins from
       star/plugins/, wires up the rcp intercept, and dynamic-imports each
       plugin via a data: URL (since we can't register https://plugins/).

Requires:
  pip install websocket-client psutil requests
  League must be launched with --remote-debugging-port=<port> in its args,
  OR Pengu's config must have RemoteDebuggingPort set.

The CDP port is auto-detected from the LeagueClientUx.exe command line.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

import psutil
import requests
import urllib3
import websocket

urllib3.disable_warnings()

# ── Pengu-compatible preload template ─────────────────────────────────────────
# Injected before any page script runs (Page.addScriptToEvaluateOnNewDocument).
# PLUGIN_ENTRIES is replaced with a JSON array of {name, code} objects.
# Each plugin's source is inlined as a data: ES module import — this sidesteps
# the need for a custom URL scheme (https://plugins/) that only Pengu's DLL can
# provide.

_PRELOAD_TEMPLATE = r"""
(function () {
  'use strict';

  // ── Pengu-compatible rcp intercept ──────────────────────────────────────
  class RCP {
    static PREF = 'riotPlugin.announce:';
    constructor() {
      const self = this;
      const orig = document.dispatchEvent.bind(document);
      document.dispatchEvent = function (event) {
        if (event.type && event.type.startsWith(RCP.PREF)) self._onAnnounce(event);
        return orig(event);
      };
    }
    _registry = new Map();
    _callbacks = new Map();
    _onAnnounce(event) {
      const name = event.type.slice(RCP.PREF.length);
      const origReg = event.registrationHandler;
      const self = this;
      Object.defineProperty(event, 'registrationHandler', {
        value: function (registrar) {
          return origReg.call(this, async function (provider) {
            const entry = { impl: null, state: 'preInit' };
            self._registry.set(name, entry);
            await self._invoke('before', name, provider);
            entry.state = 'init';
            const api = (entry.impl = await registrar(provider));
            entry.state = 'postInit';
            await self._invoke('after', name, api);
            entry.state = 'fulfilled';
            return api;
          });
        }
      });
    }
    async _invoke(type, name, arg) {
      const cb = this._callbacks.get(name + ':' + type) || [];
      await Promise.allSettled(cb.map(fn => fn(arg)));
    }
    _add(type, name, fn) {
      const k = name + ':' + type;
      const arr = this._callbacks.get(k) || [];
      arr.push(fn); this._callbacks.set(k, arr);
    }
    preInit(name, fn)  { this._add('before', name, fn); return true; }
    postInit(name, fn) { this._add('after',  name, fn); return true; }
    whenReady(name) {
      return new Promise(resolve => {
        const e = this._registry.get(name);
        if (e && e.state === 'fulfilled') return resolve(e.impl);
        this.postInit(name, resolve);
      });
    }
    get(name) { return this._registry.get(name)?.impl; }
  }

  const rcp = new RCP();
  Object.defineProperty(window, 'rcp', { value: rcp, writable: false, configurable: false });

  // ── window.Pengu shim ───────────────────────────────────────────────────
  // Plugins authored for Pengu check window.Pengu.  We expose enough of it
  // so they don't crash.  DataStore is a simple sessionStorage-backed map.
  if (!window.Pengu) {
    const _store = {};
    window.DataStore = {
      has:    k => k in _store,
      get:    (k, fb) => k in _store ? _store[k] : fb,
      set:    (k, v) => { _store[k] = v; return true; },
      remove: k => { delete _store[k]; return true; },
    };
    window.Toast = {
      success: m => console.info('[Star]', m),
      error:   m => console.error('[Star]', m),
      promise: (p) => p,
    };
    window.Pengu = {
      version: '1.1.6-star',
      superPotato: false,
      isMac: false,
      plugins: [],   // filled below
    };
  }

  // ── load-hooks shim ─────────────────────────────────────────────────────
  // Pengu patches addEventListener so late 'load' listeners still fire.
  let _windowLoaded = false;
  window.addEventListener('load', () => { _windowLoaded = true; }, { once: true });
  const _origWinAdd = window.addEventListener.bind(window);
  const _origDocAdd = document.addEventListener.bind(document);
  window.addEventListener = function (type, fn, opts) {
    if (type === 'load' && _windowLoaded) { setTimeout(fn, 1); return; }
    return _origWinAdd(type, fn, opts);
  };
  document.addEventListener = function (type, fn, opts) {
    if (type === 'DOMContentLoaded' && document.readyState !== 'loading') { setTimeout(fn, 1); return; }
    return _origDocAdd(type, fn, opts);
  };

  // ── plugin entries injected by Star ────────────────────────────────────
  // Each entry: { name: string, entry: string, code: string }
  // name  = display name (e.g. "Background-Customizer-V2.js")
  // entry = relative entry path (e.g. "league-theme-mica-main/index.js")
  // code  = full source text of the plugin file
  const PLUGIN_ENTRIES = __PLUGIN_ENTRIES__;

  // ── plugin loader ───────────────────────────────────────────────────────
  async function loadPlugin({ name, entry, code }) {
    try {
      // Inline the source as a blob URL so it looks like a module.
      const blob = new Blob([code], { type: 'text/javascript' });
      const url  = URL.createObjectURL(blob);
      const mod  = await import(url);
      URL.revokeObjectURL(url);

      const ctx = { rcp, socket: null };
      // Pengu passes meta.name for folder-style plugins
      const pluginName = entry.includes('/') ? entry.split('/')[0] : '';
      if (pluginName) ctx.meta = { name: pluginName };

      if (typeof mod.init === 'function')    await mod.init(ctx);
      if (typeof mod.load === 'function')    window.addEventListener('load', mod.load);
      else if (typeof mod.default === 'function') window.addEventListener('load', mod.default);

      console.info('%c Star ', 'background:#c89b3c;color:#000', `Loaded plugin "${name}"`);
    } catch (err) {
      console.error('%c Star ', 'background:#c89b3c;color:#000', `Failed to load plugin "${name}"`, err);
    }
  }

  // wait for rcp-fe-common-libs like Pengu does, then fire plugins
  const allLoaded = Promise.all(PLUGIN_ENTRIES.map(loadPlugin));
  rcp.preInit('rcp-fe-common-libs', async () => { await allLoaded; });

})();
"""

# ── helpers ────────────────────────────────────────────────────────────────────

def _find_cdp_port():
    """Read --remote-debugging-port from LeagueClientUx.exe command line."""
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if proc.info['name'] != 'LeagueClientUx.exe':
                continue
            for arg in (proc.info['cmdline'] or []):
                m = re.search(r'--remote-debugging-port=(\d+)', arg)
                if m:
                    return int(m.group(1))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def _get_main_target(cdp_port):
    """Return the CDP target dict for the main League client page."""
    try:
        targets = requests.get(f'http://127.0.0.1:{cdp_port}/json', timeout=3).json()
        if not isinstance(targets, list):
            return None

        # Primary: exact match for main renderer (https://riot:<port>/index.html)
        for t in targets:
            url = t.get('url', '')
            if re.match(r'https://riot:\d+/index\.html', url):
                return t

        # Fallback: any riot: page target (handles URL variations across League versions)
        for t in targets:
            url = t.get('url', '')
            if url.startswith('https://riot:') and t.get('type') == 'page':
                return t

        # Last resort: first page-type target that isn't blank/devtools
        for t in targets:
            url = t.get('url', '')
            if t.get('type') == 'page' and url and url != 'about:blank' and not url.startswith('devtools://'):
                return t

    except Exception:
        pass
    return None


def _build_preload(plugins_dir: Path) -> str:
    """
    Walk plugins_dir the same way Pengu's C++ does and build the preload
    script with all enabled plugin sources inlined.
    """
    entries = []

    if not plugins_dir.exists():
        return _PRELOAD_TEMPLATE.replace('__PLUGIN_ENTRIES__', '[]')

    def read_plugin(name: str, path: Path, entry: str):
        try:
            code = path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return
        entries.append({'name': name, 'entry': entry, 'code': code})

    # Sub-folder plugins — mirrors get_plugin_entries() in renderer.cc
    for item in sorted(plugins_dir.iterdir()):
        if item.name.startswith(('.', '_')) or not item.is_dir():
            continue
        if item.name.startswith('@'):
            for sub in sorted(item.iterdir()):
                if sub.name.startswith(('.', '_')) or not sub.is_dir():
                    continue
                idx = sub / 'index.js'
                if idx.is_file():
                    read_plugin(f'{item.name}/{sub.name}', idx, f'{item.name}/{sub.name}/index.js')
        else:
            idx = item / 'index.js'
            if idx.is_file():
                read_plugin(item.name, idx, f'{item.name}/index.js')

    # Top-level .js files (only .js, not .js_ — disabled files are skipped)
    for item in sorted(plugins_dir.iterdir()):
        if item.name.startswith(('.', '_')) or item.is_dir():
            continue
        if item.name.endswith('.js') and not item.name.endswith('.js_'):
            read_plugin(item.name, item, item.name)

    payload = json.dumps(entries, ensure_ascii=False)
    return _PRELOAD_TEMPLATE.replace('__PLUGIN_ENTRIES__', payload)


# ── CDP injection ──────────────────────────────────────────────────────────────

class PluginLoader:
    """
    Connects to the League client's CDP endpoint and injects plugins.
    Runs in a background thread; call start() once at app startup.
    """

    def __init__(self, plugins_dir: Path, log_fn=None):
        self.plugins_dir = plugins_dir
        self._log = log_fn or (lambda lvl, msg: None)
        self._ws = None
        self._script_id = None
        self._cdp_port = None
        self._running = False
        self._lock = threading.Lock()
        self._msg_id = 1
        self._pending = {}   # id → Event + result placeholder

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name='StarPluginLoader').start()

    def stop(self):
        self._running = False
        self._disconnect()

    def reload(self):
        """Re-inject plugins (call after toggling a plugin on/off)."""
        threading.Thread(target=self._inject, daemon=True).start()

    # ── internal ────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            port = _find_cdp_port()
            if port and port != self._cdp_port:
                self._cdp_port = port
                self._log('system', f'League CDP detected on port {port}')
                # Retry injection a few times — the main page may not be loaded yet
                # when the CDP port first appears (League UX starts before rendering index.html)
                for attempt in range(5):
                    self._inject()
                    if self._script_id:
                        break
                    if attempt < 4:
                        time.sleep(3)
            elif port and self._cdp_port and not self._script_id:
                # Port known but no script injected yet — keep retrying
                self._inject()
            elif not port and self._cdp_port:
                self._cdp_port = None
                self._script_id = None
                self._disconnect()
                self._log('warning', 'League client closed — plugin loader standby')
            time.sleep(4)

    def _inject(self):
        if not self._cdp_port:
            return
        with self._lock:
            try:
                target = _get_main_target(self._cdp_port)
                if not target:
                    self._log('warning', 'Plugin loader: main League page not found yet')
                    return

                ws_url = target.get('webSocketDebuggerUrl')
                if not ws_url:
                    self._log('error', 'Plugin loader: no WebSocket URL on target')
                    return

                self._disconnect()
                self._ws = websocket.create_connection(ws_url, timeout=5)

                preload = _build_preload(self.plugins_dir)

                # Remove old script if we re-injecting
                if self._script_id:
                    self._send_cmd('Page.removeScriptToEvaluateOnNewDocument',
                                   {'identifier': self._script_id})
                    self._script_id = None

                # Enable Runtime domain so evaluate works
                self._send_cmd('Runtime.enable', {})

                # Inject preload — runs before any page JS on next navigation.
                # Note: 'runImmediately' is not a standard CDP param; omit it to
                # stay compatible with the Chromium version League ships.
                res = self._send_cmd('Page.addScriptToEvaluateOnNewDocument',
                                     {'source': preload})
                if res and 'identifier' in res:
                    self._script_id = res['identifier']

                # Also evaluate immediately into the current page context so
                # plugins activate without requiring a page reload.
                self._send_cmd('Runtime.evaluate', {
                    'expression': preload,
                    'awaitPromise': False,
                    'silent': True,
                    'returnByValue': False,
                })

                # Count plugins by parsing the inlined entries array
                # _build_preload replaces __PLUGIN_ENTRIES__ with a JSON array, so count '"entry":' occurrences
                count = preload.count('"entry":')
                self._log('system', f'Plugin loader: injected {count} plugin(s) into League client')

            except Exception as e:
                self._log('error', f'Plugin loader error: {e}')
                self._disconnect()

    def _send_cmd(self, method: str, params: dict) -> dict | None:
        if not self._ws:
            return None
        mid = self._msg_id
        self._msg_id += 1
        msg = json.dumps({'id': mid, 'method': method, 'params': params})
        try:
            self._ws.send(msg)
            # Read responses until we get ours (skip events)
            for _ in range(20):
                raw = self._ws.recv()
                data = json.loads(raw)
                if data.get('id') == mid:
                    return data.get('result', {})
        except Exception:
            pass
        return None

    def _disconnect(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
