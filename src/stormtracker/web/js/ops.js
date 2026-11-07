// Settings cards for running the installation: Home Assistant (MQTT) and backup / restore.
import { h, add, clear, toast, nice } from './dom.js';
import { get, put, post } from './api.js';
import { dateTime, ago } from './time.js';

const H2 = { fontSize: '17px', marginBottom: '4px' };
const field = (lbl, el, hint) => h('label', { class: 'field' }, h('span', { class: 'lbl' }, lbl), el, hint ? h('span', { class: 'hint' }, hint) : null);

const HA_STATE = { ok: ['Publishing', 'ok'], off: ['Off', ''], error: ['Not publishing', 'bad'], needs_broker: ['Needs a broker', 'warn'] };

export async function haCard() {
  let d;
  try { d = await get('/homeassistant'); } catch (e) { return h('section', { class: 'card' }, h('h2', { style: H2 }, 'Home Assistant (MQTT)'), h('p', { class: 'err' }, e.message)); }
  const f = {
    enabled: h('input', { type: 'checkbox', checked: !!d.settings.enabled }),
    interval_s: h('input', { type: 'number', min: 15, max: 3600, step: 1, value: d.settings.interval_s }),
    discovery_prefix: h('input', { type: 'text', value: d.settings.discovery_prefix, spellcheck: 'false' }),
    base_topic: h('input', { type: 'text', value: d.settings.base_topic, spellcheck: 'false' }),
  };
  const status = h('p', { class: 'muted', 'aria-live': 'polite' }), broker = h('p', { class: 'muted' }), err = h('p', { class: 'err', role: 'alert' });
  function paint(v) {
    d = v;
    const [label, tone] = HA_STATE[v.status.state] || [v.status.state, ''];
    add(clear(status), h('span', { class: 'badge' }, h('span', { class: 'dot ' + tone }), label), v.status.last_publish ? ` Last update sent ${ago(v.status.last_publish)}.` : '', v.status.error ? h('span', { class: 'err' }, ` ${v.status.error}`) : null);
    add(clear(broker), v.broker.ready ? `Broker: ${v.broker.host}, port ${v.broker.port} (from the MQTT destination under Alerts).` : h('span', { class: 'err' }, `${v.broker.note}. Set its broker address on the `), v.broker.ready ? null : h('a', { href: '#/alerts' }, 'Alerts page'), v.broker.ready ? null : '. It doesn’t need to be switched on.');
  }
  paint(d);
  const body = () => ({ enabled: f.enabled.checked, interval_s: Number(f.interval_s.value), discovery_prefix: f.discovery_prefix.value.trim(), base_topic: f.base_topic.value.trim() });
  const act = (btn, fn) => async () => { err.textContent = ''; btn.disabled = true; try { paint(await fn()); } catch (e) { err.textContent = e.message; try { paint(await get('/homeassistant')); } catch { /* keep the old status */ } } finally { btn.disabled = false; } };
  const save = h('button', { class: 'btn primary small' }, 'Save'); save.onclick = act(save, async () => { const v = await put('/homeassistant', body()); toast(v.settings.enabled ? 'Saved. Entities will appear in Home Assistant shortly' : 'Saved (off)'); return v; });
  const now = h('button', { class: 'btn small' }, 'Send now'); now.onclick = act(now, async () => { await put('/homeassistant', body()); const v = await post('/homeassistant/publish'); toast('Sent to the broker'); return v; });
  const rm = h('button', { class: 'btn small danger' }, 'Remove from Home Assistant');
  rm.onclick = async () => {
    if (!confirm('Remove the StormTracker device and its entities from Home Assistant? If the integration is still switched on they come back at the next update.')) return;
    err.textContent = ''; rm.disabled = true;
    try { const r = await post('/homeassistant/remove'); toast(`Removed ${r.removed} entities`); } catch (e) { err.textContent = e.message; } finally { rm.disabled = false; }
  };
  return h('section', { class: 'card' }, h('h2', { style: H2 }, 'Home Assistant (MQTT)'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, `Shows up in Home Assistant as one StormTracker device with ${d.entities.length} sensors: your station’s readings, when rain is due, the nearest storm, lightning, warnings and the forecast’s strongest gust. Use them in automations like any other sensor. Home Assistant finds them by itself through its MQTT integration, using the same broker as your Alerts MQTT destination.`),
    broker, status,
    h('label', { class: 'switch', style: { marginBottom: '14px' } }, f.enabled, 'Send to Home Assistant'),
    h('div', { class: 'grid cols-2', style: { gap: '0 16px' } },
      field('Seconds between updates', f.interval_s, 'Entities show as unavailable if three updates are missed.'),
      field('Home Assistant discovery prefix', f.discovery_prefix, 'Leave as homeassistant unless you changed it in Home Assistant.'),
      field('Topic name', f.base_topic, 'Also the device’s id. Change it only to run a second StormTracker.')),
    h('details', { style: { margin: '0 0 12px' } }, h('summary', {}, `The ${d.entities.length} entities`), h('p', { class: 'hint' }, d.entities.map((e) => e.name).join(' · '))),
    err, h('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap' } }, now, rm, save));
}

