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
p.add_argument('--new-tab', action='store_true',
               help='open story links in a new tab (default: same tab, so the theme applies right away)')
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
SOLO_CSS = (
    '<style>'
    # "Solo" = opened from a chapter page: only the author sidebar over the story,
    # so the page itself goes transparent and the browse UI is hidden.
    'html.solo,html.solo body{background:transparent!important}'
    'html.solo .wrap{visibility:hidden}'
    'html.solo #author-panel-backdrop.show{opacity:1;pointer-events:auto}'
    # Wider sidebar on story pages (there is plenty of room there); width comes from --solo-panel-w.
    'html.solo .author-panel{width:var(--solo-panel-w,560px);max-width:94vw}'
    '#solo-resize{display:none;position:fixed;top:0;bottom:0;left:calc(min(var(--solo-panel-w,560px),94vw) - 4px);'
    'width:9px;cursor:col-resize;z-index:61;touch-action:none}'
    '#solo-resize::after{content:"";position:absolute;top:50%;left:3px;width:3px;height:44px;margin-top:-22px;'
    'border-radius:2px;background:var(--brass);opacity:0;transition:opacity .15s}'
    '#solo-resize:hover::after,#solo-resize.drag::after{opacity:.9}'
    'html.solo body.author-panel-open #solo-resize{display:block}'
    '@media (max-width:700px){html.solo .author-panel{width:100%;max-width:100%}#solo-resize{display:none!important}}'
    '</style>')
html = sub_once(html, '</head>', SOLO_CSS + '<!--MCO_DATA-->\n</head>')

# 3b. Let the host page open the author sidebar directly, and tell it when it closes.
html = sub_once(
    html,
    "    document.body.classList.remove('author-panel-open');\n  }\n\n  var authorPanelBookmarkBtn",
    "    document.body.classList.remove('author-panel-open');\n"
    "    if (soloMode) window.parent.postMessage({mco:'close'}, '*');\n  }\n\n  var authorPanelBookmarkBtn")
html = sub_once(
    html,
    "  render();\n  } // end init()",
    """  // ---- Hooks for the userscript host page ----
  var soloMode = false;
  function setSolo(on){ soloMode = on; document.documentElement.classList.toggle('solo', on); }
  window.addEventListener('message', function(e){
    var d = e.data;
    if (!d || !d.mco) return;
    if (d.mco === 'browse'){ setSolo(false); closeAuthorPanel(); }
    else if (d.mco === 'author'){
      var want = String(d.name || '').trim().toLowerCase(), found = null;
      stories.forEach(function(st){
        authorsOf(st).forEach(function(a){ if (!found && a.toLowerCase() === want) found = a; });
      });
      if (!found){ window.parent.postMessage({mco:'author-missing'}, '*'); return; }
      setSolo(true);
      openAuthorPanel(found, null);
      window.parent.postMessage({mco:'author-open'}, '*');
    }
  });

  render();
  } // end init()""")

# 4. "← The Archive" becomes a close button for the overlay.
html = sub_once(
    html,
    '<a class="back" href="https://mcstories.com/index.html" target="_blank" rel="noopener noreferrer">← The Archive</a>',
    '<a class="back" id="mco-close-link" href="#">← Back to MCStories</a>')

# 5. Story links open in the current tab by default (the userscript themes the page you land on).
if not a.new_tab:
    html = html.replace('target="_blank"', 'target="_top"')

# 5b. "Read story" goes to chapter 1 (the actual text page); fall back to the story's own page.
html = sub_once(html, "function storyUrl(href){ return BASE_URL + href; }",
  "function storyUrl(href){ return BASE_URL + href; }\n"
  "  function readHref(s){ return (s.chapters && s.chapters.length && s.chapters[0].href) ? s.chapters[0].href : s.href; }")
old_read = "storyUrl(s.href) + '\" target="
n = html.count("storyUrl(s.href) + '\" target=")
if n != 2:
    sys.exit('Expected 2 Read story links, found %d' % n)
html = html.replace("storyUrl(s.href) + '\" target=", "storyUrl(readHref(s)) + '\" target=")

