# Security

## Reporting a problem

Please report security problems **privately**, not in a public issue: use the repository's
**Security → Report a vulnerability** (GitHub private vulnerability reporting). Include what you found, how to reproduce it
and which version. This is a personal project maintained in spare time, so I can't promise a timetable, but I'll acknowledge
a report and credit you if you'd like.

## What this is designed for

StormTracker is built to run **on your own network** (a home server or NAS), for one household:

* There is one local admin account (argon2 password), optional single sign-on, and read-only API tokens. It is not
  multi-tenant software.
* It is **not hardened for the open internet.** If you expose it, put it behind HTTPS and a reverse proxy, set
  `COOKIE_SECURE=true` and `TRUSTED_PROXIES`, and consider extra access control in the proxy. See *Security* in the README.
* `MASTER_KEY` protects stored secrets (API keys, tokens). Keep it out of version control and back it up; anyone with the
  database and the key can read the secrets.
* The weather it shows is **not official warning information.** Follow the Bureau of Meteorology and your emergency services.

## Supported versions

Only the latest release gets fixes.
