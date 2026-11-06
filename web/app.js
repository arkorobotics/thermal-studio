'use strict';
/* Thermal Studio front-end. All temperatures are handled in Kelvin internally. */

const $ = (id) => document.getElementById(id);
const K0 = 273.15;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------ palettes ---
const PALETTE_STOPS = {
  ironbow: ['#000010', '#20085a', '#5c0c8e', '#9b1a86', '#cf3a4c', '#ea6a1a', '#f7a100', '#fdd835', '#fff9c4'],
  rainbow: ['#0a0a64', '#0040ff', '#00b4ff', '#00e6a0', '#5cff28', '#f0f000', '#ff8c00', '#ff1e00', '#ffffff'],
  whitehot: ['#000000', '#ffffff'],
  blackhot: ['#ffffff', '#000000'],
  inferno: ['#000004', '#1b0c41', '#4a0c6b', '#781c6d', '#a52c60', '#cf4446', '#ed6925', '#fb9b06', '#fcffa4'],
  lava: ['#000000', '#3b0000', '#8b0000', '#d42c00', '#ff7300', '#ffc400', '#ffffa0'],
  arctic: ['#00001a', '#082d6b', '#1066b0', '#27a7d8', '#8fdcf0', '#ffffff', '#ffd27a', '#ff8a3d'],
  viridis: ['#440154', '#472c7a', '#3b518b', '#2c718e', '#21908d', '#27ad81', '#5cc863', '#aadc32', '#fde725'],
};
const PALETTE_NAMES = { ironbow: 'Ironbow', rainbow: 'Rainbow', whitehot: 'White hot', blackhot: 'Black hot',
  inferno: 'Inferno', lava: 'Lava', arctic: 'Arctic', viridis: 'Viridis' };

function hex(c) { return [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)); }
function buildLUT(stops) {
  const lut = new Uint8Array(256 * 3);
  const cs = stops.map(hex);
  for (let i = 0; i < 256; i++) {
    const t = (i / 255) * (cs.length - 1);
    const a = Math.min(cs.length - 2, Math.floor(t)), f = t - a;
    for (let k = 0; k < 3; k++) lut[i * 3 + k] = Math.round(cs[a][k] * (1 - f) + cs[a + 1][k] * f);
  }
  return lut;
}
const LUTS = Object.fromEntries(Object.entries(PALETTE_STOPS).map(([k, v]) => [k, buildLUT(v)]));

// --------------------------------------------------------------- state ---
const S = {
  unit: 'C', palette: 'ironbow', rangeMode: 'auto', rMin: 20, rMax: 40, smooth: 'smooth',
  rot: 0, flipH: false, flipV: false, tavg: 1, ovMinMax: true, ovCenter: false,
  isoMode: 'off', isoVal: 40, ptRadius: 1, chartWin: 120,
};
try { Object.assign(S, JSON.parse(localStorage.getItem('thermalStudio') || '{}')); } catch (e) { /* ignore */ }
function savePrefs() { try { localStorage.setItem('thermalStudio', JSON.stringify(S)); } catch (e) { /* ignore */ } }

const live = {
  K: null, w: 160, h: 120, radiometric: true, ring: [], sum: null, seq: 0, t: 0,
  lo: null, hi: null, stats: null,
};
let points = [];      // series meta from server (points + builtin)
let builtin = {};
const hist = {};      // id -> {t:[], v:[]}
let histSince = 0, serverNow = 0;
let camState = null;

// --------------------------------------------------------------- units ---
function toU(K) {
  if (!live.radiometric) return K;
  if (S.unit === 'F') return (K - K0) * 9 / 5 + 32;
  if (S.unit === 'K') return K;
  return K - K0;
}
function fromU(v) {
  if (!live.radiometric) return v;
  if (S.unit === 'F') return (v - 32) * 5 / 9 + K0;
  if (S.unit === 'K') return v;
  return v + K0;
}
function unitLabel() { return live.radiometric ? (S.unit === 'K' ? 'K' : '°' + S.unit) : 'cnt'; }
function fmt(K, d) {
  if (K == null || !isFinite(K)) return '–';
  const v = toU(K);
  return (live.radiometric ? v.toFixed(d ?? 1) : v.toFixed(0)) + ' ' + unitLabel();
}

// ------------------------------------------------------ transforms ---
const mapCache = {};
function getMap(w, h) {
  const key = `${w}x${h}r${S.rot}${S.flipH ? 'h' : ''}${S.flipV ? 'v' : ''}`;
  if (mapCache[key]) return mapCache[key];
  const rot = S.rot;
  const dw = rot % 180 ? h : w, dh = rot % 180 ? w : h;
  const idx = new Int32Array(dw * dh);
  for (let v = 0; v < dh; v++) for (let u = 0; u < dw; u++) {
    const [x, y] = dispToSensorRaw(u, v, w, h, rot);
    idx[v * dw + u] = y * w + x;
  }
  return (mapCache[key] = { idx, dw, dh, w, h });
}
function dispToSensorRaw(u, v, w, h, rot) {
  let x, y;
  if (rot === 0) { x = u; y = v; }
  else if (rot === 90) { x = v; y = h - 1 - u; }
  else if (rot === 180) { x = w - 1 - u; y = h - 1 - v; }
  else { x = w - 1 - v; y = u; }
  if (S.flipH) x = w - 1 - x;
  if (S.flipV) y = h - 1 - y;
  return [x, y];
}
function sensorToDisp(x, y, w, h) {
  if (S.flipH) x = w - 1 - x;
  if (S.flipV) y = h - 1 - y;
  const rot = S.rot;
  if (rot === 0) return [x, y];
  if (rot === 90) return [h - 1 - y, x];
  if (rot === 180) return [w - 1 - x, h - 1 - y];
  return [y, w - 1 - x];
}

// ------------------------------------------------------- rendering ---
function percentileRange(K, plo, phi) {
  const n = K.length, step = Math.max(1, Math.floor(n / 6000));
  const s = [];
  for (let i = 0; i < n; i += step) s.push(K[i]);
  s.sort((a, b) => a - b);
  return [s[Math.floor(plo * (s.length - 1))], s[Math.floor(phi * (s.length - 1))]];
}