# 6. Bridge script: close link + Escape talk to the host page.
bridge = """
<script>
(function(){
  function closeOverlay(){ window.parent.postMessage({mco:'close'}, '*'); }
  var link = document.getElementById('mco-close-link');
  if (link) link.addEventListener('click', function(e){ e.preventDefault(); closeOverlay(); });
  // ---- Resizable author sidebar (story-page mode) ----
  (function(){
    var KEY = 'storyArchive.authorPanelWidth.v1', root = document.documentElement, MIN = 320, DEFAULT = 560;
    try { DEFAULT = JSON.parse(document.getElementById('mco-data').textContent).panelWidth || DEFAULT; } catch (e) {}
    // The overlay starts hidden, and a hidden frame reports a width of 0: don't clamp against that,
    // and remember the width that was asked for so it can be re-applied once the frame is visible.
    var want = DEFAULT;
    function maxW(){ return window.innerWidth ? Math.min(window.innerWidth * 0.94, 1100) : 1100; }
    function clamp(w){ return Math.max(MIN, Math.min(maxW(), w)); }
    function apply(){ root.style.setProperty('--solo-panel-w', Math.round(clamp(want)) + 'px'); }
    function setW(w){ want = w; apply(); }
    function cur(){ return Math.round(clamp(want)); }
    var saved = NaN; try { saved = parseInt(localStorage.getItem(KEY), 10); } catch (e) {}
    setW(isNaN(saved) ? DEFAULT : saved);

    var h = document.createElement('div');
    h.id = 'solo-resize'; h.title = 'Drag to resize (double-click to reset)';
    document.body.appendChild(h);
    var dragging = false;
    h.addEventListener('pointerdown', function(e){
      dragging = true; h.classList.add('drag'); h.setPointerCapture(e.pointerId);
      document.body.style.userSelect = 'none'; e.preventDefault();
    });
    h.addEventListener('pointermove', function(e){ if (dragging) setW(e.clientX); });
    function end(){
      if (!dragging) return;
      dragging = false; h.classList.remove('drag'); document.body.style.userSelect = '';
      try { localStorage.setItem(KEY, String(cur())); } catch (err) {}
    }
    h.addEventListener('pointerup', end); h.addEventListener('pointercancel', end);
    h.addEventListener('dblclick', function(){ try { localStorage.removeItem(KEY); } catch (err) {} setW(DEFAULT); });
    window.addEventListener('resize', apply);
  })();

  // Capture phase so we see Escape before the author panel's own handler closes it.
  document.addEventListener('keydown', function(e){
    if (e.key !== 'Escape') return;
    if (document.body.classList.contains('author-panel-open')) return; // let the panel close first
    if (document.querySelector('#theme-menu:not([hidden])')) return;   // ...and the theme menu
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
// @version      1.10.0
// @description  Adds a searchable, filterable browser (tags, authors, word counts, bookmarks) over the MCStories archive, plus switchable reading themes.
// @match        *://mcstories.com/*
// @match        *://www.mcstories.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_setClipboard
// @connect      __CONNECT_HOST__
__UPDATE_LINES__// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';
  if (window.top !== window.self) return;

  // ---- Settings -----------------------------------------------------------
  // Where the story metadata (authors, tags, synopses, word counts) lives.
  // Fetched once and cached; no story text is ever downloaded or stored.
  var DATA_URL = __DATA_URL__;
  var CACHE_TTL_MS = 6 * 60 * 60 * 1000; // re-check every 6 hours
  // Width in pixels of the author sidebar when it opens over a story page. You can also drag its
  // right edge to resize it (that choice is remembered; double-click the edge to go back to this).
  var AUTHOR_PANEL_WIDTH = 560;
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


  // ---- Themes -------------------------------------------------------------
  // Every palette fills the same CSS variables. "default" injects nothing at all,
  // so the site looks exactly as it normally does.
  var THEMES = {
    dark:      { label: 'Dark',              scheme: 'dark',  ink: '#15120e', ink2: '#1d1912', line: '#3a3327', text: '#ede3cf', dim: '#c9bfa8', brass: '#a8823e', bright: '#d9b46a', glow: '#241d13', visited: '#b89a60' },
    midnight:  { label: 'Midnight',          scheme: 'dark',  ink: '#0e1420', ink2: '#151d2d', line: '#27334b', text: '#dbe4f3', dim: '#9fb0c9', brass: '#4f7fd6', bright: '#8ab4ff', glow: '#16233a', visited: '#8fa6d1' },
    black:     { label: 'Black',             scheme: 'dark',  ink: '#000000', ink2: '#0e0e0e', line: '#2a2a2a', text: '#e8e8e8', dim: '#a8a8a8', brass: '#808080', bright: '#ffffff', glow: '#0b0b0b', visited: '#bdbdbd' },
    sepia:     { label: 'Sepia',             scheme: 'light', ink: '#f4ecd8', ink2: '#ebe0c6', line: '#d3c4a1', text: '#3b2f22', dim: '#6b5a45', brass: '#9a6b24', bright: '#7a4d12', glow: '#faf4e4', visited: '#8f6a3a' },
    paper:     { label: 'Paper',             scheme: 'light', ink: '#fafaf7', ink2: '#f0f0ea', line: '#d9d9d0', text: '#222222', dim: '#555555', brass: '#7a7a7a', bright: '#2a5db0', glow: '#ffffff', visited: '#6a4c93' },
    'default': { label: 'MCStories default', scheme: 'light', ink: '#ffffff', ink2: '#f2f2f2', line: '#cccccc', text: '#222222', dim: '#555555', brass: '#888888', bright: '#0645ad', glow: '#ffffff', visited: '#0645ad' }
  };
  var THEME_ORDER = ['dark', 'midnight', 'black', 'sepia', 'paper', 'default'];
  var THEME_KEY = 'mco.themeName';
  var current = 'dark';
  try {
    var saved = GM_getValue(THEME_KEY, null);
    if (!saved) saved = GM_getValue('mco.theme', 'on') === 'off' ? 'default' : 'dark'; // older versions
    if (THEMES[saved]) current = saved;
  } catch (e) {}
  var onThemeChange = null; // set by the switcher so it can re-colour itself

  function themeVars(t) {
    return ':root{--mco-ink:' + t.ink + ';--mco-ink2:' + t.ink2 + ';--mco-line:' + t.line + ';--mco-text:' + t.text +
      ';--mco-dim:' + t.dim + ';--mco-brass:' + t.brass + ';--mco-bright:' + t.bright + ';--mco-glow:' + t.glow +
      ';--mco-visited:' + t.visited + ';color-scheme:' + t.scheme + '}';
  }

  var THEME_CSS = [
    'html{background:var(--mco-ink)!important}',
    'body{background:radial-gradient(1200px 600px at 15% -10%,var(--mco-glow) 0%,transparent 60%),var(--mco-ink)!important;' +
      "color:var(--mco-text)!important;font-family:'Source Serif 4',Georgia,serif!important;font-size:1.125rem!important;" +
      'line-height:1.7!important;max-width:44rem!important;margin:0 auto!important;padding:2rem 1.25rem 5rem!important;box-sizing:border-box!important}',
    'body *{background-color:transparent!important;background-image:none!important;color:var(--mco-text)!important;border-color:var(--mco-line)!important;box-shadow:none!important}',
    'body a,body a *{color:var(--mco-bright)!important;text-decoration-color:var(--mco-brass)!important}',
    'body a:visited,body a:visited *{color:var(--mco-visited)!important}',
    'body a:hover,body a:focus,body a:hover *{color:var(--mco-text)!important}',
    "body h1,body h2,body h3,body h4,body h5{font-family:'Fraunces',Georgia,serif!important;font-weight:600!important;line-height:1.2!important}",
    'body .byline,body .byline *{color:var(--mco-dim)!important;font-weight:400!important}',
    'body hr{border:0!important;border-top:1px solid var(--mco-line)!important;height:0!important}',
    'body p{margin:0 0 1.1em!important}',
    'body nav,body #blurb{color:var(--mco-dim)!important;font-size:.9rem!important}',
    'body input,body select,body textarea{background-color:var(--mco-ink2)!important;border:1px solid var(--mco-line)!important;border-radius:4px!important;padding:.3em .5em!important}',
    'body table,body td,body th{border:1px solid var(--mco-line)!important}',
    'body ::selection{background:var(--mco-brass)!important;color:var(--mco-ink)!important}'
  ].join('\n');

  // A chapter/story page is an <article> with a lot of paragraphs and a lot of text.
  // (The home page and listing pages have an article too, but only a handful of paragraphs.)
  function readerArticle() { return document.querySelector('article#mcstories') || document.querySelector('article'); }
  function isReaderPage() {
    var art = readerArticle();
    if (!art) return false;
    return art.querySelectorAll('p').length >= 8 && (art.textContent || '').length > 4000;
  }
  function markReader() {
    try { if (isReaderPage()) document.documentElement.classList.add('mco-reader'); } catch (e) {}
  }

  // Menu command: copies a text-only outline of the page (tags, ids, classes, short heading/link text,
  // never story paragraphs) so layout problems on a particular story can be reported precisely.
  function dumpStructure() {
    var lines = ['URL path: ' + location.pathname, 'title: ' + document.title,
      'story page detected: ' + document.documentElement.classList.contains('mco-reader'),
      'theme: ' + current, ''];
    var SKIP = /^(script|style|link|meta|noscript|iframe|svg|path)$/i;
    function label(el) {
      var t = el.tagName.toLowerCase();
      if (el.id) t += '#' + el.id;
      if (typeof el.className === 'string' && el.className.trim()) t += '.' + el.className.trim().split(/\s+/).join('.');
      return t;
    }
    function isP(l) { return /^p(\.|#|$)/.test(l); }
    function walk(el, depth) {
      var kids = Array.prototype.filter.call(el.children, function (k) {
        return !SKIP.test(k.tagName) && !/^mco-/.test(k.id) && k.id !== 'solo-resize';
      });
      for (var i = 0; i < kids.length; i++) {
        var k = kids[i], lab = label(k), j = i;
        while (isP(lab) && j + 1 < kids.length && label(kids[j + 1]) === lab) j++;
        var n = j - i + 1;
        var line = new Array(depth + 1).join('  ') + lab + (n > 1 ? '  x' + n : '');
        if (n === 1 && !isP(lab) && !k.querySelector('p')) {
          var tx = (k.textContent || '').replace(/\s+/g, ' ').trim();
          if (tx) line += '   "' + tx.slice(0, 40) + '"';
        }
        lines.push(line);
        if (depth < 5 && n === 1 && !isP(lab)) walk(k, depth + 1);
        i = j;
      }
    }
    walk(document.body, 0);
    var text = lines.join('\n');
    var done = function () { alert('Copied the page structure (' + lines.length + ' lines). Paste it into the chat.'); };
    try { GM_setClipboard(text); done(); }
    catch (e) {
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () { window.prompt('Copy this:', text); });
      else window.prompt('Copy this:', text);
    }
  }

  // Move the "← 3 of 4 →" chapter arrows (the nav.arrows above the story) onto the right end of the
  // chapter-name row, so they stop taking a row of their own above the title. Single-chapter stories
  // have nothing but the (hidden) Author link in there, so nothing is moved. The copy below the
  // story is left where it is.
  // Finds the "← 3 of 4 →" chapter control by what it says rather than by its class name: the
  // smallest block around a "N of M" label that is short enough to be a control (not story prose).
  function isControlSized(e) {
    return (e.textContent || '').replace(/\s+/g, ' ').trim().length <= 120 && !e.querySelector('h1,h2,h3,h4,p,article,section');
  }
  function findChapterNav(art) {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null), node;
    while ((node = walker.nextNode())) {
      if (!/\b\d+\s+of\s+\d+\b/i.test(node.nodeValue)) continue;
      var el = node.parentElement;
      if (!el || el.closest('#mco-switcher, #mco-overlay, .mco-chapter-row')) continue;
      if (art.contains(el) && el.closest('p, section, footer')) continue;   // story text, notes, afterword
      if (!isControlSized(el)) continue;
      var box = el;
      while (box.parentElement && box.parentElement !== document.body && box.parentElement !== art &&
             !box.parentElement.contains(art) && isControlSized(box.parentElement)) box = box.parentElement;
      if (box === document.body || box === art || box.contains(art) || art.contains(box) && box.querySelector('.chapter')) continue;
      return box;
    }
    return null;
  }
  // Older guess, kept as a fallback: a nav.arrows above the story that holds a real (non-Author) link.
  function findArrowsNav(art) {
    var navs = document.querySelectorAll('nav.arrows');
    for (var i = 0; i < navs.length; i++) {
      var before = art.compareDocumentPosition(navs[i]) & Node.DOCUMENT_POSITION_PRECEDING;
      if (before && navs[i].querySelector('a[href]:not([href*="/Authors/"])')) return navs[i];
    }
    return null;
  }

  function placeChapterNav() {
    if (current === 'default' || !isReaderPage()) return;
    var art = readerArticle();
    if (!art || art.querySelector('.mco-chapter-row')) return;
    var chapter = art.querySelector(':scope > .chapter');
    if (!chapter) return;
    var nav = findChapterNav(art) || findArrowsNav(art);
    if (!nav) return;                    // single-chapter story: nothing to move
    nav.classList.add('mco-chapter-nav');
    Array.prototype.forEach.call(nav.querySelectorAll('a'), function (a) {   // the Author / Story links stay hidden
      if (/\/Authors\//.test(a.getAttribute('href') || '') || /^\s*(Author|Story):/i.test(a.textContent)) a.classList.add('mco-hide');
    });
    var row = document.createElement('div');
    row.className = 'mco-chapter-row';
    nav.__mcoHome = { parent: nav.parentNode, next: nav.nextSibling };
    chapter.parentNode.insertBefore(row, chapter);
    row.appendChild(chapter);
    row.appendChild(nav);
  }
  function restoreChapterNav() {
    var row = document.querySelector('.mco-chapter-row');
    if (!row) return;
    var chapter = row.querySelector('.chapter'), nav = row.querySelector('.mco-chapter-nav');
    if (nav) {
      nav.classList.remove('mco-chapter-nav');
      Array.prototype.forEach.call(nav.querySelectorAll('.mco-hide'), function (a) { a.classList.remove('mco-hide'); });
      if (nav.__mcoHome && nav.__mcoHome.parent) nav.__mcoHome.parent.insertBefore(nav, nav.__mcoHome.next);
    }
    if (chapter) row.parentNode.insertBefore(chapter, row);
    row.parentNode.removeChild(row);
  }

  // ---- Chapter page: make the author's name in the byline a link ----------
  var openAuthorHook = null; // set once the overlay code is ready

  function enhanceReader() {
    var byline = document.querySelector('article .byline');
    if (!byline || !isReaderPage()) return;
    var spans = byline.querySelectorAll('.docAuthor');
    if (!spans.length) return;

    // Author links the page already carries (hidden by the theme, but still in the DOM).
    var known = {};
    var anchors = document.querySelectorAll('nav.arrows a[href*="/Authors/"], nav a[href*="/Authors/"]');
    Array.prototype.forEach.call(anchors, function (a) {
      var name = (a.textContent || '').replace(/^\s*Authors?:\s*/i, '').trim().toLowerCase();
      if (name && !known[name]) known[name] = a.href;
    });
    var rel = document.querySelectorAll('link[rel~="author"][href]');

    Array.prototype.forEach.call(spans, function (span) {
      if (span.querySelector('a')) return; // already a link
      var name = (span.textContent || '').trim();
      if (!name) return;
      var href = known[name.toLowerCase()];
      if (!href && rel.length === 1 && spans.length === 1) href = rel[0].href;
      if (!href) href = location.origin + '/Authors/' + encodeURIComponent(name.replace(/\s+/g, '-')) + '.html';
      var link = document.createElement('a');
      link.href = href;
      link.title = 'More stories by ' + name;
      while (span.firstChild) link.appendChild(span.firstChild);
      span.appendChild(link);
      // Plain click opens the author sidebar; middle/ctrl/shift-click still use the real link.
      link.addEventListener('click', function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        if (!openAuthorHook || current === 'default') return;
        e.preventDefault();
        openAuthorHook(name, href);
      });
    });
  }

  // ---- Reader layout (chapter pages only) ---------------------------------
  // Applies to story/chapter pages (see isReaderPage). Every other page keeps the plain theme.
  var READER_SCOPES = [
    'body:has(> h1#masthead):has(> article#mcstories > h3.byline)',
    'html.mco-reader body'   // set by isReaderPage() below for stories whose markup differs a little
  ];
  var MONO = "'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace";
  var SERIF = "'Source Serif 4',Georgia,serif";
  var DISPLAY = "'Fraunces',Georgia,serif";
  var READER_RULES = [
    // Page: one centered column, content laid out as wrapping rows.
    ['&', 'display:flex!important;flex-wrap:wrap!important;align-items:center!important;column-gap:.6rem!important;row-gap:.5rem!important;' +
          'max-width:42rem!important;padding:1.25rem 1.25rem 4rem!important'],

    ['& > *', 'flex:0 0 100%!important;min-width:0!important'],

    // The archive title link is hidden.
    ['& h1#masthead', 'display:none!important'],

    // The site nav, the "Story:" link and the "Author:" link are hidden (the author's
    // name in the byline links to their page instead). Any other link inside
    // nav.arrows, such as previous/next chapter, is deliberately left alone.
    ['& nav.site, & nav.story', 'display:none!important'],
    ['& nav.arrows a[href*="/Authors/"]', 'display:none!important'],
    ['& nav.arrows:not(:has(a:not([href*="/Authors/"])))', 'display:none!important'],
    ['& nav.arrows', 'flex:0 0 100%!important;margin:0!important;padding:0!important;text-align:left!important'],

    // Article: a column whose children are re-ordered visually (title first).
    ['& article', 'flex:0 0 100%!important;display:flex!important;flex-direction:column!important;width:auto!important;max-width:none!important;' +
          'float:none!important;margin:0 0 1.75rem!important;padding:0!important;border:0!important'],
    ['& article > *', 'order:5'],
    ['& article > :is(h1,h2,h3).title', 'order:1;margin:0 0 .5rem!important;text-align:left!important;font:600 clamp(2rem,6vw,2.9rem)/1.08 ' + DISPLAY + '!important;letter-spacing:-.015em!important'],
    ['& article > :is(h1,h2,h3,h4,p).byline', 'order:2;margin:0 0 1.1rem!important;text-align:left!important;font:400 1.05rem/1.4 ' + SERIF + '!important;color:var(--mco-dim)!important'],
    ['& article > :is(h1,h2,h3,h4,p).byline .docAuthor', 'color:var(--mco-bright)!important;font-style:italic!important'],
    ['& article > :is(h1,h2,h3,h4,p).byline .docAuthor a, & article > :is(h1,h2,h3,h4,p).byline .docAuthor a:visited', 'color:var(--mco-bright)!important;text-decoration:underline transparent!important;text-underline-offset:.2em!important;transition:text-decoration-color .15s ease'],
    ['& article > :is(h1,h2,h3,h4,p).byline .docAuthor a:hover, & article > :is(h1,h2,h3,h4,p).byline .docAuthor a:focus-visible', 'text-decoration-color:var(--mco-bright)!important'],
    // The archive's age notice: kept visible, shown as a small note under the byline.
    ['& article > header', 'order:3;margin:0 0 .5rem!important;padding:0!important;align-self:flex-start!important'],
    ['& article > header section.disclaimer', 'border:1px solid var(--mco-line)!important;border-radius:8px!important;padding:.55rem .85rem!important'],
    ['& article > header section.disclaimer p', 'margin:0!important;font-size:.8rem!important;line-height:1.5!important;color:var(--mco-dim)!important'],
    ['& article > header + .milestone', 'display:none!important'],
    ['& article > :is(h2,h3,h4).chapter', 'order:4;margin:1rem 0 1.4rem!important;padding-top:1rem!important;border-top:1px solid var(--mco-line)!important;text-align:left!important;' +
          'font:500 .72rem/1.4 ' + MONO + '!important;letter-spacing:.2em!important;text-transform:uppercase!important;color:var(--mco-brass)!important'],

    // Chapter row: "CHAPTER 3:" on the left, the "← 3 of 4 →" arrows on the right (see placeChapterNav).
    ['& article .mco-chapter-row', 'order:4;display:flex!important;flex-wrap:wrap!important;align-items:center!important;justify-content:space-between!important;gap:.5rem 1rem!important;' +
          'margin:1rem 0 1.4rem!important;padding-top:1rem!important;border-top:1px solid var(--mco-line)!important'],
    ['& article .mco-chapter-row > :is(h2,h3,h4).chapter', 'margin:0!important;padding:0!important;border:0!important;text-align:left!important;' +
          'font:500 .72rem/1.4 ' + MONO + '!important;letter-spacing:.2em!important;text-transform:uppercase!important;color:var(--mco-brass)!important'],
    ['& article .mco-chapter-row .mco-chapter-nav', 'flex:0 0 auto!important;display:flex!important;align-items:center!important;gap:.45rem!important;margin:0 0 0 auto!important;padding:0!important;' +
          'text-align:right!important;white-space:nowrap!important;font:500 .78rem/1 ' + MONO + '!important;letter-spacing:.06em!important;color:var(--mco-dim)!important'],
    ['& article .mco-chapter-row .mco-chapter-nav a, & article .mco-chapter-row .mco-chapter-nav a:visited', 'display:inline-block!important;padding:.3rem .65rem!important;border:1px solid var(--mco-line)!important;' +
          'border-radius:999px!important;text-decoration:none!important;color:var(--mco-bright)!important'],
    ['& article .mco-chapter-row .mco-chapter-nav a:hover, & article .mco-chapter-row .mco-chapter-nav a:focus-visible', 'border-color:var(--mco-brass)!important;color:var(--mco-text)!important'],
    ['& .mco-hide, & article .mco-chapter-row .mco-chapter-nav a.mco-hide', 'display:none!important'],

    // The story text.
    ['& article p', 'font-size:1.14rem!important;line-height:1.78!important;margin:0 0 1.2em!important;text-align:left!important;text-indent:0!important;text-wrap:pretty'],

    // End marker and the author's afterword as a quiet card.
    ['& article > .milestone', 'order:6;display:block!important;text-align:center!important;margin:2.25rem 0!important;font-size:1rem!important;letter-spacing:.6em!important;color:var(--mco-brass)!important'],
    ['& article > footer', 'order:7;margin:0!important;padding:0!important;border:0!important'],
    ['& article > footer section.afterword', 'background-color:var(--mco-ink2)!important;border:1px solid var(--mco-line)!important;border-radius:10px!important;padding:1.1rem 1.3rem .3rem!important'],
    ['& article > footer section.afterword p', 'font-size:.95rem!important;line-height:1.65!important;margin:0 0 .9em!important;color:var(--mco-dim)!important'],

    // Bottom of the page: same pills, then a muted site nav and hosting credit.
    ['& nav#blurb', 'flex:0 0 100%!important;text-align:center!important;margin:.25rem 0 0!important;padding:0!important;font:400 .72rem/1.5 ' + MONO + '!important;color:var(--mco-dim)!important;opacity:.7'],
    ['& nav#blurb a, & nav#blurb a:visited', 'color:var(--mco-dim)!important;text-decoration-color:var(--mco-line)!important']
  ];
  // Split a selector list on its top-level commas only (not the ones inside :is(...) / [...]).
  function splitTop(list) {
    var out = [], cur = '', depth = 0;
    for (var i = 0; i < list.length; i++) {
      var c = list.charAt(i);
      if (c === '(' || c === '[') depth++;
      else if (c === ')' || c === ']') depth--;
      if (c === ',' && depth === 0) { out.push(cur); cur = ''; } else cur += c;
    }
    out.push(cur);
    return out;
  }
  var READER_CSS = READER_RULES.map(function (r) {
    var sels = [];
    splitTop(r[0]).forEach(function (sel) {
      READER_SCOPES.forEach(function (scope) { sels.push(sel.trim().replace(/^&/, scope)); });
    });
    return sels.join(',') + '{' + r[1] + '}';
  }).join('\n');
  READER_CSS += '\n@media (max-width:640px){' + READER_SCOPES.join(',') + '{padding-top:3.4rem!important}}';

  function removeTheme() {
    ['mco-theme', 'mco-fonts', 'mco-fonts-pre'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
  }

  function installTheme() {
    if (current === 'default' || document.getElementById('mco-theme')) return;
    var root = document.documentElement;
    if (!root) {
      // Very early in page load the <html> element may not exist yet; wait for it.
      var mo = new MutationObserver(function () {
        if (document.documentElement) { mo.disconnect(); installTheme(); }
      });
      mo.observe(document, { childList: true });
      return;
    }
    var pre = document.createElement('link');
    pre.id = 'mco-fonts-pre'; pre.rel = 'preconnect'; pre.href = 'https://fonts.googleapis.com';
    var fonts = document.createElement('link');
    fonts.id = 'mco-fonts'; fonts.rel = 'stylesheet';
    fonts.href = 'https://fonts.googleapis.com/css2?family=Fraunces:wght@600&family=Source+Serif+4:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap';
    var st = document.createElement('style');
    st.id = 'mco-theme';
    st.textContent = themeVars(THEMES[current]) + '\n' + THEME_CSS + '\n' + READER_CSS;
    root.appendChild(pre); root.appendChild(fonts); root.appendChild(st);
  }

  // Switch theme live (no reload) and remember the choice.
  function applyTheme(name) {
    if (!THEMES[name]) return;
    current = name;
    try { GM_setValue(THEME_KEY, name); } catch (e) {}
    removeTheme();
    installTheme();
    if (current === 'default') restoreChapterNav(); else placeChapterNav();
    if (onThemeChange) onThemeChange();
  }

  installTheme();

  // ---- Overlay ------------------------------------------------------------
  function init() {
  var frame = null;
  var prevOverflow = '';

  function safeJson(obj) {
    return JSON.stringify(obj)
      .replace(/</g, '\\u003c').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
  }

  function buildFrame(payload) {
    var f = document.createElement('iframe');
    f.id = 'mco-overlay';
    f.title = 'MCStories archive browser';
    f.style.cssText = 'position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;border:0!important;' +
      'z-index:2147483647!important;background:#15120e!important;display:none;';
    var withSettings = Object.assign({}, payload, { panelWidth: AUTHOR_PANEL_WIDTH });
    var tag = '<script type="application/json" id="mco-data">' + safeJson(withSettings) + '</scr' + 'ipt>';
    f.srcdoc = APP_HTML.replace('<!--MCO_DATA-->', function () { return tag; });
    document.body.appendChild(f);
    return f;
  }

  var building = null, pendingHref = null, authorTimer = null;

  function setBackground(solo) {
    frame.style.setProperty('background', solo ? 'transparent' : '#15120e', 'important');
  }
  function tell(msg) { try { frame.contentWindow.postMessage(msg, '*'); } catch (e) {} }

  function show() {
    tell({ mco: 'theme', id: current === 'default' ? 'dark' : current });
    frame.style.display = 'block';
    prevOverflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = 'hidden';
    try { frame.contentWindow.focus(); } catch (e) {}
  }

  function hide() {
    if (!frame) return;
    frame.style.display = 'none';
    document.documentElement.style.overflow = prevOverflow;
  }

  // Builds the overlay once (data first). A frame is built even if the data failed,
  // so the browse view can show its own error message.
  function ensureFrame() {
    if (building) return building;
    if (frame) return Promise.resolve({});
    building = getData(false).then(function (payload) {
      return new Promise(function (resolve) {
        frame = buildFrame(payload);
        frame.addEventListener('load', function () { resolve({ error: payload.error }); }, { once: true });
      });
    }).then(function (r) { building = null; return r; });
    return building;
  }

  function open() {
    launcher.textContent = 'Loading…';
    ensureFrame().then(function () {
      setBackground(false);
      tell({ mco: 'browse' });
      show();
    }).finally(function () { launcher.textContent = 'Browse stories'; });
  }

  // Opened from a story page: just the author sidebar over the story. If the author
  // isn't in the data (or it couldn't load) fall back to the normal author page.
  function openAuthor(name, href) {
    pendingHref = href;
    ensureFrame().then(function (r) {
      if (r.error) { location.href = href; return; }
      setBackground(true);
      clearTimeout(authorTimer);
      authorTimer = setTimeout(function () { location.href = href; }, 2500);
      tell({ mco: 'author', name: name });
    });
  }
  openAuthorHook = openAuthor;

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

  // The "Browse stories" button lives in the top-right cluster below (next to the scroll and theme buttons).
  var launcher = null;

  // ---- Theme switcher (top right) -----------------------------------------
  // Lives in a shadow root so neither the site's CSS nor the theme can restyle it.
  var switcherHost = document.createElement('div');
  switcherHost.id = 'mco-switcher';
  switcherHost.style.cssText = 'position:fixed!important;top:14px!important;right:14px!important;z-index:2147483645!important;' +
    'margin:0!important;padding:0!important;background:none!important;border:0!important;width:auto!important;height:auto!important;' +
    'font-size:16px!important;line-height:1!important;max-width:none!important;';
  var sroot = switcherHost.attachShadow({ mode: 'open' });
  sroot.innerHTML =
    '<style>' +
    '*{box-sizing:border-box;font-family:ui-monospace,Menlo,Consolas,monospace}' +
    '.bar{display:flex;flex-direction:row;align-items:center;justify-content:flex-end}' +
    '.browse{height:34px;padding:0 14px;margin-right:8px;border-radius:999px;border:1px solid var(--line);background:var(--bg);color:var(--fg);' +
      'cursor:pointer;font-size:12.5px;letter-spacing:.06em;white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.25)}' +
    '.browse:hover,.browse:focus-visible{border-color:var(--accent);outline:none}' +
    // The scroll-up button sits in a slot that takes no room until it is shown, so "Browse stories" slides left to make space.
    '.slot{display:flex;max-width:0;opacity:0;visibility:hidden;pointer-events:none;transition:max-width .2s ease,opacity .18s ease,visibility .2s}' +
    '.slot.show{max-width:42px;opacity:1;visibility:visible;pointer-events:auto}' +
    '.slot .btn{margin-right:8px;flex:none}' +
    '@media (prefers-reduced-motion:reduce){.slot{transition:none}}' +
    '.btn{width:34px;height:34px;border-radius:50%;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer;' +
      'display:flex;align-items:center;justify-content:center;padding:0;box-shadow:0 4px 14px rgba(0,0,0,.25)}' +
    '.btn:hover,.btn:focus-visible{border-color:var(--accent);outline:none}' +
    '.menu{position:absolute;top:42px;right:0;min-width:236px;background:var(--bg);color:var(--fg);border:1px solid var(--line);' +
      'border-radius:12px;padding:6px;box-shadow:0 12px 32px rgba(0,0,0,.35);display:none}' +
    '.menu.open{display:block}' +
    '.title{font-size:.64rem;letter-spacing:.18em;text-transform:uppercase;color:var(--accent);padding:.45rem .6rem .35rem}' +
    '.item{display:flex;align-items:center;gap:.65rem;width:100%;padding:.5rem .6rem;border:0;border-radius:8px;background:none;' +
      'color:var(--fg);font-size:.78rem;letter-spacing:.02em;cursor:pointer;text-align:left}' +
    '.item:hover,.item:focus-visible{background:var(--hover);outline:none}' +
    '.sw{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);position:relative;flex:none}' +
    '.sw::after{content:"";position:absolute;right:3px;bottom:3px;width:9px;height:9px;border-radius:50%;background:var(--dot)}' +
    '.check{margin-left:auto;opacity:0;color:var(--accent)}' +
    '.item[aria-checked="true"] .check{opacity:1}' +
    '.item span{white-space:nowrap}' +
    '.sep{height:1px;background:var(--line);margin:4px 2px}' +
    '</style>' +
    '<div class="bar">' +
    '<button type="button" class="browse">Browse stories</button>' +
    '<div class="slot"><button type="button" class="btn up" aria-label="Scroll to top" title="Scroll to top">' +
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12 19V6M6.5 11.5 12 6l5.5 5.5"/></svg></button></div>' +
    '<button type="button" class="btn" aria-haspopup="menu" aria-expanded="false" aria-label="Change theme" title="Change theme">' +
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/></svg></button>' +
    '</div>' +
    '<div class="menu" role="menu" aria-label="Theme"><div class="title">Theme</div></div>';
  var sBtn = sroot.querySelector('.btn[aria-haspopup]');
  launcher = sroot.querySelector('.browse');
  launcher.addEventListener('click', open);
  var sMenu = sroot.querySelector('.menu');

  THEME_ORDER.forEach(function (key) {
    var t = THEMES[key];
    if (key === 'default') { var sep = document.createElement('div'); sep.className = 'sep'; sMenu.appendChild(sep); }
    var item = document.createElement('button');
    item.type = 'button'; item.className = 'item'; item.setAttribute('role', 'menuitemradio');
    item.dataset.theme = key;
    item.innerHTML = '<span class="sw" style="background:' + t.ink + ';--dot:' + t.bright + '"></span>' +
      '<span>' + t.label + (key === 'dark' ? ' (default)' : '') + '</span><span class="check" aria-hidden="true">&#10003;</span>';
    sMenu.appendChild(item);
  });

  function paintSwitcher() {
    // Chrome follows the active theme; on the unstyled site it stays a neutral dark pill.
    var t = current === 'default'
      ? { ink2: '#1d1912', text: '#ede3cf', line: '#3a3327', bright: '#d9b46a' }
      : THEMES[current];
    switcherHost.style.setProperty('--bg', t.ink2);
    switcherHost.style.setProperty('--fg', t.text);
    switcherHost.style.setProperty('--line', t.line);
    switcherHost.style.setProperty('--accent', t.bright);
    switcherHost.style.setProperty('--hover', t.line);
    Array.prototype.forEach.call(sMenu.querySelectorAll('.item'), function (it) {
      it.setAttribute('aria-checked', it.dataset.theme === current ? 'true' : 'false');
    });
  }
  onThemeChange = paintSwitcher;
  paintSwitcher();

  function setMenu(open) {
    sMenu.classList.toggle('open', open);
    sBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  sBtn.addEventListener('click', function () { setMenu(!sMenu.classList.contains('open')); });
  sMenu.addEventListener('click', function (e) {
    var item = e.target.closest ? e.target.closest('.item') : null;
    if (!item) return;
    applyTheme(item.dataset.theme);
    setMenu(false);
    sBtn.focus();
  });
  document.addEventListener('click', function (e) {
    if (sMenu.classList.contains('open') && e.composedPath().indexOf(switcherHost) === -1) setMenu(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && sMenu.classList.contains('open')) { setMenu(false); sBtn.focus(); }
  });
  // Scroll-to-top: only shown once the page has been scrolled down a little.
  var sUp = sroot.querySelector('.up');
  var sSlot = sroot.querySelector('.slot');
  var upTicking = false;
  function updateUp() {
    upTicking = false;
    sSlot.classList.toggle('show', (window.pageYOffset || document.documentElement.scrollTop || 0) > 120);
  }
  window.addEventListener('scroll', function () {
    if (!upTicking) { upTicking = true; requestAnimationFrame(updateUp); }
  }, { passive: true });
  sUp.addEventListener('click', function () {
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: 0, behavior: calm ? 'auto' : 'smooth' });
    sUp.blur();
  });
  updateUp();
  document.body.appendChild(switcherHost);

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || !d.mco) return;
    if (d.mco === 'close') hide();
    else if (d.mco === 'theme') { if (d.id && d.id !== 'default' && THEMES[d.id]) applyTheme(d.id); }
    else if (d.mco === 'author-open') { clearTimeout(authorTimer); show(); }
    else if (d.mco === 'author-missing') { clearTimeout(authorTimer); if (pendingHref) location.href = pendingHref; }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && frame && frame.style.display === 'block') hide();
  });

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('Open archive browser', open);
    GM_registerMenuCommand('Refresh story data now', refresh);
    GM_registerMenuCommand('Copy page structure (for bug reports)', dumpStructure);
    GM_registerMenuCommand('Change theme…', function () { setMenu(!sMenu.classList.contains('open')); });
  }
  }

  function onReady() { markReader(); try { enhanceReader(); } catch (e) {} try { placeChapterNav(); } catch (e) {} init(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onReady);
  else onReady();
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