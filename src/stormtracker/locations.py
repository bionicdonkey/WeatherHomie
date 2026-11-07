from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .settings.store import SettingsStore


class Location(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=100)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "home"


class Locations:
    KEY = "locations"

    def __init__(self, store: SettingsStore):
        self.store = store

    async def list(self) -> list[Location]:
        return [Location(**x) for x in await self.store.get(self.KEY, [])]

    async def replace(self, locs: list[Location], actor: str) -> None:
        if len({l.id for l in locs}) != len(locs):
            raise ValueError("duplicate location id")
        await self.store.set(self.KEY, [l.model_dump() for l in locs], actor=actor)
