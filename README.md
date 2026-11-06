# geoh5-qgis

A QGIS 4.x plugin for browsing `.geoh5` (GEOSCIENCE ANALYST) files and adding their
objects to a QGIS project as in-memory layers.

**Status:** early development. The GDAL-based reader (`src/geoh5_qgis/reader.py`) supports
Points, Curve, Surface and Grid2D objects. The QGIS plugin UI is not built yet.

The plugin reads geoh5 with GDAL, which ships with QGIS, so users don't need to install
any extra Python packages.

## Development

```console
mamba env create -f environment.yml
mamba activate geoh5-qgis
pytest
ruff check --fix . && ruff format .
```

See `docs/research/geoh5-qgis-plugin-research.md` for background research and design
decisions.

## License

`geoh5-qgis` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html)
license.