function colorize(K, w, h, lo, hi, imgData, map) {
  const lut = LUTS[S.palette] || LUTS.ironbow;
  const d = imgData.data, idx = map.idx;
  const span = Math.max(1e-6, hi - lo);
  const iso = S.isoMode !== 'off' && live.radiometric ? fromU(+S.isoVal) : null;
  for (let p = 0; p < idx.length; p++) {
    const k = K[idx[p]];
    let o = p * 4;
    if (iso !== null && ((S.isoMode === 'above' && k >= iso) || (S.isoMode === 'below' && k <= iso))) {
      const a = Math.min(1, Math.max(0, (k - lo) / span));
      d[o] = 40 + 200 * a; d[o + 1] = 255; d[o + 2] = 80; d[o + 3] = 255;
      continue;
    }
    let c = Math.round(((k - lo) / span) * 255);
    c = c < 0 ? 0 : c > 255 ? 255 : c;
    d[o] = lut[c * 3]; d[o + 1] = lut[c * 3 + 1]; d[o + 2] = lut[c * 3 + 2]; d[o + 3] = 255;
  }
}

const off = document.createElement('canvas');
const offCtx = off.getContext('2d');
const view = $('view'), vctx = view.getContext('2d');
let viewGeom = { cssW: 1, cssH: 1, dw: 160, dh: 120 };

function fitCanvas(canvas, box, dw, dh, reserveTop = 0) {
  const bw = box.clientWidth - 2, bh = box.clientHeight - 2 - reserveTop;
  const sc = Math.max(0.1, Math.min(bw / dw, bh / dh));
  const cssW = Math.floor(dw * sc), cssH = Math.floor(dh * sc);
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    canvas.style.width = cssW + 'px'; canvas.style.height = cssH + 'px';
  }
  return { cssW, cssH, dpr };
}

function frameStats(K, w, h) {
  let mx = -Infinity, mn = Infinity, sum = 0, imx = 0, imn = 0;
  for (let i = 0; i < K.length; i++) {
    const k = K[i]; sum += k;
    if (k > mx) { mx = k; imx = i; }
    if (k < mn) { mn = k; imn = i; }
  }
  const cx = w >> 1, cy = h >> 1;
  let c = 0;
  for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) c += K[(cy + dy) * w + cx + dx];
  return { max: mx, min: mn, mean: sum / K.length, center: c / 9,
    maxXY: [imx % w, Math.floor(imx / w)], minXY: [imn % w, Math.floor(imn / w)] };
}

function regionMean(K, w, h, x, y, r) {
  let s = 0, n = 0;
  for (let yy = Math.max(0, y - r); yy <= Math.min(h - 1, y + r); yy++)
    for (let xx = Math.max(0, x - r); xx <= Math.min(w - 1, x + r); xx++) { s += K[yy * w + xx]; n++; }
  return n ? s / n : NaN;
}

function computeRange(K, smoothing = true) {
  let lo, hi;
  if (S.rangeMode === 'manual') {
    lo = fromU(+S.rMin); hi = fromU(+S.rMax);
  } else {
    if (S.rangeMode === 'pct') [lo, hi] = percentileRange(K, 0.01, 0.99);
    else { lo = live.stats.min; hi = live.stats.max; }
    const minSpan = live.radiometric ? 2 : 50;
    if (hi - lo < minSpan) { const c = (hi + lo) / 2; lo = c - minSpan / 2; hi = c + minSpan / 2; }
    if (smoothing && live.lo != null && S.rangeMode !== 'manual') {
      lo = live.lo + 0.3 * (lo - live.lo); hi = live.hi + 0.3 * (hi - live.hi);
    }
  }
  live.lo = lo; live.hi = hi;
  return [lo, hi];
}

function render() {
  if (!live.K) return;
  const { K, w, h } = live;
  const map = getMap(w, h);
  live.stats = frameStats(K, w, h);
  const [lo, hi] = computeRange(K);
  if (off.width !== map.dw || off.height !== map.dh) { off.width = map.dw; off.height = map.dh; }
  const img = offCtx.createImageData(map.dw, map.dh);
  colorize(K, w, h, lo, hi, img, map);
  offCtx.putImageData(img, 0, 0);

  const g = fitCanvas(view, $('imgbox'), map.dw, map.dh);
  viewGeom = { ...g, dw: map.dw, dh: map.dh };
  vctx.setTransform(g.dpr, 0, 0, g.dpr, 0, 0);
  vctx.imageSmoothingEnabled = S.smooth === 'smooth';
  vctx.imageSmoothingQuality = 'high';
  vctx.drawImage(off, 0, 0, g.cssW, g.cssH);
  drawOverlays(g, map);
  drawColorbar(lo, hi);
  updateSpots();
}

function sensorToCss(x, y) {
  const [u, v] = sensorToDisp(x, y, live.w, live.h);
  return [(u + 0.5) * viewGeom.cssW / viewGeom.dw, (v + 0.5) * viewGeom.cssH / viewGeom.dh];
}

