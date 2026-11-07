"""Common internal data model shared by every source plugin.

Standard observation keys (SI-ish units). Sources may add any extra numeric key
(e.g. ``temp_c_2``, ``soil_moisture_pct_1``); keys must be lower_snake_case.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .timeutil import ensure_utc

STANDARD_KEYS = {
    "temp_c": "air temperature, °C",
    "dewpoint_c": "dew point, °C",
    "humidity_pct": "relative humidity, %",
    "pressure_hpa": "pressure (sea level), hPa",
    "wind_speed_ms": "mean wind speed, m/s",
    "wind_gust_ms": "wind gust, m/s",
    "wind_dir_deg": "wind direction, degrees true",
    "rain_mm": "rain accumulated over the report period, mm",
    "rain_rate_mmh": "instantaneous rain rate, mm/h",
    "rain_daily_mm": "rain since local midnight, mm",
    "solar_wm2": "solar radiation, W/m²",
    "uv_index": "UV index",
    "lightning_count": "lightning strikes counted by a sensor",
    "lightning_distance_km": "distance of last sensor-detected strike, km",
}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

UTCDatetime = Annotated[datetime, Field(description="timezone-aware; normalised to UTC")]


class _UTCModel(BaseModel):
    @field_validator("ts", "expires", "start", "end", check_fields=False, mode="after")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)


class Observation(_UTCModel):
    kind: Literal["observation"] = "observation"
    source: str
    station_id: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    ts: UTCDatetime
    values: dict[str, float]

    @field_validator("values")
    @classmethod
    def _keys(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [k for k in v if not _KEY_RE.match(k)]
        if bad:
            raise ValueError(f"observation keys must be lower_snake_case: {bad}")
        return v


class Georef(BaseModel):
    """Gnomonic grid centred on (lat0, lon0), matching BOM's published radar projection.

    The centre point sits on the pixel with index (width//2, height//2) (verified on BOM range
    rings: radar at column/row 256 of a 512 px image). Pixel (row, col) maps to metres
    east/north of the centre:
        x = (col - width/2) * pixel_size_m ;  y = (height/2 - row) * pixel_size_m
    then to lat/lon by the inverse gnomonic projection.
    """
    projection: Literal["gnomonic"] = "gnomonic"
    lat0: float
    lon0: float
    pixel_size_m: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class RadarFrame(_UTCModel):
    """Rain-rate grid in mm/h (float32). NaN = no data / outside radar range."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: Literal["radar_frame"] = "radar_frame"
    source: str
    radar_id: str
    ts: UTCDatetime
    rain_rate_mmh: np.ndarray
    georef: Georef

    @field_validator("rain_rate_mmh")
    @classmethod
    def _shape(cls, a: np.ndarray, info) -> np.ndarray:
        if a.ndim != 2:
            raise ValueError("rain_rate_mmh must be 2-D")
        return a.astype(np.float32, copy=False)


class Event(_UTCModel):
    kind: Literal["event"] = "event"
    source: str
    event_type: Literal["lightning", "warning", "other"]
    external_id: str = Field(description="stable id for de-duplication within a source")
    ts: UTCDatetime
    lat: float | None = None
    lon: float | None = None
    geometry: dict[str, Any] | None = Field(default=None, description="GeoJSON geometry (e.g. warning polygon)")
    expires: datetime | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


Normalised = Observation | RadarFrame | Event
