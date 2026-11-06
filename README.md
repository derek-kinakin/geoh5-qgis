# geoh5-qgis

A QGIS 4.x plugin for browsing `.geoh5` (GEOSCIENCE ANALYST) files and adding their
objects to a QGIS project as in-memory layers.

**Status:** early development (experimental).

- `.geoh5` files expand in the Browser panel to show their objects.
- To add an object to the project, double-click it, drag it onto the map, or right-click
  it and choose **Add Object to Project**.
- Right-clicking a file gives **Add All Supported Objects to Project**.
- Supported objects: Points, Curve, Surface and horizontal Grid2D.

To try it, point `QGIS_PLUGINPATH` at this repo's `src` folder, start QGIS 4.x and enable
**geoh5 Browser** in the Plugin Manager.

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
