# geoh5-qgis

QGIS 4.x plugin for browsing and loading objects from geoh5 (GEOSCIENCE ANALYST) files.
Background research and decisions: `docs/research/geoh5-qgis-plugin-research.md`.

## Key decisions

- Target **QGIS 4.x only** (Qt6/PyQt6). No 3.x compatibility code.
- Initial object types: Points, Curve, Surface, Grid2D.
- Layers are in-memory; geoh5 files stay the source of truth.
- **Runtime reads geoh5 with GDAL's multidimensional API only** (`osgeo.gdal` + numpy,
  both shipped with QGIS). Do not import h5py or geoh5py in `src/`: pip h5py 3.16
  bundles HDF5 2.0 and fails to load inside QGIS (HDF5 1.14.6), and geoh5py's pins
  clash with QGIS's pydantic. geoh5py is a **test-only** dependency for fixtures.
- Classes only where PyQGIS requires them (plugin entry point, Processing provider,
  Browser data items). Reader and layer-building code are plain functions + frozen
  dataclasses.

## Environment and commands

- Dev env: `mamba env create -f environment.yml` → env `geoh5-qgis` (QGIS 4.2.3 from
  conda-forge). Keep the `libabseil=20260526` pin, or `import qgis.core` fails with a
  DLL "procedure could not be found" error.
- Tests: `mamba run -n geoh5-qgis pytest`
- Lint/format: `mamba run -n geoh5-qgis ruff check --fix . && mamba run -n geoh5-qgis ruff format .`
- Run in the real QGIS: set `QGIS_PLUGINPATH=C:\Users\dkinakin\Dev\geoh5-qgis\src` and start
  QGIS 4.2.3 (standalone or the env's `qgis.exe`), then enable "geoh5 Browser" in the
  Plugin Manager. The plugin package is `src/geoh5_qgis` (entry: `classFactory` in
  `__init__.py`, metadata in `metadata.txt`).
- Tests build fixtures with geoh5py in `tmp_path`. Some tests also use the sample files in
  `C:\Users\dkinakin\Dev\geoh5vista\assets` and are skipped if that folder is missing.

## GDAL multidim gotchas (geoh5)

- Compound attributes (e.g. Grid2D `Origin`) come back as raw bytes; decode via the
  datatype's components (`reader._read_compound_attr`).
- String datasets (Metadata JSON, Text data) need `MDArray.Read()`, not `ReadAsArray()`.
- Compounds mixing numbers and strings (`Value map`) must be read per field with
  `GetView('["Key"]')` / `GetView('["Value"]')`.
- Float no-data is `1.17549435e-38` (converted to NaN); integer no-data is `-2147483648`.
- Some files have no `/Objects` group at all (e.g. drillhole-only files).

## QGIS layer gotchas

- Memory vector layers default to EPSG:4326 when the URI has no `crs=`; set an empty
  `QgsCoordinateReferenceSystem()` when the geoh5 object has no CRS.
- Surfaces with vertex data are binary PLY files (written with numpy) opened with the
  `mdal` provider: real vertex Z, MDAL adds a "Bed Elevation" dataset, other PLY
  properties become vertex/face datasets. Do not use `mesh_memory` (x,y only, no Z,
  datasets cannot be saved, whole mesh stored in the project file). PLY property names
  use non-breaking spaces for spaces (`mesh_dataset_name`). Renderer settings getters
  return copies: set them back after editing.
- TIN geometry (`TINZ`) works in memory in 4.2.3 but QGIS cannot save it to GPKG,
  FlatGeobuf, GeoJSON, Shapefile or DXF ("Feature geometry not imported"), so surfaces
  without vertex data use a single MultiPolygonZ feature instead.
- Rotated rasters are drawn through an on-the-fly north-up warp; test raster placement
  with GDAL on the file, not by sampling the QGIS layer.
- GeoImage image files are HDF5 **opaque** datasets that GDAL multidim cannot open.
  `hdf5_blob.read_blob` reads them via ctypes against the HDF5 library GDAL already
  loaded (requires a thread-safe build; QGIS's is). This is the only direct HDF5 use.
- `QgsMeshLayer3DRenderer` is not exposed to Python in 4.2: the mesh 3D renderer is
  built from XML via `QgsApplication.renderer3DRegistry()` (call `Qgs3D.initialize()`
  outside the desktop app). There is no Python offscreen 3D engine for testing.
- Drillhole groups use concatenated storage (see `drillholes.py` docstring). GA files
  store each hole's desurveyed `Trace`; geoh5py-written files do not, so the plugin
  desurveys those with minimum curvature (stations densified every 50 m, as GA/geoh5py
  do). Interval tables are property groups of type "Interval table"; their first two
  properties are FROM and TO.
- Rasters (Grid2D, GeoImage) are files in a per-session temp folder (`tempfiles.py`),
  not /vsimem: GDAL Processing tools run as subprocesses and silently produce nothing
  from /vsimem inputs. Files are deleted on layer removal and plugin unload; folders
  untouched for 7 days are swept at start (another QGIS may be running).
- Saved projects: memory vector layers reopen empty and generated sources (/vsimem,
  temp files) are gone. The plugin rebuilds geoh5 layers on `readProject`
  (`restore.py`, matched by `geoh5/path`, `geoh5/uid`, `geoh5/part`), and saves
  generated sources as `geoh5:<file>|<uid>|<part>` tokens (path writer) that are rebuilt
  by a path preprocessor before QGIS's "unavailable layers" check.