function drawOverlays(g) {
  const c = vctx, st = live.stats;
  const pxs = g.cssW / viewGeom.dw;
  c.lineWidth = 1.5;
  c.font = '600 12px ' + getComputedStyle(document.body).getPropertyValue('--mono');
  c.textBaseline = 'middle';
  if (S.ovCenter) {
    const [cx, cy] = sensorToCss(live.w / 2 - 0.5, live.h / 2 - 0.5);
    c.strokeStyle = 'rgba(255,255,255,.85)';
    c.beginPath(); c.moveTo(cx - 12, cy); c.lineTo(cx - 4, cy); c.moveTo(cx + 4, cy); c.lineTo(cx + 12, cy);
    c.moveTo(cx, cy - 12); c.lineTo(cx, cy - 4); c.moveTo(cx, cy + 4); c.lineTo(cx, cy + 12); c.stroke();
    label(c, cx + 14, cy - 10, fmt(st.center), '#fff');
  }
  if (S.ovMinMax) {
    tri(c, ...sensorToCss(...st.maxXY), '#ff4b3e', true);
    tri(c, ...sensorToCss(...st.minXY), '#3ea0ff', false);
  }
  for (const p of points) {
    if (p.x === undefined) continue;
    const [x, y] = sensorToCss(p.x, p.y);
    const r = Math.max(5, (p.r + 0.5) * pxs);
    c.strokeStyle = 'rgba(0,0,0,.7)'; c.lineWidth = 3.5;
    c.strokeRect(x - r, y - r, 2 * r, 2 * r);
    c.strokeStyle = p.color; c.lineWidth = 1.6;
    c.strokeRect(x - r, y - r, 2 * r, 2 * r);
    c.beginPath(); c.moveTo(x - 2, y); c.lineTo(x + 2, y); c.moveTo(x, y - 2); c.lineTo(x, y + 2); c.stroke();
    const v = regionMean(live.K, live.w, live.h, p.x, p.y, p.r);
    p.live = v;
    label(c, x + r + 4, y, `${p.label} ${fmt(v)}`, p.color);
  }
}
function tri(c, x, y, col, up) {
  c.fillStyle = col; c.strokeStyle = '#000'; c.lineWidth = 1.2;
  c.beginPath();
  if (up) { c.moveTo(x, y - 7); c.lineTo(x - 6, y + 4); c.lineTo(x + 6, y + 4); }
  else { c.moveTo(x, y + 7); c.lineTo(x - 6, y - 4); c.lineTo(x + 6, y - 4); }
  c.closePath(); c.fill(); c.stroke();
}
function label(c, x, y, text, col) {
  const w = c.measureText(text).width + 8;
  if (x + w > viewGeom.cssW) x = x - w - 30;
  c.fillStyle = 'rgba(0,0,0,.65)';
  c.fillRect(x, y - 9, w, 18);
  c.fillStyle = col; c.fillText(text, x + 4, y + 0.5);
}

function drawColorbar(lo, hi) {
  const cb = $('colorbar'), ctx = cb.getContext('2d');
  const lut = LUTS[S.palette];
  const img = ctx.createImageData(16, 256);
  for (let y = 0; y < 256; y++) {
    const c = 255 - y;
    for (let x = 0; x < 16; x++) {
      const o = (y * 16 + x) * 4;
      img.data[o] = lut[c * 3]; img.data[o + 1] = lut[c * 3 + 1]; img.data[o + 2] = lut[c * 3 + 2]; img.data[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  $('cbMax').textContent = live.radiometric ? toU(hi).toFixed(1) : hi.toFixed(0);
  $('cbMin').textContent = live.radiometric ? toU(lo).toFixed(1) : lo.toFixed(0);
}

function updateSpots() {
  const st = live.stats;
  $('sMax').textContent = fmt(st.max); $('sMin').textContent = fmt(st.min);
  $('sMean').textContent = fmt(st.mean); $('sCenter').textContent = fmt(st.center);
}

// ------------------------------------------------------- frame loop ---
function ingest(u16, w, h, scale, radiometric) {
  const n = w * h;
  const K = new Float32Array(n);
  const sc = radiometric ? scale : 1;
  for (let i = 0; i < n; i++) K[i] = u16[i] * sc;
  if (live.w !== w || live.h !== h || live.radiometric !== radiometric) {
    live.ring = []; live.sum = null; live.lo = null;
  }
  live.w = w; live.h = h; live.radiometric = radiometric;
  const N = +S.tavg;
  if (N <= 1) { live.ring = []; live.sum = null; live.K = K; return; }
  if (!live.sum || live.sum.length !== n) live.sum = new Float64Array(n);
  live.ring.push(K);
  for (let i = 0; i < n; i++) live.sum[i] += K[i];
  while (live.ring.length > N) {
    const old = live.ring.shift();
    for (let i = 0; i < n; i++) live.sum[i] -= old[i];
  }
  const m = live.ring.length, out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = live.sum[i] / m;
  live.K = out;
}

async function frameLoop() {
  let after = 0;
  for (;;) {
    try {
      const r = await fetch('/api/frame?after=' + after, { cache: 'no-store' });
      if (r.status === 200) {
        const H = (k) => r.headers.get(k);
        const buf = await r.arrayBuffer();
        after = +H('X-Seq');
        if (after < live.seq) live.seq = 0;
        live.seq = after; live.t = +H('X-Time');
        ingest(new Uint16Array(buf), +H('X-Width'), +H('X-Height'), +H('X-Scale'), H('X-Radiometric') === '1');
        $('fps').textContent = (+H('X-Fps')).toFixed(1);
        if (!$('srview').hidden) continue;   // keep live data but don't redraw under SR viewer
        render();
        updateReadout();
      } else if (r.status !== 204) { await sleep(500); }
    } catch (e) { await sleep(1000); after = 0; }
  }
}

// ---------------------------------------------------- interaction ---
let hover = null, drag = null;
function evToSensor(ev) {
  const rect = view.getBoundingClientRect();
  const u = Math.floor((ev.clientX - rect.left) / rect.width * viewGeom.dw);
  const v = Math.floor((ev.clientY - rect.top) / rect.height * viewGeom.dh);
  if (u < 0 || v < 0 || u >= viewGeom.dw || v >= viewGeom.dh) return null;
  const [x, y] = dispToSensorRaw(u, v, live.w, live.h, S.rot);
  return { x, y, cx: ev.clientX - rect.left, cy: ev.clientY - rect.top };
}
function pointAt(ev) {
  const rect = view.getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  let best = null, bd = 12;
  for (const p of points) {
    if (p.x === undefined) continue;
    const [x, y] = sensorToCss(p.x, p.y);
    const d = Math.hypot(x - mx, y - my);
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}
function updateReadout() {
  const ro = $('readout');
  if (!hover || !live.K) { ro.style.display = 'none'; return; }
  const k = live.K[hover.y * live.w + hover.x];
  ro.textContent = `${fmt(k, 2)}  (${hover.x}, ${hover.y})`;
  const box = $('imgbox').getBoundingClientRect(), vr = view.getBoundingClientRect();
  let left = vr.left - box.left + hover.cx + 14, top = vr.top - box.top + hover.cy + 14;
  if (left > box.width - 170) left -= 190;
  if (top > box.height - 30) top -= 40;
  ro.style.left = left + 'px'; ro.style.top = top + 'px'; ro.style.display = 'block';
}
view.addEventListener('mousemove', (ev) => {
  hover = evToSensor(ev);
  if (drag && hover) { drag.p.x = hover.x; drag.p.y = hover.y; drag.moved = true; render(); }
  view.style.cursor = drag ? 'grabbing' : pointAt(ev) ? 'grab' : 'crosshair';
  updateReadout();
});
view.addEventListener('mouseleave', () => { hover = null; updateReadout(); });
view.addEventListener('mousedown', (ev) => {
  if (ev.button !== 0) return;
  const p = pointAt(ev);
  if (p) drag = { p, moved: false };
});
window.addEventListener('mouseup', async (ev) => {
  if (ev.button !== 0) return;
  if (drag) {
    const d = drag; drag = null;
    if (d.moved) { await api('/api/points/update', { id: +d.p.id.slice(1), x: d.p.x, y: d.p.y }); refreshPoints(true); }
    return;
  }
  if (ev.target !== view) return;
  const s = evToSensor(ev);
  if (!s) return;
  await api('/api/points/add', { x: s.x, y: s.y, r: +S.ptRadius });
  refreshPoints(true);
});
view.addEventListener('contextmenu', async (ev) => {
  ev.preventDefault();
  const p = pointAt(ev);
  if (p && p.id.startsWith('p')) { await api('/api/points/remove', { id: +p.id.slice(1) }); refreshPoints(true); }
});

// ------------------------------------------------------------ API ---
async function api(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.error || ('request failed: ' + path), true); throw new Error(j.error); }
  return j;
}
async function getJSON(path) { const r = await fetch(path, { cache: 'no-store' }); return r.json(); }

let toastTimer;
function toast(msg, bad) {
  let t = $('toast');
  if (!t) {
    t = document.createElement('div'); t.id = 'toast';
    Object.assign(t.style, { position: 'fixed', bottom: '18px', left: '50%', transform: 'translateX(-50%)',
      background: '#1f242b', border: '1px solid #333a44', padding: '9px 14px', borderRadius: '8px',
      zIndex: 99, maxWidth: '80vw', fontSize: '12px', boxShadow: '0 6px 24px rgba(0,0,0,.5)' });
    document.body.appendChild(t);
  }
  t.textContent = msg; t.style.color = bad ? '#ffb3bf' : '#d9dee5'; t.style.display = 'block';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.style.display = 'none'), bad ? 7000 : 4000);
}

