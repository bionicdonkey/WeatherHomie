import { h, clear, toast } from '../dom.js';
import { get, put, post } from '../api.js';
import { dateTime, time, ago } from '../time.js';
import { tokensCard, ssoCard } from '../access.js';
import { haCard, backupCard } from '../ops.js';

export async function mount(root, ctx = {}) {
  const session = ctx.session || {};
  const [locs, ui, audit, ncs, ncNow] = await Promise.all([get('/locations'), get('/ui'), get('/audit').catch(() => []), get('/nowcast/settings'), get('/nowcast').catch(() => null)]);
  const page = h('div', { class: 'page' }), wrap = h('div', { class: 'wrap' });
  page.append(wrap); root.append(page);
  wrap.append(h('div', { class: 'page-head' }, h('h1', {}, 'Settings')));
  const sections = h('div', { class: 'grid' });
  wrap.append(sections);

  // ---- locations ----
  let rows = locs.map((l) => ({ ...l }));
  const locBox = h('div', {}), locErr = h('p', { class: 'err', role: 'alert' });
  function paintLocs() {
    clear(locBox);
    if (!rows.length) locBox.append(h('p', { class: 'muted' }, 'No locations yet. Add your home so the radar can tell you about rain there.'));
    rows.forEach((r, i) => {
      const f = (key, lbl, attrs) => h('label', { class: 'field', style: { margin: 0 } }, h('span', { class: 'lbl' }, i === 0 ? lbl : h('span', { class: 'sr' }, lbl), i === 0 ? null : null),
        h('input', { ...attrs, value: r[key], 'aria-label': lbl, oninput: (e) => { r[key] = attrs.type === 'number' ? e.target.value : e.target.value; } }));
      locBox.append(h('div', { class: 'loc-row' }, f('name', 'Name', { type: 'text' }), f('id', 'ID', { type: 'text', pattern: '[a-z0-9][a-z0-9_-]{0,39}' }),
        f('lat', 'Latitude', { type: 'number', step: 'any' }), f('lon', 'Longitude', { type: 'number', step: 'any' }),
        h('button', { class: 'btn small danger', 'aria-label': `Remove ${r.name || 'location'}`, onclick: () => { rows.splice(i, 1); paintLocs(); } }, 'Remove')));
    });
  }
  paintLocs();
  const addLoc = h('button', { class: 'btn small', onclick: () => { rows.push({ id: rows.length ? `place-${rows.length + 1}` : 'home', name: rows.length ? 'New place' : 'Home', lat: '', lon: '' }); paintLocs(); } }, 'Add a place');
  const saveLoc = h('button', { class: 'btn primary small', onclick: async () => {
    locErr.textContent = '';
    try {
      const body = rows.map((r) => ({ id: r.id, name: r.name, lat: parseFloat(r.lat), lon: parseFloat(r.lon) }));
      if (body.some((b) => Number.isNaN(b.lat) || Number.isNaN(b.lon))) throw new Error('Every place needs a latitude and longitude.');
      await put('/locations', body); toast('Locations saved');
    } catch (e) { locErr.textContent = e.message; }
  } }, 'Save locations');
  // find a place by address: runs when asked (never while typing), through the address server set under Map and address search
  const q = h('input', { type: 'search', placeholder: 'Street, suburb or town', 'aria-label': 'Address or place', autocomplete: 'off', maxlength: 200 });
  const found = h('div', { 'aria-live': 'polite' });
  const place = (r, home) => {
    const row = { id: home ? 'home' : `place-${rows.length + 1}`, name: home ? 'Home' : r.name.split(',')[0], lat: r.lat, lon: r.lon };
    if (home && rows.length) rows[0] = { ...rows[0], lat: r.lat, lon: r.lon }; else rows.push(row);
    clear(found); paintLocs(); toast(home ? 'Home moved. Save locations to keep it.' : 'Place added. Save locations to keep it.');
  };
  const doFind = async () => {
    const text = q.value.trim(); if (text.length < 3) { clear(found).append(h('p', { class: 'hint' }, 'Type at least 3 characters.')); return; }
    clear(found).append(h('p', { class: 'hint' }, 'Searching…')); go.disabled = true;
    try {
      const { results } = await get(`/geocode?q=${encodeURIComponent(text)}`);
      clear(found).append(results.length ? h('ul', { class: 'suggest-list' }, results.map((r) => h('li', {}, h('div', {}, h('strong', {}, r.name.split(',')[0]), h('div', { class: 'hint' }, `${r.name} · ${r.lat}, ${r.lon}`)),
        h('span', { style: { display: 'flex', gap: '6px', flex: 'none' } }, h('button', { type: 'button', class: 'btn small', onclick: () => place(r, true) }, rows.length ? 'Set as home' : 'Use as home'),
          rows.length ? h('button', { type: 'button', class: 'btn small', onclick: () => place(r, false) }, 'Add place') : null)))) : h('p', { class: 'hint' }, 'Nothing found. Try the suburb and state.'));
    } catch (e) { clear(found).append(h('p', { class: 'err', role: 'alert' }, e.message)); } finally { go.disabled = false; }
  };
  const go = h('button', { type: 'button', class: 'btn small', onclick: doFind }, 'Search');
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doFind(); } });
  const finder = ui.geocoder_url ? h('div', { class: 'suggest', style: { marginBottom: '14px' } }, h('label', { class: 'field', style: { margin: 0 } }, h('span', { class: 'lbl' }, 'Find by address'),
    h('div', { style: { display: 'flex', gap: '8px' } }, q, go), h('span', { class: 'hint' }, 'Picks the coordinates for you. Only the text you type is sent, to the address search server in Map and address search.')), found) : null;
  sections.append(h('section', { class: 'card' }, h('h2', { style: { fontSize: '17px', marginBottom: '4px' } }, 'Locations'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'The first place is your home. The map marks it, and the radar timeline reports rain over it and within 25 km. Weather stations are placed at the first location unless they name another.'),
    finder, locBox, locErr, h('div', { style: { display: 'flex', gap: '10px' } }, addLoc, saveLoc)));

  // ---- storm tracking and forecast ----
  const ncVals = { ...ncs };
  const ncErr = h('p', { class: 'err', role: 'alert' });
  const ncStatus = h('p', { class: 'muted', style: { marginTop: 0 } });
  function paintNcStatus(n) {
    const st = n && n.status;
    ncStatus.textContent = !n ? '' : n.available ? `Last forecast issued ${time(n.computed_at)} (${ago(n.computed_at)}) from the ${time(n.frame_ts)} radar frame. Motion measured from ${n.motion.frames_used} frame${n.motion.frames_used === 1 ? '' : 's'}${n.motion.speed_kmh != null ? `: rain is moving about ${Math.round(n.motion.speed_kmh)} km/h` : ''}${n.motion.reason ? ` (${n.motion.reason})` : ''}.`
      : n.state === 'disabled' ? 'Forecasting is off.' : n.reason ? `No forecast yet: ${n.reason}` : st && st.error ? `The last run failed: ${st.error}` : 'No forecast yet.';
  }
  paintNcStatus(ncNow);
  const num = (key, label, hint, attrs) => h('label', { class: 'field' }, h('span', { class: 'lbl' }, label),
    h('input', { type: 'number', ...attrs, value: ncVals[key], oninput: (e) => { ncVals[key] = e.target.value === '' ? '' : Number(e.target.value); } }), h('span', { class: 'hint' }, hint));
  const ncOn = h('input', { type: 'checkbox', checked: !!ncVals.enabled, onchange: (e) => { ncVals.enabled = e.target.checked; } });
  sections.append(h('section', { class: 'card' }, h('h2', { style: { fontSize: '17px', marginBottom: '4px' } }, 'Storm tracking and forecast'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Finds storm cells in each radar frame, follows them, and moves the rain forward in time to estimate when rain reaches your home. Radar rain rates are approximate, and heavy rain is hard to place more than about half an hour ahead.'),
    ncStatus,
    h('label', { class: 'switch', style: { marginBottom: '14px' } }, ncOn, 'Forecast rain'),
    h('div', { class: 'grid cols-2', style: { gap: '0 16px' } },
      num('cell_threshold_mmh', 'Storm cell threshold (mm/h)', 'A cell is a connected area of rain at least this heavy.', { min: 1, max: 100, step: 'any' }),
      num('min_cell_area_km2', 'Smallest cell (km²)', 'Smaller patches above the threshold are ignored.', { min: 0.25, max: 500, step: 'any' }),
      num('horizon_min', 'Forecast length (minutes)', 'How far ahead to forecast, 10 to 180.', { min: 10, max: 180, step: 1 }),
      num('step_min', 'Minutes between forecast frames', 'Smaller steps look smoother but cost more.', { min: 1, max: 15, step: 1 }),
      num('arrival_mmh', 'Counts as rain at home (mm/h)', 'The forecast says rain is coming when it reaches this rate near home.', { min: 0.1, max: 20, step: 'any' }),
      num('home_radius_km', 'Near home means within (km)', 'Rain has to reach this close to your home location.', { min: 0.5, max: 15, step: 'any' })),
    ncErr, h('button', { class: 'btn primary small', onclick: async () => {
      ncErr.textContent = '';
      try { await put('/nowcast/settings', ncVals); toast('Forecast settings saved'); paintNcStatus(await get('/nowcast').catch(() => null)); }
      catch (e) { ncErr.textContent = e.message; }
    } }, 'Save forecast settings')));

  // ---- map ----
  const tile = h('input', { type: 'text', value: ui.tile_url, placeholder: 'https://…/{z}/{x}/{y}.png', spellcheck: 'false' });
  const tileDark = h('input', { type: 'text', value: ui.tile_url_dark ?? '', placeholder: 'https://…/{z}/{x}/{y}.png (optional)', spellcheck: 'false' });
  const attr = h('input', { type: 'text', value: ui.tile_attribution });
  const geo = h('input', { type: 'text', value: ui.geocoder_url ?? '', placeholder: 'https://nominatim.openstreetmap.org', spellcheck: 'false' });
  const mapErr = h('p', { class: 'err', role: 'alert' });
  const offline = h('button', { class: 'btn small', onclick: () => { tile.value = ''; attr.value = ''; } }, 'Radar map only (no internet map)');
  sections.append(h('section', { class: 'card' }, h('h2', { style: { fontSize: '17px', marginBottom: '4px' } }, 'Map and address search'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Your browser loads these map tiles directly from the provider, so their terms and attribution rules apply. The default is OpenStreetMap’s standard map, darkened in dark mode. Their tile policy allows light personal use with attribution, so keep the attribution on. Leave the address empty to show only the radar’s own land and place-name layers, which needs no internet.'),
    h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Tile address'), tile, h('span', { class: 'hint' }, 'If the provider has light and dark variants, put {style} in the address. It becomes light or dark to match the theme. Tiles should come over https. A plain http address (a tile server on your home network, such as http://192.168.1.5:9000/styles/basic/{z}/{x}/{y}.png) is allowed for that one address only; every other http image stays blocked.')),
    h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Dark mode tile address (optional)'), tileDark, h('span', { class: 'hint' }, 'If your tile server has a separate dark style, put its address here. Otherwise dark mode darkens the light tiles.')),
    h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Attribution shown on the map'), attr),
    h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Address search server (Nominatim)'), geo,
      h('span', { class: 'hint' }, 'Used by “Find by address” and the area-word suggestions. The default is OpenStreetMap’s public server, which allows light use with an identifying User-Agent (one search at a time, only when you press Search). A Nominatim of your own, for example http://192.168.1.5:8180, avoids that limit. Empty turns address search off.')),
    mapErr, h('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap' } }, offline, h('button', { class: 'btn primary small', onclick: async () => {
      mapErr.textContent = '';
      try { await put('/ui', { tile_url: tile.value.trim(), tile_url_dark: tileDark.value.trim(), tile_attribution: attr.value, radar_opacity: ui.radar_opacity, geocoder_url: geo.value.trim() }); toast('Saved. Reopen the map or this page to see the changes.'); }
      catch (e) { mapErr.textContent = e.message; }
    } }, 'Save map settings'))));

  // ---- who and what can get in ----
  sections.append(tokensCard(), await ssoCard());

  // ---- home automation and keeping the setup safe ----
  sections.append(await haCard(), await backupCard());

  // ---- account ----
  const cur = h('input', { type: 'password', autocomplete: 'current-password' }), nw = h('input', { type: 'password', autocomplete: 'new-password', minlength: 12 }), again = h('input', { type: 'password', autocomplete: 'new-password' });
  const pwErr = h('p', { class: 'err', role: 'alert' });
  const local = !session.kind || session.kind === 'local';
  sections.append(!local ? h('section', { class: 'card' }, h('h2', { style: { fontSize: '17px', marginBottom: '4px' } }, 'Account'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, session.kind === 'oidc' ? `You’re signed in as ${session.username.replace(/^sso:/, '')} through single sign-on. Change your password with your identity provider.` : `You’re signed in as ${session.username} through your reverse proxy.`),
    session.kind === 'oidc' ? h('button', { class: 'btn small', onclick: async () => { try { await post('/auth/logout'); } catch { /* ignore */ } location.reload(); } }, 'Sign out') : null)
  : h('section', { class: 'card' }, h('h2', { style: { fontSize: '17px', marginBottom: '4px' } }, 'Account'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Changing your password signs you out everywhere, so you will sign in again with the new one.'),
    h('div', { class: 'grid cols-2', style: { gap: '0 16px' } },
      h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Current password'), cur),
      h('div', {}),
      h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'New password'), nw, h('span', { class: 'hint' }, 'At least 12 characters.')),
      h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'New password again'), again)),
    pwErr,
    h('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap' } },
      h('button', { class: 'btn primary small', onclick: async () => {
        pwErr.textContent = '';
        if (nw.value !== again.value) { pwErr.textContent = 'The two new passwords don’t match.'; return; }
        try { await post('/auth/password', { current_password: cur.value, new_password: nw.value }); toast('Password changed. Sign in again.'); setTimeout(() => location.reload(), 900); }
        catch (e) { pwErr.textContent = e.message; }
      } }, 'Change password'),
      h('span', { class: 'signout' }, h('button', { class: 'btn small', onclick: async () => { try { await post('/auth/logout'); } catch { /* ignore */ } location.reload(); } }, 'Sign out')))));

  // ---- activity ----
  sections.append(h('section', { class: 'card' }, h('h2', { style: { fontSize: '17px', marginBottom: '4px' } }, 'Recent changes'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Who changed which setting. Secret values are never recorded.'),
    audit.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, h('thead', {}, h('tr', {}, ['When (Sydney)', 'Who', 'What', 'Where'].map((t) => h('th', {}, t)))),
      h('tbody', {}, audit.slice(0, 40).map((a) => h('tr', {}, h('td', {}, dateTime(a.ts)), h('td', {}, a.actor), h('td', {}, a.action), h('td', {}, a.target)))))) : h('p', { class: 'muted' }, 'Nothing yet.')));
}
