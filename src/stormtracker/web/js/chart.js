import { h, svg, clear } from './dom.js';
import { time, timeSec, dateTime, day, hourLabel, tzOffsetMs } from './time.js';
import { fmt } from './units.js';

const M = { l: 44, r: 14, t: 10, b: 24 };
const STEPS = [10, 15, 30, 60, 120, 180, 360, 720, 1440].map((m) => m * 60000);

function niceTicks(lo, hi, n = 4) {
  if (!(hi > lo)) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
  const a = Math.floor(lo / step) * step, out = [];
  for (let v = a; v <= hi + step * 0.001; v += step) out.push(+v.toFixed(10));
  return { ticks: out, step };
}

function timeTicks(t0, t1, width) {
  const want = Math.max(3, Math.floor(width / 90));
  const step = STEPS.find((s) => (t1 - t0) / s <= want) || STEPS.at(-1);
  const off = tzOffsetMs(t0);                       // align to Sydney clock times, not UTC
  const out = [];
  for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1; t += step) out.push(t);
  return { ticks: out, step };
}
function tickLabel(t, step) {
  const off = tzOffsetMs(t), local = new Date(t + off), midnight = local.getUTCHours() === 0 && local.getUTCMinutes() === 0;
  if (step >= 1440 * 60000 || midnight) return day(t).replace(/,? \d+ \w+$/, '').replace(/^(\w+) (\d+).*/, '$1 $2');
  return step < 3600000 ? time(t) : hourLabel(t);
}

/**
 * series: [{key, name, color, scale=1, dp=1}]; opts: {title, unit, height, zeroBase, yMin, yMax, area}
 * rows given to update(): [{t: ms, v: {key: number}}] sorted ascending.
 */
