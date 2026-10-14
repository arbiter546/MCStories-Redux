#!/usr/bin/env python3
"""Build mcstories-browser.user.js from the site's index.html.

Usage:
    python3 build_userscript.py index.html --data-url https://your.site/stories.json -o mcstories-browser.user.js
"""
import argparse, json, re, sys
from urllib.parse import urlparse

p = argparse.ArgumentParser()
p.add_argument('html')
p.add_argument('--data-url', default='https://YOUR-SITE.example/stories.json')
p.add_argument('-o', '--out', default='mcstories-browser.user.js')
p.add_argument('--update-url', default='',
               help='raw URL where the built .user.js is hosted; enables Tampermonkey auto-update')
p.add_argument('--same-tab', action='store_true',
               help='open story links in the current tab instead of a new one')
a = p.parse_args()

html = open(a.html, encoding='utf-8').read().replace('\r\n', '\n')


def sub_once(src, old, new):
    if src.count(old) != 1:
        sys.exit('Patch target not found exactly once (page changed?):\n' + old[:120])
    return src.replace(old, new)


# 1. Data comes from an embedded JSON block instead of fetch('stories.json').
old_fetch = re.search(
    r"fetch\('stories\.json'\)\s*\.then\(function\(r\)\{.*?return r\.json\(\);\s*\}\)\s*\.then\(function\(data\)\{",
    html, re.S)
if not old_fetch:
    sys.exit('Could not find the fetch(stories.json) block')
html = html.replace(old_fetch.group(0), """Promise.resolve()
    .then(function(){
      var payload = JSON.parse(document.getElementById('mco-data').textContent);
      if (payload.error) throw new Error(payload.error);
      return payload.data;
    })
    .then(function(data){""")

# 2. The "serve it from a local web server" hint makes no sense here.
html = sub_once(
    html,
    """'). If you\\u2019re opening this file directly from disk, try serving it from a local web server instead \\u2014 ' +
        'browsers block this kind of file loading for security reasons when a page is opened as a plain file.</div>';""",
    """'). Check that the data URL in the userscript is reachable.</div>';""")

# 3. Placeholder where the host script injects the data.
html = sub_once(html, '</head>', '<!--MCO_DATA-->\n</head>')

# 4. "← The Archive" becomes a close button for the overlay.
html = sub_once(
    html,
    '<a class="back" href="https://mcstories.com/index.html" target="_blank" rel="noopener noreferrer">← The Archive</a>',
    '<a class="back" id="mco-close-link" href="#">← Back to MCStories</a>')

# 5. Optional: story links open in the current tab.
if a.same_tab:
    html = html.replace('target="_blank"', 'target="_top"')

# 6. Bridge script: close link + Escape talk to the host page.
bridge = """
<script>
(function(){
  function closeOverlay(){ window.parent.postMessage({mco:'close'}, '*'); }
  var link = document.getElementById('mco-close-link');
  if (link) link.addEventListener('click', function(e){ e.preventDefault(); closeOverlay(); });
  // Capture phase so we see Escape before the author panel's own handler closes it.
  document.addEventListener('keydown', function(e){
    if (e.key !== 'Escape') return;
    if (document.body.classList.contains('author-panel-open')) return; // let the panel close first
    closeOverlay();
  }, true);
})();
</script>
</body>"""
html = sub_once(html, '</body>', bridge)

host = urlparse(a.data_url).hostname or 'YOUR-SITE.example'

