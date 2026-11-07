import { h, add, clear } from '../dom.js';
import { get } from '../api.js';
import { lineChart } from '../chart.js';
import { time, when, dateTime, ago, minutesSince } from '../time.js';
import { fmt, msToKmh, compass } from '../units.js';
import { ranked } from '../stations.js';

const S1 = 'var(--s1)', S2 = 'var(--s2)';
const PANELS = [
  { id: 'temp', title: 'Temperature', unit: '°C', series: [{ key: 'temp_c', name: 'Air', color: S1 }, { key: 'dewpoint_c', name: 'Dew point', color: S2 }], opts: { minSpan: 4 } },
  { id: 'rain', title: 'Rain rate', unit: 'mm/h', series: [{ key: 'rain_rate_mmh', name: 'Rain rate', color: S1 }], opts: { zeroBase: true, area: true, minSpan: 2 } },
  { id: 'rain9', title: 'Rain since 9 am', unit: 'mm', series: [{ key: 'rain_since_9am_mm', name: 'Since 9 am', color: S1 }], opts: { zeroBase: true, minSpan: 2 } },
  { id: 'rain-day', title: 'Rain since midnight', unit: 'mm', series: [{ key: 'rain_daily_mm', name: 'Rain today', color: S1 }], opts: { zeroBase: true, minSpan: 2 } },
  { id: 'wind', title: 'Wind', unit: 'km/h', series: [{ key: 'wind_speed_ms', name: 'Average', color: S1, scale: 3.6 }, { key: 'wind_gust_ms', name: 'Gust', color: S2, scale: 3.6 }],
    opts: { zeroBase: true, minSpan: 10, extra: (r) => (r.v.wind_dir_deg != null ? `From the ${compass(r.v.wind_dir_deg)} (${fmt(r.v.wind_dir_deg)}°)` : '') } },
  { id: 'pressure', title: 'Pressure', unit: 'hPa', series: [{ key: 'pressure_hpa', name: 'Sea level', color: S1 }], opts: { minSpan: 4 } },
  { id: 'humidity', title: 'Humidity', unit: '%', series: [{ key: 'humidity_pct', name: 'Relative humidity', color: S1, dp: 0 }], opts: { yMin: 0, yMax: 100, yMaxFixed: true } },
  { id: 'solar', title: 'Solar radiation', unit: 'W/m²', series: [{ key: 'solar_wm2', name: 'Solar', color: S1, dp: 0 }], opts: { zeroBase: true, area: true, minSpan: 100 } },
];
const RANGES = [['3 h', 3], ['6 h', 6], ['24 h', 24], ['3 days', 72]];
const UNIT_HINT = [[/_c(_\d+)?$/, '°C'], [/_pct(_\d+)?$/, '%'], [/_hpa$/, 'hPa'], [/_ms$/, 'm/s'], [/_mmh$/, 'mm/h'], [/_mm$/, 'mm'], [/_wm2$/, 'W/m²'], [/_deg$/, '°'], [/_km$/, 'km'], [/_ugm3(_\d+)?$/, 'µg/m³']];
const unitOf = (k) => (UNIT_HINT.find(([rx]) => rx.test(k)) || [, ''])[1];

