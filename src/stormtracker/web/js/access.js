// Settings cards for who and what can get in: read-only API tokens (for agents and other apps) and single sign-on.
import { h, clear, toast } from './dom.js';
import { get, put, post, api } from './api.js';
import { dateTime, ago } from './time.js';

const H2 = { fontSize: '17px', marginBottom: '4px' };
const copy = async (text, what) => {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); }
  catch { toast('Couldn’t copy. Select it and copy by hand.', 'bad'); }
};
const mono = { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '13px' };

function codeRow(label, text, what) {
  return h('div', { class: 'field' }, h('span', { class: 'lbl' }, label),
    h('div', { style: { display: 'flex', gap: '8px' } },
      h('input', { type: 'text', readonly: true, value: text, style: mono, 'aria-label': label, onfocus: (e) => e.target.select() }),
      h('button', { type: 'button', class: 'btn small', onclick: () => copy(text, what) }, 'Copy')));
}

export function tokensCard() {
  const list = h('div', {}), fresh = h('div', { 'aria-live': 'polite' }), err = h('p', { class: 'err', role: 'alert' });
  const name = h('input', { type: 'text', maxlength: 80, placeholder: 'What it is for, for example Claude on my desktop', 'aria-label': 'Token name' });
  async function paint() {
    let rows = [];
    try { rows = await get('/tokens'); } catch (e) { clear(list).append(h('p', { class: 'err' }, e.message)); return; }
    clear(list).append(rows.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['Name', 'Created', 'Last used', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, rows.map((t) => h('tr', {}, h('td', {}, t.name), h('td', {}, dateTime(t.created_at)), h('td', {}, t.last_used ? ago(t.last_used) : 'never'),
        h('td', {}, h('button', { class: 'btn small danger', 'aria-label': `Revoke ${t.name}`, onclick: async () => {
          if (!confirm(`Revoke “${t.name}”? Anything using it stops working immediately.`)) return;
          try { await api('DELETE', `/tokens/${t.id}`); toast('Token revoked'); clear(fresh); paint(); } catch (e) { toast(e.message, 'bad'); }
        } }, 'Revoke'))))))) : h('p', { class: 'muted' }, 'No tokens yet.'));
  }
  const make = h('button', { class: 'btn primary small', onclick: async () => {
    err.textContent = ''; make.disabled = true;
    try {
      const t = await post('/tokens', { name: name.value.trim() });
      name.value = '';
      const o = location.origin;
      clear(fresh).append(h('div', { class: 'card', style: { margin: '12px 0', borderColor: 'var(--accent, currentColor)' } },
        h('strong', {}, `Token “${t.name}” created`), h('p', { class: 'hint', style: { margin: '4px 0 10px' } }, 'Copy it now. It is shown once and can’t be recovered. If you lose it, revoke it and make another.'),
        codeRow('Token', t.token, 'Token'),
        codeRow('Read the weather over the API', `curl -H "Authorization: Bearer ${t.token}" ${o}/api/now`, 'Command'),
        codeRow('Add to Claude Code (MCP)', `claude mcp add --transport http stormtracker ${o}/mcp --header "Authorization: Bearer ${t.token}"`, 'Command'),
        h('p', { class: 'hint' }, `Other MCP clients: server address ${o}/mcp, with the header Authorization: Bearer and the token.`)));
      paint();
    } catch (e) { err.textContent = e.message; } finally { make.disabled = false; }
  } }, 'Create token');
  name.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); make.click(); } });
  paint();
  return h('section', { class: 'card' }, h('h2', { style: H2 }, 'API tokens for agents and apps'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Let an AI agent or another app use StormTracker as its weather source. A token can read the weather (conditions, radar, storm outlook, warnings, forecast) and nothing else: it can’t change settings or see any secret. Treat it like a password.'),
    h('p', { class: 'muted' }, 'The agent connects either to the MCP address shown after you create a token, or to /api/now, which returns the whole picture in one document with units on every value.'),
    h('p', { class: 'hint' }, 'Keep StormTracker on your home network. The Bureau of Meteorology’s data is free for personal use but not for passing on, so don’t put this on the public internet or hand tokens to services outside your home.'),
    list, fresh,
    h('div', { style: { display: 'flex', gap: '8px', marginTop: '10px' } }, name, make), err);
}

