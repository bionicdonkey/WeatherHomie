// Which station to show first: your own (1-minute) before cloud read-backs before official 30-minute stations.
const PRIORITY = ['ecowitt', 'ecowitt_cloud', 'wunderground_pws', 'bom_observations'];
const rank = (s) => { const i = PRIORITY.indexOf(s.source); return i < 0 ? PRIORITY.length : i; };
export const ranked = (stations) => [...(stations || [])].sort((a, b) => rank(a) - rank(b) || (b.last_ts || '').localeCompare(a.last_ts || ''));
export const own = (stations) => ranked(stations).find((s) => s.source !== 'bom_observations') || null;
export const official = (stations) => (stations || []).find((s) => s.source === 'bom_observations') || null;
