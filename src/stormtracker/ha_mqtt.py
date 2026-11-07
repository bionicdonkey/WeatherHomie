"""Home Assistant integration over MQTT discovery.

StormTracker publishes one JSON state document every interval, plus retained "discovery" messages that tell Home Assistant to
create a StormTracker device with sensors (temperature, wind, gusts, rain ETA, nearest storm, lightning, warnings...) and
binary sensors (raining, rain expected, severe warning for your area, lightning nearby, alert active). Nothing to install in
Home Assistant: its MQTT integration picks the entities up.

* The broker (host, port, TLS, login) is the one already set on the MQTT alert destination, so credentials live in one place.
  That destination does not need to be switched on; it only supplies the broker.
* Every key is present in every state message (null when unknown), because Home Assistant turns a missing key into an error and
  a null into "unknown".
* Entities carry ``expire_after`` (three intervals), so Home Assistant shows them unavailable if StormTracker stops, without
  needing a standing connection with a last-will message.
* State is not retained (Home Assistant restores its own); discovery is, so entities survive a Home Assistant restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from . import __version__, summary
from .alerts.outputs.mqtt.client import MqttError, publish
from .settings.sources import SourceSettingsError
from .timeutil import utcnow

log = logging.getLogger("stormtracker.ha")
KEY = "mqtt.ha"
DISCOVERY_EVERY_S = 3600


class NeedsBroker(RuntimeError):
    """The MQTT destination under Alerts has no broker address yet."""


class HaSettings(BaseModel):
    enabled: bool = False
    discovery_prefix: str = Field("homeassistant", pattern=r"^[A-Za-z0-9_-]{1,40}$", description="Home Assistant's MQTT discovery prefix (its default is homeassistant)")
    base_topic: str = Field("stormtracker", pattern=r"^[A-Za-z0-9_-]{1,40}$", description="topic name for this StormTracker (also its device id in Home Assistant); change it only to run two instances")
    interval_s: int = Field(60, ge=15, le=3600, description="seconds between updates")


# key, component, name, unit, device_class, state_class, icon, extra
def _e(key, component, name, unit=None, dc=None, sc=None, icon=None, **extra):
    return {"key": key, "component": component, "name": name, "unit": unit, "dc": dc, "sc": sc, "icon": icon, **extra}


ENTITIES: list[dict[str, Any]] = [
    _e("temp_c", "sensor", "Temperature", "°C", "temperature", "measurement"),
    _e("humidity_pct", "sensor", "Humidity", "%", "humidity", "measurement"),
    _e("pressure_hpa", "sensor", "Pressure", "hPa", "atmospheric_pressure", "measurement"),
    _e("wind_speed_kmh", "sensor", "Wind speed", "km/h", "wind_speed", "measurement"),
    _e("wind_gust_kmh", "sensor", "Wind gust", "km/h", "wind_speed", "measurement"),
    _e("wind_dir_deg", "sensor", "Wind direction", "°", None, None, "mdi:compass-outline"),     # no state class: an average of compass bearings is meaningless
    _e("rain_rate_mmh", "sensor", "Rain rate", "mm/h", "precipitation_intensity", "measurement"),
    _e("rain_today_mm", "sensor", "Rain today", "mm", "precipitation", "total_increasing"),
    _e("uv_index", "sensor", "UV index", "UV index", None, "measurement", "mdi:sun-wireless-outline"),
    _e("solar_wm2", "sensor", "Solar radiation", "W/m²", "irradiance", "measurement"),
    _e("rain_outlook", "sensor", "Rain outlook", None, "enum", None, "mdi:weather-pouring", options=["raining", "approaching", "clear", "unknown"]),
    _e("rain_eta_min", "sensor", "Rain arrives in", "min", "duration", "measurement", "mdi:timer-sand"),
    _e("rain_peak_mmh", "sensor", "Expected peak rain rate", "mm/h", "precipitation_intensity", "measurement"),
    _e("nearest_rain_km", "sensor", "Nearest rain on radar", "km", "distance", "measurement", "mdi:radar"),
    _e("storm_nearest_km", "sensor", "Nearest storm cell", "km", "distance", "measurement", "mdi:weather-lightning-rainy"),
    _e("storm_eta_min", "sensor", "Storm cell arrives in", "min", "duration", "measurement", "mdi:timer-sand"),
    _e("lightning_strikes_1h", "sensor", "Lightning strikes, last hour", "strikes", None, "measurement", "mdi:flash"),
    _e("lightning_nearest_km", "sensor", "Nearest lightning strike", "km", "distance", "measurement", "mdi:flash-outline"),
    _e("warnings_count", "sensor", "Warnings in force", None, None, "measurement", "mdi:alert-outline"),
    _e("warnings_area_count", "sensor", "Warnings for your area", None, None, "measurement", "mdi:alert"),
    _e("forecast_max_gust_kmh", "sensor", "Forecast strongest gust, next 12 h", "km/h", "wind_speed", "measurement"),
    _e("forecast_max_cape_jkg", "sensor", "Forecast storm fuel (CAPE), next 12 h", "J/kg", None, "measurement", "mdi:weather-lightning"),
    _e("forecast_storm_potential", "sensor", "Forecast storm potential", None, "enum", None, "mdi:weather-lightning-rainy", options=["none", "low", "moderate", "high", "unknown"]),
    _e("forecast_max_precip_prob_pct", "sensor", "Forecast rain chance, next 12 h", "%", None, "measurement", "mdi:weather-rainy"),
    _e("alerts_active", "sensor", "Alerts active", None, None, "measurement", "mdi:bell-ring-outline"),
    _e("updated", "sensor", "Last update", None, "timestamp", None, None, category="diagnostic"),
    _e("raining", "binary_sensor", "Raining", None, "moisture", None, "mdi:weather-rainy"),
    _e("rain_expected", "binary_sensor", "Rain expected", None, None, None, "mdi:weather-partly-rainy"),
    _e("severe_warning_area", "binary_sensor", "Severe thunderstorm warning for your area", None, "safety", None, "mdi:weather-lightning-rainy"),
    _e("lightning_nearby", "binary_sensor", "Lightning within 10 km", None, "safety", None, "mdi:flash-alert"),
    _e("alert_active", "binary_sensor", "Alert active", None, "problem", None, "mdi:bell-alert"),
]
BINARY = {e["key"] for e in ENTITIES if e["component"] == "binary_sensor"}


def _r(v: Any, nd: int = 1) -> Any:
    return None if v is None else round(v, nd)


def state_doc(now: dict[str, Any]) -> dict[str, Any]:
    """The flat JSON document entities read from. Every key is always present; unknown values are null."""
    own = next((c for c in now.get("conditions", []) if c.get("own_station") and not c.get("stale")), None)
    v = (own or {}).get("values", {})
    rain, so, lt, w, fc = now.get("rain", {}), now.get("storm_outlook", {}), now.get("lightning", {}), now.get("warnings", {}), now.get("forecast", {})
    status = rain.get("radar_status") if rain.get("radar_status") in ("raining", "approaching", "clear") else "unknown"
    cells = [c.get("relative_to_home") or {} for c in (so.get("tracked_cells") or [])]
    dists = [c["distance_km"] for c in cells if c.get("distance_km") is not None]
    etas = [c["eta_min"] for c in cells if c.get("approaching") and c.get("eta_min") is not None]
    sm = fc.get("summary_next_12h") or {}
    gust_ms = sm.get("max_gust_ms")
    pot = (sm.get("potential") or {}).get("level")
    in_force = w.get("in_force", [])
    station_rain = (v.get("rain_rate_mmh") or 0) >= 0.1
    doc = {
        "temp_c": v.get("temp_c"), "humidity_pct": v.get("humidity_pct"), "pressure_hpa": v.get("pressure_hpa"),
        "wind_speed_kmh": v.get("wind_speed_kmh"), "wind_gust_kmh": v.get("wind_gust_kmh"), "wind_dir_deg": v.get("wind_dir_deg"),
        "rain_rate_mmh": v.get("rain_rate_mmh"), "rain_today_mm": v.get("rain_daily_mm"), "uv_index": v.get("uv_index"), "solar_wm2": v.get("solar_wm2"),
        "rain_outlook": status,
        "rain_eta_min": 0 if status == "raining" else rain.get("eta_lead_min") if status == "approaching" else None,
        "rain_peak_mmh": rain.get("peak_mmh"), "nearest_rain_km": rain.get("nearest_rain_km"),
        "storm_nearest_km": min(dists) if dists else None, "storm_eta_min": _r(min(etas)) if etas else None,
        "lightning_strikes_1h": lt.get("strikes_last_60_min", 0), "lightning_nearest_km": lt.get("nearest_km"),
        "warnings_count": w.get("count", 0), "warnings_area_count": w.get("for_your_area_count", 0),
        "forecast_max_gust_kmh": None if gust_ms is None else round(gust_ms * 3.6), "forecast_max_cape_jkg": sm.get("max_cape_jkg"),
        "forecast_storm_potential": pot if pot in ("none", "low", "moderate", "high") else "unknown",
        "forecast_max_precip_prob_pct": sm.get("max_precip_prob_pct"),
        "alerts_active": len((now.get("alerts") or {}).get("active", [])), "updated": now.get("generated_at"),
        "raining": status == "raining" or station_rain, "rain_expected": status == "approaching",
        "severe_warning_area": any(i.get("severe_thunderstorm") and i.get("for_your_area") for i in in_force),
        "lightning_nearby": (lt.get("within_10km_last_30_min") or 0) > 0,
        "alert_active": bool((now.get("alerts") or {}).get("active")),
    }
    return {k: (("ON" if x else "OFF") if k in BINARY else x) for k, x in doc.items()}


def discovery_messages(cfg: HaSettings) -> list[tuple[str, bytes, bool]]:
    state_topic = f"{cfg.base_topic}/state"
    device = {"identifiers": [f"stormtracker_{cfg.base_topic}"], "name": "StormTracker", "manufacturer": "StormTracker", "model": "Storm and weather tracker", "sw_version": __version__}
    out = []
    for e in ENTITIES:
        c: dict[str, Any] = {"name": e["name"], "unique_id": f"stormtracker_{cfg.base_topic}_{e['key']}", "state_topic": state_topic,
                             "value_template": f"{{{{ value_json.{e['key']} }}}}", "expire_after": cfg.interval_s * 3, "device": device, "has_entity_name": True}
        if e["component"] == "binary_sensor":
            c.update(payload_on="ON", payload_off="OFF")
        else:
            if e["unit"]:
                c["unit_of_measurement"] = e["unit"]
            if e["sc"]:
                c["state_class"] = e["sc"]
            if e.get("options"):
                c["options"] = e["options"]
        if e["dc"]:
            c["device_class"] = e["dc"]
        if e["icon"]:
            c["icon"] = e["icon"]
        if e.get("category"):
            c["entity_category"] = e["category"]
        out.append((f"{cfg.discovery_prefix}/{e['component']}/{cfg.base_topic}/{e['key']}/config", json.dumps(c, separators=(",", ":")).encode(), True))
    return out


class HomeAssistantMqtt:
    def __init__(self, core) -> None:
        self.core = core
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()               # one connection at a time: a second with the same client id makes the broker drop the first
        self._last_discovery = 0.0
        self._announce = True                      # (re)send the discovery messages on the next cycle
        self.status: dict[str, Any] = {"state": "off", "last_publish": None, "error": None, "entities": len(ENTITIES)}

    async def settings(self) -> HaSettings:
        try:
            return HaSettings(**(await self.core.store.get(KEY, {})))
        except ValueError:
            return HaSettings()

    async def save(self, s: HaSettings, actor: str) -> None:
        await self.core.store.set(KEY, s.model_dump(), actor=actor)
        self.poke(announce=True)

    def poke(self, announce: bool = False) -> None:
        """Run a cycle now (settings changed, restored from a backup, or the user asked)."""
        self._announce = self._announce or announce
        self._wake.set()

    async def broker(self) -> tuple[Any | None, str | None]:
        """(the MQTT destination's validated settings, or None and the reason)."""
        try:
            cfg, _ = await self.core.alert_outputs.build("mqtt")
        except SourceSettingsError:
            return None, "the MQTT destination under Alerts isn't set up yet"
        if cfg is None:
            return None, "the MQTT destination under Alerts isn't set up yet"
        return cfg, None

    def _conn(self, b) -> dict[str, Any]:
        return dict(client_id=(b.client_id[:20] + "-ha")[:23], username=b.username or None, password=b.password if b.username else None, tls=b.tls)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="ha-mqtt")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        backoff = 0
        while True:
            wait = 30.0
            try:
                cfg = await self.settings()
                if not cfg.enabled:
                    self.status.update(state="off", error=None)
                    self._announce = True
                else:
                    await self.cycle(cfg)
                    backoff, wait = 0, float(cfg.interval_s)
            except asyncio.CancelledError:
                raise
            except NeedsBroker as e:
                self.status.update(state="needs_broker", error=str(e))
            except Exception as e:  # noqa: BLE001 - publishing must never take the app down; retry with a growing pause
                backoff = min(backoff + 1, 6)
                msg = f"{type(e).__name__}: {e}"[:200]
                if msg != self.status.get("error"):
                    log.warning("Home Assistant MQTT publish failed: %s", msg)
                self.status.update(state="error", error=msg)
                wait = min(15.0 * 2 ** backoff, 600.0)
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except TimeoutError:
                pass
            self._wake.clear()

    async def cycle(self, cfg: HaSettings) -> None:
        async with self._lock:
            await self._cycle(cfg)

    async def _cycle(self, cfg: HaSettings) -> None:
        b, why = await self.broker()
        if b is None:
            self.status.update(state="needs_broker", error=why)
            raise NeedsBroker(why)
        now = await summary.build(self.core)
        msgs: list[tuple[str, bytes, bool]] = []
        if self._announce or time.monotonic() - self._last_discovery > DISCOVERY_EVERY_S:
            msgs += discovery_messages(cfg)
        msgs.append((f"{cfg.base_topic}/state", json.dumps(state_doc(now), separators=(",", ":"), default=str).encode(), False))
        try:
            await publish(b.host, b.port, msgs, **self._conn(b))
        except (MqttError, OSError, EOFError, TimeoutError) as e:
            raise RuntimeError(f"couldn't publish to the broker: {e or type(e).__name__}") from None
        if len(msgs) > 1:
            self._announce, self._last_discovery = False, time.monotonic()
        self.status.update(state="ok", last_publish=utcnow().isoformat(), error=None)

    async def remove(self) -> int:
        """Take the StormTracker device out of Home Assistant: empty retained discovery messages, then an empty state."""
        cfg = await self.settings()
        b, why = await self.broker()
        if b is None:
            raise NeedsBroker(why)
        msgs = [(t, b"", True) for t, _, _ in discovery_messages(cfg)] + [(f"{cfg.base_topic}/state", b"", True)]
        async with self._lock:
            try:
                await publish(b.host, b.port, msgs, **self._conn(b))
            except (MqttError, OSError, EOFError, TimeoutError) as e:
                raise RuntimeError(f"couldn't publish to the broker: {e or type(e).__name__}") from None
        self._announce = True
        return len(msgs) - 1
