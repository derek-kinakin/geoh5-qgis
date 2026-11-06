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
