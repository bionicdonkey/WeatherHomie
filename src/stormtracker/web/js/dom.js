/** Tiny element builder: h('div', {class: 'x', onclick: fn}, 'text', childNode, [more]) */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'html') el.innerHTML = v;                     // only ever used with trusted constants
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
export function svg(tag, attrs, ...kids) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  append(el, kids);
  return el;
}
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
/** Like el.append(...) but skips null/false and flattens arrays (native append() writes the text "null"). */
export function add(el, ...kids) { append(el, kids); return el; }

export function toast(msg, kind) {
  const t = h('div', { class: 'toast' + (kind === 'bad' ? ' bad' : '') }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 6000 : 2800);
}

export const icons = {
  play: () => svg('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'currentColor', 'aria-hidden': 'true' }, svg('path', { d: 'M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z' })),
  pause: () => svg('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'currentColor', 'aria-hidden': 'true' }, svg('rect', { x: 6, y: 5, width: 4, height: 14, rx: 1 }), svg('rect', { x: 14, y: 5, width: 4, height: 14, rx: 1 })),
  layers: () => svg('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, svg('path', { d: 'm12 3 9 5-9 5-9-5 9-5Z' }), svg('path', { d: 'm3 13 9 5 9-5' })),
  sun: () => svg('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'aria-hidden': 'true' }, svg('circle', { cx: 12, cy: 12, r: 4 }), svg('path', { d: 'M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4' })),
  moon: () => svg('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, svg('path', { d: 'M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z' })),
  logout: () => svg('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, svg('path', { d: 'M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H4' })),
  logo: () => svg('svg', { width: 22, height: 22, viewBox: '0 0 32 32', 'aria-hidden': 'true' },
    svg('circle', { cx: 16, cy: 16, r: 13, fill: 'none', stroke: 'currentColor', 'stroke-width': 2.4, opacity: .85 }),
    svg('circle', { cx: 16, cy: 16, r: 7, fill: 'none', stroke: 'var(--s1)', 'stroke-width': 2.4 }),
    svg('circle', { cx: 16, cy: 16, r: 2.8, fill: 'var(--home)' })),
};

const ACRONYMS = { bom: 'BOM', pws: 'PWS', url: 'URL', urls: 'URLs', id: 'ID', api: 'API', mac: 'MAC', wu: 'WU', ftp: 'FTP', mmh: 'mm/h', http: 'HTTP', lat: 'latitude', lon: 'longitude' };
/** snake_case or lower-case name -> "Sentence case" with acronyms fixed (BOM, URLs, ID...). */
export function nice(name) {
  const words = String(name).replace(/[_-]+/g, ' ').trim().split(/\s+/).map((w) => ACRONYMS[w.toLowerCase()] || w.toLowerCase());
  const s = words.join(' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}
