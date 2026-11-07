import { h, add, clear, toast } from '../dom.js';
import { get, put, post, api } from '../api.js';
import { buildForm } from '../schemaform.js';
import { dateTime, ago } from '../time.js';

const SEVERITY = { info: 'Heads-up', warning: 'Warning', critical: 'Urgent' };
const OUTPUT_TITLE = { ntfy: 'ntfy (phone notifications)', apprise: 'Apprise (Telegram, Discord, Slack...)', webhook: 'Webhook', mqtt: 'MQTT', email: 'Email' };
const OUTPUT_ORDER = ['ntfy', 'apprise', 'webhook', 'mqtt', 'email'];
const KIND = { alert: 'Alert', clear: 'All clear', test: 'Test' };

function describe(r) {
  const p = r.params;
  switch (p.type) {
    case 'storm_eta': return `A storm core of ${p.min_intensity_mmh} mm/h or more expected at home within ${p.max_eta_min} min`;
    case 'rain_eta': return `Rain peaking at ${p.min_peak_mmh} mm/h or more expected within ${p.max_eta_min} min${p.min_confidence !== 'low' ? `, when the forecast is at least ${p.min_confidence} confidence` : ''}`;
    case 'raining_now': return `Raining at home at ${p.min_rate_mmh} mm/h or more`;
    case 'lightning': return `${p.min_strikes} lightning strike${p.min_strikes > 1 ? 's' : ''} within ${p.within_km} km in the last ${p.within_min} min`;
    case 'warning': return `A warning mentioning “${p.match_text}” is in force`;
    case 'storm_potential': return `The model forecasts CAPE of ${p.min_cape_jkg} J/kg or more in the next ${p.within_hours} h${p.organised_only ? ', with strong wind shear' : ''}`;
    case 'gust': return `A gust of ${p.min_gust_kmh} km/h or more at your station`;
    case 'pressure_drop': return `Pressure down ${p.min_drop_hpa} hPa or more in an hour at your station`;
    default: return p.type;
  }
}
const outputState = (o) => {
  const s = o.settings, missing = Object.entries(s.secrets_set || {}).filter(([k, v]) => !v && o.schema.required?.includes(k)).map(([k]) => k);
  if (!s.enabled) return { label: 'Off', tone: '' };
  if (missing.length) return { label: 'Needs setup', tone: 'warn' };
  if (o.health && o.health.last_error) return { label: 'Last delivery failed', tone: 'bad' };
  return { label: 'On', tone: 'ok' };
};

/** What a member sees: the alerts that are active and the recent history. Rules and destinations are for admins. */
async function mountReadOnly(wrap) {
  const box = h('div', {});
  wrap.append(h('div', { class: 'page-head' }, h('h1', {}, 'Alerts'), h('span', { class: 'muted' }, 'Alerts StormTracker has raised. An admin manages the rules and where they are sent.')), box);
  async function load() {
    const [history, status] = await Promise.all([get('/alerts/history?limit=30'), get('/alerts/status').catch(() => ({}))]);
    const active = status.active || [];
    clear(box).append(
      h('h2', { style: { fontSize: '18px', marginBottom: '10px' } }, 'Active now'),
      active.length ? h('div', { class: 'card' }, active.map((a) => h('div', { style: { padding: '4px 0' } }, h('b', {}, a.title), h('span', { class: 'muted' }, ` since ${ago(a.since)}`))))
        : h('div', { class: 'card empty' }, 'No alerts are active.'),
      h('h2', { style: { fontSize: '18px', margin: '22px 0 10px' } }, 'Recent alerts'),
      history.length ? h('div', { class: 'card' }, h('div', { class: 'tablewrap', style: { maxHeight: '420px' } }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['When (Sydney)', 'What', 'Rule'].map((t) => h('th', {}, t)))),
        h('tbody', {}, history.map((a) => h('tr', {}, h('td', {}, dateTime(a.ts)), h('td', {}, h('b', {}, a.title), ' ', h('span', { class: 'pill' }, KIND[a.kind] || a.kind), h('div', { class: 'muted' }, a.message)), h('td', {}, a.rule_name))))))) : h('div', { class: 'card empty' }, 'Nothing has been sent yet.'));
  }
  await load();
  const timer = setInterval(() => load().catch(() => {}), 20000);
  return () => clearInterval(timer);
}