// ---------------------------------------------------------- points ---
async function refreshPoints(resetHistory) {
  const j = await getJSON('/api/points');
  points = j.series; builtin = j.builtin;
  document.querySelectorAll('[data-builtin]').forEach((el) => (el.checked = !!builtin[el.dataset.builtin]));
  if (resetHistory) { for (const k in hist) delete hist[k]; histSince = 0; await pollHistory(); }
  renderPointList();
  render();
}
function renderPointList() {
  const el = $('ptList');
  const pts = points.filter((p) => p.x !== undefined);
  if (!pts.length) { el.innerHTML = '<div class="empty">No points yet — click the image.</div>'; return; }
  el.innerHTML = '';
  for (const p of pts) {
    const row = document.createElement('div'); row.className = 'pt';
    row.innerHTML = `<i style="background:${p.color}"></i><input value="${p.label.replace(/"/g, '&quot;')}">
      <span class="xy">${p.x},${p.y} · ${2 * p.r + 1}²</span><b data-v="${p.id}">–</b><button title="Remove">×</button>`;
    row.querySelector('input').addEventListener('change', async (e) => {
      await api('/api/points/update', { id: +p.id.slice(1), label: e.target.value }); refreshPoints(false);
    });
    row.querySelector('button').addEventListener('click', async () => {
      await api('/api/points/remove', { id: +p.id.slice(1) }); refreshPoints(true);
    });
    el.appendChild(row);
  }
}
function updatePointValues() {
  for (const p of points) {
    const b = document.querySelector(`#ptList b[data-v="${p.id}"]`);
    if (b) b.textContent = fmt(p.live);
  }
}

// ----------------------------------------------------------- chart ---
async function pollHistory() {
  try {
    const j = await getJSON('/api/history?since=' + histSince);
    serverNow = j.now;
    for (const s of j.series) {
      const h = hist[s.id] || (hist[s.id] = { t: [], v: [] });
      for (let i = 0; i < s.t.length; i++) { h.t.push(s.t[i]); h.v.push(s.v[i]); }
      if (s.t.length) histSince = Math.max(histSince, s.t[s.t.length - 1]);
      if (h.t.length > 200000) { h.t.splice(0, 20000); h.v.splice(0, 20000); }
    }
  } catch (e) { /* ignore */ }
}

const chart = $('chart'), cctx = chart.getContext('2d');
let chartHover = null;
chart.addEventListener('mousemove', (e) => { const r = chart.getBoundingClientRect(); chartHover = e.clientX - r.left; });
chart.addEventListener('mouseleave', () => (chartHover = null));

