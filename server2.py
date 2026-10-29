#!/usr/bin/env python3

# DT Mini Browser Server
# Copyright (C) 2026 DanlTech
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
DT Mini Browser Server - Server-seitig rendernder Browser-Server (Playwright + aiohttp)

Der Server laedt Seiten in einem echten Chromium (inkl. JS/CSS), liest das fertige
Layout aus und schickt dem Client eine kompakte "Display List" als gzip-JSON.
Der Client (Android) zeichnet sie nur noch.

Endpunkte (alle GET, Antwort = JSON "Seitenzustand", ausser /img und /ping):
  /ping
  /load?url=..&w=360&h=640&dpr=1.5[&sid=..]   Seite laden (legt Sitzung an, falls sid fehlt)
  /tap?sid=..&x=..&y=..                       Tap an Seitenkoordinate (CSS-px)
  /type?sid=..&t=text[&enter=1]               Text ins fokussierte Feld (ersetzt Inhalt)
  /back?sid=..                                Zurueck
  /state?sid=..                               Aktuellen Zustand neu auslesen (z.B. nach JS-Updates)
  /img?sid=..&id=..&tw=320&q=40               Bild verkleinert als JPEG (204 = nicht darstellbar)

Seitenzustand:
  {"v":1,"sid":"..","url":"..","title":"..","w":360,"h":4200,"bg":4294967295,
   "items":[...],"focus":null|{"type":"text","value":"..","ph":".."}, "err":".." (optional)}

Display-List-Items (Koordinaten in CSS-px, Seitenkoordinaten; Farben = ARGB als unsigned int):
  ["r", x,y,w,h, argb, radius]                         gefuelltes Rechteck (auch Rahmenlinien)
  ["t", x,y,w,h, text, argb, fontPx, flags]            eine Textzeile; flags: 1 fett, 2 kursiv,
                                                        4 unterstrichen, 8 durchgestrichen, 16 monospace
  ["i", x,y,w,h, imgId, mode(0 strecken,1 cover) [,[cx,cy,cw,ch]]]   Bild (optional mit Clip-Rechteck)
  ["a", x,y,w,h, href]                                 Link-Bereich
  ["f", x,y,w,h, kind, text, checked, isPlaceholder, argb, fontPx]   Formularfeld
Reihenfolge = Zeichenreihenfolge (spaetere Items liegen oben).

Umgebungsvariablen: MINI_PORT, MINI_HOST, MINI_CHROME (Pfad zu chromium), MINI_LOCALE,
                    MINI_FALLBACK_LANG (en|de, default en), MINI_SHOT=0 (kein Referenz-Screenshot),
                    MINI_ALLOW_PRIVATE=1 (interne Adressen erlauben)
