import { h, clear, icons, toast } from './dom.js';
import { authStatus, post, get } from './api.js';

const app = document.getElementById('app');
// admin: true views are for admins only; members (read-only single sign-on users) get the rest. The server enforces this too.
const VIEWS = {
  map: { title: 'Map', load: () => import('./views/map.js') },
  station: { title: 'Station', load: () => import('./views/station.js') },
  alerts: { title: 'Alerts', load: () => import('./views/alerts.js') },
  sources: { title: 'Sources', admin: true, load: () => import('./views/sources.js') },
  settings: { title: 'Settings', admin: true, load: () => import('./views/settings.js') },
};
const isAdmin = () => !session || session.role === 'admin';
const allowed = () => Object.entries(VIEWS).filter(([, v]) => !v.admin || isAdmin());
let cleanup = null, session = null, healthTimer = null;

const theme = {
  get() { return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); },
  toggle() {
    const next = theme.get() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('st-theme', next); } catch { /* private mode */ }
    window.dispatchEvent(new CustomEvent('theme-change', { detail: next }));
  },
};
window.stTheme = theme;
// following the system theme: when the OS switches (e.g. at sunset) and no choice was made here, the map's tiles follow too
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  if (!document.documentElement.dataset.theme) window.dispatchEvent(new CustomEvent('theme-change', { detail: e.matches ? 'dark' : 'light' }));
});

async function start() {
  try { session = await authStatus(); } catch (e) { return showFatal(e); }
  if (session.setup_required) return showAuth('setup');
  if (!session.authenticated) return showAuth('login');
  showShell();
}

function showFatal(e) {
  clear(app).append(h('div', { class: 'auth' }, h('div', { class: 'card' }, h('h1', {}, 'Can’t reach the server'), h('p', {}, e.message || String(e)),
    h('button', { class: 'btn primary', onclick: () => location.reload() }, 'Try again'))));
}

function showAuth(mode) {
  stopView();
  const setup = mode === 'setup';
  const sso = !setup && session && session.sso && session.sso.enabled ? session.sso : null;
  const user = h('input', { type: 'text', autocomplete: 'username', required: true, value: setup ? 'admin' : '' });
  const pass = h('input', { type: 'password', autocomplete: setup ? 'new-password' : 'current-password', required: true, minlength: setup ? 12 : undefined });
  const token = h('input', { type: 'password', autocomplete: 'off' });
  const err = h('p', { class: 'err', role: 'alert' });
  const form = h('form', { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = '';
    try {
      if (setup) await post('/setup', { username: user.value, password: pass.value, setup_token: token.value || null });
      await post('/auth/login', { username: user.value, password: pass.value });
      pass.value = '';
      session = await authStatus();
      showShell();
    } catch (ex) { err.textContent = ex.message; }
  } },
    h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Username'), user),
    h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Password'), pass, setup ? h('span', { class: 'hint' }, 'At least 12 characters.') : null),
    setup ? h('label', { class: 'field' }, h('span', { class: 'lbl' }, 'Setup token'), token, h('span', { class: 'hint' }, 'Printed in the server log on first start (docker compose logs stormtracker).')) : null,
    err, h('button', { class: 'btn primary', type: 'submit', style: { width: '100%', justifyContent: 'center', marginTop: '6px' } }, setup ? 'Create admin account' : 'Sign in'));
  clear(app).append(h('div', { class: 'auth' }, h('div', { class: 'card' },
    h('div', { class: 'brand', style: { marginBottom: '14px' } }, icons.logo(), h('span', {}, 'StormTracker')),
    h('h1', {}, setup ? 'Set up StormTracker' : 'Sign in'),
    h('p', {}, setup ? 'Create the admin account that manages sources and settings.' : 'Your storm and weather tracker.'),
    // a plain link, not a form: the browser is sent to the identity provider and back
    sso ? h('a', { class: 'btn primary', href: '/api/auth/oidc/login', style: { width: '100%', justifyContent: 'center', boxSizing: 'border-box', marginBottom: '14px' } }, sso.label) : null,
    sso ? h('p', { class: 'hint', style: { margin: '0 0 10px' } }, 'Or sign in with a password') : null,
    form)));
  user.focus();
}

function showShell() {
  const nav = h('nav', { class: 'nav', 'aria-label': 'Main' }, ...allowed().map(([k, v]) => h('a', { href: '#/' + k, dataset: { view: k } }, v.title)));
  const themeBtn = h('button', { class: 'iconbtn', 'aria-label': 'Switch light or dark theme', title: 'Switch light or dark theme', onclick: theme.toggle });
  const paint = () => clear(themeBtn).append(theme.get() === 'dark' ? icons.sun() : icons.moon());
  paint(); window.addEventListener('theme-change', paint);
  const dot = h('span', { class: 'dot' }), txt = h('span', {}, '');
  const chip = h('a', { class: 'health-chip', href: '#/sources', title: 'Source health' }, dot, txt);
  const who = session && session.username ? `Sign out (${session.username.replace(/^sso:/, '')}${session.role === 'member' ? ', member' : ''})` : 'Sign out';
  const out = h('button', { class: 'iconbtn logout', 'aria-label': 'Sign out', title: who, onclick: async () => { try { await post('/auth/logout'); } catch { /* ignore */ } location.reload(); } }, icons.logout());
  const main = h('main', { id: 'main' });
  clear(app).append(h('header', { class: 'topbar' }, h('a', { class: 'brand', href: '#/map' }, icons.logo(), h('span', {}, 'StormTracker')), nav, h('div', { class: 'spacer' }), isAdmin() ? chip : null, themeBtn, out), main);

  async function health() {
    try {
      const st = await get('/status');
      const all = Object.entries(st).filter(([, v]) => v.state !== 'disabled');
      const bad = all.filter(([, v]) => ['down', 'degraded', 'missing_credentials'].includes(v.state));
      dot.className = 'dot ' + (bad.some(([, v]) => v.state === 'down') ? 'bad' : bad.length ? 'warn' : all.length ? 'ok' : '');
      txt.textContent = !all.length ? 'No sources on' : bad.length ? `${bad.length} source${bad.length > 1 ? 's' : ''} need attention` : 'All sources healthy';
      chip.title = all.map(([n, v]) => `${n}: ${v.state}`).join('\n');
    } catch { /* auth-lost handled globally */ }
  }
  clearInterval(healthTimer);
  if (isAdmin()) { health(); healthTimer = setInterval(health, 30000); }
  window.addEventListener('hashchange', route);
  route();
}

async function route() {
  const main = document.getElementById('main');
  if (!main) return;
  const key = (location.hash.match(/^#\/(\w+)/) || [])[1];
  if (!VIEWS[key] || (VIEWS[key].admin && !isAdmin())) { location.hash = '#/map'; return; }
  document.querySelectorAll('.nav a').forEach((a) => (a.dataset.view === key ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  document.title = `${VIEWS[key].title} – StormTracker`;
  stopView();
  clear(main);
  try {
    const mod = await VIEWS[key].load();
    cleanup = (await mod.mount(main, { session })) || null;
  } catch (e) {
    console.error(e);
    main.append(h('div', { class: 'page' }, h('div', { class: 'wrap' }, h('div', { class: 'card empty' }, h('strong', {}, 'This page didn’t load'), e.message || String(e)))));
  }
}
function stopView() { if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; } }

window.addEventListener('auth-lost', () => { clearInterval(healthTimer); window.removeEventListener('hashchange', route); stopView(); toast('Your session ended. Sign in again.', 'bad'); showAuth('login'); });
start();