function niceTicks(lo, hi, n) {
  const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const W = chart.clientWidth, H = chart.clientHeight;
  if (chart.width !== Math.round(W * dpr) || chart.height !== Math.round(H * dpr)) {
    chart.width = Math.round(W * dpr); chart.height = Math.round(H * dpr);
  }
  const c = cctx;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  const L = 52, R = 12, T = 8, B = 22;
  const pw = W - L - R, ph = H - T - B;
  const series = points.map((p) => ({ ...p, h: hist[p.id] })).filter((s) => s.h && s.h.t.length);
  const legend = $('legend');
  legend.innerHTML = points.map((p) => {
    const h = hist[p.id]; const last = h && h.v.length ? h.v[h.v.length - 1] : null;
    return `<span><i style="background:${p.color}"></i>${p.label} <b>${fmt(last, 2)}</b></span>`;
  }).join('') || '<span style="color:var(--dim)">Click the thermal image to add measurement points</span>';
  c.strokeStyle = '#262b33'; c.lineWidth = 1;
  c.strokeRect(L + 0.5, T + 0.5, pw, ph);
  if (!series.length || pw < 20 || ph < 20) return;

  let tmax = 0, tmin = Infinity;
  for (const s of series) { tmax = Math.max(tmax, s.h.t[s.h.t.length - 1]); tmin = Math.min(tmin, s.h.t[0]); }
  if (S.chartWin > 0) tmin = tmax - S.chartWin;
  if (tmax - tmin < 1) tmin = tmax - 1;
  let vmin = Infinity, vmax = -Infinity;
  const vis = series.map((s) => {
    const t = s.h.t; let i0 = 0;
    let lo = 0, hi = t.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (t[m] < tmin) lo = m + 1; else hi = m; }
    i0 = lo;
    for (let i = i0; i < t.length; i++) { const v = toU(s.h.v[i]); if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
    return { s, i0 };
  });
  if (!isFinite(vmin)) return;
  const pad = Math.max(0.2, (vmax - vmin) * 0.08);
  vmin -= pad; vmax += pad;
  const X = (t) => L + ((t - tmin) / (tmax - tmin)) * pw;
  const Y = (v) => T + ph - ((v - vmin) / (vmax - vmin)) * ph;

  c.font = '11px ' + getComputedStyle(document.body).getPropertyValue('--mono');
  c.fillStyle = '#8a929e'; c.textAlign = 'right'; c.textBaseline = 'middle';
  for (const v of niceTicks(vmin, vmax, Math.max(2, Math.floor(ph / 32)))) {
    const y = Math.round(Y(v)) + 0.5;
    c.strokeStyle = '#1f242b'; c.beginPath(); c.moveTo(L, y); c.lineTo(L + pw, y); c.stroke();
    c.fillText(v.toFixed(Math.abs(vmax - vmin) < 5 ? 1 : 0), L - 6, y);
  }
  c.textAlign = 'center'; c.textBaseline = 'top';
  const span = tmax - tmin;
  const tstep = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400].find((s) => span / s <= Math.max(2, pw / 90)) || 14400;
  for (let t = Math.ceil(tmin / tstep) * tstep; t <= tmax; t += tstep) {
    const x = Math.round(X(t)) + 0.5;
    c.strokeStyle = '#1f242b'; c.beginPath(); c.moveTo(x, T); c.lineTo(x, T + ph); c.stroke();
    const d = new Date(t * 1000);
    const txt = tstep >= 60 ? d.toTimeString().slice(0, 5) : d.toTimeString().slice(0, 8);
    c.fillText(txt, x, T + ph + 5);
  }
  c.save(); c.beginPath(); c.rect(L, T, pw, ph); c.clip();
  for (const { s, i0 } of vis) {
    const t = s.h.t, v = s.h.v, n = t.length - i0;
    const stride = Math.max(1, Math.floor(n / (pw * 2)));
    c.strokeStyle = s.color; c.lineWidth = 1.6; c.beginPath();
    for (let i = i0, first = true; i < t.length; i += stride) {
      const x = X(t[i]), y = Y(toU(v[i]));
      if (first) { c.moveTo(x, y); first = false; } else c.lineTo(x, y);
    }
    c.stroke();
  }
  c.restore();
  if (chartHover != null && chartHover > L && chartHover < L + pw) {
    const th = tmin + ((chartHover - L) / pw) * (tmax - tmin);
    c.strokeStyle = 'rgba(255,255,255,.3)'; c.beginPath(); c.moveTo(chartHover + 0.5, T); c.lineTo(chartHover + 0.5, T + ph); c.stroke();
    const lines = [new Date(th * 1000).toTimeString().slice(0, 8)];
    for (const { s } of vis) {
      const t = s.h.t; let lo = 0, hi = t.length - 1;
      while (lo < hi) { const m = (lo + hi) >> 1; if (t[m] < th) lo = m + 1; else hi = m; }
      lines.push([s.color, `${s.label}: ${fmt(s.h.v[lo], 2)}`]);
    }
    c.textAlign = 'left'; c.textBaseline = 'top';
    const bw = Math.max(...lines.map((l) => c.measureText(typeof l === 'string' ? l : l[1]).width)) + 14;
    let bx = chartHover + 10; if (bx + bw > L + pw) bx = chartHover - bw - 10;
    c.fillStyle = 'rgba(10,12,15,.9)'; c.fillRect(bx, T + 4, bw, lines.length * 15 + 8);
    lines.forEach((l, i) => {
      c.fillStyle = typeof l === 'string' ? '#8a929e' : l[0];
      c.fillText(typeof l === 'string' ? l : l[1], bx + 7, T + 9 + i * 15);
    });
  }
}

// -------------------------------------------------------- controls ---
function segBind(id, key, onChange) {
  const el = $(id);
  const set = (v) => el.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === String(v)));
  set(S[key]);
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    const v = isNaN(+b.dataset.v) ? b.dataset.v : +b.dataset.v;
    S[key] = v; set(v); savePrefs(); onChange && onChange(v); render();
  });
  return set;
}