Client kann ?lang=de|en senden; sonst MINI_FALLBACK_LANG.
"""

import asyncio
import base64
import gzip
import io
import ipaddress
import json
import logging
import os
import socket
import textwrap
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes, urlparse

from aiohttp import web
from PIL import Image
from playwright.async_api import async_playwright

from i18n import t, normalize_lang, FALLBACK_LANG

HOST = os.environ.get("MINI_HOST", "0.0.0.0")
PORT = int(os.environ.get("MINI_PORT", "8080"))
CHROME = os.environ.get("MINI_CHROME", "")
LOCALE = os.environ.get("MINI_LOCALE", "en-US")
SHOT = os.environ.get("MINI_SHOT", "1") != "0"
ALLOW_PRIVATE = os.environ.get("MINI_ALLOW_PRIVATE") == "0"
MAX_SESSIONS = 4
IDLE_SECONDS = 600
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)
STATES_DIR = Path("states")
STATES_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
MAX_DOWNLOAD_MB = 80

# Fallback, wird beim Start durch echte Chromium-Version ersetzt
CHROME_VER = "156.0.0.0"
UA_PRODUCT = "DTMiniBrowser-Alpha/0.9-r4"
DTMB_VER = "0.9-r4"

def build_ua(android_ver="13", model="Pixel 7"):
    """Baut einen Mobile-Chrome-UA mit echter Chrome-Version + Client-Gerätedaten."""
    model = (model or "Pixel 7").replace(";", " ").strip()[:40] or "Pixel 7"
    android_ver = (android_ver or "13").strip()[:12] or "13"
    return (f"Mozilla/5.0 (Linux; Android {android_ver}; {model}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{CHROME_VER} Mobile Safari/537.36 {UA_PRODUCT}")

UA = build_ua()  # Default bis Client-Daten kommen
BLOCK_HOSTS = ("doubleclick.net", "googlesyndication.com", "google-analytics.com",
               "googletagmanager.com", "googleadservices.com", "adservice.google.",
               "facebook.net", "taboola.com", "outbrain.com", "scorecardresearch.com",
               "hotjar.com", "criteo.", "adnxs.com", "amazon-adsystem.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mini")

# --------------------------------------------------------------------------- JS im Browser

INIT_JS = r"""
(() => {
  document.addEventListener('click', e => {
    const a = e.target && e.target.closest && e.target.closest('a[target]');
    if (a) a.target = '_self';
  }, true);
  window.open = (u) => { if (u) location.href = u; return null; };
})();
"""

EXTRACT_JS = r"""
async () => {
  let items = [];
  const sx = scrollX, sy = scrollY, R = Math.round;
  const VW = innerWidth, VH = innerHeight;
  // Overflow-Vollinhalt-Fix hilft Wikipedia, zerlegt aber Amazon-Layout → nur nicht-Amazon
  const expandScrollOverflow = !/amazon\./i.test(location.hostname || '');
  const SKIP = new Set(['script','style','noscript','template','iframe','svg','video','audio',
                        'canvas','object','embed','head','link','meta','title']);

  const cache = new Map();
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', {willReadFrequently: true});
  const col = (s) => {
    if (!s || s === 'transparent' || s === 'rgba(0, 0, 0, 0)') return 0;
    let v = cache.get(s);
    if (v !== undefined) return v;
    cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#000'; cx.fillStyle = s; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data;
    v = ((d[3] << 24) | (d[0] << 16) | (d[1] << 8) | d[2]) >>> 0;
    cache.set(s, v);
    return v;
  };
  const inter = (a, c) => {
    if (!c) return a;
    const x1 = Math.max(a[0], c[0]), y1 = Math.max(a[1], c[1]);
    const x2 = Math.min(a[0] + a[2], c[0] + c[2]), y2 = Math.min(a[1] + a[3], c[1] + c[3]);
    return (x2 > x1 && y2 > y1) ? [x1, y1, x2 - x1, y2 - y1] : null;
  };
  const box = (el) => { const b = el.getBoundingClientRect(); return [b.x + sx, b.y + sy, b.width, b.height]; };
  const rect = (bx, clip, argb, rad) => {
    const v = inter(bx, clip);
    if (!v) return;
    items.push(['r', R(v[0]), R(v[1]), Math.max(1, R(v[2])), Math.max(1, R(v[3])), argb, R(rad || 0)]);
  };

  const absUrl = (u) => {
    if (!u || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('http')) return u;
    try { return new URL(u, document.baseURI).href; } catch (e) { return u; }
  };

  // ---- Hilfsfunktionen fuer CSS-Werte (Hintergrundbilder, object-position, ...)
  const splitTop = (s) => {            // an Kommas auf oberster Ebene trennen
    const out = []; let depth = 0, q = '', cur = '';
    s = s || '';
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (q) { cur += ch; if (ch === '\\') cur += s[++i] || ''; else if (ch === q) q = ''; continue; }
      if (ch === '"' || ch === "'") { q = ch; cur += ch; continue; }
      if (ch === '(') depth++; else if (ch === ')') depth--;
      if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; } else cur += ch;
    }
    if (cur.trim()) out.push(cur.trim());
    return out;
  };
  const tokens = (s) => {              // an Leerzeichen auf oberster Ebene trennen
    const out = []; let depth = 0, cur = '';
    for (const ch of (s || '').trim()) {
      if (ch === '(') depth++; else if (ch === ')') depth--;
      if (/\s/.test(ch) && depth === 0) { if (cur) out.push(cur); cur = ''; } else cur += ch;
    }
    if (cur) out.push(cur);
    return out;
  };
  const evalLen = (tok, base) => {     // px / % / calc(..) -> px; null bei auto/unbekannt
    if (!tok || tok === 'auto') return null;
    const re = /([+-]?)\s*(\d*\.?\d+(?:e[+-]?\d+)?)(%|px)?/gi;
    let m, tot = 0, any = false;
    while ((m = re.exec(tok))) {
      let v = parseFloat(m[2]); if (m[1] === '-') v = -v;
      tot += m[3] === '%' ? base * v / 100 : v; any = true;
    }
    return any ? tot : null;
  };
  const pick = (arr, i) => arr[i % arr.length];
  const inset = (bx, mode, cs) => {    // border-/padding-/content-box eines Elements
    if (!mode || mode === 'border-box' || mode === 'text') return bx;
    const pf = (p) => parseFloat(cs[p]) || 0;
    let l = pf('borderLeftWidth'), t = pf('borderTopWidth'), r = pf('borderRightWidth'), b = pf('borderBottomWidth');
    if (mode === 'content-box') {
      l += pf('paddingLeft'); t += pf('paddingTop'); r += pf('paddingRight'); b += pf('paddingBottom');
    }
    return [bx[0] + l, bx[1] + t, Math.max(0, bx[2] - l - r), Math.max(0, bx[3] - t - b)];
  };

  // Natuerliche Bildgroesse (fuer Sprites): sofort, wenn im Cache; sonst nachladen + zweiter Durchlauf
  const nat = new Map();
  const pendingImgs = new Map();
  const natural = (u) => {
    if (nat.has(u)) return nat.get(u);
    if (pendingImgs.has(u)) return null;
    const im = new Image();
    im.src = u;
    if (im.complete) {
      const v = im.naturalWidth > 0 && im.naturalHeight > 0 ? [im.naturalWidth, im.naturalHeight] : null;
      nat.set(u, v);
      return v;
    }
    pendingImgs.set(u, im);
    return null;
  };

  // Bild-Item; Clip nur wenn das Bild ueber den sichtbaren Bereich hinausragt
  const pushImg = (u, x, y, w, h, mode, vis) => {
    const it = ['i', R(x), R(y), Math.max(1, R(w)), Math.max(1, R(h)), absUrl(u), mode];
    const inside = vis[0] <= x + 0.5 && vis[1] <= y + 0.5 &&
                   x + w <= vis[0] + vis[2] + 0.5 && y + h <= vis[1] + vis[3] + 0.5;
    if (!inside) it.push([R(vis[0]), R(vis[1]), R(vis[2]), R(vis[3])]);
    items.push(it);
  };

  // (Ggf. gekacheltes) Bild platzieren. orig = Positionierungsbereich, vis = sichtbarer Bereich
  const placeTiles = (u, orig, vis, tw, th, px, py, rx, ry) => {
    const rng = (p, t, lo, hi, rep) => {
      if (!rep) return [p];
      const k0 = Math.floor((lo - p) / t), k1 = Math.ceil((hi - p) / t) - 1;
      const out = [];
      for (let k = k0; k <= k1; k++) { out.push(p + k * t); if (out.length > 40) break; }
      return out;
    };
    const xr = rng(px, tw, vis[0] - orig[0], vis[0] + vis[2] - orig[0], rx);
    const yr = rng(py, th, vis[1] - orig[1], vis[1] + vis[3] - orig[1], ry);
    if (xr.length * yr.length > 36) return;   // feine Muster-Kacheln: nicht darstellbar -> weglassen
    for (const yy of yr) for (const xx of xr) {
      const X = orig[0] + xx, Y = orig[1] + yy;
      if (X + tw <= vis[0] + 0.5 || X >= vis[0] + vis[2] - 0.5 ||
          Y + th <= vis[1] + 0.5 || Y >= vis[1] + vis[3] - 0.5) continue;
      pushImg(u, X, Y, tw, th, 0, vis);
    }
  };

  // Alle url()-Hintergrundebenen eines Elements (background-size/-position/-repeat/-origin/-clip)
  const bgImages = (cs, bx, clip) => {
    const bi = cs.backgroundImage;
    if (!bi || bi === 'none' || !bi.includes('url(')) return;
    const layers = splitTop(bi), n = layers.length;
    const sizes = splitTop(cs.backgroundSize || 'auto'), poss = splitTop(cs.backgroundPosition || '0% 0%'),
          reps = splitTop(cs.backgroundRepeat || 'repeat'), origins = splitTop(cs.backgroundOrigin || 'padding-box'),
          clips = splitTop(cs.backgroundClip || 'border-box');
    for (let i = n - 1; i >= 0; i--) {          // letzte Ebene liegt ganz unten
      const m = /url\(\s*(["']?)(.*?)\1\s*\)/.exec(layers[i]);
      if (!m || !m[2]) continue;
      const u = absUrl(m[2]);
      const orig = inset(bx, pick(origins, i), cs);
      const vis = inter(inset(bx, pick(clips, i), cs), clip);
      if (!vis || orig[2] < 1 || orig[3] < 1) continue;
      const st = tokens(pick(sizes, i));
      const pt = tokens(pick(poss, i));
      const pxTok = pt[0] || '0%', pyTok = pt[1] || '50%';
      const kw = (st[0] === 'cover' || st[0] === 'contain') ? st[0] : '';
      const big = orig[2] >= 16 && orig[3] >= 16;
      let tw, th;
      if (kw) {
        if (kw === 'cover' && pxTok === '50%' && pyTok === '50%') {   // zentriert: Client macht Cover selbst
          pushImg(u, orig[0], orig[1], orig[2], orig[3], 1, vis);
          continue;
        }
        const nz = natural(u);
        if (!nz) { if (big) pushImg(u, orig[0], orig[1], orig[2], orig[3], kw === 'cover' ? 1 : 0, vis); continue; }
        const s = kw === 'cover' ? Math.max(orig[2] / nz[0], orig[3] / nz[1])
                                 : Math.min(orig[2] / nz[0], orig[3] / nz[1]);
        tw = nz[0] * s; th = nz[1] * s;
      } else {
        let sw = evalLen(st[0], orig[2]), sh = evalLen(st[1] || 'auto', orig[3]);
        if (sw === null || sh === null) {
          const nz = natural(u);
          if (!nz) { if (big) pushImg(u, orig[0], orig[1], orig[2], orig[3], 0, vis); continue; }
          if (sw === null && sh === null) { sw = nz[0]; sh = nz[1]; }
          else if (sw === null) sw = sh * nz[0] / nz[1];
          else sh = sw * nz[1] / nz[0];
        }
        tw = sw; th = sh;
      }
      if (!(tw >= 1 && th >= 1)) continue;
      const px = evalLen(pxTok, orig[2] - tw) ?? 0, py = evalLen(pyTok, orig[3] - th) ?? 0;
      const rt = tokens(pick(reps, i));
      let rx, ry;
      if (rt[0] === 'repeat-x') { rx = true; ry = false; }
      else if (rt[0] === 'repeat-y') { rx = false; ry = true; }
      else { rx = rt[0] !== 'no-repeat'; ry = (rt[1] || rt[0]) !== 'no-repeat'; }
      placeTiles(u, orig, vis, tw, th, px, py, rx, ry);
    }
  };

  const text = (node, cs, clip) => {
    const raw = node.nodeValue;
    if (!raw || !raw.trim()) return;
    const color = col(cs.color);
    if (!(color >>> 24)) return;
    const fs = parseFloat(cs.fontSize) || 16;
    let flags = 0;
    if (parseInt(cs.fontWeight) >= 600 || cs.fontWeight === 'bold') flags |= 1;
    if (cs.fontStyle === 'italic' || cs.fontStyle === 'oblique') flags |= 2;
    const td = cs.textDecorationLine || '';
    if (td.includes('underline')) flags |= 4;
    if (td.includes('line-through')) flags |= 8;
    if (/mono|courier|consolas/i.test(cs.fontFamily)) flags |= 16;
    const tt = cs.textTransform;
    const fixCase = (s) => {
      if (tt === 'uppercase') return s.toUpperCase();
      if (tt === 'lowercase') return s.toLowerCase();
      if (tt === 'capitalize') return s.replace(/(^|\s)(\S)/g, (a, b, c) => b + c.toUpperCase());
      return s;
    };
    // Wichtig: Box = exakt von erstem bis letztem WORT. Die Range-Rects enthalten sonst
    // Leerzeichen am Rand (vor/nach <b>, <a>, ...) und der Client wuerde den (getrimmten)
    // Text auf diese zu breite Box strecken -> Woerter kleben aneinander. Der Client skaliert
    // die Textbreite selbst auf die Box, daher darf hier KEIN Extra-Padding drauf.
    const range = document.createRange();
    range.selectNodeContents(node);
    let rects = [];
    try { rects = Array.from(range.getClientRects()); } catch (e) {}
    const words = [];
    const re = /\S+/g; let m;
    while ((m = re.exec(raw))) {
      range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
      const rr = range.getBoundingClientRect();
      if (rr.width >= 0.5 && rr.height >= 1)
        words.push({t: m[0], top: rr.top, left: rr.left, right: rr.right, bottom: rr.bottom, used: false});
    }
    if (!words.length) return;
    const emit = (left, top, right, height, lw) => {
      const bl = [left + sx, top + sy, right - left, height];
      if (!inter(bl, clip)) return;
      const s = fixCase(lw.map(w => w.t).join(' '));
      items.push(['t', R(bl[0]), R(bl[1]), Math.max(1, R(bl[2])), R(bl[3]), s, color, Math.round(fs * 10) / 10, flags]);
    };
    if (rects.length > 0 && rects.length <= 40) {
      for (const r of rects) {
        if (r.width < 0.5 || r.height < 1) continue;
        const midY = r.top + r.height * 0.5;
        const lw = words.filter(w => !w.used && w.top < midY && w.bottom > midY &&
                                     w.left < r.right + 2 && w.right > r.left - 2);
        if (!lw.length) continue;
        lw.sort((a, b) => a.left - b.left);
        let right = -Infinity;
        for (const w of lw) { w.used = true; if (w.right > right) right = w.right; }
        emit(lw[0].left, r.top, right, r.height, lw);
      }
    } else {
      // Fallback: Woerter zu Zeilen gruppieren
      const lines = [];
      for (const w of words) {
        const L = lines.length ? lines[lines.length - 1] : null;
        const tol = Math.min(w.bottom - w.top, L ? (L.bottom - L.top) : (w.bottom - w.top)) * 0.35;
        if (L && Math.abs(w.top - L.top) < tol && w.left >= L.left - 1) {
          L.words.push(w); L.right = Math.max(L.right, w.right); L.bottom = Math.max(L.bottom, w.bottom);
        } else {
          lines.push({top: w.top, left: w.left, right: w.right, bottom: w.bottom, words: [w]});
        }
      }
      for (const L of lines) emit(L.left, L.top, L.right, L.bottom - L.top, L.words);
    }
  };

  const decorate = (el, cs, bx, clip) => {
    const ln = el.localName;
    const bg = col(cs.backgroundColor);
    const rad = parseFloat(cs.borderTopLeftRadius) || 0;
    // Icons per mask-image (z.B. Wikipedia): Der Client kann nicht maskieren, die einfarbige
    // Flaeche waere nur ein schwarzes Quadrat -> weglassen.
    const mk = (cs.webkitMaskImage || cs.maskImage || '');
    const masked = mk.includes('url(');
    if ((bg >>> 24) && !masked) rect(bx, clip, bg, rad);

    bgImages(cs, bx, clip);

    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      const bw = parseFloat(cs['border' + side + 'Width']);
      const st = cs['border' + side + 'Style'];
      if (bw > 0 && st !== 'none' && st !== 'hidden') {
        const bc = col(cs['border' + side + 'Color']);
        if (bc >>> 24) {
          const t = Math.max(1, R(bw));
          const r = side === 'Top' ? [bx[0], bx[1], bx[2], t]
                  : side === 'Bottom' ? [bx[0], bx[1] + bx[3] - t, bx[2], t]
                  : side === 'Left' ? [bx[0], bx[1], t, bx[3]]
                  : [bx[0] + bx[2] - t, bx[1], t, bx[3]];
          rect(r, clip, bc, 0);
        }
      }
    }

    if (ln === 'img') {
      let u = el.currentSrc || el.src || '';
      if (!u) u = el.getAttribute('data-src') || el.getAttribute('data-lazy-src') || el.getAttribute('data-original') || '';
      if (!u && el.srcset) {
        const first = (el.srcset || '').split(',')[0].trim().split(/\s+/)[0];
        if (first) u = first;
      }
      u = absUrl(u);
      const v = inter(bx, clip);
      if (u && v && bx[2] >= 1 && bx[3] >= 1) {
        const fit = (cs.objectFit || '').toLowerCase();
        const nw = el.naturalWidth, nh = el.naturalHeight;
        const op = tokens(cs.objectPosition || '50% 50%');
        const oxT = op[0] || '50%', oyT = op[1] || '50%';
        const centered = oxT === '50%' && oyT === '50%';
        let done = false;
        if (nw > 0 && nh > 0 && (fit === 'contain' || fit === 'none' || fit === 'scale-down' ||
                                  (fit === 'cover' && !centered))) {
          let s = fit === 'none' ? 1
                : fit === 'cover' ? Math.max(bx[2] / nw, bx[3] / nh)
                : Math.min(bx[2] / nw, bx[3] / nh);
          if (fit === 'scale-down') s = Math.min(1, s);
          const tw = nw * s, th = nh * s;
          const px = evalLen(oxT, bx[2] - tw) ?? 0, py = evalLen(oyT, bx[3] - th) ?? 0;
          pushImg(u, bx[0] + px, bx[1] + py, tw, th, 0, v);
          done = true;
        }
        if (!done) pushImg(u, bx[0], bx[1], bx[2], bx[3], (fit === 'cover' || fit === 'none') ? 1 : 0, v);
      }
    } else if (ln === 'input' || ln === 'textarea' || ln === 'select') {
      let kind = ln === 'input' ? (el.type || 'text') : ln;
      if (kind === 'hidden' || !inter(bx, clip)) return;
      let txt = '', chk = 0, ph = 0;
      if (ln === 'select') {
        const o = el.selectedOptions && el.selectedOptions[0]; txt = o ? o.text : '';
      } else if (kind === 'checkbox' || kind === 'radio') {
        chk = el.checked ? 1 : 0;
      } else {
        txt = el.value || '';
        if (kind === 'password') txt = '\u2022'.repeat(txt.length);
        if (!txt && el.placeholder) { txt = el.placeholder; ph = 1; }
      }
      items.push(['f', R(bx[0]), R(bx[1]), R(bx[2]), R(bx[3]), kind, txt, chk, ph,
                  col(cs.color), parseFloat(cs.fontSize) || 16]);
    } else if (ln === 'a' && el.href && typeof el.href === 'string' && !el.href.startsWith('javascript')) {
      for (const r of el.getClientRects()) {
        if (r.width >= 4 && r.height >= 4)
          items.push(['a', R(r.x + sx), R(r.y + sy), R(r.width), R(r.height), el.href]);
      }
    }
  };

  const walk = (el, clip) => {
    const ln = el.localName;
    if (SKIP.has(ln)) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || parseFloat(cs.opacity) === 0) return;
    // content-visibility:auto ueberspringt Layout fuer Offscreen-Inhalt -> Platzhaltergroesse
    // statt echter Hoehe, Inhalt ueberlappt. Echtes Layout erzwingen.
    if (cs.contentVisibility === 'auto') el.style.setProperty('content-visibility', 'visible', 'important');
    const bx = box(el);
    // fixed-Overlays (Cookie-Banner etc.) absichtlich mitnehmen
    const vis = cs.visibility === 'visible';
    const hasBox = bx[2] >= 1 && bx[3] >= 1;
    if (vis && hasBox) decorate(el, cs, bx, clip);
    if (ln === 'input' || ln === 'textarea' || ln === 'select' || ln === 'img') return;
    // Nicht gerenderter Inhalt (zugeklappte Wikipedia-Abschnitte = hidden=until-found bzw.
    // content-visibility:hidden): Chromium liefert dort trotzdem (uebereinanderliegende)
    // Boxen -> komplett ueberspringen. Geschlossenes <details>: nur das <summary> zeigen.
    if (cs.contentVisibility === 'hidden') return;
    const onlySummary = ln === 'details' && !el.open;
    let cclip = clip;
    if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
      const v = inter(bx, clip);
      if (!v) return;
      const oy = cs.overflowY, ox = cs.overflowX;
      const scrollable = oy === 'auto' || oy === 'scroll' || ox === 'auto' || ox === 'scroll';
      // Nur außerhalb von Amazon: scrollbare Container voll mitnehmen (Wikipedia etc.)
      if (expandScrollOverflow && scrollable) {
        const fullW = Math.max(bx[2], el.scrollWidth || 0);
        const fullH = Math.max(bx[3], el.scrollHeight || 0);
        cclip = inter([bx[0], bx[1], fullW, fullH], clip) || v;
      } else {
        cclip = v;
      }
    }
    const root = el.shadowRoot || el;
    for (let n = root.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 1) {
        if (onlySummary && n.localName !== 'summary') continue;
        walk(n, cclip);
      } else if (n.nodeType === 3 && vis && !onlySummary) text(n, cs, cclip);
    }
  };

  const runWalk = () => { items = []; walk(document.body, null); };
  runWalk();
  // Sprites/Hintergrundbilder, deren Groesse noch unbekannt war: kurz nachladen, dann neu extrahieren
  if (pendingImgs.size) {
    await Promise.all(Array.from(pendingImgs.entries()).map(([u, im]) => new Promise(res => {
      const done = () => {
        nat.set(u, im.naturalWidth > 0 && im.naturalHeight > 0 ? [im.naturalWidth, im.naturalHeight] : null);
        res();
      };
      if (im.complete) return done();
      im.onload = done; im.onerror = done;
      setTimeout(() => { if (!nat.has(u)) nat.set(u, null); res(); }, 2500);
    })));
    pendingImgs.clear();
    runWalk();
  }

  const de = document.documentElement;
  let pbg = col(getComputedStyle(de).backgroundColor);
  if (!(pbg >>> 24)) pbg = col(getComputedStyle(document.body).backgroundColor);
  if (!(pbg >>> 24)) pbg = 0xFFFFFFFF;

  let focus = null;
  const a = document.activeElement;
  if (a && (a.localName === 'input' || a.localName === 'textarea')) {
    const t = a.type || 'text';
    if (!['button','submit','checkbox','radio','image','reset','file','hidden','range','color'].includes(t))
      focus = {type: t, value: a.value || '', ph: a.placeholder || ''};
  }
  // Hoehe: scrollHeight, aber nicht sinnlos groesser als der tatsaechliche Inhalt
  let contentBottom = 0;
  for (const it of items) {
    const bottom = (it[2] || 0) + (it[4] || 0); // y + h
    if (bottom > contentBottom) contentBottom = bottom;
  }
  const sh = de.scrollHeight || VH;
  // Wenn kaum Inhalt extrahiert wurde, scrollHeight trotzdem nehmen (Seite noch am Laden)
  // Wenn Inhalt da ist, Hoehe an Inhalt anlehnen + Puffer, damit kein riesiger Leerraum entsteht
  let pageH = sh;
  if (contentBottom > 80) {
    // Inhalt bestimmt die Hoehe, scrollHeight nur als Obergrenze wenn aehnlich
    pageH = Math.max(contentBottom + 24, Math.min(sh, contentBottom + 400));
  }
  return {w: Math.max(de.scrollWidth, VW), h: pageH, title: document.title,
          bg: pbg, items: items, focus: focus};
}
"""


# Wikimedia-Seiten (mobil) liefern alle Abschnitte zugeklappt (hidden=until-found).
# Wir klappen sie einmal pro Dokument auf; danach laesst sich per Tippen wieder zuklappen.
EXPAND_JS = r"""
() => {
  if (window.__dtExpanded) return 0;
  if (!/(^|\.)(wikipedia|wikimedia|wiktionary|wikibooks|wikiquote|wikisource|wikivoyage|wikinews|wikiversity|mediawiki)\.org$/i
        .test(location.hostname || '')) return 0;
  window.__dtExpanded = 1;
  let n = 0;
  // 1) wie ein Nutzer: Ueberschrift antippen (aktualisiert auch Pfeil-Icon und Zustand der Seite)
  document.querySelectorAll('.collapsible-heading:not(.open-block)').forEach(h => {
    try { h.click(); n++; } catch (e) {}
  });
  // 2) Rest (z.B. falls die Seiten-JS noch nicht bereit war): Attribut direkt entfernen
  document.querySelectorAll('[hidden="until-found"]').forEach(e => { e.removeAttribute('hidden'); n++; });
  return n;
}
"""

# --------------------------------------------------------------------------- Hilfsfunktionen

_dns_cache = {}


async def url_allowed(url):
    """Blockt interne/private Adressen (Schutz fuers Heimnetz)."""
    if ALLOW_PRIVATE:
        return True
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme in ("data", "blob", "about"):
        return True
    if u.scheme not in ("http", "https"):
        return False
    host = u.hostname
    if not host:
        return False
    ok = _dns_cache.get(host)
    if ok is None:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
            ok = all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)
        except Exception:
            ok = False
        _dns_cache[host] = ok
    return ok


def normalize(u):
    u = (u or "").strip()
    if not u:
        return "about:blank"
    if "://" in u:
        return u
    if " " in u or "." not in u:
        return "https://html.duckduckgo.com/html/?q=" + quote(u)
    return "https://" + u


def clampi(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(float(v))))
    except Exception:
        return default


def clampf(v, default, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return default


def json_response(data, status=200):
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    resp = web.Response(body=body, status=status, content_type="application/json", charset="utf-8")
    resp.enable_compression()
    return resp


def short(e):
    return str(e).strip().splitlines()[0][:200] if str(e).strip() else e.__class__.__name__


# --------------------------------------------------------------------------- Sitzungen

class Session:
    def __init__(self, sid, ctx, w, h, dpr, cid=None, lang=None):
        self.sid, self.ctx, self.w, self.h, self.dpr = sid, ctx, w, h, dpr
        self.cid = cid or ""
        self.lang = normalize_lang(lang)
        self.page = None
        self.lock = asyncio.Lock()
        self.last = time.time()
        self.img_urls, self.img_ids, self.cache = [], {}, {}
        self.pending_download = None  # dict or None
        self.download_files = {}      # id -> path

    def img_id(self, url):
        i = self.img_ids.get(url)
        if i is None:
            i = len(self.img_urls)
            self.img_urls.append(url)
            self.img_ids[url] = i
        return i

    def state_path(self):
        if not self.cid:
            return None
        # nur sichere Zeichen erlauben
        safe = "".join(c for c in self.cid if c.isalnum() or c in "-_")[:64]
        if not safe:
            return None
        return STATES_DIR / (safe + ".json")

    async def save_state(self):
        path = self.state_path()
        if not path:
            return
        try:
            await self.ctx.storage_state(path=str(path))
            log.info("State gespeichert fuer cid=%s", self.cid[:12])
        except Exception as e:
            log.warning("State speichern fehlgeschlagen: %s", short(e))

    async def close(self):
        try:
            await self.save_state()
            await self.ctx.close()
        except Exception:
            pass


def attach(s, page):
    s.page = page
    page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

    async def on_download(download):
        try:
            suggested = download.suggested_filename or "download.bin"
            # sicherer Dateiname
            safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in suggested)[:120].strip() or "download.bin"
            did = uuid.uuid4().hex[:10]
            dest = DOWNLOADS_DIR / f"{s.sid}_{did}_{safe}"
            await download.save_as(str(dest))
            size = dest.stat().st_size if dest.is_file() else 0
            if size > MAX_DOWNLOAD_MB * 1024 * 1024:
                dest.unlink(missing_ok=True)
                log.warning("Download zu gross, verworfen: %s (%d bytes)", safe, size)
                return
            s.download_files[did] = dest
            s.pending_download = {
                "id": did,
                "name": suggested,
                "size": size,
                "url": download.url or "",
            }
            log.info("Download bereit: %s (%d KB) id=%s", suggested, size // 1024, did)
        except Exception as e:
            log.warning("Download-Handler: %s", short(e))

    page.on("download", lambda d: asyncio.ensure_future(on_download(d)))


async def new_session(app, sid, w, h, dpr, cid=None, android_ver=None, model=None, lang=None):
    ua = build_ua(android_ver or "13", model or "Pixel 7") if (android_ver or model) else UA
    kw = dict(viewport={"width": w, "height": h}, device_scale_factor=1,
              user_agent=ua, locale=LOCALE)
    if cid:
        safe = "".join(c for c in cid if c.isalnum() or c in "-_")[:64]
        path = STATES_DIR / (safe + ".json") if safe else None
        if path and path.is_file():
            kw["storage_state"] = str(path)
            log.info("Lade gespeicherten State fuer cid=%s", cid[:12])
    ctx = await app["browser"].new_context(**kw)
    await ctx.add_init_script(INIT_JS)

    async def guard(route):
        try:
            req = route.request
            host = urlparse(req.url).hostname or ""
            if req.resource_type in ("media", "font") or any(b in host for b in BLOCK_HOSTS):
                return await route.abort()
            if not await url_allowed(req.url):
                log.warning("blockiert (intern): %s", req.url)
                return await route.abort()
            await route.continue_()
        except Exception:
            pass

    await ctx.route("**/*", guard)
    s = Session(sid, ctx, w, h, dpr, cid=cid, lang=lang)
    attach(s, await ctx.new_page())
    ctx.on("page", lambda p: attach(s, p))  # Popups uebernehmen
    return s


async def get_session(app, sid, w, h, dpr, cid=None, android_ver=None, model=None, lang=None):
    ss = app["sessions"]
    s = ss.get(sid) if sid else None
    if s and (s.w != w or (cid and s.cid and s.cid != cid)):
        ss.pop(sid, None)
        await s.close()
        s = None
    if not s:
        if len(ss) >= MAX_SESSIONS:
            old = min(ss.values(), key=lambda x: x.last)
            ss.pop(old.sid, None)
            await old.close()
        sid = sid or uuid.uuid4().hex[:12]
        s = await new_session(app, sid, w, h, dpr, cid=cid,
                              android_ver=android_ver, model=model, lang=lang)
        ss[sid] = s
    s.dpr, s.h, s.last = dpr, h, time.time()
    if cid and not s.cid:
        s.cid = cid
    if lang:
        s.lang = normalize_lang(lang)
    return s


def find_session(req):
    s = req.app["sessions"].get(req.query.get("sid"))
    if s:
        s.last = time.time()
        lang = (req.query.get("lang") or "").strip()
        if lang:
            s.lang = normalize_lang(lang)
    return s


async def reaper(app):
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for sid, s in list(app["sessions"].items()):
            if now - s.last > IDLE_SECONDS and not s.lock.locked():
                app["sessions"].pop(sid, None)
                await s.close()
                log.info("Sitzung %s wegen Inaktivitaet geschlossen", sid)


# --------------------------------------------------------------------------- Seitenzustand

async def settle(page, extra=0.4):
    """Wartet bis die Seite halbwegs ruhig ist. networkidle ist bei Amazon/Wiki oft langsam."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        # Laenger warten – viele Seiten feuern noch Tracking/Lazy-Requests
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(extra)