export function lineChart(series, opts = {}) {
  const height = opts.height || 170;
  const root = h('div', { class: 'chart', tabindex: 0 });
  const svgEl = svg('svg', { height, role: 'img' });
  const tip = h('div', { class: 'tip', hidden: true });
  root.append(svgEl, tip);
  let rows = [], domain = [0, 1], width = 600, hover = -1;

  const pts = (s) => rows.map((r) => (r.v[s.key] == null ? null : { t: r.t, y: r.v[s.key] * (s.scale || 1) }));

  function yRange() {
    let lo = Infinity, hi = -Infinity;
    for (const s of series) for (const p of pts(s)) if (p) { lo = Math.min(lo, p.y); hi = Math.max(hi, p.y); }
    if (!isFinite(lo)) return [0, 1];
    if (opts.zeroBase) lo = Math.min(lo, 0);
    if (opts.yMin != null) lo = opts.yMin;
    if (opts.yMax != null) hi = Math.max(hi, opts.yMax);
    if (hi - lo < (opts.minSpan || 1)) {
      if (opts.zeroBase && lo >= 0) hi = lo + (opts.minSpan || 1);              // a flat series of rain or wind stays on a 0-based axis
      else { const m = (hi + lo) / 2; lo = m - (opts.minSpan || 1) / 2; hi = m + (opts.minSpan || 1) / 2; }
    }
    const pad = (hi - lo) * 0.08;
    return [opts.yMin != null || opts.zeroBase ? lo : lo - pad, opts.yMax != null && opts.yMaxFixed ? opts.yMax : hi + pad];
  }

  function draw() {
    clear(svgEl);
    svgEl.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const [t0, t1] = domain, iw = width - M.l - M.r, ih = height - M.t - M.b;
    const [y0, y1] = yRange();
    const { ticks: yt } = niceTicks(y0, y1, 4);
    const lo = opts.zeroBase ? Math.min(y0, yt[0]) : y0, hi = Math.max(y1, yt.at(-1));
    const X = (t) => M.l + ((t - t0) / (t1 - t0)) * iw;
    const Y = (v) => M.t + (1 - (v - lo) / (hi - lo)) * ih;

    const grid = svg('g', { class: 'grid' }), axis = svg('g', { class: 'axis' });
    for (const v of yt) {
      if (v < lo - 1e-9 || v > hi + 1e-9) continue;
      grid.append(svg('line', { x1: M.l, x2: width - M.r, y1: Y(v), y2: Y(v) }));
      axis.append(svg('text', { x: M.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, fmt(v, opts.tickDp ?? (yt[1] - yt[0] < 1 ? 1 : 0))));
    }
    const tt = timeTicks(t0, t1, iw);
    for (const t of tt.ticks) {
      grid.append(svg('line', { x1: X(t), x2: X(t), y1: M.t, y2: M.t + ih, opacity: 0.6 }));
      axis.append(svg('text', { x: X(t), y: height - 6, 'text-anchor': 'middle' }, tickLabel(t, tt.step)));
    }
    svgEl.append(grid, axis);

    // break the line where data stops (device offline) rather than drawing across the gap
    const dts = rows.slice(1).map((r, i) => r.t - rows[i].t).sort((a, b) => a - b);
    const gap = Math.max(5 * 60000, (dts[Math.floor(dts.length / 2)] || 60000) * 4);

    series.forEach((s) => {
      const p = pts(s);
      let d = '', started = false, prev = null, area = '';
      const segs = [];
      let cur = [];
      p.forEach((q) => {
        if (!q) return;
        if (prev && q.t - prev.t > gap) { segs.push(cur); cur = []; }
        cur.push(q); prev = q;
      });
      if (cur.length) segs.push(cur);
      for (const seg of segs) {
        if (seg.length === 1) { svgEl.append(svg('circle', { cx: X(seg[0].t), cy: Y(seg[0].y), r: 2.5, fill: s.color })); continue; }
        d = seg.map((q, i) => `${i ? 'L' : 'M'}${X(q.t).toFixed(1)} ${Y(q.y).toFixed(1)}`).join('');
        if (opts.area && series.length === 1) {
          area = d + `L${X(seg.at(-1).t).toFixed(1)} ${Y(Math.max(lo, 0)).toFixed(1)}L${X(seg[0].t).toFixed(1)} ${Y(Math.max(lo, 0)).toFixed(1)}Z`;
          svgEl.append(svg('path', { d: area, fill: s.color, 'fill-opacity': 0.12 }));
        }
        svgEl.append(svg('path', { d, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      }
      const last = [...p].reverse().find(Boolean);
      if (last) svgEl.append(svg('circle', { cx: X(last.t), cy: Y(last.y), r: 4, fill: s.color, stroke: 'var(--card)', 'stroke-width': 2 }));
    });

    if (hover >= 0 && rows[hover]) {
      const r = rows[hover], x = X(r.t);
      svgEl.append(svg('line', { x1: x, x2: x, y1: M.t, y2: M.t + ih, stroke: 'var(--ink-3)', 'stroke-width': 1 }));
      series.forEach((s) => { if (r.v[s.key] != null) svgEl.append(svg('circle', { cx: x, cy: Y(r.v[s.key] * (s.scale || 1)), r: 4.5, fill: s.color, stroke: 'var(--card)', 'stroke-width': 2 })); });
      tip.hidden = false;
      clear(tip).append(h('div', { class: 't' }, dateTime(r.t)),
        ...series.filter((s) => r.v[s.key] != null).map((s) => h('div', { class: 'r' }, h('i', { style: { background: s.color } }), s.name, h('b', {}, `${fmt(r.v[s.key] * (s.scale || 1), s.dp ?? 1)}${opts.unit ? ' ' + opts.unit : ''}`))),
        ...(opts.extra ? [h('div', { class: 't' }, opts.extra(r))] : []));
      const tw = tip.offsetWidth || 120;
      tip.style.left = Math.min(Math.max(x, tw / 2 + 4), width - tw / 2 - 4) + 'px';
      tip.style.top = Math.max(M.t + 6, Math.min(...series.map((s) => (r.v[s.key] == null ? ih : Y(r.v[s.key] * (s.scale || 1))))) - 12) + 'px';
    } else tip.hidden = true;

    // summary for assistive tech
    const latest = rows.at(-1);
    svgEl.setAttribute('aria-label', `${opts.title || 'Chart'}. ` + (latest ? series.map((s) => latest.v[s.key] != null ? `${s.name} ${fmt(latest.v[s.key] * (s.scale || 1), s.dp ?? 1)} ${opts.unit || ''}` : '').filter(Boolean).join(', ') + ` at ${time(latest.t)}.` : 'No data.'));
  }

  function nearest(clientX) {
    const box = svgEl.getBoundingClientRect(), x = ((clientX - box.left) / box.width) * width;
    const t = domain[0] + ((x - M.l) / (width - M.l - M.r)) * (domain[1] - domain[0]);
    let lo = 0, hi = rows.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (rows[m].t < t) lo = m + 1; else hi = m; }
    if (lo > 0 && Math.abs(rows[lo - 1].t - t) < Math.abs(rows[lo].t - t)) lo--;
    return lo;
  }
  svgEl.style.touchAction = 'pan-y';
  svgEl.addEventListener('pointermove', (e) => { if (rows.length) { hover = nearest(e.clientX); draw(); } });
  svgEl.addEventListener('pointerdown', (e) => { if (rows.length) { hover = nearest(e.clientX); draw(); } });
  svgEl.addEventListener('pointerleave', () => { hover = -1; draw(); });
  root.addEventListener('keydown', (e) => {
    if (!rows.length || !['ArrowLeft', 'ArrowRight', 'Escape'].includes(e.key)) return;
    e.preventDefault();
    hover = e.key === 'Escape' ? -1 : Math.max(0, Math.min(rows.length - 1, (hover < 0 ? rows.length - 1 : hover) + (e.key === 'ArrowLeft' ? -1 : 1) * Math.max(1, Math.round(rows.length / 60))));
    draw();
  });
  root.addEventListener('blur', () => { hover = -1; draw(); });

  const ro = new ResizeObserver(() => { const w = Math.round(root.clientWidth); if (w && w !== width) { width = w; draw(); } });
  ro.observe(root);
  width = root.clientWidth || 600;

  return {
    el: root,
    update(newRows, newDomain) { rows = newRows; domain = newDomain; hover = -1; width = root.clientWidth || width; draw(); },
    table() {
      const head = h('tr', {}, h('th', {}, 'Time (Sydney)'), ...series.map((s) => h('th', {}, `${s.name}${opts.unit ? ' (' + opts.unit + ')' : ''}`)));
      const body = [...rows].reverse().map((r) => h('tr', {}, h('td', {}, `${day(r.t)}, ${timeSec(r.t)}`), ...series.map((s) => h('td', {}, r.v[s.key] == null ? '–' : fmt(r.v[s.key] * (s.scale || 1), s.dp ?? 1)))));
      return h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, h('thead', {}, head), h('tbody', {}, body)));
    },
    hasData: () => rows.some((r) => series.some((s) => r.v[s.key] != null)),
    destroy() { ro.disconnect(); },
  };
}