function initDisplay() {
  const pal = $('palettes');
  for (const [k, name] of Object.entries(PALETTE_NAMES)) {
    const b = document.createElement('button'); b.dataset.v = k;
    const cv = document.createElement('canvas'); cv.width = 128; cv.height = 1;
    const ctx = cv.getContext('2d'), im = ctx.createImageData(128, 1);
    for (let x = 0; x < 128; x++) { const c = x * 2; for (let j = 0; j < 3; j++) im.data[x * 4 + j] = LUTS[k][c * 3 + j]; im.data[x * 4 + 3] = 255; }
    ctx.putImageData(im, 0, 0);
    b.append(cv, document.createTextNode(name));
    b.classList.toggle('on', S.palette === k);
    b.onclick = () => { S.palette = k; pal.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b)); savePrefs(); render(); };
    pal.appendChild(b);
  }
  segBind('rangeMode', 'rangeMode', (v) => { if (v === 'manual') syncManual(); });
  $('rMin').value = S.rMin; $('rMax').value = S.rMax;
  $('rMin').onchange = () => { S.rMin = +$('rMin').value; S.rangeMode = 'manual'; setRangeSeg(); savePrefs(); render(); };
  $('rMax').onchange = () => { S.rMax = +$('rMax').value; S.rangeMode = 'manual'; setRangeSeg(); savePrefs(); render(); };
  $('rLock').onclick = () => { syncManual(); S.rangeMode = 'manual'; setRangeSeg(); savePrefs(); render(); };
  const setRangeSeg = () => $('rangeMode').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === S.rangeMode));
  function syncManual() {
    if (live.lo == null) return;
    S.rMin = +toU(live.lo).toFixed(1); S.rMax = +toU(live.hi).toFixed(1);
    $('rMin').value = S.rMin; $('rMax').value = S.rMax;
  }
  const prevUnit = { u: S.unit };
  segBind('units', 'unit', (u) => {
    // convert unit-bearing UI values
    const conv = (v) => { const saved = S.unit; S.unit = prevUnit.u; const k = fromU(v); S.unit = saved; return +toU(k).toFixed(1); };
    S.rMin = conv(S.rMin); S.rMax = conv(S.rMax); S.isoVal = conv(S.isoVal);
    $('rMin').value = S.rMin; $('rMax').value = S.rMax; $('isoVal').value = S.isoVal;
    prevUnit.u = u; savePrefs(); drawChart();
  });
  const bindSel = (id, key, num) => {
    const el = $(id); el.value = String(S[key]);
    el.onchange = () => { S[key] = num ? +el.value : el.value; savePrefs(); render(); };
  };
  bindSel('smooth', 'smooth'); bindSel('rotate', 'rot', true); bindSel('tavg', 'tavg', true);
  bindSel('isoMode', 'isoMode'); bindSel('ptRadius', 'ptRadius', true);
  $('isoVal').value = S.isoVal; $('isoVal').onchange = () => { S.isoVal = +$('isoVal').value; savePrefs(); render(); };
  for (const [id, key] of [['flipH', 'flipH'], ['flipV', 'flipV'], ['ovMinMax', 'ovMinMax'], ['ovCenter', 'ovCenter']]) {
    $(id).checked = !!S[key];
    $(id).onchange = () => { S[key] = $(id).checked; savePrefs(); render(); };
  }
  segBind('chartWin', 'chartWin', () => drawChart());
  $('chartClear').onclick = async () => { await api('/api/history/clear'); for (const k in hist) delete hist[k]; histSince = 0; };
  $('chartExport').onclick = exportChart;
  document.querySelectorAll('#tabs button').forEach((b) => b.onclick = () => {
    document.querySelectorAll('#tabs button').forEach((x) => x.classList.toggle('on', x === b));
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.id === 'tab-' + b.dataset.tab));
  });
}

// ---------------------------------------------------------- camera ---
function applyCamState(st) {
  camState = st;
  const pill = $('camPill');
  pill.className = 'pill ' + (st.connected ? (st.demo ? 'demo' : 'ok') : 'err');
  $('camName').textContent = st.connected ? st.name : 'not connected';
  const ban = $('banner');
  if (!st.connected && st.error) {
    ban.hidden = false;
    ban.innerHTML = '';
    ban.append(document.createTextNode('Camera error: ' + st.error + '\n'));
    const b1 = document.createElement('button'); b1.textContent = 'Retry'; b1.onclick = () => connect(false);
    const b2 = document.createElement('button'); b2.textContent = 'Use simulated camera'; b2.onclick = () => connect(true);
    ban.append(b1, b2);
  } else ban.hidden = true;
  if (st.settings) fillSettings(st.settings);
  $('camErr').textContent = st.settings && st.settings._errors
    ? 'Some controls could not be read: ' + Object.keys(st.settings._errors).join(', ') : (st.settings_error || '');
  const kv = { Camera: st.name || '–', ...(st.info || {}) };
  if (st.scale) kv['TLinear scale'] = st.scale + ' K/count';
  const fw = st.firmware_notice;
  $('infoKv').innerHTML = Object.entries(kv).map(([k, v]) => {
    const warn = fw && k === 'board_firmware';
    return `<label>${k.replace(/_/g, ' ')}</label><span${warn ? ' class="warn"' : ''}>${v}${warn ? ` (update to ${fw.latest})` : ''}</span>`;
  }).join('');
  showFirmwareNotice(fw);
  $('outDir').textContent = st.out_dir || '';
  setTimeout(() => window.dispatchEvent(new Event('resize')), 0);
}
function showFirmwareNotice(fw) {
  $('infoWarn').hidden = !fw;
  $('fwNotice').hidden = !fw;
  if (!fw) return;
  $('fwText').textContent = `This PureThermal runs firmware ${fw.current}; the latest is ${fw.latest}. ` + (fw.reason || '');
  const name = `pt1-${fw.latest}`;
  $('fwTgz').textContent = `${name}.tar.gz`;
  $('fwCmd').textContent = `dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D ~/Downloads/${name}/${name}.bin`;
  $('fwRelLink').href = fw.releases_url;
  $('fwGuideLink').href = fw.guide_url;
}
async function connect(demo) {
  $('camName').textContent = 'connecting…';
  try { applyCamState(await api('/api/camera/connect', { demo })); } catch (e) { /* toast shown */ }
}
function fillSettings(s) {
  document.querySelectorAll('[data-setting]').forEach((el) => {
    const k = el.dataset.setting; if (!(k in s)) return;
    const v = s[k];
    if (el.classList.contains('seg')) el.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === String(v)));
    else if (el.type === 'checkbox') el.checked = !!v;
    else if (document.activeElement !== el) el.value = String(v);
  });
}
async function setSetting(key, value) {
  try {
    const j = await api('/api/camera/setting', { key, value });
    fillSettings(j.settings);
    toast(`${key.replace(/_/g, ' ')} → ${value}`);
  } catch (e) { refreshCam(); }
}
async function refreshCam() { applyCamState(await getJSON('/api/camera')); }
function initCamera() {
  document.querySelectorAll('[data-setting]').forEach((el) => {
    const k = el.dataset.setting;
    if (el.classList.contains('seg')) {
      el.addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setSetting(k, b.dataset.v); });
    } else if (el.type === 'checkbox') el.onchange = () => setSetting(k, el.checked);
    else el.onchange = () => setSetting(k, el.tagName === 'SELECT' && isNaN(+el.value) ? el.value : +el.value);
  });
  $('emisPreset').onchange = () => { const v = $('emisPreset').value; if (v) setSetting('emissivity', +v); };
  const ffc = async () => { await api('/api/camera/ffc'); toast('FFC triggered'); };
  $('btnFFC').onclick = ffc; $('btnFFC2').onclick = ffc;
  $('reconnect').onclick = () => connect(false);
  $('useDemo').onclick = () => connect(true);
  $('openFolder').onclick = () => api('/api/open_folder');
  $('btnSnap').onclick = snapshot;
}
// Export what the chart shows: all plotted series, current window and unit.
async function exportChart() {
  try {
    const r = await fetch(`/api/history/export.csv?window=${S.chartWin}&unit=${S.unit}`, { cache: 'no-store' });
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const rows = +r.headers.get('X-Rows');
    if (!rows) { toast('Nothing plotted yet: add a point first'); return; }
    const name = (r.headers.get('Content-Disposition').match(/filename="([^"]+)"/) || [])[1] || 'chart.csv';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob()); a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast(`Exported ${rows} rows → ${r.headers.get('X-Saved-Path')}`);
  } catch (e) { toast('Export failed: ' + e.message); }
}
async function snapshot() {
  try {
    const j = await api('/api/snapshot', { unit: S.unit });
    const a = document.createElement('a');
    a.download = 'thermal_' + new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19) + '.png';
    a.href = view.toDataURL('image/png'); a.click();
    toast('Saved ' + j.files.length + ' files: ' + j.files[0].replace(/_kelvin\.npy$/, '_*'));
  } catch (e) { /* toast shown */ }
}