TEMPLATE = r"""// ==UserScript==
// @name         MCStories Archive Browser
// @namespace    https://github.com/your-name/mcstories-browser
// @version      1.0.0
// @description  Adds a searchable, filterable browser (tags, authors, word counts, bookmarks) over the MCStories archive.
// @match        *://mcstories.com/*
// @match        *://www.mcstories.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      __CONNECT_HOST__
__UPDATE_LINES__// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';
  if (window.top !== window.self) return;

  // ---- Settings -----------------------------------------------------------
  // Where the story metadata (authors, tags, synopses, word counts) lives.
  // Fetched once and cached; no story text is ever downloaded or stored.
  var DATA_URL = __DATA_URL__;
  var CACHE_TTL_MS = 6 * 60 * 60 * 1000; // re-check every 6 hours
  var CACHE_KEY = 'mco.data.v1';

  var APP_HTML = __APP_HTML__;

  // ---- Data loading -------------------------------------------------------
  function fetchData() {
    return new Promise(function (resolve, reject) {
      GM_xmlhttpRequest({
        method: 'GET',
        url: DATA_URL,
        timeout: 30000,
        onload: function (r) {
          if (r.status < 200 || r.status >= 300) return reject(new Error('HTTP ' + r.status));
          try { resolve(JSON.parse(r.responseText)); } catch (e) { reject(new Error('Bad JSON')); }
        },
        onerror: function () { reject(new Error('Network error')); },
        ontimeout: function () { reject(new Error('Timed out')); }
      });
    });
  }

  function getData(force) {
    var cached = null;
    try { cached = GM_getValue(CACHE_KEY, null); } catch (e) {}
    if (!force && cached && cached.data && Date.now() - cached.t < CACHE_TTL_MS) {
      return Promise.resolve({ data: cached.data });
    }
    return fetchData().then(function (data) {
      try { GM_setValue(CACHE_KEY, { t: Date.now(), data: data }); } catch (e) {}
      return { data: data };
    }).catch(function (err) {
      // Fall back to stale cache rather than showing nothing.
      if (cached && cached.data) return { data: cached.data };
      return { error: err.message };
    });
  }

  // ---- Overlay ------------------------------------------------------------
  var frame = null, opening = false;
  var prevOverflow = '';

  function safeJson(obj) {
    return JSON.stringify(obj)
      .replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
  }

  function buildFrame(payload) {
    var f = document.createElement('iframe');
    f.id = 'mco-overlay';
    f.title = 'MCStories archive browser';
    f.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;border:0;' +
      'z-index:2147483647;background:#15120e;display:none;';
    var tag = '<script type="application/json" id="mco-data">' + safeJson(payload) + '</scr' + 'ipt>';
    f.srcdoc = APP_HTML.replace('<!--MCO_DATA-->', function () { return tag; });
    document.body.appendChild(f);
    return f;
  }

  function show() {
    frame.style.display = 'block';
    prevOverflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = 'hidden';
    launcher.style.display = 'none';
    try { frame.contentWindow.focus(); } catch (e) {}
  }

  function hide() {
    if (!frame) return;
    frame.style.display = 'none';
    document.documentElement.style.overflow = prevOverflow;
    launcher.style.display = '';
  }

  function open() {
    if (frame) return show();
    if (opening) return;
    opening = true;
    launcher.textContent = 'Loading…';
    getData(false).then(function (payload) {
      frame = buildFrame(payload);
      frame.addEventListener('load', function () { show(); }, { once: true });
    }).finally(function () {
      opening = false;
      launcher.textContent = 'Browse stories';
    });
  }

  function refresh() {
    var wasOpen = frame && frame.style.display === 'block';
    if (frame) { frame.remove(); frame = null; }
    launcher.textContent = 'Refreshing…';
    getData(true).then(function (payload) {
      frame = buildFrame(payload);
      frame.addEventListener('load', function () {
        if (wasOpen) show();
        launcher.textContent = 'Browse stories';
      }, { once: true });
    });
  }

  // ---- Launcher button ----------------------------------------------------
  var launcher = document.createElement('button');
  launcher.type = 'button';
  launcher.textContent = 'Browse stories';
  launcher.style.cssText =
    'position:fixed;right:18px;bottom:18px;z-index:2147483646;padding:.55em 1.1em;' +
    'font:500 13px/1.2 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.06em;' +
    'color:#ede3cf;background:#1d1912;border:1px solid #a8823e;border-radius:999px;' +
    'cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.35);';
  launcher.addEventListener('mouseenter', function () { launcher.style.borderColor = '#d9b46a'; });
  launcher.addEventListener('mouseleave', function () { launcher.style.borderColor = '#a8823e'; });
  launcher.addEventListener('click', open);
  document.body.appendChild(launcher);

  window.addEventListener('message', function (e) {
    if (e.data && e.data.mco === 'close') hide();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && frame && frame.style.display === 'block') hide();
  });

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('Open archive browser', open);
    GM_registerMenuCommand('Refresh story data now', refresh);
  }
})();
"""

upd = ('// @updateURL    %s\n// @downloadURL  %s\n' % (a.update_url, a.update_url)) if a.update_url else ''
out = (TEMPLATE
       .replace('__UPDATE_LINES__', upd)
       .replace('__CONNECT_HOST__', host)
       .replace('__DATA_URL__', json.dumps(a.data_url))
       .replace('__APP_HTML__', json.dumps(html, ensure_ascii=False)))
open(a.out, 'w', encoding='utf-8').write(out)
print('wrote', a.out, len(out) // 1024, 'KB')
