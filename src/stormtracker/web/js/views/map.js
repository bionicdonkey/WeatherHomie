import { h, add, clear, icons } from '../dom.js';
import { get, put } from '../api.js';
import { time, when, ago, minutesSince } from '../time.js';
import { fmt, msToKmh, compass } from '../units.js';
import { own, official } from '../stations.js';

const OVERLAY_LABELS = { locations: 'Place names', range: 'Range rings', roads: 'Roads', background: 'Land and borders', topography: 'Terrain', waterways: 'Rivers and lakes', rail: 'Railways', catchments: 'Catchments', wthrDistricts: 'Forecast districts' };
const OVERLAY_ORDER = ['background', 'topography', 'waterways', 'roads', 'rail', 'range', 'locations'];
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const store = {
  get(k, d) { try { const v = localStorage.getItem('st-' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('st-' + k, JSON.stringify(v)); } catch { /* ignore */ } },
};
const minsFromNow = (iso) => Math.round((new Date(iso).getTime() - Date.now()) / 60000);
const inWords = (m) => (m <= 1 ? 'now' : m < 90 ? `in about ${m} min` : `in about ${Math.round(m / 6) / 10} h`);
const destination = (lat, lon, bearing, km) => {            // point km away along a bearing (spherical, good enough at storm scale)
  const R = 6371, b = (bearing * Math.PI) / 180, p1 = (lat * Math.PI) / 180, l1 = (lon * Math.PI) / 180, d = km / R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [(p2 * 180) / Math.PI, (l2 * 180) / Math.PI];
};

export async function mount(root) {
  const settled = (p) => p.then((v) => v, () => null);
  const sinceIso = (h) => new Date(Date.now() - h * 3600e3).toISOString();
  const [ui, locs, frames0, overlays, legend, stations0, nc0, model0, strikes0, warns0, wind0] = await Promise.all([
    get('/ui'), get('/locations'), settled(get('/radar/frames?hours=3')), settled(get('/radar/overlays')), settled(get('/radar/legend')),
    settled(get('/stations')), settled(get('/nowcast')), settled(get('/forecast')),
    settled(get(`/events?event_type=lightning&since=${encodeURIComponent(sinceIso(1))}&limit=500`)), settled(get(`/events?event_type=warning&since=${encodeURIComponent(sinceIso(48))}&limit=100`)), settled(get('/wind'))]);
  let frames = frames0 || [], stations = stations0, nc = nc0 && nc0.available ? nc0 : null, model = (model0 || [])[0] || null, strikes = strikes0 || [], warnings = warns0 || [], windField = (wind0 || [])[0] || null;
  const home = locs[0] || null;
  const classes = legend ? legend.classes : [];
  const colourFor = (v) => { if (v == null || v < 0.2) return null; let c = null; for (const k of classes) if (v >= k.min_mm_h) c = k.colour; return c; };

  // ---------- DOM ----------
  const mapEl = h('div', { id: 'map' });
  const loading = h('div', { class: 'loading', hidden: true }, 'Loading radar…');
  const fcChip = h('div', { class: 'fc-chip', hidden: true, role: 'status' });
  const nowPanel = h('section', { class: 'floating now', 'aria-label': 'Conditions at home' });
  const layersPanel = h('div', { class: 'floating layers' });
  const legendBox = h('div', { class: 'floating legendbox', 'aria-label': 'Rain intensity colours' });
  const timelineEl = h('section', { class: 'floating timeline', 'aria-label': 'Radar loop and forecast' });
  const view = h('div', { class: 'mapview' }, mapEl, nowPanel, layersPanel, legendBox, loading, fcChip, timelineEl);
  root.append(view);

  // ---------- map ----------
  const map = L.map(mapEl, { zoomControl: false, zoomSnap: 0.5, minZoom: 5, maxZoom: 13, attributionControl: true });
  mapEl.__map = map;                    // handle for automated visual tests
  L.control.zoom({ position: 'topright' }).addTo(map);
  map.createPane('radarPane').style.zIndex = 320;
  map.createPane('overlayImgPane').style.zIndex = 450;
  map.getPane('overlayImgPane').style.pointerEvents = 'none';
  map.createPane('cellPane').style.zIndex = 470;
  const firstBounds = frames[0] ? frames[0].bounds : null;
  if (home) map.setView([home.lat, home.lon], 9); else if (firstBounds) map.fitBounds(firstBounds); else map.setView([-33.87, 151.21], 8);

  let base = null;
  const dark = () => (window.stTheme ? window.stTheme.get() === 'dark' : false);
  function setBase() {
    if (base) { map.removeLayer(base); base = null; }
    if (!ui.tile_url) return;
    const own = dark() && ui.tile_url_dark;                    // a separate dark address, if one is set
    base = L.tileLayer((own || ui.tile_url).replace('{style}', dark() ? 'dark' : 'light'), { attribution: ui.tile_attribution, subdomains: 'abcd', maxZoom: 18, detectRetina: false }).addTo(map);
    // providers without a dark style (OpenStreetMap) are darkened with a filter on the tile pane
    mapEl.classList.toggle('basemap-invert', dark() && !own && !ui.tile_url.includes('{style}'));
  }
  setBase();
  if (!ui.tile_url) map.attributionControl.setPrefix(false);

  // ---------- home ----------
  let homeDot = null, homeRing = null;
  function paintHome() {
    if (!home) return;
    [homeDot, homeRing].forEach((l) => l && map.removeLayer(l));
    homeRing = L.circle([home.lat, home.lon], { radius: 25000, color: css('--home'), weight: 1.5, dashArray: '4 7', fill: false, opacity: 0.8, interactive: false }).addTo(map);
    homeDot = L.circleMarker([home.lat, home.lon], { radius: 7, color: '#fff', weight: 2.5, fillColor: css('--home'), fillOpacity: 1 }).addTo(map)
      .bindTooltip(home.name, { permanent: true, direction: 'right', offset: [10, 0], className: 'home-label' });
  }
  paintHome();

  // ---------- slots: observed radar frames, then forecast frames ----------
  let disposed = false;               // set when the page is left: late timers and image callbacks must not touch the removed map
  let S = [], nowIdx = -1, idx = -1, playing = false, timer = null, userPaused = false, wantAutoplay = !REDUCED;
  let opacity = ui.radar_opacity ?? 0.8, pendingObs = 0, pendingFc = 0, fcReady = false;
  const imgs = new Map();            // slot key -> L.ImageOverlay
  function rebuildSlots() {
    S = [
      ...frames.map((f) => ({ kind: 'obs', key: `o${f.id}`, ts: f.ts, png: f.png, bounds: f.bounds, home: f.home })),
      ...(nc ? nc.leads.map((l) => ({ kind: 'fc', key: `f${nc.run}-${l.lead_min}`, ts: l.valid_ts, lead: l.lead_min, png: l.png, bounds: nc.bounds, home: l.home })) : []),
    ];
    nowIdx = frames.length - 1;
  }
  function syncImages() {
    const keys = new Set(S.map((s) => s.key));
    for (const [k, l] of imgs) if (!keys.has(k)) { map.removeLayer(l); imgs.delete(k); }
    const finishObs = () => { if (disposed) return; if (--pendingObs <= 0) { pendingObs = 0; loading.hidden = true; startIfReady(); } };
    const finishFc = () => { if (disposed) return; if (--pendingFc <= 0) { pendingFc = 0; fcReady = true; } };
    for (const s of S) {
      if (imgs.has(s.key)) continue;
      const l = L.imageOverlay(s.png, s.bounds, { pane: 'radarPane', opacity: 0, interactive: false, className: 'radar-img' });
      if (s.kind === 'obs') { pendingObs++; l.once('load', finishObs); l.once('error', finishObs); } else { pendingFc++; fcReady = false; l.once('load', finishFc); l.once('error', finishFc); }
      l.addTo(map); imgs.set(s.key, l);
    }
    if (!S.some((s) => s.kind === 'fc')) fcReady = true;
    if (pendingObs) { loading.hidden = false; setTimeout(() => { if (!disposed && pendingObs) { pendingObs = 0; loading.hidden = true; startIfReady(); } }, 8000); } // don't wait forever on a missing frame
    else startIfReady();
    if (pendingFc) setTimeout(() => { if (!disposed && pendingFc) { pendingFc = 0; fcReady = true; } }, 15000);
  }
  function startIfReady() { if (!disposed && wantAutoplay && !userPaused && S.length > 1 && pendingObs === 0) { wantAutoplay = false; play(); } }
  function show(i) {
    if (disposed || !S.length) return;
    idx = Math.max(0, Math.min(S.length - 1, i));
    S.forEach((s, k) => { const l = imgs.get(s.key); if (l) l.setOpacity(k === idx ? opacity * (s.kind === 'fc' ? 0.92 : 1) : 0); });
    paintTimeline(); paintRadarLine();
    const s = S[idx];
    fcChip.hidden = s.kind !== 'fc';
    if (s.kind === 'fc') fcChip.textContent = `Forecast: ${s.lead} min ahead, ${time(s.ts)}`;
  }

  // ---------- overlays (BOM place names, range rings...) ----------
  const radarId = frames[0] ? frames[0].radar_id : null;
  const layerList = ((overlays || []).find((o) => o.radar_id === radarId) || {}).layers || [];
  const ovDefault = (n) => !ui.tile_url && (n === 'locations' || n === 'background');   // a real basemap already has labels
  const ovOn = store.get('overlays', {});
  const ovLayers = {};
  function setOverlay(name, on) {
    ovOn[name] = on; store.set('overlays', ovOn);
    if (on && !ovLayers[name]) {
      ovLayers[name] = L.imageOverlay(`/api/radar/overlays/${radarId}/${name}.png`, frames[0].bounds, { pane: 'overlayImgPane', interactive: false, className: `overlay-img ov-${name}` });
    }
    if (ovLayers[name]) (on ? ovLayers[name].addTo(map) : map.removeLayer(ovLayers[name]));
  }
  if (frames[0]) for (const n of layerList) if (ovOn[n] ?? ovDefault(n)) setOverlay(n, true);

  // ---------- storm cells: outline, 30-minute motion arrow, trail, label ----------
  const cellGroup = L.layerGroup();
  let showCells = store.get('cells', true);
  function paintCells() {
    cellGroup.clearLayers();
    if (!nc || !showCells) { map.removeLayer(cellGroup); return; }
    cellGroup.addTo(map);
    const ink = css('--ink'), hot = css('--home');
    const ranked = [...nc.cells].sort((a, b) => (b.home?.approaching - a.home?.approaching) || (b.max_mmh - a.max_mmh));
    ranked.forEach((c, i) => {
      const coming = c.home && c.home.approaching, col = coming ? hot : ink;
      if (c.track.length > 1) L.polyline(c.track, { pane: 'cellPane', color: col, weight: 1.5, opacity: 0.45, dashArray: '2 5', interactive: false }).addTo(cellGroup);
      const ring = L.circle([c.lat, c.lon], { pane: 'cellPane', radius: Math.max(1800, c.radius_km * 1000), color: col, weight: 2, fill: false, opacity: 0.9 }).addTo(cellGroup);
      if (c.speed_kmh >= 5 && c.bearing_deg != null) {
        const end = destination(c.lat, c.lon, c.bearing_deg, c.speed_kmh * 0.5);     // where it will be in 30 minutes
        L.polyline([[c.lat, c.lon], end], { pane: 'cellPane', color: col, weight: 2.5, opacity: 0.95, interactive: false }).addTo(cellGroup);
        L.marker(end, { pane: 'cellPane', interactive: false, icon: L.divIcon({ className: 'cell-arrow', html: `<i style="transform:rotate(${c.bearing_deg}deg);border-bottom-color:${col}"></i>`, iconSize: [14, 14], iconAnchor: [7, 7] }) }).addTo(cellGroup);
      }
      if (i < 6 || coming) {
        const lines = [`<b>${fmt(c.max_mmh, c.max_mmh < 10 ? 1 : 0)} mm/h</b>`];
        if (c.speed_kmh >= 5 && c.bearing_deg != null) lines.push(`heading ${compass(c.bearing_deg)} at ${fmt(c.speed_kmh)} km/h`);
        if (coming && c.home.eta_min != null) lines.push(`<b>core at home ${inWords(Math.max(0, minsFromNow(nc.frame_ts) + c.home.eta_min))}</b>`);
        ring.bindTooltip(lines.join('<br>'), { permanent: true, direction: 'top', offset: [0, -4], className: 'cell-label' });
      }
    });
  }

  // ---------- lightning: markers where a feed gives coordinates, distance rings for your own station's sensor ----------
  const strikeGroup = L.layerGroup();
  let showStrikes = store.get('strikes', true);
  function paintStrikes() {
    strikeGroup.clearLayers();
    if (!showStrikes || !strikes.length) { map.removeLayer(strikeGroup); return; }
    strikeGroup.addTo(map);
    const col = css('--warn'), now = Date.now();
    let rings = 0;
    for (const e of strikes) {
      const ageMin = Math.max(0, (now - new Date(e.ts).getTime()) / 60000);
      if (ageMin > 60) continue;
      const fade = 1 - (ageMin / 60) * 0.75, p = e.properties || {}, dist = p.distance_km != null ? `${fmt(p.distance_km)} km from ${p.station_lat != null ? 'your station' : 'home'}` : '';
      const tip = `Lightning ${Math.round(ageMin)} min ago${dist ? ', ' + dist : ''}`;
      if (e.lat != null && e.lon != null) {
        L.circleMarker([e.lat, e.lon], { pane: 'cellPane', radius: 5, color: '#1a1a1a', weight: 1.5, fillColor: col, fillOpacity: fade, opacity: fade }).bindTooltip(tip).addTo(strikeGroup);
      } else if (p.station_lat != null && p.distance_km != null && rings < 8) {
        rings++;                                              // the sensor reports a distance but no direction: show the ring it could be on
        L.circle([p.station_lat, p.station_lon], { pane: 'cellPane', radius: p.distance_km * 1000, color: col, weight: 1.5, dashArray: '2 5', fill: false, opacity: fade * 0.8 }).bindTooltip(tip).addTo(strikeGroup);
      }
    }
  }

  // ---------- wind: arrows for stations (bold, with the speed) and for the weather model's grid around home (light) ----------
  const windGroup = L.layerGroup();
  let showWind = store.get('wind', true);
  const arrowIcon = (dirFrom, kmh, strong, label) => {
    const size = Math.round(Math.min(strong ? 42 : 32, (strong ? 24 : 17) + kmh * 0.5)), rot = (dirFrom + 180) % 360;   // the arrow points where the wind is going
    const fill = strong ? css('--ink') : css('--ink-3');
    const svg = `<svg viewBox="-12 -12 24 24" width="${size}" height="${size}" style="transform:rotate(${rot}deg)" aria-hidden="true"><path d="M0 -10 L6 4 L0 1.5 L-6 4 Z" fill="${fill}" stroke="${css('--card')}" stroke-width="${strong ? 1.6 : 1.2}" stroke-linejoin="round"/></svg>`;
    return L.divIcon({ className: `wind-arrow${strong ? ' strong' : ''}`, html: svg + (label ? `<span class="wind-lbl">${label}</span>` : ''), iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
  };
  function paintWind() {
    windGroup.clearLayers();
    if (!showWind) { map.removeLayer(windGroup); return; }
    windGroup.addTo(map);
    for (const p of (windField && windField.points) || []) {
      const kmh = msToKmh(p.speed_ms);
      L.marker([p.lat, p.lon], { pane: 'cellPane', icon: arrowIcon(p.dir_deg, kmh, false), keyboard: false })
        .bindTooltip(`Weather model: ${fmt(kmh)} km/h from the ${compass(p.dir_deg)}${p.gust_ms != null ? `, gusts ${fmt(msToKmh(p.gust_ms))} km/h` : ''}`).addTo(windGroup);
    }
    for (const st of stations || []) {
      const w = st.wind;
      if (!w || w.wind_speed_ms == null || w.wind_dir_deg == null || st.lat == null || minutesSince(st.last_ts) > 90) continue;
      const kmh = msToKmh(w.wind_speed_ms), name = st.source === 'bom_observations' ? st.station_id.replace(/^bom_/, 'BOM ') : 'Your station';
      L.marker([st.lat, st.lon], { pane: 'cellPane', zIndexOffset: 500, icon: arrowIcon(w.wind_dir_deg, kmh, true, `${fmt(kmh)}`), keyboard: false })
        .bindTooltip(`${name}: ${fmt(kmh)} km/h from the ${compass(w.wind_dir_deg)}${w.wind_gust_ms != null ? `, gusts ${fmt(msToKmh(w.wind_gust_ms))} km/h` : ''}`).addTo(windGroup);
    }
  }

  // ---------- timeline: one column per slot; bar = heaviest rain within 25 km; mark = rain over home ----------
  const playBtn = h('button', { class: 'tl-play', 'aria-label': 'Play radar loop', onclick: () => (playing ? pause(true) : play()) }, icons.play());
  const tlTime = h('span', { class: 'tl-time' }), tlAge = h('span', { class: 'tl-age' }), tlNote = h('span', { class: 'tl-note' });
  const track = h('div', { class: 'tl-track', role: 'slider', tabindex: 0, 'aria-label': 'Radar frame', 'aria-orientation': 'horizontal' });
  const axis = h('div', { class: 'tl-axis' });
  const key = h('div', { class: 'tl-key' }, h('span', {}, h('i', { style: { background: 'var(--ink-2)' } }), 'Heaviest rain within 25 km'), h('span', {}, h('i', { style: { background: 'var(--home)' } }), 'Rain over home'),
    h('span', { class: 'fc-key' }, h('i', { class: 'hatch' }), 'Forecast'));
  timelineEl.append(playBtn, h('div', { class: 'tl-main' }, h('div', { class: 'tl-head' }, tlTime, tlAge, tlNote), track, axis, key));

  let cols = [];
  function buildColumns() {
    clear(track); cols = [];
    S.forEach((s, k) => {
      if (k === nowIdx + 1 && nowIdx >= 0 && s.kind === 'fc') track.append(h('span', { class: 'tl-now', 'aria-hidden': 'true', title: 'Now: forecast starts here' }));
      const mx = s.home ? s.home.max_within_mm_h : null, at = s.home ? s.home.at_point_mm_h : null;
      const frac = mx ? Math.min(1, Math.log10(1 + mx) / Math.log10(1 + 150)) : 0;
      const what = s.kind === 'fc' ? `Forecast ${time(s.ts)}, ${s.lead} min ahead` : time(s.ts);
      const col = h('div', { class: 'tl-col' + (s.kind === 'fc' ? ' fc' : ''), role: 'presentation',
        title: `${what}${mx != null ? `: up to ${fmt(mx, mx < 10 ? 1 : 0)} mm/h within 25 km` : ''}${at ? `, ${fmt(at, 1)} mm/h over home` : ''}` },
        h('span', { class: 'bar', style: { height: `${Math.max(6, frac * 40)}px`, backgroundColor: colourFor(mx) || 'var(--rule)' } }),
        h('span', { class: 'home' + (at >= 0.2 ? ' wet' : '') }));
      cols.push(col); track.append(col);
    });
  }
  function paintTimeline() {
    if (!S.length) { clear(timelineEl).append(h('div', { class: 'empty', style: { padding: '8px 12px' } }, 'No radar frames yet.')); return; }
    const s = S[idx] ?? S[nowIdx];
    tlTime.textContent = time(s.ts);
    tlAge.classList.remove('stale', 'fc');
    tlNote.textContent = '';
    const latestObs = frames.at(-1), mins = latestObs ? minutesSince(latestObs.ts) : 0, atNow = idx === nowIdx;
    if (s.kind === 'fc') { tlAge.textContent = `Forecast, ${s.lead} min after the latest frame`; tlAge.classList.add('fc'); }
    else if (atNow) { tlAge.textContent = ago(s.ts); if (mins > 20) { tlAge.classList.add('stale'); tlNote.textContent = 'Radar looks stale'; } }
    else tlAge.textContent = `${Math.round((new Date(latestObs.ts) - new Date(s.ts)) / 60000)} min before latest`;
    cols.forEach((c, k) => c.setAttribute('aria-selected', k === idx ? 'true' : 'false'));
    track.setAttribute('aria-valuemin', 1); track.setAttribute('aria-valuemax', S.length); track.setAttribute('aria-valuenow', idx + 1);
    track.setAttribute('aria-valuetext', `${s.kind === 'fc' ? `Forecast ${s.lead} minutes ahead, ` : ''}${time(s.ts)}, frame ${idx + 1} of ${S.length}`);
    // time labels sit under the columns they describe
    const at = (k, label) => h('span', { style: { left: `${((k + 0.5) / S.length) * 100}%` } }, label);
    const picks = [0, nowIdx, S.length - 1].filter((k, i, a) => k >= 0 && a.indexOf(k) === i);
    add(clear(axis), picks.map((k) => at(k, k === nowIdx && S.length > nowIdx + 1 ? `Now ${time(S[k].ts)}` : time(S[k].ts))));
  }
  function indexAt(e) {
    let best = 0, bestD = Infinity;
    cols.forEach((c, k) => { const r = c.getBoundingClientRect(), d = Math.abs(e.clientX - (r.left + r.width / 2)); if (d < bestD) { best = k; bestD = d; } });
    return best;
  }
  let dragging = false;
  track.addEventListener('pointerdown', (e) => { dragging = true; pause(true); track.setPointerCapture(e.pointerId); show(indexAt(e)); });
  track.addEventListener('pointermove', (e) => { if (dragging) show(indexAt(e)); });
  track.addEventListener('pointerup', () => { dragging = false; });
  track.addEventListener('pointercancel', () => { dragging = false; });
  track.addEventListener('keydown', (e) => {
    const k = { ArrowLeft: idx - 1, ArrowRight: idx + 1, Home: 0, End: S.length - 1, n: nowIdx, N: nowIdx }[e.key];
    if (k != null) { e.preventDefault(); pause(true); show(k); } else if (e.key === ' ') { e.preventDefault(); playing ? pause(true) : play(); }
  });
  function setPlayIcon() { clear(playBtn).append(playing ? icons.pause() : icons.play()); playBtn.setAttribute('aria-label', playing ? 'Pause radar loop' : 'Play radar loop'); }
  // The loop runs continuously: observed frames, a beat on "now", the forecast, a rest at the end, then again.
  function lastPlayable() { return fcReady ? S.length - 1 : nowIdx; }
  function tick() {
    clearTimeout(timer);
    if (!playing || disposed) return;
    const end = idx >= lastPlayable();
    timer = setTimeout(() => {
      if (!playing || disposed) return;
      show(end ? 0 : idx + 1);
      tick();
    }, end ? 1400 : idx === nowIdx && lastPlayable() > nowIdx ? 900 : 420);
  }
  function play() { if (S.length < 2) return; userPaused = false; playing = true; setPlayIcon(); tick(); }
  function pause(byUser = false) { if (byUser) userPaused = true; playing = false; clearTimeout(timer); setPlayIcon(); }
  // don't animate a hidden tab; carry on when it comes back (unless the user paused it)
  let resumeOnShow = false;
  const onVisibility = () => {
    if (document.hidden) { resumeOnShow = playing; if (playing) pause(); }
    else if (resumeOnShow && !userPaused) { resumeOnShow = false; play(); }
  };
  document.addEventListener('visibilitychange', onVisibility);

  // ---------- rain over home at the selected frame (observed or forecast) ----------
  const radarLine = h('div', { class: 'radarline' });
  function paintRadarLine() {
    const s = S[idx];
    if (!s || !s.home) { clear(radarLine); return; }
    const at = s.home.at_point_mm_h, mx = s.home.max_within_mm_h, fc = s.kind === 'fc';
    const c = colourFor(at);
    add(clear(radarLine),
      h('span', { class: 'chip', style: { background: c || 'transparent', borderStyle: c ? 'solid' : 'dashed' } }),
      h('div', {}, at >= 0.2 ? h('b', {}, `${fc ? 'Forecast: about' : 'About'} ${fmt(at, at < 10 ? 1 : 0)} mm/h over home`) : h('b', {}, fc ? 'No rain forecast over home' : 'No rain over home'),
        h('div', { class: 'muted', style: { fontSize: '13px' } }, mx >= 0.2 ? `Heaviest within 25 km: about ${fmt(mx, mx < 10 ? 1 : 0)} mm/h` : 'Nothing within 25 km', fc ? ' (forecast)' : ' (radar estimate)')));
  }

  // ---------- what the forecast means for home ----------
  function impactBlock() {
    if (!nc || !nc.impact) return null;
    const imp = nc.impact, conf = imp.confidence;
    let head, sub = [], tone = '';
    if (imp.status === 'approaching') {
      const m = Math.max(0, minsFromNow(imp.eta_ts));
      head = `Rain reaches home ${inWords(m)}`;
      tone = m <= 30 ? 'soon' : 'later';
      sub.push(`Heaviest about ${fmt(imp.peak_mmh, imp.peak_mmh < 10 ? 1 : 0)} mm/h around ${time(imp.peak_ts)}`);
      if (imp.end_ts) sub.push(`Easing about ${time(imp.end_ts)}`);
    } else if (imp.status === 'raining') {
      head = 'Raining at home now';
      tone = 'now';
      sub.push(imp.end_ts ? `Easing ${inWords(Math.max(0, minsFromNow(imp.end_ts)))}` : `Rain looks set to continue for the next ${nc.horizon_min} min`);
      if (imp.peak_mmh > imp.now_mmh + 2) sub.push(`Heavier to come: about ${fmt(imp.peak_mmh)} mm/h`);
    } else if (imp.status === 'clear') {
      head = `No rain expected at home in the next ${nc.horizon_min} min`;
      if (imp.nearest_rain_km != null) sub.push(`Nearest rain is ${fmt(imp.nearest_rain_km)} km ${compass(imp.nearest_rain_bearing_deg)} of home`);
    } else { head = 'Can’t forecast for home'; sub.push(imp.reason || ''); }
    const core = (nc.cells || []).filter((c) => c.home && c.home.approaching && c.home.eta_min != null).sort((a, b) => a.home.eta_min - b.home.eta_min)[0];
    if (core && imp.status !== 'clear') sub.push(`Storm core (${fmt(core.max_mmh)} mm/h) ${inWords(Math.max(0, minsFromNow(nc.frame_ts) + core.home.eta_min))}`);
    const st = imp.station;
    if (st && st.available) {
      const bits = [];
      if (st.agrees_with_radar === true) bits.push(st.station_rain_mmh >= 0.1 ? 'Your station is recording the rain too' : 'Your station agrees: dry so far');
      else if (st.agrees_with_radar === false) bits.push(st.station_rain_mmh >= 0.1 ? `Your station already reads ${fmt(st.station_rain_mmh, 1)} mm/h` : 'Your station is dry while radar shows rain');
      if (st.pressure_falling_fast) bits.push(`Pressure falling fast (${fmt(st.pressure_change_1h_hpa, 1)} hPa in 1 h)`);
      if (st.gusty) bits.push(`Gusts ${st.gust_kmh} km/h`);
      if (st.lightning_within_10km_30min) bits.push(`${st.lightning_within_10km_30min} lightning strike${st.lightning_within_10km_30min > 1 ? 's' : ''} within 10 km in the last 30 min`);
      if (bits.length) sub.push(bits.join('. '));
    }
    return h('div', { class: `impact ${tone}`, role: 'status' },
      h('b', { class: 'head' }, head),
      h('div', { class: 'detail' }, sub.filter(Boolean).map((t) => h('div', {}, t)),
        conf ? h('div', { class: 'conf', title: conf.reasons.join('. ') }, h('span', { class: `dot ${conf.level === 'high' ? 'ok' : conf.level === 'medium' ? 'warn' : ''}` }), `Confidence ${conf.level}`) : null,
        h('div', { class: 'faint', style: { fontSize: '12px', marginTop: '2px' } }, `Forecast from radar at ${time(nc.frame_ts)}`)));
  }

  // ---------- warnings in force (BOM RSS: titles and links only, no polygons to draw) ----------
  function warningsBlock() {
    const live = (warnings || []).filter((w) => !w.expires || new Date(w.expires) > Date.now());
    if (!live.length) return null;
    const mine = live.filter((w) => w.properties.in_area !== false), others = live.length - mine.length;
    const clean = (t) => t.replace(/^\d{1,2}\/\d{2}:\d{2}\s+[A-Z]{2,5}\s+/, '');
    const shown = [...mine].sort((a, b) => (b.properties.severe_thunderstorm - a.properties.severe_thunderstorm) || new Date(b.ts) - new Date(a.ts)).slice(0, 3);
    if (!shown.length) return others ? h('div', { class: 'modelline' }, h('span', { class: 'muted' }, `${others} other warning${others > 1 ? 's' : ''} in force elsewhere in the state, none marked for your area. `), h('a', { href: '#/sources' }, 'Area words')) : null;
    return h('div', { class: 'warnings' }, shown.map((w) => h('div', { class: 'warn' + (w.properties.severe_thunderstorm ? ' severe' : '') },
      h('div', {}, w.properties.link ? h('a', { href: w.properties.link, target: '_blank', rel: 'noopener noreferrer' }, clean(w.properties.title)) : clean(w.properties.title)))),
      mine.length > shown.length ? h('div', { class: 'faint', style: { fontSize: '12.5px' } }, `${mine.length - shown.length} more for your area`) : null,
      others ? h('div', { class: 'faint', style: { fontSize: '12.5px' } }, `${others} other in the state`) : null);
  }

  // ---------- Now panel (own station + forecast for home) ----------
  let obs = [], st = null, bom = null;
  async function loadObs() {
    st = own(stations);
    const ref = official(stations);
    const q = (x, h, n) => get(`/observations?source=${encodeURIComponent(x.source)}&station_id=${encodeURIComponent(x.station_id)}&since=${new Date(Date.now() - h * 3600e3).toISOString()}&order=asc&limit=${n}`);
    try { obs = st ? await q(st, 3.2, 2000) : []; } catch { obs = []; }
    try { bom = ref ? { station: ref, latest: (await q(ref, 3, 20)).at(-1) } : null; } catch { bom = null; }
    paintNow();
  }
  // the weather model's view of the next 12 hours (Open-Meteo)
  function modelLine() {
    const sm = model && model.summary, pot = sm && sm.potential;
    if (!pot || pot.level === 'none') return null;
    const where = sm.max_cape_at ? ` around ${when(sm.max_cape_at)}` : '';
    return h('div', { class: 'modelline' }, h('b', {}, `Storm potential ${pot.level}${pot.organised ? ', organised' : ''}`),
      h('div', { class: 'muted' }, `Model CAPE up to ${fmt(sm.max_cape_jkg)} J/kg${where}${pot.organised ? ', with strong wind shear' : ''}. The ingredients, not a forecast of a storm.`),
      h('div', { class: 'faint', style: { fontSize: '11.5px' } }, model.attribution || 'Open-Meteo'));
  }
  function officialLine() {
    const l = bom && bom.latest;
    if (!l) return null;
    const v = l.values;
    return h('div', { class: 'modelline' }, h('b', {}, `${bom.station.station_id.replace(/_/g, ' ')} (BOM)`),
      h('div', { class: 'muted' }, [v.temp_c != null ? `${fmt(v.temp_c, 1)}°C` : null, v.rain_since_9am_mm != null ? `${fmt(v.rain_since_9am_mm, 1)} mm since 9 am` : null,
        v.wind_speed_ms != null ? `wind ${fmt(msToKmh(v.wind_speed_ms))} km/h${v.wind_dir_deg != null ? ' ' + compass(v.wind_dir_deg) : ''}` : null].filter(Boolean).join(', ') + `, ${ago(l.ts)}`));
  }
  function paintNow() {
    const latest = obs.at(-1);
    const wasOpen = nowPanel.classList.contains('open');
    clear(nowPanel);               // the 'open' class is kept so a refresh doesn't collapse the mobile details
    const impact = impactBlock(), warn = warningsBlock();
    if (!latest) {
      add(nowPanel, h('h2', {}, home ? home.name : 'Conditions'), warn, impact,
        h('p', { class: 'none', style: { margin: '8px 0 0' } }, 'No station readings yet. ', h('a', { href: '#/sources' }, 'Connect a weather station')),
        home ? null : h('p', { class: 'none', style: { margin: '8px 0 0' } }, 'No home location is set. ', h('a', { href: '#/settings' }, 'Add your location')), radarLine, modelLine(), officialLine());
      return;
    }
    const v = latest.values, stale = minutesSince(latest.ts) > 15;
    const press = (() => {
      if (v.pressure_hpa == null) return null;
      const target = new Date(latest.ts).getTime() - 3 * 3600e3;
      const past = obs.find((o) => o.values.pressure_hpa != null && Math.abs(new Date(o.ts).getTime() - target) < 25 * 60e3);
      const trend = past ? v.pressure_hpa - past.values.pressure_hpa : null;
      return { v: v.pressure_hpa, trend };
    })();
    const rows = [];
    if (v.wind_speed_ms != null) rows.push(['Wind', `${fmt(msToKmh(v.wind_speed_ms))} km/h${v.wind_dir_deg != null ? ' ' + compass(v.wind_dir_deg) : ''}`, v.wind_gust_ms != null ? `Gusts ${fmt(msToKmh(v.wind_gust_ms))} km/h` : null]);
    if (v.rain_rate_mmh != null) rows.push(['Rain now', `${fmt(v.rain_rate_mmh, 1)} mm/h`, v.rain_daily_mm != null ? `${fmt(v.rain_daily_mm, 1)} mm today` : null]);
    if (v.humidity_pct != null) rows.push(['Humidity', `${fmt(v.humidity_pct)}%`, v.dewpoint_c != null ? `Dew point ${fmt(v.dewpoint_c)}°` : null]);
    if (press) rows.push(['Pressure', `${fmt(press.v)} hPa`, press.trend == null ? null : Math.abs(press.trend) < 0.5 ? 'Steady over 3 h' : `${press.trend < 0 ? 'Falling' : 'Rising'} ${fmt(Math.abs(press.trend), 1)} in 3 h`]);
    add(nowPanel,
      h('h2', {}, h('span', {}, home ? home.name : 'Conditions'), h('span', { style: stale ? { color: 'var(--warn)', fontWeight: 650 } : {} }, stale ? `Last report ${ago(latest.ts)}` : ago(latest.ts))),
      warn, impact,
      h('div', { class: 'head2' }, v.temp_c != null ? h('div', { class: 'temp' }, fmt(v.temp_c, 1), h('small', {}, '°C')) : null,
        v.feels_like_c != null ? h('div', { class: 'sub' }, `Feels like ${fmt(v.feels_like_c)}°`) : null),
      h('button', { class: 'linkbtn toggle', 'aria-expanded': String(wasOpen), onclick: (e) => { const o = nowPanel.classList.toggle('open'); e.currentTarget.textContent = o ? 'Hide details' : 'Details'; e.currentTarget.setAttribute('aria-expanded', String(o)); } }, wasOpen ? 'Hide details' : 'Details'),
      h('div', { class: 'rows' }, rows.map(([k, val, sub]) => h('div', {}, h('span', {}, k), h('b', {}, val), sub ? h('span', {}, sub) : null))),
      radarLine, modelLine(), officialLine());
    paintRadarLine();
  }

  // ---------- layers + legend ----------
  function legendContent() {
    if (!classes.length) return null;
    const picks = [0.5, 2.5, 10, 30, 100];
    return h('div', { class: 'legend' }, h('div', { class: 'bar', 'aria-hidden': 'true' }, classes.map((c) => h('i', { style: { background: c.colour } }))),
      h('div', { class: 'ticks' }, picks.map((p) => h('span', {}, p)), h('span', {}, 'mm/h')), h('div', { class: 'faint', style: { fontSize: '11.5px', marginTop: '2px' } }, 'Approximate: from BOM colour classes'));
  }
  add(clear(legendBox), legendContent());
  legendBox.hidden = !classes.length;

  const opSlider = h('input', { type: 'range', min: 20, max: 100, value: Math.round(opacity * 100), 'aria-label': 'Radar opacity',
    oninput: () => { opacity = opSlider.value / 100; show(idx); },
    onchange: () => { put('/ui', { ...ui, radar_opacity: opacity }).then((u) => Object.assign(ui, u)).catch(() => {}); } });
  const popOverlays = layerList.length ? [...layerList].sort((a, b) => OVERLAY_ORDER.indexOf(a) - OVERLAY_ORDER.indexOf(b)).map((n) =>
    h('label', {}, h('input', { type: 'checkbox', checked: !!(ovOn[n] ?? ovDefault(n)), onchange: (e) => setOverlay(n, e.target.checked) }), OVERLAY_LABELS[n] || n)) : [h('div', { class: 'muted', style: { padding: '4px 0' } }, 'Radar overlays appear after the first frame.')];
  const btn = h('button', { 'aria-expanded': 'false', onclick: () => { const o = layersPanel.classList.toggle('open'); btn.setAttribute('aria-expanded', o); } }, icons.layers(), 'Layers');
  add(layersPanel, btn, h('div', { class: 'pop' }, h('h3', {}, 'Radar opacity'), opSlider,
    h('h3', {}, 'Storms'), h('label', {}, h('input', { type: 'checkbox', checked: showStrikes, onchange: (e) => { showStrikes = e.target.checked; store.set('strikes', showStrikes); paintStrikes(); } }), 'Lightning strikes (last hour)'),
    h('label', {}, h('input', { type: 'checkbox', checked: showCells, onchange: (e) => { showCells = e.target.checked; store.set('cells', showCells); paintCells(); } }), 'Storm cells and where they’re heading'),
    h('h3', {}, 'Wind'), h('label', {}, h('input', { type: 'checkbox', checked: showWind, onchange: (e) => { showWind = e.target.checked; store.set('wind', showWind); paintWind(); } }), 'Wind arrows'),
    h('div', { class: 'faint', style: { fontSize: '12px', margin: '0 0 6px' } }, 'Arrows point the way the wind is blowing. Dark: stations (km/h). Light: weather model.'),
    h('h3', {}, 'Map details'), popOverlays,
    h('div', { class: 'mobile-legend' }, h('h3', {}, 'Rain intensity'), legendContent())));

  // ---------- empty state, refresh, theme ----------
  if (!frames.length) {
    timelineEl.hidden = true; legendBox.hidden = true;
    view.append(h('div', { class: 'floating', style: { left: '50%', top: '40%', transform: 'translate(-50%,-50%)', padding: '18px 22px', maxWidth: '86%' } },
      h('div', { class: 'empty', style: { padding: 0 } }, h('strong', {}, 'No radar frames yet'), 'Turn on the BOM radar source and set your radar. ', h('a', { href: '#/sources' }, 'Open sources'))));
  }
  rebuildSlots(); buildColumns(); syncImages();
  show(nowIdx);
  paintCells(); paintStrikes(); paintWind();
  loadObs();

  const refresh = setInterval(async () => {
    const [fresh, ncNew] = await Promise.all([settled(get('/radar/frames?hours=3')), settled(get('/nowcast'))]);
    const newNc = ncNew && ncNew.available ? ncNew : null;
    const framesChanged = fresh && fresh.length && fresh.at(-1).id !== frames.at(-1)?.id;
    const ncChanged = (newNc && newNc.run) !== (nc && nc.run);
    if (framesChanged || ncChanged) {
      const atNow = idx >= nowIdx || idx < 0, was = idx;
      if (framesChanged) frames = fresh;
      nc = newNc;
      rebuildSlots(); buildColumns(); syncImages(); paintCells();
      show(playing || !atNow ? Math.min(was, S.length - 1) : nowIdx);
      if (playing) tick();
    }
    const s = await settled(get('/stations')); if (s) stations = s;
    const wf = await settled(get('/wind')); if (wf) windField = wf[0] || null;
    paintWind();
    const m = await settled(get('/forecast')); if (m) model = m[0] || null;
    const ls = await settled(get(`/events?event_type=lightning&since=${encodeURIComponent(sinceIso(1))}&limit=500`)); if (ls) { strikes = ls; paintStrikes(); }
    const ws = await settled(get(`/events?event_type=warning&since=${encodeURIComponent(sinceIso(48))}&limit=100`)); if (ws) warnings = ws;
    loadObs();
  }, 60000);
  const onTheme = () => { setBase(); paintHome(); paintCells(); paintStrikes(); paintWind(); };
  window.addEventListener('theme-change', onTheme);
  const sizeTimer = setTimeout(() => { if (!disposed) map.invalidateSize(); }, 50);

  return () => { disposed = true; clearTimeout(sizeTimer); clearInterval(refresh); clearTimeout(timer); window.removeEventListener('theme-change', onTheme); document.removeEventListener('visibilitychange', onVisibility); map.remove(); };
}