export async function ssoCard() {
  let c;
  try { c = await get('/auth/oidc/settings'); } catch (e) { return h('section', { class: 'card' }, h('h2', { style: H2 }, 'Single sign-on'), h('p', { class: 'err' }, e.message)); }
  const lines = (a) => (a || []).join('\n');
  const parse = (t) => t.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
  const f = {
    enabled: h('input', { type: 'checkbox', checked: !!c.enabled }),
    label: h('input', { type: 'text', value: c.label, maxlength: 40 }),
    issuer: h('input', { type: 'text', value: c.issuer, spellcheck: 'false', placeholder: 'The provider’s issuer address' }),
    client_id: h('input', { type: 'text', value: c.client_id, spellcheck: 'false', autocomplete: 'off' }),
    secret: h('input', { type: 'password', autocomplete: 'new-password', placeholder: c.client_secret_set ? 'Saved. Leave blank to keep it' : 'Client secret' }),
    public_url: h('input', { type: 'text', value: c.public_url || location.origin, spellcheck: 'false' }),
    scopes: h('input', { type: 'text', value: c.scopes, spellcheck: 'false' }),
    username_claim: h('input', { type: 'text', value: c.username_claim, spellcheck: 'false' }),
    groups_claim: h('input', { type: 'text', value: c.groups_claim, spellcheck: 'false' }),
    admin_groups: h('textarea', { rows: 3, spellcheck: 'false', placeholder: 'One group name per line' }, lines(c.admin_groups)),
    member_groups: h('textarea', { rows: 3, spellcheck: 'false', placeholder: 'One group name per line' }, lines(c.member_groups)),
  };
  const err = h('p', { class: 'err', role: 'alert' }), result = h('div', { 'aria-live': 'polite' }), redirect = h('div', {});
  const paintRedirect = () => { clear(redirect); const u = f.public_url.value.trim().replace(/\/+$/, ''); if (u) redirect.append(codeRow('Redirect address to give your provider', `${u}/api/auth/oidc/callback`, 'Redirect address')); };
  f.public_url.addEventListener('input', paintRedirect); paintRedirect();
  let dropSecret = false;
  const body = () => ({
    enabled: f.enabled.checked, label: f.label.value.trim() || 'Single sign-on', issuer: f.issuer.value.trim(), client_id: f.client_id.value.trim(), public_url: f.public_url.value.trim().replace(/\/+$/, ''),
    scopes: f.scopes.value.trim(), username_claim: f.username_claim.value.trim() || 'preferred_username', groups_claim: f.groups_claim.value.trim() || 'groups',
    admin_groups: parse(f.admin_groups.value), member_groups: parse(f.member_groups.value),
    ...(dropSecret ? { client_secret: '' } : f.secret.value ? { client_secret: f.secret.value } : {}),
  });
  const field = (lbl, el, hint) => h('label', { class: 'field' }, h('span', { class: 'lbl' }, lbl), el, hint ? h('span', { class: 'hint' }, hint) : null);
  const save = h('button', { class: 'btn primary small', onclick: async () => {
    err.textContent = ''; save.disabled = true;
    try { const r = await put('/auth/oidc/settings', body()); f.secret.value = ''; dropSecret = false; f.secret.placeholder = r.client_secret_set ? 'Saved. Leave blank to keep it' : 'Client secret'; toast(r.enabled ? 'Single sign-on saved and on' : 'Single sign-on saved (off)'); }
    catch (e) { err.textContent = e.message; } finally { save.disabled = false; }
  } }, 'Save');
  const test = h('button', { class: 'btn small', onclick: async () => {
    clear(result).append(h('p', { class: 'hint' }, 'Checking…')); test.disabled = true;
    try {
      await put('/auth/oidc/settings', body());
      const r = await post('/auth/oidc/test');
      clear(result).append(r.ok ? h('p', { class: 'ok' }, `Reached the provider: ${r.signing_keys} signing key${r.signing_keys === 1 ? '' : 's'}${r.has_userinfo ? ', user info available' : ''}${r.supports_pkce ? '' : '. It doesn’t advertise PKCE, so sign-in may be refused'}.`) : h('p', { class: 'err', role: 'alert' }, r.error));
    } catch (e) { clear(result).append(h('p', { class: 'err', role: 'alert' }, e.message)); } finally { test.disabled = false; }
  } }, 'Save and test connection');
  return h('section', { class: 'card' }, h('h2', { style: H2 }, 'Single sign-on (OpenID Connect)'),
    h('p', { class: 'muted', style: { marginTop: 0 } }, 'Let people sign in with your identity provider (authentik, Keycloak, Authelia and similar). Their provider groups decide their role: admins can change everything, members can only look at the map, station and alert history. Someone in neither list is refused. Password sign-in keeps working, so you can always get back in.'),
    h('label', { class: 'switch', style: { marginBottom: '14px' } }, f.enabled, 'Allow single sign-on'),
    h('div', { class: 'grid cols-2', style: { gap: '0 16px' } },
      field('Button text', f.label),
      field('StormTracker’s address', f.public_url, 'The https address people type to reach StormTracker. Your provider sends them back here.'),
      field('Issuer', f.issuer, 'For authentik this is shown on the application’s provider page and usually ends in /application/o/ and the application name. It must be https.'),
      field('Client ID', f.client_id),
      field('Client secret', f.secret, 'Stored encrypted and never shown again.'),
      h('div', {}, c.client_secret_set ? h('button', { type: 'button', class: 'btn small', style: { marginTop: '22px' }, onclick: (e) => { dropSecret = true; f.secret.value = ''; f.secret.placeholder = 'Will be removed when you press Save'; e.target.disabled = true; } }, 'Remove saved secret') : null)),
    redirect,
    h('div', { class: 'grid cols-2', style: { gap: '0 16px' } },
      field('Admin groups', f.admin_groups, 'People in any of these groups can change everything.'),
      field('Member groups', f.member_groups, 'People in any of these (and no admin group) can view the weather but change nothing.'),
      field('Groups claim', f.groups_claim, 'The name of the field that lists a person’s groups. In authentik it comes with the profile scope.'),
      field('Username claim', f.username_claim),
      field('Scopes', f.scopes, 'Must include openid.')),
    h('p', { class: 'hint' }, `A person’s role is set when they sign in and lasts ${c.session_hours} hours, so a change of group applies after that.`),
    err, result, h('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap' } }, test, save));
}
