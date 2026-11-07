// Stored in SI-ish units (m/s, mm, hPa, degC). Shown the Australian way: km/h for wind.
export const msToKmh = (v) => v * 3.6;
export const COMPASS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
export const compass = (deg) => COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
export const fmt = (v, dp = 0) => (v == null || Number.isNaN(v) ? '–' : Number(v).toFixed(dp).replace(/^-0(\.0+)?$/, '0$1'));
