const TZ = 'Australia/Sydney';          // everything is stored in UTC; shown in Sydney time
const fTime = new Intl.DateTimeFormat('en-AU', { timeZone: TZ, hour: 'numeric', minute: '2-digit', hour12: true });
const fTimeS = new Intl.DateTimeFormat('en-AU', { timeZone: TZ, hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
const fDay = new Intl.DateTimeFormat('en-AU', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short' });
const fHour = new Intl.DateTimeFormat('en-AU', { timeZone: TZ, hour: 'numeric', hour12: true });
const fKey = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });

const clean = (s) => s.replace(/ /g, ' ').replace(/\s?(am|pm)/i, (m, x) => ' ' + x.toLowerCase()).replace(/^\s+/, '');
export const d = (v) => (v instanceof Date ? v : new Date(v));
export const time = (v) => clean(fTime.format(d(v)));
export const timeSec = (v) => clean(fTimeS.format(d(v)));
export const day = (v) => fDay.format(d(v));
export const hourLabel = (v) => clean(fHour.format(d(v))).replace(' ', '');
export const dayKey = (v) => fKey.format(d(v));
export const dateTime = (v) => `${day(v)}, ${time(v)}`;

export function ago(v, now = Date.now()) {
  const s = Math.max(0, Math.round((now - d(v).getTime()) / 1000));
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const hr = Math.floor(m / 60);
  if (hr < 24) return `${hr} h ${m % 60} min ago`;
  return `${Math.floor(hr / 24)} d ago`;
}
export const minutesSince = (v, now = Date.now()) => (now - d(v).getTime()) / 60000;

const fParts = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric' });
/** Sydney's offset from UTC (ms) at instant t, DST-aware. */
export function tzOffsetMs(t) {
  const p = Object.fromEntries(fParts.formatToParts(new Date(t)).map((x) => [x.type, +x.value]));
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(t / 1000) * 1000;
}

const fWeekday = new Intl.DateTimeFormat('en-AU', { timeZone: TZ, weekday: 'short' });
/** "3:40 pm" if it is today (Sydney), else "Sat 3:40 pm": for times that could be mistaken for today's. */
export const when = (v, now = Date.now()) => (dayKey(v) === dayKey(now) ? time(v) : `${fWeekday.format(d(v))} ${time(v)}`);
