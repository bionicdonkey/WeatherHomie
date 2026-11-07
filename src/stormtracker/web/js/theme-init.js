// Sets the theme before first paint so there is no flash. A separate file because the Content-Security-Policy forbids inline scripts.
try { var t = localStorage.getItem('st-theme'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) { /* private mode */ }
