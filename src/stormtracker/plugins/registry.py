from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import Source

log = logging.getLogger(__name__)


class PluginRegistry:
    """Plugins of one kind: data sources by default, or any other base class (e.g. alert outputs)."""

    def __init__(self, base: type = Source, package: str = "stormtracker.sources") -> None:
        self.base, self.package = base, package
        self._sources: dict[str, type] = {}

    def register(self, cls: type) -> None:
        name = cls.meta.name
        if name in self._sources and self._sources[name] is not cls:
            raise ValueError(f"duplicate source plugin name: {name}")
        self._sources[name] = cls

    def discover(self, package: str | None = None) -> None:
        """Import ``<package>.<x>.plugin`` for every sub-package; register subclasses of ``base``.

        A plugin that fails to import is logged and skipped; it never stops others loading.
        """
        package = package or self.package
        pkg = importlib.import_module(package)
        for m in pkgutil.iter_modules(pkg.__path__):
            if not m.ispkg:
                continue
            try:
                mod = importlib.import_module(f"{package}.{m.name}.plugin")
            except Exception:
                log.exception("failed to load plugin %s.%s", package, m.name)
                continue
            for obj in vars(mod).values():
                if (isinstance(obj, type) and issubclass(obj, self.base) and obj is not self.base
                        and not getattr(obj, "__abstractmethods__", None) and hasattr(obj, "meta")):
                    self.register(obj)

    def get(self, name: str) -> type:
        return self._sources[name]

    def all(self) -> dict[str, type]:
        return dict(self._sources)
