# StormTracker

A self-hosted storm and weather tracker for Australia, for one household. It puts the Bureau of Meteorology's radar loop on
a map beside your own weather station, tracks storm cells, forecasts when rain will reach your home, and sends alerts to your
phone or Home Assistant. Data sources are plugins you switch on in the web UI; nothing is configured in files.

* **Radar and storm nowcast:** BOM radar on a map, storm cells followed frame to frame, and an estimate of when rain reaches
  home (pySTEPS optical flow), with the measured skill published below.
* **Your station:** Ecowitt consoles (push, cloud or forwarding), Weather Underground read-back, BOM stations, wind, rain,
  pressure and lightning charts.
* **Warnings and forecast:** BOM warnings for your area, and a weather-model forecast of storm ingredients and gusts (Open-Meteo).
* **Alerts:** rules for storms, rain, lightning, warnings, gusts and wind changes, delivered by ntfy, Apprise, webhook, MQTT or email.
* **Connects to other things:** Home Assistant through MQTT discovery, and read-only tokens plus an MCP server so an AI agent can
  use it as a weather source.
* **Yours to keep:** settings live in the database and are edited in the UI, secrets are encrypted at rest, and settings can be
  backed up and restored.

![The map: a storm forecast to reach home, with the radar timeline underneath](docs/images/map-forecast.png)

<p align="center">
<img src="docs/images/map-dark.png" width="62%" alt="The same map in dark mode, showing the latest radar frame">
<img src="docs/images/map-phone.png" width="22%" alt="The map on a phone">
</p>

*Screenshots use a synthetic demo storm over Sydney, not real radar. Map tiles are OpenStreetMap.*

> **Not for safety-critical use.** Radar and model figures here are estimates. For severe weather follow the Bureau of Meteorology
> (bom.gov.au) and your emergency services. This software comes with no warranty (see the licence).
>
> **Data terms apply to you, not just to this code.** BOM's web products are free for personal use but not for redistribution
> or commercial use; Open-Meteo is free for non-commercial use with attribution; OpenStreetMap's public tile and address servers
> have usage policies. Each source's terms are shown in its card in the UI and noted below. Run it for yourself, on your own network.

## Quick start (Docker)

You need Docker with Compose, and a machine that stays on.

```bash
git clone https://github.com/bionicdonkey/WeatherHomie.git stormtracker && cd stormtracker
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"      # paste the output after MASTER_KEY= in .env
# edit docker-compose.yml: the image ghcr.io/bionicdonkey/weatherhomie:latest is used by default; or switch to "build: ." to build from source
docker compose up -d
docker compose logs stormtracker | grep "setup token"             # the one-time token for creating the admin account
```

Open `http://<your-server>:8000`, create the admin account (or set `ADMIN_PASSWORD` in `.env` first), then:

1. **Settings → Locations:** set your home (type an address, or enter coordinates).
2. **Sources:** switch on `bom_radar` and press **Fill from my location** for the nearest radar; add `bom_observations`,
   `bom_warnings` and `open_meteo` the same way, and your weather station (see below).
3. **Alerts:** add a destination (ntfy is the quickest) and the rules you want.

`.env` holds bootstrap values only (`MASTER_KEY`, optional admin password and database address); everything else is stored in the
database. **Keep `MASTER_KEY` safe and backed up:** it encrypts your stored API keys, and without it they must be entered again.
Releases are published as multi-arch images (amd64 and arm64) to GitHub Container Registry, with build provenance.

## Getting your weather station in (pick any, or several)

An Ecowitt console has one "Customized" upload slot. If something else already uses it, you have three
ways in. They are independent sources; enable whichever fit. Each stores observations under its own
`source` name, so enabling more than one gives you one series per source (query with `?source=`).

| Option | Source | Latency | Needs |
|---|---|---|---|
| 1. Device pushes to this server | `ecowitt` | real time | the Customized slot |
| 2. Relay on to Home Assistant etc., or be relayed to | `ecowitt` | real time | the Customized slot (or a forwarder) |
| 3a. Read back from Ecowitt.net | `ecowitt_cloud` | device's cloud upload interval | application key, API key, MAC |
| 3b. Read back from Weather Underground | `wunderground_pws` | WU upload interval | PWS API key, station id |