const LABEL = { locations: 'Places', ui: 'Map and address search', retention: 'Data retention', nowcast: 'Storm tracking and forecast', 'alerts.rules': 'Alert rules', 'alerts.settings': 'Alert settings', 'auth.oidc': 'Single sign-on', 'mqtt.ha': 'Home Assistant' };
const keyLabel = (k) => LABEL[k] || (k.startsWith('source.') ? `Source: ${nice(k.slice(7))}` : k.startsWith('alert_output.') ? `Alert destination: ${nice(k.slice(13))}` : k);
const STATUS = { new: 'Will be added', changed: 'Will change', removed: 'Will be removed', same: 'Same' };

function download(name, obj) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' }));
  const a = h('a', { href: url, download: name, style: { display: 'none' } });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export async function backupCard() {
  let info = { undo: null, min_passphrase: 12 };
  try { info = await get('/backup'); } catch { /* the card still works for export */ }
  // ---- download ----
  const withSecrets = h('input', { type: 'checkbox' }), pass = h('input', { type: 'password', autocomplete: 'new-password', minlength: info.min_passphrase }), pass2 = h('input', { type: 'password', autocomplete: 'new-password' });
  const passBox = h('div', { class: 'grid cols-2', style: { gap: '0 16px', display: 'none' } },
    field('Passphrase for the secrets', pass, `At least ${info.min_passphrase} characters. You need it to restore the secrets, and it can’t be recovered.`), field('Passphrase again', pass2));
  withSecrets.onchange = () => { passBox.style.display = withSecrets.checked ? '' : 'none'; };
  const dlErr = h('p', { class: 'err', role: 'alert' });
  const dl = h('button', { class: 'btn primary small', onclick: async () => {
    dlErr.textContent = '';
    if (withSecrets.checked && pass.value !== pass2.value) { dlErr.textContent = 'The two passphrases don’t match.'; return; }
    dl.disabled = true;
    try {
      const doc = await post('/backup/export', { include_secrets: withSecrets.checked, passphrase: withSecrets.checked ? pass.value : null });
      download(`stormtracker-backup-${new Date().toISOString().slice(0, 10)}${withSecrets.checked ? '-with-secrets' : ''}.json`, doc);
      pass.value = pass2.value = ''; toast('Backup downloaded');
    } catch (e) { dlErr.textContent = e.message; } finally { dl.disabled = false; }
  } }, 'Download backup');

  // ---- restore ----
  const file = h('input', { type: 'file', accept: 'application/json,.json', 'aria-label': 'Backup file' }), rpass = h('input', { type: 'password', autocomplete: 'off' });
  const out = h('div', { 'aria-live': 'polite' }), rErr = h('p', { class: 'err', role: 'alert' });
  let doc = null;
  const readFile = async () => {
    const f = file.files[0]; if (!f) throw new Error('Choose a backup file first.');
    if (f.size > 2_000_000) throw new Error('That file is too large to be a StormTracker backup.');
    try { return JSON.parse(await f.text()); } catch { throw new Error('That file isn’t valid JSON, so it isn’t a StormTracker backup.'); }
  };
  const check = h('button', { class: 'btn small', onclick: async () => {
    rErr.textContent = ''; clear(out); check.disabled = true;
    try {
      doc = await readFile();
      const p = await post('/backup/preview', { backup: doc, passphrase: rpass.value || null });
      const rows = p.settings.filter((r) => r.status !== 'same');
      clear(out).append(h('div', { class: 'card', style: { margin: '12px 0' } },
        h('strong', {}, p.will_change ? `${p.will_change} setting${p.will_change === 1 ? '' : 's'} will change` : 'This backup matches your current settings'),
        p.created_at ? h('span', { class: 'muted' }, ` · made ${dateTime(p.created_at)}`) : null,
        p.warnings.length ? h('ul', { class: 'warnlist' }, p.warnings.map((w) => h('li', {}, w))) : null,
        rows.length ? h('div', { class: 'tablewrap', style: { margin: '10px 0' } }, h('table', { class: 'data' }, h('tbody', {}, rows.map((r) => h('tr', {}, h('td', {}, keyLabel(r.key)), h('td', {}, STATUS[r.status]))))))
          : null,
        h('p', { class: 'hint' }, p.secrets.included ? `Includes ${p.secrets.count} secret${p.secrets.count === 1 ? '' : 's'}; your current ones will be replaced.` : 'Secrets are not restored; your current ones are kept.'),
        p.ignored.length ? h('p', { class: 'hint' }, `Skipped ${p.ignored.length} item${p.ignored.length === 1 ? '' : 's'} this version doesn’t use: ${p.ignored.map((i) => i.key).join(', ')}.`) : null,
        h('p', { class: 'hint' }, 'Restoring replaces your current settings with the backup’s, and keeps a copy so you can undo it.'),
        h('div', { style: { display: 'flex', gap: '10px' } }, h('button', { class: 'btn danger small', onclick: restore }, 'Restore this backup'))));
    } catch (e) { rErr.textContent = e.message; } finally { check.disabled = false; }
  } }, 'Check backup');
  async function restore(ev) {
    if (!confirm('Replace your current settings with this backup? A copy of the current settings is kept so you can undo it.')) return;
    ev.target.disabled = true; rErr.textContent = '';
    try { await post('/backup/restore', { backup: doc, passphrase: rpass.value || null }); toast('Backup restored. Reloading…'); setTimeout(() => location.reload(), 1200); }
    catch (e) { rErr.textContent = e.message; ev.target.disabled = false; }
  }
  const undo = info.undo ? h('div', { style: { marginTop: '14px' } }, h('p', { class: 'muted', style: { margin: '0 0 6px' } }, `You restored a backup, and the settings from before it were saved ${ago(info.undo.created_at)}.`),
    h('button', { class: 'btn small', onclick: async (e) => {
      if (!confirm('Put your settings back to how they were before the last restore?')) return;
      e.target.disabled = true; try { await post('/backup/undo'); toast('Undone. Reloading…'); setTimeout(() => location.reload(), 1200); } catch (x) { rErr.textContent = x.message; e.target.disabled = false; }
    } }, 'Undo last restore')) : null;

  return h('section', { class: 'card' }, h('h2', { style: H2 }, 'Backup and restore'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Saves everything you’ve set up here (places, map settings, sources, alert rules and destinations, forecast, Home Assistant and sign-in settings) in one file, so you can move to a new machine or recover from a mistake. Readings, radar images and your password aren’t included.'),
    h('p', { class: 'hint' }, 'The file contains your home location, so keep it private. API keys and tokens are left out unless you add a passphrase: then they’re included, encrypted with it.'),
    h('label', { class: 'switch', style: { margin: '10px 0' } }, withSecrets, 'Include secrets (API keys, tokens), protected by a passphrase'), passBox, dlErr, dl,
    h('hr', { style: { border: 0, borderTop: '1px solid var(--line, #8883)', margin: '18px 0' } }),
    h('h3', { style: { fontSize: '15px', margin: '0 0 6px' } }, 'Restore'),
    h('div', { class: 'grid cols-2', style: { gap: '0 16px' } }, field('Backup file', file), field('Passphrase (if it includes secrets)', rpass, 'Without it, the settings are restored and secrets are left as they are.')),
    rErr, check, out, undo);
}
