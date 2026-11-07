let csrf = null;
export const setCsrf = (v) => { csrf = v; };

export class ApiError extends Error {
  constructor(status, detail) { super(typeof detail === 'string' ? detail : JSON.stringify(detail)); this.status = status; this.detail = detail; }
}

function describe(detail) {
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
}

export async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrf && method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const r = await fetch('/api' + path, { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  const text = await r.text();
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  if (!r.ok) {
    if (r.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('auth-lost'));
    throw new ApiError(r.status, describe(data && data.detail !== undefined ? data.detail : data) || r.statusText);
  }
  return data;
}
export const get = (p) => api('GET', p);
export const put = (p, b) => api('PUT', p, b);
export const post = (p, b) => api('POST', p, b);

export async function authStatus() {
  const s = await get('/auth/status');
  setCsrf(s.csrf);
  return s;
}