let lastStatus = {};
// Watchdog gave up: camera is connected but sends no video.
function showStreamError(msg) {
  const ban = $('banner');
  if (msg) {
    if (ban.dataset.stream === msg && !ban.hidden) return;
    ban.dataset.stream = msg;
    ban.hidden = false;
    ban.innerHTML = '';
    ban.append(document.createTextNode(msg + '\n'));
    const b = document.createElement('button'); b.textContent = 'Reconnect'; b.onclick = () => connect(false);
    ban.append(b);
  } else if (ban.dataset.stream) {
    delete ban.dataset.stream;
    ban.hidden = true;
  }
}
async function pollStatus() {
  try {
    const st = await getJSON('/api/status');
    lastStatus = st;
    showStreamError(st.stream_error);
    $('fpa').textContent = st.fpa_C != null ? st.fpa_C.toFixed(1) + ' °C' : '–';
    let ffc = st.ffc_status || '–';
    if (st.time_since_ffc_s != null && ffc === 'ready') ffc = `${Math.round(st.time_since_ffc_s)} s ago`;
    $('ffcStat').textContent = ffc;
    if (!st.connected) $('fps').textContent = '–';
    $('logBadge').hidden = !st.logging.active;
    $('recBadge').hidden = !st.recording.active;
    const lb = $('logBtn');
    lb.textContent = st.logging.active ? 'Stop logging' : 'Start logging';
    lb.classList.toggle('stop', st.logging.active);
    if (st.logging.active) $('logInfo').textContent = `${st.logging.rows} rows → ${st.logging.path}`;
    const rb = $('recBtn');
    rb.textContent = st.recording.active ? 'Stop recording' : 'Record raw frames';
    if (st.recording.active) $('recInfo').textContent = `${st.recording.frames} frames → ${st.recording.path}`;
    if (camState && camState.connected !== st.connected) refreshCam();
  } catch (e) { /* ignore */ }
}
function initLogging() {
  document.querySelectorAll('[data-builtin]').forEach((el) => el.onchange = async () => {
    await api('/api/points/builtin', { [el.dataset.builtin]: el.checked }); refreshPoints(true);
  });
  $('ptClear').onclick = async () => { await api('/api/points/clear'); refreshPoints(true); };
  $('logBtn').onclick = async () => {
    if (lastStatus.logging && lastStatus.logging.active) {
      const j = await api('/api/log/stop'); toast(`Log saved: ${j.path} (${j.rows} rows)`);
      $('logInfo').textContent = 'Saved ' + j.path;
    } else {
      if (!points.length) { toast('Add a point (click the image) or enable a frame statistic first', true); return; }
      const j = await api('/api/log/start', { interval_s: +$('logInt').value, unit: live.radiometric ? S.unit : 'K' });
      toast('Logging to ' + j.path);
    }
    pollStatus();
  };
  $('recBtn').onclick = async () => {
    if (lastStatus.recording && lastStatus.recording.active) {
      const j = await api('/api/record/stop'); toast(`Recording saved: ${j.path} (${j.frames} frames)`);
      $('recInfo').textContent = 'Saved ' + j.path;
    } else { const j = await api('/api/record/start'); toast('Recording to ' + j.path); }
    pollStatus();
  };
}

