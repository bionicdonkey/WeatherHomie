from __future__ import annotations

from pydantic import BaseModel, Field


class NowcastSettings(BaseModel):
    """Stored in the DB (key ``nowcast``) and edited from the UI/API like every other setting."""
    enabled: bool = True
    cell_threshold_mmh: float = Field(10.0, ge=1.0, le=100.0, description="rain rate a storm cell core must reach")
    min_cell_area_km2: float = Field(4.0, ge=0.25, le=500.0, description="smaller areas above the threshold are ignored")
    horizon_min: int = Field(90, ge=10, le=180, description="how far ahead to forecast")
    step_min: int = Field(5, ge=1, le=15, description="minutes between forecast frames")
    arrival_mmh: float = Field(1.0, ge=0.1, le=20.0, description="rain rate that counts as 'rain at home'")
    home_radius_km: float = Field(2.0, ge=0.5, le=15.0, description="how close to home rain has to be")