### 1. Direct push (`ecowitt`)

Enable the `ecowitt` source and set a `push_token` secret (any random string). In the Ecowitt app
(WS View Plus -> device -> Customized) set:

| Field | Value |
|---|---|
| Protocol | Ecowitt (or Wunderground; both are accepted) |
| Server IP / hostname | the LAN address of this server |
| Path | `/ingest/ecowitt/<push_token>` |
| Port | `8000` (or your `HTTP_PORT`) |
| Upload interval | 60 s |

Gateways that implement the local HTTP API (GW1100/GW2000/GW3000) can instead be polled by setting
`gateway_host`. Consoles such as the HP25xx (EasyWeatherPro firmware) do not serve live data locally.
Unrecognised numeric fields are stored as `x_<field>` so extra sensors are never lost.

### 2. Forwarding (`ecowitt`, same source)

The console has one Customized slot, but one receiver can feed many systems. Point the console at this
server (option 1) and set the `forward_urls` secret on the `ecowitt` source: one URL per line, blank lines
and `# comments` ignored. Every accepted push is then relayed, byte for byte (same method, body, query and
content type), to each URL, for example Home Assistant's Ecowitt webhook:

```
http://homeassistant.local:8123/api/webhook/<ecowitt webhook id>
```

* Relaying happens in the background after the device is answered, so a slow or dead target never delays
  or breaks ingestion. Each target has a 5 s timeout and at most 5 in-flight relays; beyond that pushes are
  dropped for that target and counted.
* Per-target counters (`ok`, `failed`, `dropped`, `last_status`, `last_error`) are shown in the source's
  health detail, labelled by host and port only. The URLs are stored encrypted because they can contain
  secrets, and the path and query are never logged or returned.
* Our `push_token` is not sent to targets. Relayed requests carry `X-Stormtracker-Forwarded: 1`, and a
  request that already has it is ingested but not relayed again, so two instances pointed at each other
  cannot loop.
* Only accepted pushes are relayed (token and `allowed_passkey` already checked).

The reverse also works: if another receiver already owns the console's slot and can forward, send to
`POST http://<server>:8000/ingest/ecowitt/<push_token>`, or post to `/ingest/ecowitt` with the token in an
`X-Stormtracker-Token` or `Authorization: Bearer` header if the forwarder cannot change the path.

### 3a. Ecowitt.net cloud API (`ecowitt_cloud`)

Needs nothing from the console. Create an application key and API key at ecowitt.net (Profile -> API keys)
and note the device MAC (Devices page). Enable the `ecowitt_cloud` source and set `application_key`,
`api_key` (secrets, write-only) and `mac`. It polls `api/v3/device/real_time` once a minute. Values are
converted using the unit string the API returns with each reading, and anything with an unrecognised unit
is skipped and counted in the source's health detail rather than guessed. If the device stops uploading,
the cloud keeps returning old values; health goes `degraded` when the newest reading is over 30 minutes old.

### 3b. Weather Underground read-back (`wunderground_pws`)

Enable the `wunderground_pws` source, set the `api_key` secret (the PWS-contributor key from your WU
account) and `wu_station_id` (e.g. `IABC123`). It reads your own station's latest observation every two
minutes. WU returns nothing if the station has not reported within the last hour (health shows `degraded`).
Check Weather Underground's current API terms: this is for personal use of your own station's data only.

Keys that travel in a query string (options 3a/3b) are redacted from stored errors and httpx request
logging is silenced, so they do not reach logs or the status API.

## Web UI

Open `http://<server>:8000/`. On first start, create the admin account (the setup token is printed in the
server log: `docker compose logs stormtracker`), then:

