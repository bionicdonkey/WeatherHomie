import { h, nice, toast } from './dom.js';

/** Resolve pydantic's anyOf [{type}, {type: null}] to the one real type. */
function resolve(p) {
  if (p.anyOf) { const real = p.anyOf.find((x) => x.type !== 'null') || p.anyOf[0]; return { ...real, ...p, anyOf: undefined, type: real.type, enum: real.enum || p.enum }; }
  return p;
}
// pydantic always fills in a title (the field name in Title Case); only a title someone wrote on purpose is used
const label = (name, raw) => (raw && raw.title && raw.title !== name.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) ? raw.title : nice(name));
const isScalarList = (p) => p.type === 'array' && p.items && ['string', 'integer', 'number'].includes(p.items.type);
/** 24 URL-safe characters (144 bits) from the browser's CSPRNG. */
function randomToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(18));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_');
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Build a form for one source from its pydantic JSON schema.
 *   src: one entry of GET /api/sources  ({meta, schema, secret_fields, settings})
 * collect() -> {enabled, interval_s, config, secrets}. Secret values are write-only: the form only ever
 * shows "set / not set", never a value, and sends nothing for a secret left blank.
 */
export function buildForm(src, opts = {}) {
  const props = src.schema.properties || {}, required = new Set(src.schema.required || []);
  const cfg = src.settings.config || {}, isSet = src.settings.secrets_set || {};
  const inputs = {}, secretInputs = {}, removed = new Set();
  const root = h('div', {});

  const showEnabled = opts.showEnabled !== false;                 // rule parameters have no on/off switch of their own
  const enabled = h('input', { type: 'checkbox', checked: showEnabled ? !!src.settings.enabled : true });
  if (showEnabled) root.append(h('label', { class: 'switch', style: { marginBottom: '16px' } }, enabled, 'Enabled'));

  for (const [name, raw] of Object.entries(props)) {
    const p = resolve(raw), isSecret = !!raw.secret;
    const lbl = label(name, raw);
    let field;
    if (isSecret) {
      const multiline = p.format === 'textarea';
      const input = multiline ? h('textarea', { autocomplete: 'off', spellcheck: 'false', placeholder: isSet[name] ? 'Stored. Type here to replace it.' : '' })
        : h('input', { type: 'password', autocomplete: 'new-password', placeholder: isSet[name] ? 'Stored. Type here to replace it.' : '' });
      secretInputs[name] = input;
      const state = h('div', { class: 'secret-state' }, h('span', { class: 'dot ' + (isSet[name] ? 'ok' : '') }), isSet[name] ? 'A value is stored (hidden)' : 'Not set');
      if (isSet[name]) {
        const clr = h('button', { type: 'button', class: 'linkbtn', onclick: () => { removed.add(name); state.lastChild.textContent = 'Will be removed when you save'; clr.remove(); } }, 'Remove it');
        state.append(clr);
      }
      let extra = null;
      if (raw.generate && !multiline) {
        // The value is write-only once saved, so show it plainly here, and say so, so it can be copied into the device.
        const pathLine = h('div', { class: 'pushurl', hidden: true });
        const showPath = () => {
          pathLine.hidden = !input.value;
          if (typeof raw.generate === 'string') pathLine.replaceChildren(h('b', {}, raw.generate.replace('{value}', input.value)));   // plugin supplied the wording
          else pathLine.replaceChildren('Enter this in your device: ', h('b', {}, `${location.origin}/ingest/${src.meta.name}/${input.value}`));
        };
        input.addEventListener('input', showPath);
        const gen = h('button', { type: 'button', class: 'btn small', onclick: () => {
          input.type = 'text'; input.value = randomToken(); showPath();
          copy.hidden = false; note.hidden = false;
        } }, isSet[name] ? 'Generate a new one' : 'Generate');
        const copy = h('button', { type: 'button', class: 'btn small', hidden: true, onclick: async () => {
          try { await navigator.clipboard.writeText(input.value); toast('Copied'); } catch { input.select(); toast('Press Ctrl+C to copy', 'bad'); }
        } }, 'Copy');
        const note = h('span', { class: 'hint', hidden: true }, 'Copy it now. After you save, it is stored hidden and can’t be shown again.');
        extra = h('div', {}, h('div', { style: { display: 'flex', gap: '8px', margin: '6px 0' } }, gen, copy), note, pathLine);
      }
      // A <div>, not a <label>: a label that wraps buttons ("Remove it", "Generate") would hand its click to the first button.
      const id = `secret-${src.meta.name}-${name}`;
      input.id = id;
      field = h('div', { class: 'field' }, h('label', { class: 'lbl', for: id }, lbl, required.has(name) ? ' (required to enable)' : ''), state, input,
        p.description ? h('span', { class: 'hint' }, p.description) : null, extra);
    } else {
      let input;
      const val = cfg[name] ?? p.default ?? '';
      if (p.type === 'boolean') {
        input = h('input', { type: 'checkbox', checked: !!(cfg[name] ?? p.default) });
        field = h('label', { class: 'field' }, h('span', { class: 'switch' }, input, lbl), p.description ? h('span', { class: 'hint' }, p.description) : null);
      } else {
        if (p.enum) input = h('select', {}, ...p.enum.map((v) => h('option', { value: v, selected: v === val }, String(v))));
        else if (p.type === 'integer' || p.type === 'number') input = h('input', { type: 'number', step: p.type === 'integer' ? 1 : 'any', min: p.minimum, max: p.maximum, value: val });
        else if (p.type === 'array' && isScalarList(p)) input = h('textarea', { spellcheck: 'false', placeholder: 'One value per line' }, Array.isArray(val) ? val.join('\n') : '');
        else if (p.type === 'array' || p.type === 'object') input = h('textarea', { spellcheck: 'false' }, val === '' ? '' : JSON.stringify(val, null, 2));
        else if (p.format === 'textarea') input = h('textarea', {}, val);
        else input = h('input', { type: 'text', value: val, pattern: p.pattern, maxlength: p.maxLength, autocomplete: 'off', spellcheck: 'false' });
        field = h('label', { class: 'field' }, h('span', { class: 'lbl' }, lbl, required.has(name) ? ' (required)' : ''), input, p.description ? h('span', { class: 'hint' }, p.description) : null);
      }
      inputs[name] = { input, p };
    }
    root.append(field);
  }

  const minInt = opts.showInterval === false ? 0 : (src.meta.poll_interval_s || 0);
  const interval = h('input', { type: 'number', min: minInt || 1, step: 1, value: src.settings.interval_s ?? '', placeholder: String(minInt || '') });
  if (minInt) root.append(h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Poll interval (seconds)'), interval,
    h('span', { class: 'hint' }, `Leave empty for the default (${minInt} s). Faster than that is not allowed: it is this source's polite minimum.`)));

  return {
    el: root,
    /** Fill plain (non-secret) fields from {name: value}; nothing is saved until the form is. Returns the names that changed. */
    set(values) {
      const changed = [];
      for (const [name, v] of Object.entries(values || {})) {
        const f = inputs[name];
        if (!f || f.p.type === 'boolean') continue;
        const next = Array.isArray(v) ? v.join('\n') : String(v);
        if (f.input.value !== next) { f.input.value = next; changed.push(name); }
        f.input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return changed;
    },
    collect() {
      // Send a value only if it was already stored or differs from the schema default, so saving without
      // edits changes nothing and a future change to a default still reaches installs that never set it.
      const config = {};
      const put = (name, v, p) => { if (name in cfg || !same(v, p.default ?? null)) config[name] = v; };
      for (const [name, { input, p }] of Object.entries(inputs)) {
        if (p.type === 'boolean') { put(name, input.checked, p); continue; }
        const v = input.value.trim();
        if (v === '') continue;                      // empty = default (config is replaced wholesale, so omitting resets it)
        if (p.type === 'integer') put(name, parseInt(v, 10), p);
        else if (p.type === 'number') put(name, parseFloat(v), p);
        else if (p.type === 'array' && isScalarList(p)) put(name, v.split(/\r?\n/).map((x) => x.trim()).filter(Boolean).map((x) => (p.items.type === 'string' ? x : Number(x))), p);
        else if (p.type === 'array' || p.type === 'object') put(name, JSON.parse(v), p);
        else put(name, v, p);
      }
      const secrets = {};
      for (const [name, input] of Object.entries(secretInputs)) {
        if (input.value !== '') secrets[name] = input.value;
        else if (removed.has(name)) secrets[name] = null;
      }
      return { enabled: enabled.checked, config, secrets, interval_s: interval.value === '' ? null : parseInt(interval.value, 10), clear_interval: interval.value === '' };
    },
  };
}
