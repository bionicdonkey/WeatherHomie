"""BOM radar source (single-site rain-rate PNGs).

Discovery: BOM's loop page is now a JS app with no frame list, so frame names come from the
anonymous FTP directory listing (names only). Each PNG is then downloaded over HTTPS (preferred),
falling back to FTP. Overlay layers (background, topography, locations, range, ...) are fetched
once per layer and kept under ``<data_dir>/radar_overlays/<radar_id>/``.

Personal, non-commercial use only; see the licence notes below.
"""
from __future__ import annotations

import asyncio
import io
import re
from datetime import UTC, datetime
from ftplib import FTP
from typing import Any

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from ...alerts.rules import haversine_km
from ...models import RadarFrame
from ...plugins import Candidate, Licence, RateLimit, Source, SourceMeta, Suggestion
from .georef import RADAR_SITES, georef_for, parse_radar_id
from .sites_table import SITES
from .palette import RAIN_CLASSES, rgba_to_rain_rate

FTP_HOST = "ftp.bom.gov.au"
FRAME_DIR = "/anon/gen/radar"
OVERLAY_DIR = "/anon/gen/radar_transparencies"
_FRAME_RE = re.compile(r"^(IDR\d{2,3}[1-4])\.T\.(\d{12})\.png$")
DEFAULT_LAYERS = ["background", "topography", "locations", "range"]