* **Map**: BOM radar loop on a map, reprojected on the server so it lines up with the basemap. The timeline
  at the bottom has one column per radar frame: bar height is the heaviest rain within 25 km of home, and the
  magenta mark means rain over home itself. Scrub it with the mouse, touch or arrow keys; Space plays and
  pauses. The Now panel shows your station's latest readings (and the 3-hour pressure trend) beside what the
  radar shows over home. Layers: radar opacity, lightning strikes from the last hour (markers where a feed gives coordinates, distance rings
  for your own station's sensor, which knows how far a strike was but not which way), storm cells, and BOM's own overlays
  (place names, range rings, roads...). **Wind arrows** (Layers → Wind) point the way the wind is blowing: dark with the speed in km/h
  for your station and BOM stations, light for the weather model's wind on a 5 x 5 grid around home (`open_meteo`, `wind_grid`). Warnings in force for your area are listed at the top of the Now panel with links to BOM.
* **Station**: charts for temperature and dew point, rain rate, rain since midnight, wind and gusts, pressure,
  humidity and solar, over 3 h to 3 days, a **wind rose** (how often the wind came from each direction, by strength, with a table view), the model's
  **wind and gust forecast**, plus a table view of every chart, the station's lightning strikes, and
  a list of every field the device sent (including any not yet mapped, shown as `x_...`).
* **Sources**: health of every source, on/off switches, and a settings form generated from each plugin's
  schema. Secrets are write-only: the form shows "stored (hidden)" and never a value.
* **Settings**: locations (the first is home), storm tracking, map and address search, API tokens, single sign-on, Home Assistant, backup and restore, and your account.
* **Alerts** and **Station** are also open to read-only *members* (single sign-on users); see *Access* below.

Times are shown in Australia/Sydney; everything is stored in UTC. The UI is plain HTML/JS/CSS served by the
backend (no build step, no CDN). Leaflet (BSD-2) and the Schibsted Grotesk font (SIL OFL) are vendored in
`src/stormtracker/web/vendor/` with their licences.

**Address search.** Settings → Locations has **Find by address** (press Search; nothing is sent while you type), and the
`bom_warnings` "Fill from my location" also offers area words taken from the place names around your home. Both ask a
[Nominatim](https://nominatim.org) server, set under *Map and address search*. The default is OpenStreetMap's public
server, which allows light, identified use (one request a second); note that it then sees the text you search for, and your
home coordinates for the area-word lookup. Point it at your own Nominatim (for example `http://192.168.1.5:8180`) to keep
that on your network, or clear it to turn address search off.

**Map tiles.** The default basemap is OpenStreetMap's standard tile server, loaded by your browser, darkened in
dark mode. Its tile policy allows light personal use with attribution (shown on the map); heavier use or a
public deployment needs a different provider. Change the address under Settings, or leave it empty for the
radar-only map, which needs no internet. Tiles should be served over https; the page's security policy blocks plain-http images, except the one origin you save as the tile address (so a tile server on your home network, such as `http://192.168.1.5:9000/...`, works). Radar intensity colours are BOM's colour classes mapped to approximate
mm/h (see the radar source notes), so treat the numbers as estimates.

## More data sources

Turn these on under **Sources**; each is its own plugin with its own settings.

| Source | What it gives you | Notes |
|---|---|---|
| `bom_observations` | An official BOM weather station (every 30 min, 3 days of history on the first poll), for example Sydney Observatory Hill | Needs the state product and the station's WMO number, both in the station page's URL on bom.gov.au (`/products/IDN60801/IDN60801.94768.shtml` is Sydney Observatory Hill, WMO 94768). **Fill from my location** finds the nearest reporting stations for you. Shown on the map as a reference beside your own station. |
| `bom_warnings` | BOM warnings for your state (NSW/ACT by default), with severe thunderstorm warnings flagged | Nothing is ever dropped. `area_keywords` only marks warnings as yours when their title contains one of the words, and the Now panel and the alert rule favour those. Titles name BOM's forecast districts and vary ("…for Sydney Metropolitan, Hunter and Central Tablelands forecast districts", "…for Sydney and Wollondilly/Wingecarribee", "…for parts of Sydney"), so list several words, and watch the source's "for your area" count; leave it empty to treat every warning as yours. A warning is "in force" while it is in the feed and for 25 minutes after, so withdrawn warnings lapse by themselves, and a re-issue of the same warning refreshes it instead of alerting again. The feed has titles and links only: there are no warning polygons to draw. Use BOM and emergency services for anything safety-critical. |
| `open_meteo` | A weather-model forecast for home: CAPE, lifted index, wind shear, chance of rain, hourly | Free for non-commercial use with attribution (shown where the data appears). Stored as one refreshed record, not as observations. Storm potential is a rule of thumb on peak CAPE; it says the ingredients are there, not that a storm will form. |
| `lightning_feed` | Lightning strikes pushed to `POST /ingest/lightning_feed/<token>` from a feed you have access to | See the note below. Your own Ecowitt lightning sensor needs none of this. |
| `wunderground_pws`, `ecowitt_cloud` | Read your own station back from the cloud | See the station section above. |

**Fill from my location.** The settings for `bom_radar`, `bom_observations` and `bom_warnings` have a **Fill from my location**
button. It works from your home location (Settings → Locations) and offers choices; it never saves or switches anything by itself.

- **Radar:** the three nearest BOM radars that are publishing now, with the range product that covers you (128 km, else 256 km). Positions
  come from BOM's radar pages, mostly to about 1 km (the Sydney/Terrey Hills position is exact), which can shift that radar's image by a pixel or two.
  The nearest radar isn't always the best one (terrain can block the beam), so compare them on the map.
- **Observations:** the nearest open stations, each checked against BOM's live product for its state, so you only see stations that are reporting.
- **Warnings:** the state feed for your location, and the neighbouring state's too if a border is within 30 km. Area words are not guessed, because warning
  titles use forecast-district names and turning a point into a district needs BOM's district boundaries.

BOM's station list (about 2.6 MB) is downloaded when you first press the button and cached in the data directory for 30 days; it isn't part of this repository.
The list of radar sites is generated by `scripts/gen_radar_sites.py`, which you can re-run when BOM adds a radar.

BOM web products are covered by BOM's copyright notice: free for personal use, not for redistribution or commercial use
without a registered subscription. Real BOM downloads used for local testing live in git-ignored `tests/fixtures/*/real/`.

**Lightning.** There is deliberately no built-in client for Blitzortung.org: its operators say third-party apps should not
query its servers directly and tie data access to running a registered station, so connecting automatically would
break its terms. `lightning_feed` accepts strikes from whatever you legitimately have (your own Blitzortung station's
output, a licensed commercial feed, a relay in n8n or Node-RED):

```json
{"strikes": [{"time": "2026-11-06T10:15:02Z", "lat": -33.87, "lon": 151.21, "polarity": -1, "amplitude_ka": -12.5}]}
```

`time` can be ISO 8601 or a Unix timestamp in seconds, milliseconds, microseconds or nanoseconds. Only strikes within
`max_distance_km` of home are kept, and the "lightning" alert rule uses them like any other.

## Storm tracking and nowcast

Every new radar frame is processed in the background:

1. **Cells.** Connected areas of rain at or above the cell threshold (default 10 mm/h, at least 4 km²) are
   found and followed from frame to frame (optimal assignment, so cells side by side keep their own ids). Each
   gets a speed and heading from its recent track and a kinematic arrival estimate for home.
2. **Motion.** pySTEPS' dense Lucas-Kanade optical flow measures how the rain is moving from the last 2 to 4
   consecutive frames (a gap over 7.5 minutes starts a new run).
3. **Forecast.** The newest frame is advected along that motion field (semi-Lagrangian extrapolation) in 5
   minute steps out to 90 minutes. This moves rain around; it does not model storms growing or dying.
4. **Home.** From the forecast frames: whether rain of at least 1 mm/h reaches within 2 km of home, when, how
   heavy it gets and when it eases. The station is cross-checked (is it raining there, pressure change over the
   last hour, gusts, lightning within 10 km in the last 30 minutes).

On the map the loop continues past "Now" into hatched forecast frames. Storm cells show an outline, a 30 minute
motion arrow and a label; the Now panel says what it means for home. All thresholds are under Settings and in
`/api/nowcast/settings`; `GET /api/nowcast` returns the cells, motion, forecast frames and home impact.

**How reliable is it?** Replaying 306 stored frames (about 150 forecasts per lead time), compared with simply
assuming nothing moves (critical success index, 1 is perfect):

| Lead | Rain >= 1 mm/h | | Rain >= 10 mm/h | |
|---|---|---|---|---|
| | forecast | no motion | forecast | no motion |
| 10 min | 0.67 | 0.62 | 0.37 | 0.33 |
| 30 min | 0.48 | 0.43 | 0.17 | 0.15 |
| 60 min | 0.32 | 0.29 | 0.10 | 0.09 |
| 90 min | 0.22 | 0.20 | 0.07 | 0.07 |

It beats persistence consistently but modestly, which is normal for extrapolation: light, widespread rain is
placed well for the first half hour, heavy convective cells much less so. The confidence shown with the forecast
(high, medium or low) is a rough rule built from these numbers plus how consistent the motion is. It is not a
calibrated probability. Radar rain rates are themselves approximate (see the radar source notes). Reproduce the
table on your own data with `docker compose exec stormtracker python -m stormtracker.cli hindcast /data`.
Dense optical flow over 3 to 4 frames beat a single global motion vector, smoothed fields and blends of the two
in the same replay.

**Dependencies and Docker.** The nowcast uses pySTEPS (with SciPy, matplotlib and OpenCV headless). pySTEPS
publishes source only and compiles two C extensions, so the Dockerfile builds wheels in a first stage with a
compiler and installs them into a clean runtime image (plus `libgomp1`, the OpenMP runtime). The first build
takes a minute or two; the image is about 1 GB, roughly 370 MB of it these libraries.

## Alerts

**Alerts** in the UI (or `/api/alerts/...`) holds three things.

**Destinations** deliver a message. Each is a plugin with its own settings; secrets are stored encrypted and are
write-only, like everywhere else. "Send a test" tries one.

| Destination | Notes |
|---|---|
| ntfy | Push notifications to phones/desktops via ntfy.sh or your own server. The topic is the only secret on the public server, so generate a long random one (the Generate button). Urgent alerts use ntfy's highest priority. |
| Apprise | One destination that reaches 100+ services (Telegram, Discord, Slack, Pushover, Matrix, SMS...). Paste one [Apprise URL](https://github.com/caronc/apprise/wiki) per line; they contain tokens, so they are stored encrypted, checked when you save, and never shown again. |
| Webhook | POSTs the alert as JSON. Optional `Authorization` header and an HMAC-SHA256 signature of the exact body in `X-StormTracker-Signature: sha256=<hex>`. The URL is stored encrypted (n8n and Home Assistant webhook ids are secrets). |
| MQTT | Publishes to `<prefix>/alerts/<rule id>` and a retained `<prefix>/last_alert`, QoS 1, with username/password and TLS. Uses a small built-in MQTT 3.1.1 publisher, so no extra dependency (checked against Mosquitto 2). |
| Email | SMTP with STARTTLS, implicit TLS, or plain for a local relay. |

**Rules** say what to watch for: a storm cell reaching home within N minutes, rain starting, raining now,
lightning within N km, a warning whose text matches (works once a warnings source is on), strong gusts now or forecast, a sudden change of wind direction (gust front or southerly change), falling
pressure. Each rule picks its destinations and an urgency, and has:

* **Dedup:** one storm, one alert. A condition is an *episode* (for example one tracked cell) that alerts once however long
  it lasts. It counts as over after it has been gone for 10 minutes (Settings: `clear_after_min`), so a cell flickering
  around a threshold does not alert repeatedly.
* **Cooldown:** the least minutes between alerts from one rule, so two cells arriving together send one message now and
  the second when the cooldown ends, if it still applies.
* **Reminders** (off by default) and **all-clear** messages (off by default).
* Alerts survive a restart: recent ones are read back from the history so an ongoing storm is not announced twice.
* Alerts are never sent from radar older than 25 minutes or station readings older than 15.

**History** shows every alert, all-clear and test, and per destination whether it was delivered or why not. Failures
are recorded, redact secrets, and never stop the other destinations.

Set "Address of this server" so alerts link to the map. Times in messages are Sydney time.

### Writing a new alert destination

Add `src/stormtracker/alerts/outputs/<name>/plugin.py` (it is discovered automatically):

```python
from pydantic import BaseModel
from stormtracker.alerts.base import Alert, AlertOutput, OutputMeta
from stormtracker.plugins import secret_field

class MyConfig(BaseModel):
    token: str | None = secret_field("API token", required=True)      # encrypted, write-only

class MyOutput(AlertOutput):
    meta = OutputMeta(name="my_output", description="Shown in the UI.")
    ConfigModel = MyConfig                                           # the UI form is generated from this

    async def send(self, alert: Alert) -> None:
        r = await self.ctx.http.post("https://api.example.org/notify", json={"text": alert.message},
                                     headers={"Authorization": f"Bearer {self.cfg.token}"})
        r.raise_for_status()                                         # raise on failure; the service records it
```

## Access: roles, agents and single sign-on

Who can do what:

| | Admin | Member | API token |
|---|---|---|---|
| Map, Station, radar, nowcast, forecast, alert history | yes | yes | yes (read) |
| Sources, Settings, alert rules and destinations, tokens, single sign-on setup | yes | no | no |
| Changes anything | yes | no | no (read-only) |

The password account and a trusted-header proxy sign-in are always admin. A member sees only Map, Station and a read-only
Alerts page (the active alerts and recent history, not the rules or where they are sent). The server enforces this on every
route, and a test lists the whole API and fails if a new route forgets its permission check.

### Using StormTracker as a weather source for an AI agent or another app

**Settings → API tokens for agents and apps** makes read-only tokens (`st_<id>_<secret>`). A token is shown once, stored only
as a hash, can be revoked at any time, and can read the weather and nothing else. It is accepted only in an
`Authorization: Bearer ...` header, never in a URL.

* **`GET /api/now`** returns one document: your own station and the nearest BOM stations, rain and storm outlook from the
  radar, lightning, warnings in force, the next hours of model forecast, active alerts and how fresh each source is. Every
  block of values has a `units` map, every reading an age and a `stale` flag, and the document opens with an `authority` note
  that BOM warnings are the official word. `?sections=warnings,lightning` returns just those.
* **MCP server at `/mcp`** (Streamable HTTP, read-only tools): `get_weather_now`, `get_current_conditions`,
  `get_rain_outlook`, `get_warnings`, `get_lightning`, `get_forecast`, `get_observation_history`. They return the same data as
  `/api/now`. It speaks both the current protocol (2026-07-28, stateless, per-request headers) and the 2025 `initialize`
  handshake that most clients still use. Add it to Claude Code with the command the token screen prints:
  `claude mcp add --transport http stormtracker <your address>/mcp --header "Authorization: Bearer <token>"`.
  Claude.ai's web connectors expect OAuth, which this does not provide; use a client that lets you set a header.

BOM's web data is free for personal use but not for passing on. Keep StormTracker on your home network and don't hand tokens
to services outside your home.

### Single sign-on (OpenID Connect)

**Settings → Single sign-on** lets people sign in through authentik, Keycloak, Authelia or any OpenID provider. It uses the
authorisation-code flow with PKCE and checks the ID token's signature, issuer, audience, expiry and nonce. Everything is set
in the UI and stored in the database; the client secret is encrypted and write-only.

1. In your provider make an application for StormTracker (confidential client) and give it the **redirect address** shown in
   the card (your StormTracker address plus `/api/auth/oidc/callback`).
2. Make sure the provider sends a `groups` claim. In authentik it comes with the `profile` scope; otherwise StormTracker also
   asks the provider's user-info address.
3. Enter the issuer, client ID and secret, StormTracker's own https address, and the group names: **admin groups** get the
   admin role, **member groups** get the member role. Someone in neither is refused, never defaulted to member.
4. **Save and test connection** reads the provider's configuration and signing keys. Then switch it on; the sign-in page
   gets a button above the password form.

A person's role is fixed when they sign in and single sign-on sessions last 12 hours, so a group change applies by then.
Password sign-in stays available as the way back in if the provider is down. The issuer must be https (plain http is accepted
only for localhost, for testing).

## Home Assistant (MQTT)

**Settings → Home Assistant (MQTT)** makes StormTracker appear in Home Assistant as one device with 31 entities, with nothing to
install in Home Assistant beyond its MQTT integration (which finds them through MQTT discovery).

* **Sensors:** temperature, humidity, pressure, wind speed, gust and direction, rain rate, rain today, UV, solar radiation (from
  your own station, shown as unknown if it stops reporting); *Rain outlook*, *Rain arrives in*, expected peak rain rate, nearest
  rain on radar; nearest storm cell and when it arrives; lightning strikes in the last hour and the nearest; warnings in force
  and for your area; the forecast's strongest gust, storm fuel (CAPE), storm potential and rain chance; active StormTracker
  alerts; time of last update.
* **Binary sensors:** raining, rain expected, severe thunderstorm warning for your area, lightning within 10 km, StormTracker
  alert active. These are the ones to hang automations on.
* **Broker:** it uses the broker already set on the **MQTT alert destination** (Alerts page), so credentials live in one
  place. That destination doesn't have to be switched on.
* **How it behaves:** one state message every interval (default 60 s), and retained discovery messages sent when you save, on
  "Send now" and hourly. If three updates are missed Home Assistant shows the entities as unavailable. **Remove from Home
  Assistant** clears the device and its entities (switch the integration off too, or they return at the next update). A
  second StormTracker can use a different *Topic name*.
* Alerts themselves still go out through the MQTT destination (`<prefix>/alerts/<rule>` and `<prefix>/last_alert`) if you want
  them as events.

## Backup and restore

**Settings → Backup and restore** saves your setup to one JSON file and puts it back.

* **What's in it:** places, map and address settings, retention, forecast settings, alert rules, settings and destinations,
  every source's settings, Home Assistant and single sign-on settings. **Not** in it: readings, events and radar frames (data,
  not settings), the admin password, API tokens and sign-in sessions. The file contains your home location, so keep it private.
* **Secrets** (API keys, push tokens, webhook addresses, the single sign-on client secret) are left out unless you tick
  *Include secrets* and give a passphrase of 12 or more characters. They're then encrypted with AES-256-GCM under a key derived
  from the passphrase (scrypt), so the file restores on a machine with a different `MASTER_KEY`. There's no way to recover the
  secrets if the passphrase is lost; the settings alone still restore.
* **Restore** checks the whole file first and shows what will be added, changed or removed, with warnings for anything that
  changes who can sign in or where alerts go, and for sources that will still need credentials. Restoring **replaces** your
  configuration with the backup's (anything in the app but not in the backup is removed); it is applied in one database
  transaction, so a bad file changes nothing. Restoring without the passphrase restores the settings and keeps your current
  secrets. Sources are restarted with the restored settings.
* **Undo:** the settings from before the last restore are kept in `<data dir>/backups/` (mode 0600; the secrets in them stay
  encrypted with this installation's key, the three newest are kept) and **Undo last restore** puts them back.
* A backup can switch on single sign-on, change alert destinations and point sources elsewhere, so only restore files you made.

## Security

* **One local admin account** (argon2 password, at least 12 characters), changeable under Settings, which also signs out every other
  session. Sessions are signed cookies (HttpOnly, SameSite=Strict) with a CSRF token on every change; signing out revokes
  the token itself, so a copied cookie stops working. Optional trusted-header sign-in for a proxy such as Authentik
  (`TRUSTED_AUTH_HEADER`, honoured only from `TRUSTED_PROXIES`, always admin). Members and read-only API tokens are described
  above.
* **Secrets** (API keys, tokens, URLs with tokens) are AES-256-GCM encrypted in the database with a key derived from
  `MASTER_KEY`, write-only through the API, and blanked from errors and logs.
* **Throttling:** five failed logins in five minutes block that address; wrong tokens on `/ingest/*` are throttled too, so
  guessing a push token gets nowhere.
* **Headers:** a strict Content-Security-Policy (own scripts only, no inline script), `X-Frame-Options: DENY`, `nosniff`,
  and `no-store` on API responses. (`/docs`, FastAPI's interactive API page, loads its scripts from a CDN and is the one
  page without the CSP.)
* **Behind a reverse proxy:** pass the original `Host` header (writes are refused when `Origin` and `Host` disagree), set
  `TRUSTED_PROXIES` to the proxy's address so throttling sees the real caller from `X-Forwarded-For`, and set
  `COOKIE_SECURE=true` when serving over HTTPS. Plain HTTP on a trusted LAN is what the example `.env` assumes.
* StormTracker is meant for your own network. If you expose it to the internet, put it behind HTTPS and a proxy.

## Database

SQLite (the default, in the `stormtracker-data` volume) needs nothing. For Postgres set
`DATABASE_URL=postgresql+asyncpg://user:pass@host/db` in `.env`; the tables are created on first start. The storage paths
(settings and encrypted secrets, observations, events and their upserts, radar frame index, alerts, sessions, retention) were
checked against Postgres 16, and `TEST_DATABASE_URL=... pytest tests/core/test_postgres.py` repeats that against a throwaway
database. There are no migrations yet: new releases only ever add tables, which are created automatically, but a release that
changes an existing table will need a migration step first.

**Back up** the `/data` volume (SQLite database, radar frames) and your `MASTER_KEY`. Without the key, stored secrets cannot be
decrypted and must be entered again.

## Development

```bash
uv venv --python 3.12 && uv pip install -e '.[dev]' && .venv/bin/pytest
```

## Data sources and terms

| Source | Terms |
|---|---|
| BOM radar, observations, warnings | Free for personal use under BOM's copyright notice; not for redistribution or commercial use without a BOM subscription. Radar comes from BOM's anonymous FTP site, the other two from bom.gov.au. The radar site list (`sites_table.py`) is generated from BOM's public radar pages and holds only site names and positions. |
| Open-Meteo | Free for non-commercial use with attribution. |
| OpenStreetMap tiles and Nominatim | The public servers allow light, attributed use; use your own or a commercial provider for more (see *Map tiles* above). |
| Ecowitt.net, Weather Underground | Your own station's data, under their API terms. |
| Blitzortung | Deliberately not built in; see *Lightning* above. |

## Licence

StormTracker is licensed under the **GNU Affero General Public License v3.0** (see `LICENSE`). In short: you can use and modify
it freely, and if you run a modified version as a network service you must offer its source to the people using it. The bundled
Leaflet (BSD-2-Clause) and Schibsted Grotesk font (SIL OFL) keep their own licences, in `src/stormtracker/web/vendor/`. Data from the
sources above keeps its own terms whatever this licence says.

## Releases

Bump the version in `pyproject.toml` and `src/stormtracker/__init__.py`, merge, then push a tag such as `v0.2.0`. The release
workflow runs the tests, builds amd64 and arm64 images, pushes them to `ghcr.io/<owner>/<repo>` (tags `0.2.0`, `0.2` and `latest`;
pre-releases only get their own tag), attests their provenance, and creates the GitHub release.

## Status

Early release (0.x). The core, all bundled sources, the nowcast, alerts, access control, Home Assistant integration and backups work and
are tested; there are no database migrations yet (new releases add tables, and a release that changes an existing one will say so).
Contributions and bug reports are welcome; see `CONTRIBUTING.md` and `SECURITY.md`.
