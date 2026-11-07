Fixtures for the bom_radar plugin.

* Synthetic frames are generated in `tests/sources/bom_radar/conftest.py` using BOM's exact palette
  (committed as code, so no third-party imagery is stored in git).
* `real/` (gitignored) may hold genuine BOM frames (e.g. `IDR713.T.202611030759.png`) recorded from
  the anonymous FTP site. They are BOM-copyright; tests that use them skip when the folder is empty.