export async function mount(root, ctx = {}) {
  const page = h('div', { class: 'page' }), wrap = h('div', { class: 'wrap' });
  page.append(wrap); root.append(page);
  if (ctx.session && ctx.session.role !== 'admin') return mountReadOnly(wrap);
  const [types, presets] = await Promise.all([get('/alerts/rule-types'), get('/alerts/presets')]);
  const typeInfo = Object.fromEntries(types.map((t) => [t.type, t]));
  let outputs = [], rules = [], preview = {}, history = [], settings = {}, status = {}, drawerOpen = false;

  const head = h('div', { class: 'page-head' }, h('h1', {}, 'Alerts'), h('span', { class: 'muted' }, 'Rules watch the radar forecast, your station, lightning and warnings, and send a message when one applies.'));
  const globalBox = h('section', { class: 'card', style: { marginBottom: '14px' } });
  const rulesBox = h('section', { style: { marginBottom: '22px' } });
  const outBox = h('section', { style: { marginBottom: '22px' } });
  const histBox = h('section', {});
  wrap.append(head, globalBox, rulesBox, outBox, histBox);

  async function load() {
    [outputs, rules, preview, history, settings, status] = await Promise.all([get('/alerts/outputs'), get('/alerts/rules'), get('/alerts/preview').catch(() => ({})), get('/alerts/history?limit=30'), get('/alerts/settings'), get('/alerts/status').catch(() => ({}))]);
    outputs.sort((a, b) => OUTPUT_ORDER.indexOf(a.meta.name) - OUTPUT_ORDER.indexOf(b.meta.name));
    paint();
  }

  // ---------- global ----------
  function paintGlobal() {
    const on = h('input', { type: 'checkbox', checked: !!settings.enabled });
    const url = h('input', { type: 'url', value: settings.public_url || '', placeholder: 'http://homeserver:8000', spellcheck: 'false' });
    const err = h('p', { class: 'err', role: 'alert' });
    add(clear(globalBox),
      h('div', { style: { display: 'flex', gap: '16px', flexWrap: 'wrap', alignItems: 'flex-start' } },
        h('label', { class: 'switch', style: { marginTop: '6px' } }, on, 'Send alerts'),
        h('label', { class: 'field', style: { flex: '1 1 280px', margin: 0 } }, h('span', { class: 'lbl' }, 'Address of this server'), url,
          h('span', { class: 'hint' }, 'Alerts include a link to the map here. Use the address your phone can reach, for example http://homeserver:8000.')),
        h('button', { class: 'btn primary small', style: { marginTop: '28px' }, onclick: async () => {
          err.textContent = '';
          try { settings = await put('/alerts/settings', { ...settings, enabled: on.checked, public_url: url.value.trim() || null }); toast('Saved'); paint(); }
          catch (e) { err.textContent = e.message; }
        } }, 'Save')),
      err,
      status.state === 'error' || status.state === 'degraded' ? h('p', { class: 'err' }, status.state === 'error' ? `Alert checks are failing: ${status.error}` : `Some rules couldn’t be checked: ${status.error}`) : null,
      status.last_run ? h('p', { class: 'faint', style: { margin: '6px 0 0', fontSize: '13px' } }, `Rules were last checked ${ago(status.last_run)}.`) : null);
  }

  // ---------- rules ----------
  function paintRules() {
    const active = new Set((status.active || []).map((a) => a.rule_id));
    clear(rulesBox);
    add(rulesBox, h('div', { style: { display: 'flex', alignItems: 'baseline', gap: '12px', marginBottom: '10px', flexWrap: 'wrap' } }, h('h2', { style: { fontSize: '18px' } }, 'Rules'),
      h('button', { class: 'btn small', onclick: () => editRule(null) }, 'Add a rule')));
    if (!rules.length) {
      add(rulesBox, h('div', { class: 'card' }, h('div', { class: 'empty', style: { padding: '8px' } }, h('strong', {}, 'No alert rules yet'), 'Start from one of these, then adjust it to suit.'),
        h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center', marginTop: '8px' } }, presets.map((p) => h('button', { class: 'btn small', onclick: () => editRule(null, p) }, p.name)))));
      return;
    }
    add(rulesBox, h('div', { class: 'grid cols-2' }, rules.map((r) => {
      const matches = preview[r.id] || [], offOutputs = r.outputs.filter((n) => { const o = outputs.find((x) => x.meta.name === n); return !o || !o.settings.enabled; });
      const sw = h('input', { type: 'checkbox', checked: r.enabled, onchange: async () => { sw.disabled = true; try { await put(`/alerts/rules/${r.id}`, { ...r, enabled: sw.checked }); } catch (e) { toast(e.message, 'bad'); } load(); } });
      return h('article', { class: 'card source' },
        h('header', {}, h('div', { style: { flex: 1, minWidth: 0 } }, h('h3', { style: { fontSize: '16px' } }, r.name), h('p', { class: 'desc' }, describe(r))),
          h('span', { class: 'badge' }, h('span', { class: 'dot ' + (r.severity === 'critical' ? 'bad' : r.severity === 'warning' ? 'warn' : '') }), SEVERITY[r.severity])),
        (status.rule_errors || {})[r.id] ? h('div', { class: 'errbox' }, `This rule couldn’t be checked: ${status.rule_errors[r.id]}`) : null,
        matches.length ? h('div', { class: 'match' }, h('b', {}, 'Matching right now'), matches.map((m) => h('div', {}, m.message))) : null,
        active.has(r.id) && !matches.length ? h('div', { class: 'faint', style: { fontSize: '13px' } }, 'Alerted recently; waiting to see if it is over.') : null,
        h('div', { class: 'row' }, r.outputs.length ? r.outputs.map((n) => h('span', { class: 'pill' }, OUTPUT_TITLE[n] || n)) : h('span', { class: 'pill' }, 'No destinations: nothing will be sent'),
          r.cooldown_min ? h('span', { class: 'pill' }, `At most every ${r.cooldown_min} min`) : null, r.notify_clear ? h('span', { class: 'pill' }, 'Tells you when it is over') : null),
        offOutputs.length ? h('div', { class: 'faint', style: { fontSize: '13px' } }, `Not delivered yet: ${offOutputs.map((n) => OUTPUT_TITLE[n] || n).join(', ')} ${offOutputs.length > 1 ? 'are' : 'is'} turned off.`) : null,
        h('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: 'auto' } }, h('label', { class: 'switch' }, sw, 'On'), h('button', { class: 'btn small', onclick: () => editRule(r) }, 'Edit')));
    })));
  }

  function editRule(rule, preset) {
    drawerOpen = true;
    const base = rule || (preset ? { ...preset, outputs: outputs.filter((o) => o.settings.enabled).map((o) => o.meta.name), enabled: true } : { name: '', severity: 'warning', cooldown_min: 30, repeat_after_min: 0, notify_clear: false, enabled: true, outputs: outputs.filter((o) => o.settings.enabled).map((o) => o.meta.name), params: { type: 'storm_eta' } });
    let type = base.params.type, paramsForm = null;
    const name = h('input', { type: 'text', value: base.name, maxlength: 80, placeholder: 'For example: Storm heading for home' });
    const typeSel = h('select', { onchange: () => { type = typeSel.value; if (!rule && !name.value.trim()) name.value = typeInfo[type].title; drawParams({ type }); } }, types.map((t) => h('option', { value: t.type, selected: t.type === type }, t.title)));
    const typeHint = h('span', { class: 'hint' }), paramsEl = h('div', {});
    function drawParams(values) {
      typeHint.textContent = typeInfo[type].description;
      paramsForm = buildForm({ meta: { name: `rule-${type}` }, schema: typeInfo[type].schema, secret_fields: [], settings: { enabled: true, config: values, secrets_set: {} } }, { showEnabled: false, showInterval: false });
      clear(paramsEl).append(paramsForm.el);
    }
    drawParams(base.params);
    const sev = h('select', {}, Object.entries(SEVERITY).map(([k, v]) => h('option', { value: k, selected: k === base.severity }, v)));
    const picks = outputs.map((o) => ({ o, box: h('input', { type: 'checkbox', checked: base.outputs.includes(o.meta.name) }) }));
    const cool = h('input', { type: 'number', min: 0, max: 1440, value: base.cooldown_min });
    const rep = h('input', { type: 'number', min: 0, max: 1440, value: base.repeat_after_min });
    const clr = h('input', { type: 'checkbox', checked: !!base.notify_clear });
    const on = h('input', { type: 'checkbox', checked: base.enabled !== false });
    const err = h('p', { class: 'err', role: 'alert' });
    const close = () => { drawerOpen = false; scrim.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const save = h('button', { class: 'btn primary', onclick: async () => {
      err.textContent = ''; save.disabled = true;
      const body = { name: name.value.trim(), enabled: on.checked, severity: sev.value, outputs: picks.filter((x) => x.box.checked).map((x) => x.o.meta.name),
        cooldown_min: Number(cool.value || 0), repeat_after_min: Number(rep.value || 0), notify_clear: clr.checked, params: { type, ...paramsForm.collect().config } };
      try { await (rule ? put(`/alerts/rules/${rule.id}`, body) : post('/alerts/rules', body)); toast('Saved'); close(); load(); }
      catch (e) { err.textContent = e.message; save.disabled = false; }
    } }, 'Save rule');
    const del = rule ? h('button', { class: 'btn danger', onclick: async () => {
      if (!confirm(`Delete “${rule.name}”?`)) return;
      try { await api('DELETE', `/alerts/rules/${rule.id}`); toast('Deleted'); close(); load(); } catch (e) { err.textContent = e.message; }
    } }, 'Delete') : null;
    const scrim = h('div', { class: 'drawer-scrim', onclick: (e) => { if (e.target === scrim) close(); } },
      h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': rule ? `Edit ${rule.name}` : 'New alert rule' },
        h('h2', {}, rule ? 'Edit rule' : 'New alert rule'),
        h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Name'), name),
        h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Alert me when'), typeSel, typeHint),
        paramsEl,
        h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'How urgent'), sev, h('span', { class: 'hint' }, 'Urgent alerts use the highest priority your destination supports.')),
        h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Send to'), outputs.length ? picks.map(({ o, box }) => h('label', { style: { display: 'flex', gap: '8px', alignItems: 'center', padding: '4px 0' } }, box,
          OUTPUT_TITLE[o.meta.name] || o.meta.name, !o.settings.enabled ? h('span', { class: 'faint' }, '(turned off)') : null)) : null,
          h('span', { class: 'hint' }, 'Set destinations up below. A rule with none selected records alerts in the history only.')),
        h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Wait at least this many minutes between alerts'), cool,
          h('span', { class: 'hint' }, 'Stops a burst of messages. The same storm or event only alerts once however long it lasts, unless you ask for reminders.')),
        h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Remind me every (minutes)'), rep, h('span', { class: 'hint' }, '0 means never remind.')),
        h('label', { class: 'switch', style: { marginBottom: '12px' } }, clr, 'Tell me when it’s over'),
        h('label', { class: 'switch', style: { marginBottom: '8px', display: 'flex' } }, on, 'Rule is on'),
        err, h('div', { class: 'actions', style: { justifyContent: 'space-between' } }, del || h('span'), h('div', { style: { display: 'flex', gap: '10px' } }, h('button', { class: 'btn', onclick: close }, 'Cancel'), save))));
    document.addEventListener('keydown', onKey);
    document.body.append(scrim);
    name.focus();
  }

  // ---------- destinations ----------
  function paintOutputs() {
    clear(outBox);
    add(outBox, h('h2', { style: { fontSize: '18px', marginBottom: '10px' } }, 'Destinations'),
      h('div', { class: 'grid cols-2' }, outputs.map((o) => {
        const name = o.meta.name, st = outputState(o);
        const sw = h('input', { type: 'checkbox', checked: o.settings.enabled, onchange: async () => { sw.disabled = true; try { await put(`/alerts/outputs/${name}`, { enabled: sw.checked }); } catch (e) { toast(e.message, 'bad'); } load(); } });
        const test = h('button', { class: 'btn small', onclick: async () => {
          test.disabled = true; test.textContent = 'Sending…';
          try { const r = await post(`/alerts/outputs/${name}/test`); r.ok ? toast('Test sent. Check your destination.') : toast(`Test failed: ${r.error}`, 'bad'); }
          catch (e) { toast(e.message, 'bad'); }
          load();
        } }, 'Send a test');
        return h('article', { class: 'card source' },
          h('header', {}, h('div', { style: { flex: 1, minWidth: 0 } }, h('h3', { style: { fontSize: '16px' } }, OUTPUT_TITLE[name] || name), h('p', { class: 'desc' }, o.meta.description)),
            h('span', { class: 'badge' }, h('span', { class: 'dot ' + st.tone }), st.label)),
          o.health && o.health.last_error ? h('div', { class: 'errbox' }, o.health.last_error) : null,
          o.health && o.health.last_success ? h('div', { class: 'muted', style: { fontSize: '14px' } }, `Last delivered ${ago(o.health.last_success)}`) : null,
          h('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: 'auto' } }, h('label', { class: 'switch' }, sw, 'On'),
            h('div', { class: 'row' }, o.settings.enabled ? test : null, h('button', { class: 'btn small', onclick: () => editOutput(o) }, 'Settings'))));
      })));
  }

  function editOutput(o) {
    drawerOpen = true;
    const form = buildForm({ ...o, meta: { ...o.meta, poll_interval_s: 0 } }, { showInterval: false });
    const err = h('p', { class: 'err', role: 'alert' });
    const close = () => { drawerOpen = false; scrim.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const save = h('button', { class: 'btn primary', onclick: async () => {
      err.textContent = ''; save.disabled = true;
      let body; try { body = form.collect(); } catch { err.textContent = 'One of the fields isn’t valid.'; save.disabled = false; return; }
      try { await put(`/alerts/outputs/${o.meta.name}`, { enabled: body.enabled, config: body.config, secrets: body.secrets }); toast('Saved'); close(); load(); }
      catch (e) { err.textContent = e.message; save.disabled = false; }
    } }, 'Save changes');
    const scrim = h('div', { class: 'drawer-scrim', onclick: (e) => { if (e.target === scrim) close(); } },
      h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': `${OUTPUT_TITLE[o.meta.name] || o.meta.name} settings` },
        h('h2', {}, OUTPUT_TITLE[o.meta.name] || o.meta.name), h('p', { class: 'muted', style: { marginTop: 0 } }, o.meta.description), form.el, err,
        h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: close }, 'Cancel'), save)));
    document.addEventListener('keydown', onKey);
    document.body.append(scrim);
  }

  // ---------- history ----------
  function paintHistory() {
    clear(histBox);
    add(histBox, h('h2', { style: { fontSize: '18px', marginBottom: '10px' } }, 'Recent alerts'),
      history.length ? h('div', { class: 'card' }, h('div', { class: 'tablewrap', style: { maxHeight: '340px' } }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['When (Sydney)', 'What', 'Rule', 'Delivery'].map((t) => h('th', {}, t)))),
        h('tbody', {}, history.map((a) => {
          const res = Object.entries(a.results || {});
          return h('tr', {}, h('td', {}, dateTime(a.ts)), h('td', {}, h('b', {}, a.title), ' ', h('span', { class: 'pill' }, KIND[a.kind] || a.kind), h('div', { class: 'muted' }, a.message)),
            h('td', {}, a.rule_name), h('td', {}, res.length ? res.map(([n, r]) => h('div', {}, `${OUTPUT_TITLE[n] || n}: `, r.ok ? 'delivered' : `failed (${r.error})`)) : 'Not sent anywhere'));
        }))))) : h('div', { class: 'card empty' }, 'Nothing has been sent yet. Use “Send a test” on a destination to try one.'));
  }

  function paint() { paintGlobal(); paintRules(); paintOutputs(); paintHistory(); }
  await load();
  const timer = setInterval(() => { if (!drawerOpen) load().catch(() => {}); }, 20000);
  return () => clearInterval(timer);
}
