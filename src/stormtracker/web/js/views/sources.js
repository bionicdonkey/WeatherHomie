import { h, clear, toast, nice } from '../dom.js';
import { get, put } from '../api.js';
import { buildForm } from '../schemaform.js';
import { ago } from '../time.js';

const STATE = {
  ok: ['Working', 'ok'], starting: ['Waiting for data', ''], degraded: ['Needs attention', 'warn'], down: ['Not working', 'bad'],
  disabled: ['Off', ''], missing_credentials: ['Needs credentials', 'warn'],
};
const CATEGORY = { radar: 'Radar', observations: 'Observations', lightning: 'Lightning', warnings: 'Warnings', model_forecast: 'Forecast model' };
const title = (n) => ({ wunderground_pws: 'Weather Underground (PWS)', ecowitt_cloud: 'Ecowitt.net cloud' })[n] || nice(n);

export async function mount(root) {
  const page = h('div', { class: 'page' }), wrap = h('div', { class: 'wrap' }), list = h('div', { class: 'grid cols-2' });
  wrap.append(h('div', { class: 'page-head' }, h('h1', {}, 'Sources'), h('span', { class: 'muted' }, 'Where StormTracker gets its data. Each source runs on its own, so one failing never stops the others.')), list);
  page.append(wrap); root.append(page);
  let drawerOpen = false;

  async function load() {
    let sources;
    try { sources = await get('/sources'); } catch (e) { clear(list).append(h('div', { class: 'card empty' }, h('strong', {}, 'Couldn’t load sources'), e.message)); return; }
    clear(list).append(...sources.map(card));
  }

  function detailRows(d) {
    const out = [];
    for (const [k, v] of Object.entries(d || {})) {
      if (k === 'forward') {
        const t = v.targets || {};
        out.push(h('dt', {}, 'Relaying to'), h('dd', {}, Object.keys(t).length ? Object.entries(t).map(([label, s]) =>
          h('div', {}, label.replace(/^\d+:/, ''), ': ', `${s.ok} sent`, s.failed ? `, ${s.failed} failed${s.last_error ? ' (' + s.last_error + ')' : ''}` : '', s.dropped ? `, ${s.dropped} dropped` : '')) : 'nothing valid',
          v.invalid_lines ? h('div', { class: 'err' }, `${v.invalid_lines} line(s) in the URL list aren’t valid http(s) URLs`) : null));
      } else if (v == null || typeof v === 'object') continue;
      else if (k.endsWith('_time') || k.startsWith('last_') && typeof v === 'string' && /^\d{4}-/.test(v)) out.push(h('dt', {}, title(k)), h('dd', {}, ago(v)));
      else out.push(h('dt', {}, title(k)), h('dd', {}, String(v)));
    }
    return out;
  }

  function card(src) {
    const name = src.meta.name, hl = src.health || {}, [label, tone] = STATE[hl.state] || [hl.state, ''];
    const enabled = src.settings.enabled;
    const sw = h('input', { type: 'checkbox', checked: enabled, onchange: async () => {
      sw.disabled = true;
      try { await put(`/sources/${name}`, { enabled: sw.checked }); toast(sw.checked ? `${title(name)} turned on` : `${title(name)} turned off`); }
      catch (e) { toast(e.message, 'bad'); }
      load();
    } });
    const hasPushToken = (src.secret_fields || []).includes('push_token');
    const dr = detailRows(hl.detail);
    return h('article', { class: 'card source' },
      h('header', {}, h('div', { style: { flex: 1, minWidth: 0 } }, h('h2', {}, title(name)), h('p', { class: 'desc' }, src.meta.description)),
        h('span', { class: 'badge' }, h('span', { class: 'dot ' + tone }), label)),
      h('div', { class: 'row' }, h('span', { class: 'pill' }, CATEGORY[src.meta.category] || src.meta.category),
        src.meta.poll_interval_s ? h('span', { class: 'pill' }, `Checks every ${src.settings.interval_s || src.meta.poll_interval_s} s`) : h('span', { class: 'pill' }, 'Receives pushes'),
        h('span', { class: 'pill' }, `Licence: ${src.meta.licence.type}`)),
      hl.last_error ? h('div', { class: 'errbox' }, hl.last_error) : null,
      hl.last_success ? h('div', { class: 'muted', style: { fontSize: '14px' } }, `Last data ${ago(hl.last_success)}`) : null,
      dr.length ? h('dl', { class: 'kv' }, dr) : null,
      hasPushToken && enabled ? h('div', { class: 'pushurl' }, 'Point your device’s upload at ', h('b', {}, `${location.origin}/ingest/${name}/<push token>`)) : null,
      src.meta.licence.notes ? h('div', { class: 'faint', style: { fontSize: '13px' } }, src.meta.licence.notes) : null,
      h('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: 'auto' } }, h('label', { class: 'switch' }, sw, 'On'), h('button', { class: 'btn small', onclick: () => openDrawer(src) }, 'Settings')));
  }

  function openDrawer(src) {
    drawerOpen = true;
    const form = buildForm(src), err = h('p', { class: 'err', role: 'alert' });
    const close = () => { drawerOpen = false; scrim.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const save = h('button', { class: 'btn primary', onclick: async () => {
      err.textContent = ''; save.disabled = true;
      let body;
      try { body = form.collect(); } catch (e) { err.textContent = 'One of the fields isn’t valid JSON.'; save.disabled = false; return; }
      try {
        await put(`/sources/${src.meta.name}`, body);
        toast('Saved'); close(); load();
      } catch (e) { err.textContent = e.message; save.disabled = false; }
    } }, 'Save changes');
    const scrim = h('div', { class: 'drawer-scrim', onclick: (e) => { if (e.target === scrim) close(); } },
      h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': `${title(src.meta.name)} settings` },
        h('h2', {}, title(src.meta.name)), h('p', { class: 'muted', style: { marginTop: 0 } }, src.meta.description),
        src.meta.licence.notes ? h('p', { class: 'faint', style: { fontSize: '13px' } }, `Terms: ${src.meta.licence.notes}`) : null,
        src.can_suggest ? suggestions(src, form) : null,
        form.el, err, h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: close }, 'Cancel'), save)));
    document.addEventListener('keydown', onKey);
    document.body.append(scrim);
    scrim.querySelector('input,select,textarea')?.focus();
  }

  /** "Fill from my location": offers choices worked out from the home location; picking one fills the form, it doesn't save. */
  function suggestions(src, form) {
    const out = h('div', { class: 'suggest-out', 'aria-live': 'polite' });
    const btn = h('button', { type: 'button', class: 'btn small', onclick: async () => {
      btn.disabled = true; out.replaceChildren(h('p', { class: 'hint' }, 'Looking up options near your home…'));
      try {
        const r = await get(`/sources/${src.meta.name}/suggest`);
        if (!r.candidates.length) { out.replaceChildren(h('p', { class: 'hint' }, r.note)); return; }
        out.replaceChildren(
          h('p', { class: 'hint' }, `Options near ${r.location.name}. Choosing one fills the form below; it isn’t saved until you press Save changes.`),
          h('ul', { class: 'suggest-list' }, r.candidates.map((c) => h('li', {},
            h('div', {}, h('strong', {}, c.label), c.current ? h('span', { class: 'pill', style: { marginLeft: '8px' } }, 'in use') : null,
              h('div', { class: 'hint' }, c.detail)),
            h('button', { type: 'button', class: 'btn small', onclick: () => {
              const n = form.set(c.values);
              toast(n.length ? 'Filled in. Save to keep it.' : 'Already set to this');
            } }, c.current ? 'Use again' : 'Use this')))),
          r.note ? h('p', { class: 'hint' }, r.note) : null);
      } catch (e) { out.replaceChildren(h('p', { class: 'err', role: 'alert' }, e.message)); }
      finally { btn.disabled = false; }
    } }, 'Fill from my location');
    return h('div', { class: 'suggest' }, btn, out);
  }

  await load();
  const timer = setInterval(() => { if (!drawerOpen) load(); }, 15000);
  return () => clearInterval(timer);
}