class BomRadarConfig(BaseModel):
    radar_id: str = Field(..., description="BOM radar product id, e.g. IDR713 (Sydney 128 km). Required: choose your nearest radar")
    radar_lat: float | None = Field(None, description="override radar site latitude (needed for sites not built in)")
    radar_lon: float | None = Field(None, description="override radar site longitude")
    max_frames: int = Field(6, ge=1, le=36, description="newest frames to fetch per poll (backfills on start)")
    overlay_layers: list[str] = Field(default_factory=lambda: list(DEFAULT_LAYERS))
    class_rates_mmh: list[float] | None = Field(
        None, description=f"override the {len(RAIN_CLASSES)} per-colour rain rates (mm/h), light to heavy")
    http_base: str = "https://reg.bom.gov.au"
    use_ftp_fallback: bool = True

    @field_validator("radar_id")
    @classmethod
    def _rid(cls, v: str) -> str:
        parse_radar_id(v)
        return v

    @field_validator("overlay_layers")
    @classmethod
    def _layers(cls, v: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", x) for x in v):
            raise ValueError("invalid overlay layer name")
        return v


def parse_frame_name(name: str) -> tuple[str, datetime] | None:
    m = _FRAME_RE.match(name)
    if not m:
        return None
    return m.group(1), datetime.strptime(m.group(2), "%Y%m%d%H%M").replace(tzinfo=UTC)


def decode_frame(png: bytes, class_rates: list[float] | None = None) -> tuple[np.ndarray, dict]:
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    return rgba_to_rain_rate(np.asarray(im), class_rates)


class _Ftp:
    """Tiny sync FTP helper; each call opens a short-lived anonymous session (run in a thread)."""

    @staticmethod
    def list_names(directory: str, prefix: str) -> list[str]:
        f = FTP(FTP_HOST, timeout=30)
        try:
            f.login()
            f.cwd(directory)
            return [n for n in f.nlst() if n.startswith(prefix)]
        finally:
            _quit(f)

    @staticmethod
    def retrieve(directory: str, name: str) -> bytes:
        f = FTP(FTP_HOST, timeout=30)
        try:
            f.login()
            f.cwd(directory)
            buf = io.BytesIO()
            f.retrbinary(f"RETR {name}", buf.write)
            return buf.getvalue()
        finally:
            _quit(f)


def _quit(f: FTP) -> None:
    try:
        f.quit()
    except Exception:
        f.close()


class BomRadar(Source):
    meta = SourceMeta(
        name="bom_radar",
        category="radar",
        description="BOM single-site rain-rate radar (approximate mm/h from the colour palette).",
        poll_interval_s=300,                       # BOM publishes a new frame about every 5 minutes
        rate_limit=RateLimit(max_requests=2, period_s=300),
        licence=Licence(
            type="personal",
            terms_url="https://www.bom.gov.au/other/copyright.shtml",
            attribution="Radar imagery © Commonwealth of Australia, Bureau of Meteorology",
            notes="Anonymous FTP products are covered by BOM's copyright notice "
                  "(reg.bom.gov.au/catalogue/anon-ftp.shtml): you may download, use and copy them for personal "
                  "use or use within your organisation, but may not supply them to any other person or use "
                  "them commercially. Publishing BOM data requires a Registered User subscription. Polling "
                  "is limited to one new frame per ~5 minutes with an identifying User-Agent."),
    )
    ConfigModel = BomRadarConfig
    cfg: BomRadarConfig
    ftp = _Ftp                                     # replaced in tests

    @classmethod
    async def suggest(cls, location, ctx, current):
        """The nearest radars that are publishing, with the range product that covers the location. Closest is not always
        best (terrain, beam blockage, what the nowcast was tuned on), so these are offered, never applied."""
        try:
            names = await asyncio.to_thread(cls.ftp.list_names, FRAME_DIR, "IDR")
            live: set[str] | None = {m.group(1) for n in names if (m := re.match(r"^(IDR\d{2,3}[1-4])\.T\.", n))}
            warn = ""
        except Exception as e:                                     # FTP down: still rank by distance, and say so
            ctx.log.info("radar listing failed during suggest: %s", e)
            live, warn = None, " The list of radars publishing right now couldn't be fetched, so these are by distance only."
        have = {}
        for site, (name, state, lat, lon) in SITES.items():
            la, lo = RADAR_SITES.get(site, (lat, lon))
            have[site] = (name, state, haversine_km(location["lat"], location["lon"], la, lo), site in RADAR_SITES)
        cur_site = None
        try:
            cur_site = parse_radar_id(current["radar_id"])[0] if current.get("radar_id") else None
        except ValueError:
            pass
        out: list[Candidate] = []
        for site, (name, state, km, exact) in sorted(have.items(), key=lambda kv: kv[1][2]):
            if len(out) == 3 or km > 400:
                break
            prefs = ["3", "2", "1"] if km <= 120 else (["2", "1"] if km <= 240 else ["1"])
            rid = next((f"{site}{r}" for r in prefs if live is None or f"{site}{r}" in live), None)
            if rid is None:
                continue                                           # not publishing (or not at a range that covers you)
            rng = {"3": 128, "2": 256, "1": 512, "4": 64}[rid[-1]]
            out.append(Candidate(
                label=f"{name}, {state}", values={"radar_id": rid}, current=site == cur_site,
                detail=f"{km:.0f} km away · {rng} km range" + ("" if exact else " · position known to about 1 km")))
        if not out:
            return Suggestion(candidates=[], note="No BOM radar within 400 km of this location is publishing.")
        note = ("Closest first. Distances can be close, and a nearer radar isn't always the better one: hills and buildings can block "
                "its beam in some directions. Compare the choices on the map before switching." + warn)
        return Suggestion(candidates=out, note=note)

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.georef = georef_for(cfg.radar_id, cfg.radar_lat, cfg.radar_lon)
        self._seen: set[str] = set()
        self._overlays_done: set[str] = set()

    # ---- network ------------------------------------------------------
    async def _download(self, name: str, ftp_dir: str, http_path: str) -> bytes:
        try:
            r = await self.ctx.http.get(f"{self.cfg.http_base}{http_path}{name}")
            r.raise_for_status()
            if not r.content.startswith(b"\x89PNG"):
                raise ValueError("response is not a PNG")
            return r.content
        except Exception as e:
            if not self.cfg.use_ftp_fallback:
                raise
            self.ctx.log.info("HTTPS fetch of %s failed (%s); falling back to FTP", name, e)
            return await asyncio.to_thread(self.ftp.retrieve, ftp_dir, name)

    async def _fetch_overlays(self) -> None:
        if self.ctx.data_dir is None:
            return
        d = self.ctx.data_dir / "radar_overlays" / self.cfg.radar_id
        for layer in self.cfg.overlay_layers:
            if layer in self._overlays_done:
                continue
            path = d / f"{layer}.png"
            if path.exists():
                self._overlays_done.add(layer)
                continue
            try:
                data = await self._download(f"{self.cfg.radar_id}.{layer}.png", OVERLAY_DIR,
                                            "/products/radar_transparencies/")
            except Exception as e:                  # overlays are cosmetic; never fail the poll for them
                self.ctx.log.warning("overlay %s unavailable: %s", layer, e)
                continue
            d.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._overlays_done.add(layer)
            await asyncio.sleep(0.5)

    async def fetch(self) -> dict[str, Any]:
        names = await asyncio.to_thread(self.ftp.list_names, FRAME_DIR, f"{self.cfg.radar_id}.T.")
        frames = sorted(n for n in names if parse_frame_name(n))[-self.cfg.max_frames:]
        new = [n for n in frames if n not in self._seen]
        out: dict[str, bytes] = {}
        for n in new:
            out[n] = await self._download(n, FRAME_DIR, "/radar/")
            await asyncio.sleep(0.5)               # be polite between files
        self._seen &= set(frames)        # keep memory bounded
        await self._fetch_overlays()
        return {"frames": out}

    async def on_saved(self, items: list) -> None:
        """Only frames that were decoded AND stored count as seen, so failures are retried."""
        for f in items:
            self._seen.add(f"{f.radar_id}.T.{f.ts:%Y%m%d%H%M}.png")

    # ---- pure conversion ----------------------------------------------
    def normalise(self, raw: dict[str, Any]) -> list[RadarFrame]:
        items: list[RadarFrame] = []
        for name, png in sorted(raw["frames"].items()):
            parsed = parse_frame_name(name)
            if parsed is None:
                continue
            radar_id, ts = parsed
            arr, stats = decode_frame(png, self.cfg.class_rates_mmh)
            if arr.shape != (self.georef.height, self.georef.width):
                raise ValueError(f"{name}: unexpected image size {arr.shape}")
            if stats["unknown_pixels"] > 0.2 * arr.size:
                raise ValueError(f"{name}: {stats['unknown_pixels']} unrecognised pixels; palette may have changed")
            items.append(RadarFrame(source=self.meta.name, radar_id=radar_id, ts=ts,
                                    rain_rate_mmh=arr, georef=self.georef))
        return items