async def lazy_scroll(page):
    """Mehrfach durchscrollen, damit Lazy-Load-Bilder und Infinite-Content nachladen."""
    try:
        vh = page.viewport_size["height"] or 640
        y, steps = 0, 0
        h = await page.evaluate("document.documentElement.scrollHeight")
        while y < min(h, 50000) and steps < 28:
            y += max(vh // 2, 180)
            steps += 1
            await page.evaluate("y => scrollTo(0, y)", y)
            await asyncio.sleep(0.28)
            h = await page.evaluate("document.documentElement.scrollHeight")
        await page.evaluate("scrollTo(0, 0)")
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await asyncio.sleep(0.5)
    except Exception:
        pass


def error_state(s, msg, lang=None):
    lang = normalize_lang(lang or getattr(s, "lang", None))
    prefix = t("error_prefix", lang)
    title = t("error_title", lang)
    lines = textwrap.wrap(prefix + msg, width=max(10, (s.w - 20) // 9)) or [title]
    items = [["t", 10, 10 + i * 24, s.w - 20, 22, ln, 0xFFCC0000, 16, 0] for i, ln in enumerate(lines)]
    return {"v": 1, "sid": s.sid, "url": "", "title": title, "w": s.w,
            "h": 20 + 24 * len(lines), "bg": 0xFFFFFFFF, "items": items, "focus": None, "err": msg}


async def snapshot(s, lazy=False, error=None, cert=None):
    if error:
        return error_state(s, error)
    page, t0, data = s.page, time.time(), None
    for attempt in range(2):
        try:
            try:
                if await page.evaluate(EXPAND_JS):
                    await asyncio.sleep(0.3)  # Layout nach dem Aufklappen setzen lassen
            except Exception as e:
                log.warning("Aufklappen fehlgeschlagen: %s", short(e))
            if lazy:
                await lazy_scroll(page)
            else:
                await page.evaluate("scrollTo(0, 0)")
            data = await page.evaluate(EXTRACT_JS)
            break
        except Exception as e:
            log.warning("Extraktion fehlgeschlagen (%s), Versuch %d", short(e), attempt + 1)
            await settle(page)
    if data is None:
        return error_state(s, t("page_read_failed", getattr(s, "lang", None)))

    for it in data["items"]:
        if it[0] == "i":
            it[5] = s.img_id(it[5])
    # Leerraum unten vermeiden: Hoehe an den tiefsten Inhalt anpassen
    if data["items"]:
        bottom = 0
        for it in data["items"]:
            try:
                b = float(it[2]) + float(it[4])
                if b > bottom:
                    bottom = b
            except Exception:
                pass
        if bottom > 80:
            # nicht viel groesser als der Inhalt (+ kleiner Puffer)
            data["h"] = max(bottom + 32, min(float(data.get("h") or bottom), bottom + 480))
    data.update({"v": 1, "sid": s.sid, "url": page.url})
    if cert is not None:
        data["cert"] = cert
    elif page.url.startswith("https://"):
        data["cert"] = {"ok": True, "https": True}
    elif page.url.startswith("http://"):
        data["cert"] = {"ok": False, "https": False}
    if s.pending_download:
        data["download"] = s.pending_download
        s.pending_download = None  # nur einmal melden
    dt = time.time() - t0

    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    log.info("%s | %d Items | %.0f KB roh | %.0f KB gzip | %.1fs Extraktion | %d Bilder",
             page.url[:90], len(data["items"]), len(raw) / 1024,
             len(gzip.compress(raw)) / 1024, dt, len(s.img_urls))
    try:
        (DEBUG_DIR / "last_page.json").write_bytes(raw)
        if SHOT:
            await page.screenshot(path=str(DEBUG_DIR / "last_page.png"), full_page=True, timeout=8000)
    except Exception as e:
        log.warning("Debug-Ausgabe fehlgeschlagen: %s", short(e))
    return data


# --------------------------------------------------------------------------- Bilder

def shrink(raw, tw, quality):
    try:
        im = Image.open(io.BytesIO(raw))
        im.draft("RGB", (tw * 2, tw * 2))  # beschleunigt JPEG-Decode, sonst ohne Wirkung
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[3])
            im = bg
        else:
            im = im.convert("RGB")
        if im.width > tw:
            im = im.resize((tw, max(1, round(im.height * tw / im.width))), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception:
        return None


async def fetch_image(s, url):
    if not url:
        return None
    if url.startswith("data:"):
        try:
            head, _, payload = url.partition(",")
            return base64.b64decode(payload) if ";base64" in head else unquote_to_bytes(payload)
        except Exception:
            return None
    # Resolve relative / protocol-relative URLs against the page
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        try:
            from urllib.parse import urljoin
            url = urljoin(s.page.url if s.page else "", url)
        except Exception:
            return None
    if not await url_allowed(url):
        return None
    try:
        r = await s.ctx.request.get(url, headers={"Referer": s.page.url if s.page else url}, timeout=15000)
        if not r.ok:
            return None
        body = await r.body()
        return body if len(body) < 15_000_000 else None
    except Exception:
        return None


# --------------------------------------------------------------------------- HTTP-Handler

def api(fn):
    async def wrapper(req):
        try:
            return await fn(req)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("Fehler in %s", fn.__name__)
            return json_response({"error": short(e)}, 500)
    return wrapper


def no_session():
    return json_response({"error": "session"}, 404)


@api
async def h_ping(req):
    return web.Response(text="ok")


@api
async def h_load(req):
    q = req.query
    w = clampi(q.get("w"), 360, 200, 1600)
    h = clampi(q.get("h"), 640, 200, 3000)
    dpr = clampf(q.get("dpr"), 1.0, 0.5, 4.0)
    url = normalize(q.get("url"))
    cid = (q.get("cid") or "").strip() or None
    android_ver = (q.get("av") or "").strip() or None
    model = (q.get("model") or "").strip() or None
    lang = (q.get("lang") or "").strip() or None
    s = await get_session(req.app, q.get("sid"), w, h, dpr, cid=cid,
                          android_ver=android_ver, model=model, lang=lang)
    async with s.lock:
        err = None
        cert = None
        if not await url_allowed(url) and url != "about:blank":
            err = t("address_not_allowed", getattr(s, "lang", None))
        else:
            try:
                # "load" wartet auf window.load; Fallback auf domcontentloaded bei Timeout
                try:
                    resp = await s.page.goto(url, wait_until="load", timeout=30000)
                except Exception:
                    resp = await s.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if resp:
                    try:
                        sd = await resp.security_details()
                        if sd:
                            cert = {
                                "ok": True,
                                "https": True,
                                "subject": sd.get("subjectName") or "",
                                "issuer": sd.get("issuer") or "",
                                "protocol": sd.get("protocol") or "",
                                "validFrom": sd.get("validFrom"),
                                "validTo": sd.get("validTo"),
                            }
                        elif (resp.url or "").startswith("https"):
                            cert = {"ok": True, "https": True}
                        else:
                            cert = {"ok": False, "https": False}
                    except Exception as e:
                        log.warning("security_details: %s", short(e))
                        cert = {"ok": url.startswith("https"), "https": url.startswith("https")}
            except Exception as e:
                err = short(e)
        await settle(s.page, extra=0.8)
        data = await snapshot(s, lazy=True, error=err, cert=cert)
        # Manche Seiten (Amazon-Suche etc.) liefern Inhalt erst sehr spaet.
        # Wenn wenig Items da sind, nochmal warten und erneut extrahieren.
        if not err and data and len(data.get("items") or []) < 40:
            log.info("Wenige Items (%d) – warte nach und extrahiere erneut", len(data.get("items") or []))
            try:
                await settle(s.page, extra=2.0)
                await lazy_scroll(s.page)
                data2 = await snapshot(s, lazy=False, error=None, cert=cert)
                if data2 and len(data2.get("items") or []) > len(data.get("items") or []):
                    data = data2
                    log.info("Nach-Extraktion: jetzt %d Items", len(data["items"]))
            except Exception as e:
                log.warning("Nach-Extraktion fehlgeschlagen: %s", short(e))
        await s.save_state()
        return json_response(data)


def is_external_scheme(url):
    if not url:
        return False
    u = url.strip().lower()
    for p in ("market:", "intent:", "tel:", "mailto:", "sms:", "geo:",
              "whatsapp:", "tg:", "vnd.", "play.google.com/store", "fdroid."):
        if u.startswith(p) or p in u[:64]:
            return True
    return False


@api
async def h_tap(req):
    s = find_session(req)
    if not s:
        return no_session()
    cid = (req.query.get("cid") or "").strip()
    if cid and not s.cid:
        s.cid = cid
    x = clampf(req.query.get("x"), 0, 0, 100000)
    y = clampf(req.query.get("y"), 0, 0, 100000)
    href = (req.query.get("href") or "").strip()
    async with s.lock:
        page = s.page
        before = page.url
        # Externe Schemas (Play Store etc.) – nicht im Browser oeffnen
        if href and is_external_scheme(href):
            data = await snapshot(s)
            data["external"] = href
            return json_response(data)
        try:
            if href and href.startswith(("http://", "https://", "/")):
                # Direkte Navigation zum Link (zuverlaessiger als Mausklick auf Buttons)
                target = href
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    target = urljoin(page.url, href)
                if not await url_allowed(target):
                    log.warning("Tap-href blockiert: %s", target)
                else:
                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=25000)
                    except Exception as e:
                        log.warning("Tap-href goto: %s", short(e))
            else:
                await page.evaluate("([x, y, w]) => scrollTo(Math.max(0, x - w / 2), Math.max(0, y - 150))",
                                    [x, y, s.w])
                sx, sy = await page.evaluate("[scrollX, scrollY]")
                vx, vy = x - sx, y - sy
                await page.mouse.click(vx, vy, delay=40)
                try:
                    await page.evaluate("""([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el) return;
                        const a = el.closest && el.closest("a[href]");
                        if (a && a.href && !String(a.href).startsWith("javascript:")) { a.click(); return; }
                        const b = el.closest && el.closest("button,[role=button],input[type=submit],input[type=button]");
                        if (b) b.click(); else el.click();
                    }""", [vx, vy])
                except Exception:
                    pass
        except Exception as e:
            log.warning("Tap fehlgeschlagen: %s", short(e))
        await settle(s.page, extra=0.7)
        data = await snapshot(s, lazy=(s.page.url != before))
        # Falls Navigation auf externes Schema landete
        if is_external_scheme(s.page.url):
            data["external"] = s.page.url
        await s.save_state()
        return json_response(data)


@api
async def h_type(req):
    s = find_session(req)
    if not s:
        return no_session()
    cid = (req.query.get("cid") or "").strip()
    if cid and not s.cid:
        s.cid = cid
    text = req.query.get("t", "")
    enter = req.query.get("enter") == "1"
    async with s.lock:
        page = s.page
        before = page.url
        try:
            await page.keyboard.press("Control+A")
            if text:
                await page.keyboard.type(text)
            else:
                await page.keyboard.press("Backspace")
            if enter:
                await page.keyboard.press("Enter")
        except Exception as e:
            log.warning("Tippen fehlgeschlagen: %s", short(e))
        await settle(s.page, extra=0.5 if enter else 0.25)
        data = await snapshot(s, lazy=(s.page.url != before))
        await s.save_state()
        return json_response(data)


@api
async def h_back(req):
    s = find_session(req)
    if not s:
        return no_session()
    cid = (req.query.get("cid") or "").strip()
    if cid and not s.cid:
        s.cid = cid
    async with s.lock:
        try:
            await s.page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            log.warning("Zurueck fehlgeschlagen: %s", short(e))
        await settle(s.page, extra=0.4)
        data = await snapshot(s, lazy=True)
        await s.save_state()
        return json_response(data)


@api
async def h_state(req):
    s = find_session(req)
    if not s:
        return no_session()
    cid = (req.query.get("cid") or "").strip()
    if cid and not s.cid:
        s.cid = cid
    async with s.lock:
        return json_response(await snapshot(s))


@api
async def h_img(req):
    s = find_session(req)
    if not s:
        return web.Response(status=404)
    try:
        url = s.img_urls[int(req.query["id"])]
    except Exception:
        return web.Response(status=404)
    tw = clampi(req.query.get("tw"), 320, 16, 2000)
    quality = clampi(req.query.get("q"), 40, 10, 90)
    key = (url, tw, quality)
    data = s.cache.get(key)
    if data is None:
        raw = await fetch_image(s, url)
        if raw is not None:
            data = await asyncio.to_thread(shrink, raw, tw, quality)
        if data is None:
            return web.Response(status=204)
        if len(s.cache) > 300:
            s.cache.clear()
        s.cache[key] = data
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "max-age=600"})