export async function mount(root) {
  const stations = ranked(await get('/stations'));
  const page = h('div', { class: 'page' }), wrap = h('div', { class: 'wrap' });
  page.append(wrap); root.append(page);
  if (!stations.length) {
    wrap.append(h('div', { class: 'page-head' }, h('h1', {}, 'Station')),
      h('div', { class: 'card empty' }, h('strong', {}, 'No station has reported yet'), 'Once a source that reads your weather station is working, its readings appear here. ', h('a', { href: '#/sources' }, 'Set up a source')));
    return;
  }

  let cur = stations[0], hours = 24, timer = null, charts = [];
  const select = h('select', { 'aria-label': 'Station', onchange: () => { cur = stations[+select.value]; load(); } },
    stations.map((s, i) => h('option', { value: i }, `${s.station_id} (${s.source})`)));
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Time range' }, RANGES.map(([l, hr]) =>
    h('button', { 'aria-pressed': String(hr === hours), onclick: (e) => { hours = hr; seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === e.currentTarget))); load(); } }, l)));
  const heroEl = h('div', {}), chartsEl = h('div', { class: 'grid cols-2' }), modelEl = h('div', { class: 'grid cols-2', style: { marginTop: '14px' } }), extraEl = h('div', { class: 'grid', style: { marginTop: '14px' } });
  wrap.append(h('div', { class: 'page-head' }, h('h1', {}, 'Station')), h('div', { class: 'toolbar' }, stations.length > 1 ? select : h('strong', {}, `${cur.station_id}`), seg), heroEl, chartsEl, modelEl, extraEl);

  async function paintModel() {
    clear(modelEl);
    const m = ((await get('/forecast').catch(() => [])) || [])[0];
    if (!m || !m.hours.length) return;
    const now = Date.now(), rows = m.hours.map((x) => ({ t: new Date(x.t).getTime(), v: x })).filter((r) => r.t >= now - 3600e3 && r.t <= now + 30 * 3600e3);
    const sm = m.summary || {}, pot = sm.potential || {};
    const mk = (title, series, opts) => { const c = lineChart(series, { title, ...opts }); c.update(rows, [now - 3600e3, now + 30 * 3600e3]); charts.push(c);
      return h('section', { class: 'card chart-card' }, h('header', {}, h('h3', {}, title), h('span', { class: 'faint', style: { fontSize: '13px' } }, opts.unit)), c.el); };
    add(modelEl, h('section', { class: 'card', style: { gridColumn: '1 / -1' } },
      h('h3', { style: { marginBottom: '4px' } }, 'Storm ingredients (weather model)'),
      h('p', { style: { margin: '0 0 6px', fontSize: '15px' } }, pot.level && pot.level !== 'none' ? `Storm potential ${pot.level}${pot.organised ? ', organised' : ''} over the next 12 hours: CAPE up to ${fmt(sm.max_cape_jkg)} J/kg${sm.max_cape_at ? ` around ${when(sm.max_cape_at)}` : ''}, strong shear ${pot.organised ? 'present' : 'not present'}.` : 'Little storm fuel in the model over the next 12 hours.'),
      h('p', { class: 'muted', style: { margin: 0, fontSize: '13px' } }, `CAPE is the energy available to storms; lifted index below zero means unstable air; shear (wind change with height) lets storms last. These are ingredients, not a forecast. ${m.attribution || ''}`)),
      mk('CAPE', [{ key: 'cape_jkg', name: 'CAPE', color: S1, dp: 0 }], { unit: 'J/kg', zeroBase: true, area: true, minSpan: 500 }),
      mk('Chance of rain', [{ key: 'precip_prob_pct', name: 'Chance of rain', color: S1, dp: 0 }], { unit: '%', yMin: 0, yMax: 100, yMaxFixed: true }),
      rows.some((r) => r.v.gust_10m_ms != null || r.v.wind_10m_ms != null)
        ? mk('Wind forecast', [{ key: 'wind_10m_ms', name: 'Average', color: S1, scale: 3.6 }, { key: 'gust_10m_ms', name: 'Gust', color: S2, scale: 3.6 }],
          { unit: 'km/h', zeroBase: true, minSpan: 20, extra: (r) => (r.v.wind_dir_10m_deg != null ? `From the ${compass(r.v.wind_dir_10m_deg)} (${fmt(r.v.wind_dir_10m_deg)}°)` : '') }) : null,
      sm.max_gust_ms != null ? h('section', { class: 'card' }, h('h3', { style: { marginBottom: '4px' } }, 'Strongest gust forecast'),
        h('p', { style: { margin: 0, fontSize: '15px' } }, `${fmt(msToKmh(sm.max_gust_ms))} km/h${sm.max_gust_at ? ` around ${when(sm.max_gust_at)}` : ''}, over the next 12 hours. A model forecast at 10 m for your location: local gusts can be stronger.`)) : null);
  }

  async function load() {
    const now = Date.now(), since = new Date(now - hours * 3600e3).toISOString();
    let rows = [];
    try {
      rows = (await get(`/observations?source=${encodeURIComponent(cur.source)}&station_id=${encodeURIComponent(cur.station_id)}&since=${since}&order=asc&limit=20000`)).map((o) => ({ t: new Date(o.ts).getTime(), v: o.values }));
    } catch (e) { clear(heroEl).append(h('p', { class: 'err' }, e.message)); return; }
    const events = await get(`/events?event_type=lightning&since=${since}&limit=50`).catch(() => []);
    paintHero(rows); paintCharts(rows, [now - hours * 3600e3, now]); paintExtra(rows, events);
    await paintModel();
  }

  function paintHero(rows) {
    const last = rows.at(-1);
    clear(heroEl);
    if (!last) { heroEl.append(h('div', { class: 'card empty', style: { marginBottom: '14px' } }, h('strong', {}, 'Nothing in this time range'), 'Try a longer range. The station may be offline.')); return; }
    const v = last.v, stale = minutesSince(last.t) > 15;
    const stat = (label, val) => (val == null ? null : h('div', {}, h('b', {}, val), h('span', {}, label)));
    heroEl.append(h('div', { class: 'hero' },
      v.temp_c != null ? h('div', {}, h('div', { class: 'big' }, fmt(v.temp_c, 1), h('small', {}, '°C')), h('div', { class: stale ? '' : 'muted', style: stale ? { color: 'var(--warn)', fontWeight: 650 } : {} }, stale ? `Last report ${ago(last.t)}` : `Updated ${ago(last.t)}`)) : null,
      h('div', { class: 'stats' },
        stat('Feels like', v.feels_like_c != null ? `${fmt(v.feels_like_c)}°` : null),
        stat('Humidity', v.humidity_pct != null ? `${fmt(v.humidity_pct)}%` : null),
        stat('Wind', v.wind_speed_ms != null ? `${fmt(msToKmh(v.wind_speed_ms))} km/h${v.wind_dir_deg != null ? ' ' + compass(v.wind_dir_deg) : ''}` : null),
        stat('Rain today', v.rain_daily_mm != null ? `${fmt(v.rain_daily_mm, 1)} mm` : null),
        stat('Pressure', v.pressure_hpa != null ? `${fmt(v.pressure_hpa)} hPa` : null))));
  }

  function paintCharts(rows, domain) {
    charts.forEach((c) => c.destroy()); charts = [];
    clear(chartsEl);
    for (const p of PANELS) {
      if (!rows.some((r) => p.series.some((s) => r.v[s.key] != null))) continue;
      const series = p.series.filter((s) => rows.some((r) => r.v[s.key] != null));
      const chart = lineChart(series, { title: p.title, unit: p.unit, ...p.opts });
      chart.update(rows, domain);
      charts.push(chart);
      const tableHolder = h('div', {});
      const tbtn = h('button', { class: 'linkbtn', 'aria-expanded': 'false', onclick: () => {
        const open = tbtn.getAttribute('aria-expanded') === 'true';
        tbtn.setAttribute('aria-expanded', String(!open)); tbtn.textContent = open ? 'View as table' : 'Hide table';
        clear(tableHolder); if (!open) tableHolder.append(chart.table());
      } }, 'View as table');
      chartsEl.append(h('section', { class: 'card chart-card' },
        h('header', {}, h('h3', {}, p.title), h('span', { class: 'faint', style: { fontSize: '13px' } }, p.unit),
          series.length > 1 ? h('div', { class: 'legend-inline' }, series.map((s) => h('span', {}, h('i', { style: { background: s.color } }), s.name))) : null),
        chart.el, h('div', { class: 'chart-actions' }, tbtn), tableHolder));
    }
    if (!charts.length) chartsEl.append(h('div', { class: 'card empty' }, 'No chartable readings in this range.'));
  }

  // Wind rose: how often the wind came from each direction, split by speed. Bars point at the direction the wind blows FROM.
  const BANDS = [[0, 10, 'under 10'], [10, 20, '10–20'], [20, 30, '20–30'], [30, 40, '30–40'], [40, Infinity, '40 and over']], OPAC = [0.25, 0.45, 0.65, 0.85, 1];
  function roseCard(rows) {
    const pts = rows.filter((r) => r.v.wind_dir_deg != null && r.v.wind_speed_ms != null);
    if (pts.length < 8) return null;
    const bins = Array.from({ length: 16 }, () => new Array(BANDS.length).fill(0));
    let calm = 0;
    for (const r of pts) {
      const kmh = msToKmh(r.v.wind_speed_ms);
      if (kmh < 1) { calm++; continue; }
      bins[Math.round((((r.v.wind_dir_deg % 360) + 360) % 360) / 22.5) % 16][BANDS.findIndex(([a, b]) => kmh >= a && kmh < b)]++;
    }
    const totals = bins.map((b) => b.reduce((x, y) => x + y, 0)), n = pts.length, top = Math.max(...totals, 1);
    const best = totals.indexOf(top), R = 92, rad = (i) => (i * 22.5 * Math.PI) / 180;
    const pt = (r, i) => `${(Math.sin(rad(i)) * r).toFixed(1)},${(-Math.cos(rad(i)) * r).toFixed(1)}`;
    let g = [0.25, 0.5, 0.75, 1].map((f) => `<circle r="${(R * f).toFixed(1)}" fill="none" stroke="var(--rule-2)" stroke-width="1"/>`).join('');
    bins.forEach((b, i) => {
      let inner = 0;
      b.forEach((c, k) => {
        if (!c) return;
        const outer = inner + (c / top) * R, w = 11.25, a0 = rad(i) - (w * Math.PI) / 180, a1 = rad(i) + (w * Math.PI) / 180;
        const P = (r, a) => `${(Math.sin(a) * r).toFixed(1)} ${(-Math.cos(a) * r).toFixed(1)}`;
        g += `<path d="M${P(inner, a0)} L${P(outer, a0)} A${outer.toFixed(1)} ${outer.toFixed(1)} 0 0 1 ${P(outer, a1)} L${P(inner, a1)} A${inner.toFixed(1)} ${inner.toFixed(1)} 0 0 0 ${P(inner, a0)}Z" fill="var(--s1)" fill-opacity="${OPAC[k]}" stroke="var(--card)" stroke-width="1"/>`;
        inner = outer;
      });
    });
    g += [['N', 0], ['E', 4], ['S', 8], ['W', 12]].map(([l, i]) => `<text x="${pt(R + 12, i).split(',')[0]}" y="${+pt(R + 12, i).split(',')[1] + 4}" text-anchor="middle" font-size="12" font-weight="650" fill="var(--ink-2)">${l}</text>`).join('');
    const svg = h('div', {}); svg.innerHTML = `<svg viewBox="-115 -115 230 230" width="230" height="230" role="img" aria-label="Wind rose: the wind came most often from the ${compass(best * 22.5)}">${g}</svg>`;
    const pct = (c) => (c / n < 0.005 ? '<1%' : `${Math.round((c / n) * 100)}%`);
    const table = h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, h('thead', {}, h('tr', {}, h('th', {}, 'From'), ...BANDS.map((b) => h('th', {}, `${b[2]} km/h`)), h('th', {}, 'All'))),
      h('tbody', {}, bins.map((b, i) => (totals[i] ? h('tr', {}, h('td', {}, compass(i * 22.5)), ...b.map((c) => h('td', {}, c ? pct(c) : '–')), h('td', {}, pct(totals[i]))) : null)))));
    const holder = h('div', {}), tbtn = h('button', { class: 'linkbtn', 'aria-expanded': 'false', onclick: () => {
      const open = tbtn.getAttribute('aria-expanded') === 'true'; tbtn.setAttribute('aria-expanded', String(!open)); tbtn.textContent = open ? 'View as table' : 'Hide table'; clear(holder); if (!open) holder.append(table);
    } }, 'View as table');
    return h('section', { class: 'card' }, h('header', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' } }, h('h3', {}, 'Wind rose'), h('span', { class: 'faint', style: { fontSize: '13px' } }, `last ${hours >= 72 ? '3 days' : hours + ' h'}`)),
      h('div', { style: { display: 'flex', gap: '20px', flexWrap: 'wrap', alignItems: 'center' } }, svg,
        h('div', { style: { flex: '1 1 200px', minWidth: 0 } },
          h('p', { style: { margin: '0 0 8px', fontSize: '15px' } }, `The wind came mostly from the ${compass(best * 22.5)} (${pct(top)} of readings)${calm ? `; calm ${pct(calm)} of the time` : ''}.`),
          h('div', { class: 'legend-inline', style: { flexWrap: 'wrap' } }, BANDS.map((b, k) => h('span', {}, h('i', { style: { background: 'var(--s1)', opacity: OPAC[k] } }), `${b[2]} km/h`))),
          h('p', { class: 'muted', style: { margin: '8px 0 0', fontSize: '13px' } }, 'Each bar points the way the wind blows from; longer means more often. Shading shows how strong.'), h('div', { class: 'chart-actions' }, tbtn), holder)));
  }

  function paintExtra(rows, events) {
    clear(extraEl);
    const rose = roseCard(rows);
    if (rose) extraEl.append(rose);
    if (events.length) {
      extraEl.append(h('section', { class: 'card' }, h('h3', { style: { marginBottom: '8px' } }, 'Lightning detected by the station'),
        h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, h('thead', {}, h('tr', {}, h('th', {}, 'When (Sydney)'), h('th', {}, 'Distance'))),
          h('tbody', {}, events.map((e) => h('tr', {}, h('td', {}, dateTime(e.ts)), h('td', {}, e.properties.distance_km != null ? `${fmt(e.properties.distance_km)} km` : '–'))))))));
    }
    const last = rows.at(-1);
    if (last) {
      const keys = Object.keys(last.v).sort();
      const unmapped = keys.filter((k) => k.startsWith('x_')).length;
      extraEl.append(h('details', { class: 'card readings' }, h('summary', {}, `All readings from the latest report (${keys.length})`, unmapped ? `, ${unmapped} not yet mapped` : ''),
        h('p', { class: 'muted', style: { margin: '4px 0 10px', fontSize: '14px' } }, `Reported ${dateTime(last.t)}. Values are stored in metric units. Names starting with x_ are fields from the device that StormTracker doesn’t have a standard name for.`),
        h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, h('thead', {}, h('tr', {}, h('th', {}, 'Reading'), h('th', {}, 'Value'), h('th', {}, 'Unit'))),
          h('tbody', {}, keys.map((k) => h('tr', {}, h('td', {}, k), h('td', {}, fmt(last.v[k], Math.abs(last.v[k]) < 100 ? 2 : 0)), h('td', {}, unitOf(k)))))))));
    }
  }

  await load();
  timer = setInterval(load, 60000);
  return () => { clearInterval(timer); charts.forEach((c) => c.destroy()); };
}
