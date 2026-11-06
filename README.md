# geoh5-qgis

A QGIS 4.x plugin for browsing `.geoh5` (GEOSCIENCE ANALYST) files and adding their
objects to a QGIS project as layers, with the geoh5 file as the source of truth.

**Status:** early development (experimental).

- `.geoh5` files expand in the Browser panel to show their GEOSCIENCE ANALYST group
  folders and objects.
- To add an object to the project, double-click it, drag it onto the map, or right-click
  it and choose **Add Object to Project**.
- Right-clicking a file or group gives **Add All Supported Objects to Project** or
  **Add All Objects in Group to Project**.
- Supported objects: Points, Curve, Surface, horizontal Grid2D, GeoImage and drillhole
  groups (collars, traces and interval tables).
- Surfaces load as mesh layers with real vertex elevations, coloured by elevation
  ("Bed Elevation"); they work with the mesh renderer, Processing (contours, rasterize),
  the Elevation Profile tool and 3D views. Right-click a surface and choose **Add
  Surface as Polygons** for a vector layer instead: one PolygonZ per triangle with
  `z_min`, `z_max` and `z_mean` fields (for graduated styling) plus the triangle data.

The plugin reads geoh5 with GDAL, which ships with QGIS, so users don't need to install
any extra Python packages.

## Installing from this repo

Requires QGIS 4.x. The plugin is the folder `src\geoh5_qgis`. Pick one option:

### Option 1: point QGIS at the repo (best while developing)

QGIS loads the plugin straight from the repo, so code changes apply after restarting QGIS.

1. In QGIS, open **Settings → Options → System → Environment**, tick **Use custom
   variables** and add:
   - Apply: **Append**
   - Variable: `QGIS_PLUGINPATH`
   - Value: the repo's `src` folder, e.g. `C:\Users\<you>\Dev\geoh5-qgis\src`
2. Restart QGIS.
3. Open **Plugins → Manage and Install Plugins → Installed** and tick **geoh5 Browser**.

To reload after code changes without restarting, install the **Plugin Reloader** plugin.

### Option 2: link the folder into your QGIS profile

1. In QGIS, open **Settings → User Profiles → Open Active Profile Folder**, then go into
   `python\plugins` (create it if it is missing).
2. From a Command Prompt in that `plugins` folder, link it to the repo:

   ```console
   mklink /J geoh5_qgis C:\Users\<you>\Dev\geoh5-qgis\src\geoh5_qgis
   ```

   (Copying the folder also works, but must be repeated after every change.)
3. Restart QGIS and enable **geoh5 Browser** as in Option 1.

### Option 3: install from a ZIP (for sharing with colleagues)

1. Build the ZIP from the repo root (any Python 3.12+, including QGIS's own):

   ```console
   python scripts/build_plugin_zip.py
   ```

   This writes `dist/geoh5_qgis-<version>.zip` with the plugin folder at its top level
   and no Python caches. Use `--out <folder>` to write it elsewhere.
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP** and choose
   the ZIP.

### After enabling

- In the **Browser** panel, go to a folder containing `.geoh5` files and expand them.
- The plugin is marked experimental. If it does not appear in the Plugin Manager, tick
  **Show also experimental plugins** on the manager's **Settings** tab.
- The development environment below (conda env, pytest, geoh5py) is not needed to use
  the plugin.

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