@api
async def h_download(req):
    """Datei ausliefern, die zuvor per page.on(\"download\") empfangen wurde."""
    s = find_session(req)
    if not s:
        return web.Response(status=404, text="session")
    did = (req.query.get("id") or "").strip()
    path = s.download_files.get(did)
    if not path or not path.is_file():
        return web.Response(status=404, text="not found")
    name = path.name
    # originalen Namen aus dem Dateinamen extrahieren (nach sid_id_)
    parts = name.split("_", 2)
    filename = parts[2] if len(parts) >= 3 else name
    data = path.read_bytes()
    # optional: nach dem Ausliefern loeschen, damit die Platte nicht vollaeuft
    try:
        path.unlink(missing_ok=True)
        s.download_files.pop(did, None)
    except Exception:
        pass
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(data)),
        "Cache-Control": "no-store",
    }
    return web.Response(body=data, headers=headers)


# --------------------------------------------------------------------------- Start

async def on_start(app):
    global CHROME_VER, UA
    app["pw"] = await async_playwright().start()
    kw = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    if os.environ.get("MINI_NO_SANDBOX") == "1":
        kw["args"].append("--no-sandbox")
    if CHROME:
        kw["executable_path"] = CHROME
    app["browser"] = await app["pw"].chromium.launch(**kw)
    try:
        ver = app["browser"].version
        # z.B. "130.0.6723.58" -> für UA oft major.0.0.0 üblich, wir nehmen die echte
        CHROME_VER = ver.split()[0] if ver else CHROME_VER
        UA = build_ua()
        log.info("Started Chromium, Version %s", CHROME_VER)
        log.info("DTMiniBrowserServer, Version %s", DTMB_VER)
    except Exception as e:
        log.warning("Chrome-Version could not be read: %s", short(e))
        log.info("Started Chromium")
    app["reaper"] = asyncio.create_task(reaper(app))


async def on_stop(app):
    app["reaper"].cancel()
    for s in list(app["sessions"].values()):
        await s.close()
    await app["browser"].close()
    await app["pw"].stop()


def lan_ip():
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    app = web.Application()
    app["sessions"] = {}
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    app.add_routes([web.get("/ping", h_ping), web.get("/load", h_load), web.get("/tap", h_tap),
                    web.get("/type", h_type), web.get("/back", h_back), web.get("/state", h_state),
                    web.get("/img", h_img), web.get("/download", h_download)])
    log.info("Echtes Geraet: http://%s:%d   |   Android-Emulator: http://10.0.2.2:%d", lan_ip(), PORT, PORT)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