// --------------------------------------------------- super-resolution ---
const SR = { mode: 'superres', which: 'image', img: null, base: null, w: 0, h: 0, polling: false, shownId: 0 };
const SR_HELP = {
  superres: 'Hold the camera in your hand and keep it aimed at a still scene. The small, natural hand tremor '
    + 'makes each frame sample the scene at a slightly different sub-pixel offset; frames are registered and fused '
    + 'onto a finer grid, then sharpened by iterative back-projection. A tripod gives no new detail — use Integrate.',
  integrate: 'Camera on a tripod, still scene. Frames are averaged to cut temporal noise by about √N '
    + '(48 frames ≈ 7× lower noise). Enable drift compensation if the camera may creep.',
};
function initSR() {
  segBind('srMode', 'srMode_', (v) => { SR.mode = v; srModeUI(); });
  SR.mode = 'superres'; $('srMode').querySelector('[data-v=superres]').classList.add('on');
  const srModeUI = () => {
    $('srHelp').textContent = SR_HELP[SR.mode];
    $('srOpts').hidden = SR.mode !== 'superres';
    $('srAlignRow').hidden = SR.mode !== 'integrate';
  };
  srModeUI();
  $('srFrames').oninput = () => ($('srFramesV').textContent = $('srFrames').value);
  $('srStart').onclick = async () => {
    await api('/api/superres/start', { mode: SR.mode, frames: +$('srFrames').value, scale: +$('srScale').value,
      iterations: +$('srIter').value, align: $('srAlign').checked, ffc_first: $('srFFC').checked, unit: S.unit });
    pollSR();
  };
  $('srCancel').onclick = () => api('/api/superres/cancel');
  $('srClose').onclick = () => { $('srview').hidden = true; };
  $('srShow').onclick = () => openSRView();
  $('srWhich').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    SR.which = b.dataset.v;
    $('srWhich').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    renderSR();
  });
  $('srDownload').onclick = () => {
    const c = document.createElement('canvas');
    renderSRTo(c, true);
    const a = document.createElement('a');
    a.download = `thermal_${SR.mode}_${SR.which}_${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}.png`;
    a.href = c.toDataURL('image/png'); a.click();
  };
  const sc = $('srcanvas');
  sc.addEventListener('mousemove', (ev) => {
    if (!SR.img) return;
    const r = sc.getBoundingClientRect(), map = getMap(SR.w, SR.h);
    const u = Math.floor((ev.clientX - r.left) / r.width * map.dw), v = Math.floor((ev.clientY - r.top) / r.height * map.dh);
    const ro = $('srReadout');
    if (u < 0 || v < 0 || u >= map.dw || v >= map.dh) { ro.style.display = 'none'; return; }
    const arr = SR.which === 'baseline' ? SR.base : SR.img;
    const k = arr[map.idx[v * map.dw + u]];
    ro.textContent = fmt(k, 2);
    const box = $('srview').getBoundingClientRect();
    ro.style.left = (ev.clientX - box.left + 14) + 'px'; ro.style.top = (ev.clientY - box.top + 14) + 'px';
    ro.style.display = 'block';
  });
  sc.addEventListener('mouseleave', () => ($('srReadout').style.display = 'none'));
  pollSR();
}
async function pollSR() {
  if (SR.polling) return;
  SR.polling = true;
  try {
    for (;;) {
      const st = await getJSON('/api/superres');
      const active = st.state === 'capturing' || st.state === 'processing';
      $('srProg').style.width = (st.progress * 100).toFixed(1) + '%';
      $('srMsg').textContent = st.state === 'idle' && !st.message ? '' : (st.state === 'error' ? 'Error: ' : '') + (st.message || '');
      $('srStart').disabled = active; $('srCancel').hidden = !active;
      if (st.state === 'done' && st.result) {
        showSRStats(st.result);
        $('srShow').hidden = false;
        if (SR.shownId !== st.id) { SR.shownId = st.id; await loadSR(st.result); openSRView(); }
      }
      if (!active) break;
      await sleep(250);
    }
  } finally { SR.polling = false; }
}
function showSRStats(r) {
  const kv = {
    'Frames used': r.frames_used, Output: `${r.shape[1]} × ${r.shape[0]}`,
    'Camera motion': (r.motion_px ?? 0).toFixed(2) + ' px',
  };
  if (r.subpixel_coverage != null) kv['Sub-pixel coverage'] = Math.round(r.subpixel_coverage * 100) + '%';
  if (r.noise_single_K != null) kv['Noise / frame'] = (r.noise_single_K * 1000).toFixed(0) + ' mK';
  if (r.noise_result_K != null) kv['Noise / result'] = (r.noise_result_K * 1000).toFixed(0) + ' mK';
  kv['Compute time'] = (r.seconds ?? 0).toFixed(1) + ' s';
  $('srStats').innerHTML = Object.entries(kv).map(([k, v]) => `<label>${k}</label><span>${v}</span>`).join('');
  let warn = '';
  if (r.scale > 1 && r.motion_px < 0.25) warn = 'Very little camera motion was detected, so the result is mostly a sharpened average. Hand-hold the camera (no tripod) for real resolution gain.';
  else if (r.scale > 1 && r.subpixel_coverage != null && r.subpixel_coverage < 0.5) warn = 'Sub-pixel coverage is low — capture more frames or move a little more.';
  if (warn) $('srMsg').textContent = warn;
  $('srInfo').textContent = `${r.shape[1]}×${r.shape[0]} · ${r.frames_used} frames · motion ${(r.motion_px ?? 0).toFixed(2)} px`;
}
async function loadSR(r) {
  const get = async (which) => {
    const resp = await fetch('/api/superres/image?which=' + which, { cache: 'no-store' });
    return { a: new Float32Array(await resp.arrayBuffer()), w: +resp.headers.get('X-Width'), h: +resp.headers.get('X-Height') };
  };
  const a = await get('image'), b = await get('baseline');
  SR.img = a.a; SR.w = a.w; SR.h = a.h;
  // baseline may be a different size (integrate mode): resample nearest to result size
  if (b.w === a.w && b.h === a.h) SR.base = b.a;
  else {
    SR.base = new Float32Array(a.w * a.h);
    for (let y = 0; y < a.h; y++) for (let x = 0; x < a.w; x++)
      SR.base[y * a.w + x] = b.a[Math.floor(y * b.h / a.h) * b.w + Math.floor(x * b.w / a.w)];
  }
  const [lo, hi] = percentileRange(SR.img, 0.005, 0.995);
  SR.lo = lo; SR.hi = hi;
}
function openSRView() { if (!SR.img) return; $('srview').hidden = false; renderSR(); }
function renderSRTo(canvas, native) {
  const map = getMap(SR.w, SR.h);
  const arr = SR.which === 'baseline' ? SR.base : SR.img;
  let lo = SR.lo, hi = SR.hi;
  if (S.rangeMode === 'manual') { lo = fromU(+S.rMin); hi = fromU(+S.rMax); }
  const o = document.createElement('canvas'); o.width = map.dw; o.height = map.dh;
  const octx = o.getContext('2d'), img = octx.createImageData(map.dw, map.dh);
  colorize(arr, SR.w, SR.h, lo, hi, img, map);
  octx.putImageData(img, 0, 0);
  if (native) { canvas.width = map.dw; canvas.height = map.dh; canvas.getContext('2d').drawImage(o, 0, 0); return; }
  const g = fitCanvas(canvas, $('srview'), map.dw, map.dh, 46);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(g.dpr, 0, 0, g.dpr, 0, 0);
  ctx.imageSmoothingEnabled = S.smooth === 'smooth';
  ctx.drawImage(o, 0, 0, g.cssW, g.cssH);
  drawColorbar(lo, hi);
}
function renderSR() { if (SR.img && !$('srview').hidden) renderSRTo($('srcanvas'), false); }

// ------------------------------------------------------------- boot ---
window.addEventListener('resize', () => { render(); renderSR(); drawChart(); });
async function boot() {
  initDisplay(); initCamera(); initLogging(); initSR();
  try { applyCamState(await getJSON('/api/camera')); } catch (e) { /* server down */ }
  await refreshPoints(true);
  frameLoop();
  setInterval(pollStatus, 1500); pollStatus();
  setInterval(async () => { await pollHistory(); drawChart(); updatePointValues(); }, 500);
}
boot();
