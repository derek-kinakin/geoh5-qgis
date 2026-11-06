# geoh5-qgis plugin: background research

Researched 2026-11-05. Every claim cites its source: a URL for web material, or a
`path:line` for local code. **[local]** = checked on this machine; **[unverified]** = not
confirmed from a primary source.

- **Part A** covers QGIS plugin development, existing HDF5 readers, dependency handling and
  the dev environment (topics 1–3).
- **Part B** covers what geoh5vista and geoh5py already teach us (topic 4).

## Summary of conclusions

1. **Target both QGIS 3.44 LTR and the 4.x line.** QGIS 4 (Qt6) shipped in March 2026, and
   4.4 becomes the LTR around March 2027. This machine has only QGIS 4.2.3 (Python 3.12,
   GDAL 3.13). Write Qt5/Qt6-agnostic code (`qgis.PyQt` imports, scoped enums, no `.qrc`),
   with `qgisMinimumVersion=3.40` and `qgisMaximumVersion=4.99`.
2. **Read geoh5 with h5py + numpy and keep geoh5py out of the runtime.** geoh5py 0.13.1 pins
   pydantic <2.13, but QGIS 4.2.3 ships 2.13.5; OSGeo4W ships numpy 2.5.4 and h5py 3.16.0,
   which also break its pins. geoh5vista already reads the hierarchy with raw h5py, and that
   code is the starting point. geoh5py stays a test-only dependency for building fixtures.
3. **Getting h5py into QGIS is the main open risk.**
   - The plugin repository forbids bundled binaries, so h5py can't ship inside the plugin.
   - Options are the QPIP plugin with a `requirements.txt`, or a guarded `import h5py` that
     shows install instructions.
   - The pip h5py wheel bundles a DLL with the same name as QGIS's own `hdf5.dll`, so
     `import h5py` needs a smoke test inside the installed QGIS before we rely on it.
4. **There is no existing geoh5, GEOSCIENCE ANALYST, Mira or OMF plugin for QGIS.** QGIS's
   built-in HDF5 support (through GDAL) is raster-only and can't open geoh5 objects.
5. **Proposed layer per object type** (see A §Recommendations and B §3):

   | geoh5 object | QGIS layer |
   |---|---|
   | Points | `PointZ` memory layer |
   | Curve | `MultiLineStringZ` |
   | Surface | `PolygonZ` triangles, or a mesh layer |
   | Grid2D, GeoImage | GDAL raster with a rotated geotransform (flat grids only) |
   | Drillholes | collar `PointZ` plus trace `LineStringZ` |
   | BlockModel | slice rasters, or centroid points |
   | Workspace groups | `QgsLayerTreeGroup` |

6. **CRS.** geoh5vista ignored it, but objects carry it in their `Metadata` JSON. Most sample
   files have none or "Unknown", so the plugin must let the user pick one.
7. **Classes only where QGIS requires them.** The plugin entry point, the Processing
   provider and any Browser panel items must be classes. The reader and the layer builders
   can be pure functions.
8. **Order of delivery:**
   1. An "Open geoh5…" action.
   2. A Processing provider.
   3. A Browser panel provider (`QgsDataItemProvider`).
9. **Dev environment.** A mamba env from conda-forge with qgis, h5py, geoh5py, pytest-qgis
   and debugpy for development and tests. Use Plugin Reloader to iterate, then check end to
   end in the installed QGIS 4.2.3 with `QGIS_PLUGINPATH` pointing at the repo.

## Open questions

- Does a pip-installed h5py import cleanly inside standalone QGIS 4.2.3?
- Does the conda-forge env (qgis 4.2.3 + h5py + geoh5py) solve?
- Should layers persist as memory layers, as GeoPackage, or as an auto-written sidecar?
- How should dipping or vertical Grid2D objects and 3D data be shown in 2D?
- Can drillholes be desurveyed without geoh5py, including the concatenated drillhole format?

---

# Part A — QGIS plugins, HDF5 readers, dependencies, dev environment

Researched 2026-11-05. Every claim has a URL. **[local]** marks something checked on this
machine, and **[unverified]** marks something I could not confirm from a primary source.

> **Context that changes the brief:** QGIS 4 shipped on 2026-03-06 and uses Qt6/PyQt6. The
> current long-term release (LTR) is still **3.44.x** (3.44.15). The latest regular release is
> **4.2.3**, and 4.4.0 was scheduled for 2026-10-30. Per the roadmap, the 4.4 line takes over
> the LTR repository around 2027-03 (4.4.4)
> (https://www.qgis.org/resources/roadmap/). The machine has QGIS **4.2.3** installed, not a
> 3.x version. The PyQGIS cookbook's newest version is still 3.44 and is Qt5-oriented
> (https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/plugins.html).

---

## 1. Writing QGIS plugins

### Structure and metadata
- **Required files:** `metadata.txt` and `__init__.py`, which must define `classFactory()`.
  `LICENSE` is also required to publish, and must be plain text with no file extension.
  Common optional files are `mainPlugin.py`, `.ui` forms and `resources.qrc`
  (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/plugins.html).
- **Required `metadata.txt` fields:** `name`, `qgisMinimumVersion`, `description`, `about`,
  `version`, `author`, `email`, `repository`. The file must be UTF-8.
  - Optional fields include `qgisMaximumVersion`, `tracker`, `homepage`, `icon`, `tags`,
    `category`, `changelog`, `experimental`, `deprecated` and `server`.
  - `hasProcessingProvider` flags a plugin that provides Processing algorithms.
  - `plugin_dependencies` is a pip-like list of *other QGIS plugins*, not PyPI packages.
    It has been available since QGIS 3.8 (same URL).
- **QGIS 4 compatibility:**
  - Set `qgisMaximumVersion=4.99`, or a `qgisMinimumVersion` of 4.0 or higher.
  - `supportsQt6=True` is "no longer recognised".
  - Uploads are checked by the informational *pyqgis4-checker*
    (https://plugins.qgis.org/docs/migrate-qgis4).
- **Plugin search paths:** QGIS looks in the profile's `python/plugins` folder and in the
  install's `python/plugins` folder. `QGIS_PLUGINPATH` adds further folders
  (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/index.html).
  - **[local]** The user profile is `%APPDATA%\QGIS\QGIS4\profiles\default`, which already
    holds a `vernier` plugin.

### Where classes are required
This is the one place we cannot stay functional:
- `classFactory(iface)` must return an object with `initGui()` and `unload()` methods
  (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/plugins.html).
- `initGui()` creates `QAction`s and adds them through `iface.addToolBarIcon` and
  `iface.addPluginToMenu`. `unload()` removes them and disconnects signals (same URL).
- These must also be subclasses: Processing providers and algorithms (`QgsProcessingProvider`,
  `QgsProcessingAlgorithm`), Browser items (`QgsDataItemProvider`, `QgsDataCollectionItem`,
  ...) and any dialogs. Everything else, such as geoh5 reading and building layers, can be
  plain functions.

### Resources, translation and settings
- **Resources:** The cookbook uses `pyrcc5`.
  - PyQt6 removed Qt's resource system: "there is no `pyrcc6`"
    (https://www.riverbankcomputing.com/static/Docs/PyQt6/pyqt5_differences.html).
  - For a plugin that must run on QGIS 3 and 4, load icons from file paths, for example
    `os.path.dirname(__file__)`, instead of `.qrc` files. This is an inference from the two
    sources above.
- **Translation (i18n):**
  - `.ts` files go in `i18n/` and must be named `<plugin>_<lang>.ts`.
  - Generate them with `pylupdate5` and compile them to `.qm` with `lrelease`
    (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/plugins.html).
  - Do not use f-strings inside `self.tr()`, because `pylupdate` cannot extract them
    (https://qgis.github.io/QGIS-Hub-Plugin/development/qt6-migration.html).
- **Settings:**
  - `QgsSettings().setValue("myplugin/key", v)` and `.value(key, default)` store per-user
    settings.
  - `QgsProject.writeEntry` and `readEntry` store per-project settings.
  - `layer.setCustomProperty` stores per-layer settings, which are saved in the project
    (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/settings.html).
  - Per-layer custom properties are useful for recording the source geoh5 path and object UID.
- **Qt6 migration rules:**
  - Always import from `qgis.PyQt`.
  - Use fully scoped enums, such as `Qt.ItemDataRole.UserRole`.
  - `QRegExp` is gone.
  - All three sources: https://qgis.github.io/QGIS-Hub-Plugin/development/qt6-migration.html
  - `exec_()` was removed in PyQt6
    (https://www.riverbankcomputing.com/static/Docs/PyQt6/pyqt5_differences.html).

### Releasing to plugins.qgis.org
- **Account and approval:** You need an OSGeo ID. Staff approve each upload.
  - The zip must contain a single folder whose name is ASCII letters, digits, `_` or `-`.
  - The zip must include `metadata.txt`, `__init__.py` and `LICENSE`.
  - Each upload needs a unique `version`.
  - Source: https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/releasing.html
- **Publishing rules (https://plugins.qgis.org/docs/publish):**
  - The licence must be compatible with GPLv2 or later. The geoh5py licence is LGPL-3.0, so
    depending on it is fine.
  - The package must be under 25 MB.
  - Do not include compiled binaries.
  - The source repository must be public.
  - External dependencies "need to be clearly stated in the About metadata field".
  - If a dependency is not available in OSGeo4W Python, provide Windows installation
    instructions.
- **Binaries:** "Plugins that utilize binaries will not be approved." Uploads are
  security-scanned, and critical findings block approval
  (https://plugins.qgis.org/docs/approval).

### Testing, debugging and IDE (cookbook)
- The cookbook recommends **Plugin Reloader** and **First Aid**.
- On Windows it suggests copying the QGIS `.bat` launcher, which calls `o4w_env.bat`, and
  launching the IDE from that copy.
- It covers debugging with PyDev remote debug, PyCharm Professional remote debug, and `pdb`
  with `pyqtRemoveInputHook()`.
- The page warns that some content "may not be updated for QGIS 3". It does not cover VS Code
  or debugpy.
- Source: https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/ide_debugging.html

### Adding data to the map
- **Memory vector layers:** use the URI pattern
  `QgsVectorLayer("PointZ?crs=EPSG:xxxx&field=name:string(20)&index=yes", "name", "memory")`.
  - The documented types are `point`, `linestring`, `polygon` and their `multi*` forms, and
    the `crs` value can be anything `createFromString` accepts
    (https://qgis.org/pyqgis/master/core/QgsVectorLayer.html).
  - **Z and M types:** The provider parses the geometry string with
    `QgsWkbTypes::parseType`, so `PointZ`, `LineStringZ` and so on are accepted. It also sets
    `elevationProperties()->setContainsElevationData(hasZ)`.
  - **Field types:** The provider accepts `integer`, `int8`, `double`, `string`, `date`,
    `datetime`, `boolean`, `binary`, `stringlist`, `integerlist` and `doublelist`, plus a
    `[]` list suffix. Unknown types fall back to string.
  - Source for the last two points:
    https://raw.githubusercontent.com/qgis/QGIS/master/src/core/providers/memory/qgsmemoryprovider.cpp
  - The cookbook adds fields with `dataProvider().addAttributes([QgsField(name,
    QMetaType.Type.X)])` and then calls `updateFields()` and `addFeatures()`. Call
    `updateExtents()` after adding features to a memory layer
    (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/vector.html).
- **Memory layers are not saved** with the project.
  - `QgsVectorFileWriter.writeAsVectorFormatV3(layer, path.gpkg, transformContext, options)`
    writes a layer to a GeoPackage, which is the default format (same cookbook URL).
  - Writing to a sidecar or temp `.gpkg` and loading it with the `ogr` provider makes layers
    persist. This is a design trade-off, not a rule.
- **Raster layers:** `QgsRasterLayer(path, name)` loads through GDAL
  (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/loadlayer.html).
  - A geoh5 Grid2D can be written to a GeoTIFF or a GDAL MEM/VRT dataset, then loaded.
  - The GDAL HDF5 driver cannot georeference geoh5 grids directly; see section 2.
- **Mesh layers:**
  - The `mdal` provider reads any MDAL format.
  - The `mesh_memory` provider builds a mesh from a text URI: a vertex list, a `---`
    separator, then faces or edges. A mesh has faces *or* edges, not both. Datasets can be
    added at runtime.
  - 3D stacked meshes (`DataOnVolumes`) are supported.
  - Sources: https://qgis.org/pyqgis/master/core/QgsMeshLayer.html and
    https://raw.githubusercontent.com/qgis/QGIS/master/src/core/providers/meshmemory/qgsmeshmemorydataprovider.h
  - **[unverified]** The provider's doc comment describes vertices as "x and y" only, so
    whether `mesh_memory` keeps a vertex Z for geoh5 Surfaces needs a test. The fallback is
    writing a temporary `.2dm` file.
- **Point clouds:** `QgsPointCloudLayer(uri, name, provider)`. Its point-cloud providers are
  `pdal`, `ept`, `copc` and `vpc`, and there is **no memory point-cloud provider**. The
  provider folders are listed at https://api.github.com/repos/qgis/QGIS/contents/src/core/providers
  and https://api.github.com/repos/qgis/QGIS/contents/src/providers; the class is at
  https://qgis.org/pyqgis/master/core/QgsPointCloudLayer.html.
  - Large geoh5 Points objects would need writing to LAS/LAZ or COPC first, for example with
    PDAL, which QGIS bundles (**[local]** `pdal.exe` is in `bin`).
  - Smaller objects can go in a `PointZ` vector layer.
- **Tiled scenes:** `QgsTiledSceneLayer` (QGIS 3.34+) displays tiled 3D scene objects
  (https://qgis.org/pyqgis/master/core/QgsTiledSceneLayer.html). It is not relevant to geoh5.
- **Layer tree groups:** These can mirror the geoh5 workspace's group hierarchy.
  - Get the root with `root = QgsProject.instance().layerTreeRoot()` and create a group with
    `g = root.addGroup("name")`. Groups nest the same way.
  - Add the layer with `QgsProject.instance().addMapLayer(layer, False)` so it does not appear
    at the root, then call `g.addLayer(layer)`.
  - `QgsLayerTreeGroup("x")` with `insertChildNode` or `addChildNode` places a group at a
    specific position.
  - Source: https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/cheat_sheet.html
- **3D display:**
  - Vector layers with Z can be drawn in the 3D map view once "Enable 3D Renderer" is
    switched on.
  - Mesh layers and raster DEMs can serve as terrain, and point clouds render in 3D
    (https://docs.qgis.org/3.44/en/docs/user_manual/map_views/3d_map_view.html).
  - QGIS remains map- and 2.5D-oriented. Mira's own marketing positions Geoscience ANALYST as
    the 3D complement to QGIS
    (https://www.mirageoscience.com/revolutionizing-subsurface-interpretation-and-3d-visualization/).
    This is a secondary source and should be treated as context only.

### Processing provider (alternative or complement to the GUI plugin)
- Set `hasProcessingProvider=yes`.
- In `initProcessing()`, which `initGui` calls, run
  `QgsApplication.processingRegistry().addProvider(provider)`, and remove the provider in
  `unload`.
- The provider subclasses `QgsProcessingProvider` and implements `loadAlgorithms`, `id` and
  `name`. Each algorithm subclasses `QgsProcessingAlgorithm` and declares its parameters in
  `initAlgorithm`.
- Outputs go through `parameterAsSink`.
- Source: https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/processing.html
- An algorithm such as "geoh5 object → GeoPackage" gives batch mode, model-builder use and
  `qgis_process` command-line use for free. The cookbook does not cover
  `QgsProcessingParameterFile` with an extension filter.

### Browser panel: can .geoh5 be browsable? Yes, from Python
- `QgsDataItemProvider` is subclassable from Python and has three required methods:
  - `capabilities()` must return `File` or `Dir` for directory children.
  - `createDataItem(path, parentItem)` is called for each file in a directory.
  - `name()` returns the provider's name.
  - `handlesDirectoryPath` is also available to override.
  - Source: https://qgis.org/pyqgis/master/core/QgsDataItemProvider.html
- Register the provider with `QgsApplication.dataItemProviderRegistry().addProvider(p)`. The
  registry takes ownership, and `removeProvider` deletes the provider
  (https://qgis.org/pyqgis/master/core/QgsDataItemProviderRegistry.html).
  - QGIS's own Python tests subclass the provider with `capabilities()` returning
    `QgsDataProvider.DataCapability.File`
    (https://raw.githubusercontent.com/qgis/QGIS/master/tests/src/python/test_qgsdataitemproviderregistry.py).
- **Real plugin example:** The MapTiler plugin subclasses `QgsDataItemProvider` and
  `QgsDataCollectionItem`, implements `createChildren()` and `actions()`, and uses
  `sip.transferto` to hand ownership to C++
  (https://github.com/maptiler/qgis-maptiler-plugin/blob/master/browser_root_collection.py).
- **Design implication:** for `foo.geoh5`, return a `QgsDataCollectionItem` whose children
  mirror the workspace groups and objects.
  - A standard `QgsLayerItem` needs a provider key and URI that QGIS can open itself.
  - There is no geoh5 data provider, so child items would have to load data through custom
    actions or double-click handlers that call our own converter.
  - **[unverified]** I found no example of overriding `handleDoubleClick` or
    `QgsDataItemGuiProvider` in Python.
- **Pure-Python vector data providers** (subclassing `QgsVectorDataProvider`) exist in QGIS's
  test suite
  (https://raw.githubusercontent.com/qgis/QGIS/master/tests/src/python/provider_python.py).
  This is a heavier route to a native `geoh5` provider key. Registration was not shown in
  that file.

---

## 2. Existing QGIS plugins and readers for HDF5

| Tool | What it does | How it reads HDF5 | Dependencies | Licence | Last update / QGIS range |
|---|---|---|---|---|---|
| **GDAL HDF5 driver** (in QGIS) | Raster-only access to 2D/3D arrays as subdatasets `HDF5:"file":/path`, plus the multidimensional API. It georeferences only HDF-EOS5 and some swath products, and does not expose arbitrary groups, compound data or vectors (https://gdal.org/en/stable/drivers/raster/hdf5.html). | GDAL | Bundled. **[local]** QGIS 4.2.3 ships GDAL 3.13.3 with the HDF5, HDF5Image, netCDF, BAG, S102 and HDF4 drivers. | MIT (GDAL) | n/a |
| **MDAL / QGIS mesh layer** | Core mesh provider since QGIS 3.4. It replaced Crayfish's C++ library (https://github.com/lutraconsulting/qgis-crayfish-plugin). Its HDF-based drivers are HEC-RAS and FLO-2D; other drivers include XMDF, XDMF and UGRID/NetCDF (https://www.mdal.xyz/drivers/index.html). | MDAL (C++) | Bundled | n/a | n/a |
| **Crayfish** | Plots and animations for mesh layers (https://plugins.qgis.org/plugins/crayfish/) | Uses QGIS mesh/MDAL | Not stated | Not stated on the listing (the repo has a LICENSE.txt) | 4.0.0, 2025-11-14; QGIS 3.18–4.99 |
| **HDF-EOS Extractor** | Explores hyperspectral HDF-EOS5 cubes (https://plugins.qgis.org/plugins/hdfeos_extractor/) | GDAL is required; h5py is used "if present" | "only numpy and GDAL, both bundled with QGIS". h5py is optional. | Not stated | 2.21, 2026-09-23; QGIS 3.16–3.99 |
| **NISAR Processor** | Reads NISAR L2 GCOV HDF5 products (https://plugins.qgis.org/plugins/NISAR_Processor/) | Requires GDAL 3.13 or newer; the reading method is not stated | Not stated | Not stated | 1.0.0, 2026-09-17; QGIS 3.20–4.99 |
| **Radar RS120 Tools** | Weather-radar HDF5 to GeoTIFF (https://plugins.qgis.org/plugins/RadaresUTPL/) | "Procesamiento con GDAL (no requiere h5py)" | SciPy and PyKrige are optional | Not stated | 1.3, 2025-06-26 |
| **CAMS Europe AQ Data Manager** | Air-quality reanalysis (https://plugins.qgis.org/plugins/cams_eaq_data_manager/) | Uses h5py, netCDF4 and others | Tells users to `pip install ... h5py ...` into QGIS Python themselves | Not stated | 0.3, 2025-07-18 (experimental); QGIS 3.0–3.99 |

- **No geoh5, GEOSCIENCE ANALYST or OMF plugin exists.**
  - Searches of plugins.qgis.org returned no results for "geoh5"
    (https://plugins.qgis.org/search/?q=geoh5) or "omf"
    (https://plugins.qgis.org/search/?q=omf).
  - The Mira Geoscience GitHub organisation has no QGIS repository
    (https://github.com/MiraGeoscience).
  - `geoh5-interop` (MIT) groups geoh5py, mira-omf and las-geoh5, with no QGIS component
    (https://github.com/MiraGeoscience/geoh5-interop).
  - OMF's Python libraries have no QGIS plugin (https://github.com/gmggroup/omf-python).
- **Takeaway:** Plugins that avoid the dependency problem stick to GDAL. The h5py-based ones
  either make h5py optional or ask users to pip-install it.

---

## 3. Dependency handling (critical)

### What QGIS ships
- **[local] QGIS 4.2.3 standalone installer** (`C:\Program Files\QGIS 4.2.3`) ships:
  - Python **3.12.15**, PyQt6 6.11 and GDAL 3.13.3
  - numpy **2.4.6**, pandas 3.0.6, scipy 1.18.1, **pydantic 2.13.5**, Pillow 12.3.0,
    shapely, pyproj and geopandas
  - **No h5py, no psutil, no debugpy**
- **[local] HDF5 DLLs:** `bin\hdf5.dll` (HDF5 **1.14.6**), `hdf5_hl.dll`, and a PDAL HDF
  reader plugin.
- **OSGeo4W package index**
  (https://download.osgeo.org/osgeo4w/v2/x86_64/setup.ini, fetched 2026-11-05):
  - `qgis` 4.2.3-1 and `qgis-ltr` 3.44.15-1
  - `python3-core` 3.12.16 and `python3-numpy` 2.5.4
  - **`python3-h5py` 3.16.0-1 exists.** It requires `python3-numpy` and OSGeo4W's own
    `hdf5` 1.14.6.
  - `python3-pydantic` 2.14.1, `python3-debugpy` and `python3-pytest`
  - OSGeo4W users can therefore get h5py through the OSGeo4W installer. Standalone `.msi`
    users do not have it.
- **conda-forge:**
  - `qgis` 4.2.3 for win-64 is built for py312, py313 and py314, against Qt 6.11 and libgdal
    3.13. 3.44.x LTR builds are also available. Licence: GPL-2.0-only
    (https://api.anaconda.org/package/conda-forge/qgis).
  - `h5py` 3.16.0 (https://api.anaconda.org/package/conda-forge/h5py)
  - `geoh5py` 0.13.0 (https://api.anaconda.org/package/conda-forge/geoh5py)

### geoh5py's pins clash with QGIS
- geoh5py 0.13.1, released 2026-08-21, is pure Python (`py3-none-any`) and requires
  Python >=3.12,<3.15 (https://pypi.org/pypi/geoh5py/json). Its pins:
  - `h5py>=3.15,<3.16`
  - `numpy>=2.4,<2.5`
  - `pydantic>=2.12,<2.13`
  - `Pillow>=12.2,<13`
  - `psutil>=7.2.2,<7.3`
- Conflicts:
  - **pydantic:** The QGIS standalone build has 2.13.5 and OSGeo4W has 2.14.1. Both are
    **outside** geoh5py's `<2.13` pin.
  - **numpy:** OSGeo4W's numpy 2.5.4 is outside the pin.
  - **h5py:** OSGeo4W's h5py 3.16.0 is outside the pin.
- A strict pip install of geoh5py into QGIS Python would therefore **downgrade
  QGIS-provided packages** or fail. This is a strong argument for reading geoh5 with
  **h5py directly**, using our own small reader, rather than depending on geoh5py at runtime.
  The pins come from PyPI metadata; whether geoh5py actually *breaks* on newer pydantic is
  **[unverified]**.

### h5py on Windows inside QGIS: the DLL issue
- **Old conflict:** h5py issue #828 (2017) documented `DLL load failed` when a pip h5py wheel
  ran in QGIS's process. The explanation given was "an expected conflict of the HDF5 DLLs
  shipped with QGIS/OSGeo and h5py" (https://github.com/h5py/h5py/issues/828).
- **[local] The current wheel still bundles same-named DLLs.** I downloaded
  `h5py-3.15.1-cp312-cp312-win_amd64.whl` and only listed it, without installing. It bundles
  `h5py/hdf5.dll`, `hdf5_hl.dll` and `zlib.dll` with **no name mangling**. The bundled HDF5
  is **1.14.6**, the same version as QGIS's `bin\hdf5.dll`.
- **What that probably means:** Windows will likely reuse the already-loaded QGIS `hdf5.dll`.
  Because the versions match, it will probably work, but this is **[unverified]**. A smoke
  test inside QGIS's Python console is required, and it may break whenever QGIS or h5py
  change HDF5 versions.
- **OSGeo4W `python3-h5py`** is built against the same `hdf5` package, so it avoids the
  mismatch by construction. That follows from the package's `requires:` line in setup.ini.

### Ways to get packages into QGIS
1. **`plugin_dependencies` in metadata.txt.** This lists only *QGIS plugins*, not PyPI
   packages
   (https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/plugins.html).
2. **QPIP** (opengisch, GPL-3.0; plugin `a00_qpip` 1.7.0, 2026-09-25, QGIS 3.18–4.99):
   - The plugin sets `plugin_dependencies=qpip` and ships a `requirements.txt`.
   - QPIP pip-installs into `<profile>/python/dependencies` with `--prefix`, adds that folder
     to `sys.path`, and pins QGIS-provided packages with a constraints file. If no solution
     exists under those pins, it retries without them.
   - Conflicts between plugins are not solved.
   - Sources: https://github.com/opengisch/qpip and https://plugins.qgis.org/plugins/a00_qpip/
   - The plugins.qgis.org listing still says it is "in early development".
3. **Vendoring into the plugin zip.**
   - `qgis-plugin-dev-tools` (NLS Finland, GPL-3.0, 0.13.2) copies `runtime_requires` into
     the zip and rewrites imports. It warns that this may fail with native libraries and
     advises pure-Python dependencies only
     (https://pypi.org/project/qgis-plugin-dev-tools/).
   - h5py has compiled extensions, so vendoring it also breaks the official repository's
     no-binaries rule (https://plugins.qgis.org/docs/approval,
     https://plugins.qgis.org/docs/publish).
4. **A first-run pip install** into the profile or user site-packages, as the iamap and
   Deepness plugins do (https://pypi.org/project/qgis-plugin-dev-tools/0.10.0/ search
   result). These are third-party examples; I did not deep-verify them.
5. **Documenting manual install steps**, which the repository policy requires when a
   dependency is not in OSGeo4W (https://plugins.qgis.org/docs/publish).
   - OSGeo4W users install `python3-h5py` with the OSGeo4W setup.
   - Standalone users run `pip install h5py` from the OSGeo4W Shell. For example, the
     EO Time Series Viewer docs give this instruction for its own dependencies
     (https://scm.cms.hu-berlin.de/jakimowb/eo-time-series-viewer/-/blame/7877d60b4953aff46332802ad213a1f732b036b9/doc/source/installation.rst).
   - Conda users run `mamba install h5py` next to `qgis`.

---

## 4. Development environment on Windows

### What is installed on this machine [local]
- **QGIS:** `C:\Program Files\QGIS 4.2.3` is a full install with Python 3.12.15, GDAL
  3.13.3 and Qt6.
  - `QGIS 4.0.0`, `4.2.0` and `4.2.2` are leftover `bin/etc/var` folders, not working
    installs.
  - There is no `C:\OSGeo4W*` and no QGIS 3.x LTR.
- **Launchers:** `bin\python-qgis.bat` calls `o4w_env.bat` and `qt6_env.bat`, then sets the
  following and runs `python`:
  - `QGIS_PREFIX_PATH`
  - `PYTHONPATH=%OSGEO4W_ROOT%\apps\qgis\python`
  - `QT_PLUGIN_PATH`
  - `PATH += apps\qgis\bin`
  - Use this launcher, not `python -I`, because `-I` ignores `PYTHONHOME`. I hit a
    "No module named encodings" error that way.
- **Type stubs:** QGIS ships `.pyi` stubs (`_core.pyi`, `_gui.pyi`, `_analysis.pyi`,
  `_3d_p.pyi`) in `apps\qgis\python\qgis`.
- **Conda environments:** none contain QGIS.
  - The `geoh5vista` and `mirageo` envs (the latter at
    `C:\ProgramData\MiraGeosciencePy\...`) both have Python 3.12.13, geoh5py 0.13.1,
    h5py 3.15.1, numpy 2.4.6 and pydantic 2.12.5.
  - `geoh5vista` also has stray numpy-2.5.3 `dist-info` metadata.
  - These envs are handy for reference reads and test fixtures.

### Tools
- **Plugin Reloader:** 0.24, 2026-09-08, QGIS 3.0–4.99
  (https://plugins.qgis.org/plugins/plugin_reloader/).
- **Plugin Builder:**
  - Plugin Builder 3 is in maintenance mode for QGIS 3 only
    (https://plugins.qgis.org/plugins/pluginbuilder3/).
  - Plugin Builder 4 (v3.4.13, 2026-10-20) is listed for QGIS 4.0–4.99
    (https://plugins.qgis.org/plugins/pluginbuilder4/).
  - Templates lean OOP and pb_tool, so hand-writing a minimal skeleton is probably simpler.
- **pb_tool:** 3.5.4, GPL-2.0+ (https://pypi.org/pypi/pb_tool/json).
- **qgis-plugin-ci:** 2.11.0, GPLv3. It packages with `git archive` and releases to
  plugins.qgis.org and GitHub. It also fills the changelog from `CHANGELOG.md`, generates
  `plugins.xml`, and handles Transifex translations. Configure it under `[qgis-plugin-ci]`
  in `pyproject.toml` (https://github.com/qgis/qgis-plugin-ci). It fits a hatch project well.
- **qgis-plugin-dev-tools:** `qpdt start` launches QGIS with the plugin and an optional
  debugger, and `qpdt build` makes the zip. Configure it under
  `[tool.qgis_plugin_dev_tools]` in `pyproject.toml`
  (https://pypi.org/project/qgis-plugin-dev-tools/).
- **Debugging with VS Code and debugpy:**
  - The **QGIS DevTools** plugin (NextGIS, 1.3.4, 2026-08-31, QGIS 3.32–4.99) starts a
    debugpy server for VS Code to attach to
    (https://plugins.qgis.org/plugins/devtools/, https://docs.nextgis.com/docs_ngqgis/source/devtools.html).
  - It needs debugpy installed in QGIS Python; debugpy is 1.8.22, MIT
    (https://pypi.org/pypi/debugpy/json). OSGeo4W also has `python3-debugpy`.
  - The older `debug_vs` plugin is deprecated (https://plugins.qgis.org/plugins/debug_vs/).
- **Standalone PyQGIS scripts:**
  - Call `QgsApplication.setPrefixPath(...)`, then `qgs = QgsApplication([], False)` and
    `qgs.initQgis()`, and finish with `qgs.exitQgis()`.
  - On Windows, set `PYTHONPATH` to `<qgis>\python` and add `bin` and `apps\<release>\bin`
    to `PATH`.
  - Source: https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/intro.html
- **pytest-qgis:** 5.0.2, 2026-10-22, GPL-2.0+. It provides the `qgis_app`, `qgis_iface`
  (a stub), `qgis_canvas`, `qgis_new_project` and `qgis_bot` fixtures, and requires
  QGIS 3.34 or later (https://github.com/osgeosuomi/pytest-qgis).
  - QGIS 4 support is not stated in the README. The release date suggests it is current.
- **Type stubs:**
  - QGIS's own `.pyi` files are the primary stubs **[local]**.
  - `qgis-stubs` on PyPI is a third-party package (0.5.0, GPL-3.0;
    https://github.com/leonhard-s/qgis-stubs). Its target QGIS version is **[unverified]**.
- **CI images:** `qgis/qgis:4.0` uses Debian Trixie and Python 3.13 with PEP 668, so pip
  needs `--break-system-packages`. Local virtualenvs need `--system-site-packages` because
  PyQGIS is not on PyPI
  (https://qgis.github.io/QGIS-Hub-Plugin/development/qt6-migration.html).

### Conda-forge as a development environment
`mamba create -n geoh5-qgis -c conda-forge qgis=4.2 h5py geoh5py pytest pytest-qgis
debugpy ruff` should give one environment that can launch QGIS (the `qgis` command) and run
pytest with real PyQGIS. All of these are on conda-forge or PyPI (see the URLs above).
- **Caveats:**
  - geoh5py's numpy and pydantic pins will constrain the solve. This is **[unverified]**
    because I did not run the solver.
  - Conda-forge QGIS is not what end users run, so smoke-test the plugin in the standalone
    4.2.3 install as well. The bundled numpy and pydantic versions differ (see section 3).

---

## Recommendations and open questions

1. **Target versions:**
   - Write Qt5/Qt6-agnostic code (`qgis.PyQt`, scoped enums, no `.qrc`).
   - Set `qgisMinimumVersion=3.40` (or 3.44) and `qgisMaximumVersion=4.99`, so one codebase
     covers 3.44 LTR and the 4.x line. 4.4 becomes the LTR around March 2027.
   - Develop and test primarily on 4.2.3, which is what is installed here.
2. **Dependencies:**
   - **Read geoh5 with h5py and numpy directly** in a small functional reader module. Do not
     depend on geoh5py at runtime: its tight pins conflict with QGIS's pydantic and with
     OSGeo4W's numpy and h5py. geoh5py can still be a **test-time** dependency for writing
     fixture files and cross-checking.
   - Do **not** vendor h5py: it violates the no-binaries rule.
   - Use the QPIP route (`plugin_dependencies=qpip` plus `requirements.txt: h5py`), or a
     guarded `import h5py` that shows clear install instructions:
     - OSGeo4W users: `python3-h5py`
     - standalone users: `pip install h5py` from the OSGeo4W Shell
     - conda users: `mamba install h5py`
   - **Open question:** smoke-test `import h5py` from a pip wheel inside the standalone
     QGIS 4.2.3 process, given the same-named `hdf5.dll`.
3. **Layer type per geoh5 object** (proposed; verify against geoh5py's object list):
   - **Points:** `PointZ` memory layer. Above a size threshold, convert to COPC/LAZ with
     PDAL and load as `QgsPointCloudLayer`.
   - **Curve:** `LineStringZ` or `MultiLineStringZ`, segmented by parts.
   - **Surface (triangles):** a `mesh_memory` or temporary `.2dm` mesh layer with vertex
     data as datasets. The fallback is `PolygonZ` triangles.
   - **Grid2D:** a GDAL raster (GeoTIFF or VRT) with a rotation-aware geotransform.
   - **Drillholes:** `LineStringZ` traces plus a `PointZ` collar layer and interval
     attribute tables.
   - **BlockModel / Octree:** no good native layer. Use 2D slices to raster, or
     cell-centroid `PointZ`.
   - Data channels become attribute fields. Workspace groups map to `QgsLayerTreeGroup`s.
   - Store the geoh5 path and UID in the layer's custom properties.
4. **Persistence:**
   - Default to memory layers for quick viewing.
   - Offer "save to GeoPackage", or a Processing algorithm that writes GPKG, so projects
     reopen.
   - **Open question:** should the plugin auto-write a sidecar `.gpkg`?
5. **Delivery:**
   - Start with a GUI action ("Open geoh5…") plus a Processing provider.
   - A Browser `QgsDataItemProvider` that makes `.geoh5` files expandable is feasible in
     Python, but loading needs custom actions because there is no native provider key. It
     is a later phase.
6. **Dev setup:**
   - Run a conda-forge env (qgis + h5py + geoh5py + pytest-qgis + debugpy) for development
     and tests. Use hatch only for packaging metadata and lint/format.
   - Use qgis-plugin-ci, or a small zip script, for releases.
   - Iterate with Plugin Reloader and debug with DevTools/debugpy.
   - Test end to end in the installed QGIS 4.2.3, launched with `QGIS_PLUGINPATH` pointing
     at the repository.

---

# Part B — Reusing geoh5vista and geoh5py knowledge

Repo: `C:\Users\dkinakin\Dev\geoh5vista` (v0.13.0). In this note, `gv/` means
`geoh5vista/src/geoh5vista/` and `gpy/` means
`C:\Users\dkinakin\AppData\Local\miniforge3\envs\geoh5vista\Lib\site-packages\geoh5py\`.

---

## 1. Geoh5 format knowledge in geoh5vista

### HDF5 layout (confirmed against sample files with h5py)
- **One top-level project group.** It is normally `/GEOSCIENCE`. The code requires exactly one (`gv/io/_hdf5.py:8-15`).
- **Project attributes:**
  - `Version`: 2.1 in the samples. It is parsed as a float and checked to be finite and >0 (`gv/io/hierarchy.py:455-457`).
  - `Distance unit`: seen as both `"metres"` and `"meter"`.
  - `GA Version`, e.g. `4.7.0-rc.2…`.
  - `Contributors`.
- **Project children:** `Data`, `Groups`, `Objects`, `Root`, `Types/{Data types, Group types, Object types}` and `Visualisation`.
- **Flat indexes:** `Groups/{uid}`, `Objects/{uid}` and `Data/{uid}`. Each entity group has these attributes:
  - `ID` (a braced UUID string), `Name`, `Visible` (int8).
  - For geometry objects, the geometry parameters are also attributes. For Grid2D these are `Origin` (compound x/y/z), `U/V Count`, `U/V Size`, `Rotation`, `Dip` and `Vertical`.
- **Entity datasets and subgroups:**
  - `Vertices` is a compound `(x,y,z) <f8` dataset.
  - `Cells` is `uint32`.
  - There is also a `Type` subgroup, a `Data` subgroup, `Metadata` (a JSON text dataset) and `PropertyGroups`.
- **Hierarchy:** child links nest as `Groups/` and `Objects/` inside the parent.
- **`/PROJECT/Root`:**
  - The spec treats `/Root` as an *optional* hard-link alias of the indexed root (`README.md:315-320`).
  - Some files store distinct, divergent roots. geoh5vista treats the indexed root as authoritative. Unioning the two would resurrect entities that were deleted in GA (`README.md:321-328`; `CHANGELOG.md:70-80`; `gv/io/hierarchy.py:443-517`).
  - Rootless files are handled as a forest (`gv/io/hierarchy.py:367-440`; `CHANGELOG.md:33-39`).
- **Hierarchy-only entities:** reachable entities may be missing from the flat index (`README.md:336-341`; `CHANGELOG.md:82-88`).
- **Type identity comes from the `Type` subgroup's ID.** It is resolved by type UUID through geoh5py's `TYPE_UID_TO_CLASS`, not by name. When `Version > 1.0`, drillhole groups become `Concatenator*` and holes become `ConcatenatedDrillhole` (`gv/io/hierarchy.py:64-76`). Unknown type UIDs are kept but marked unsupported (`README.md:308-309`, `352-360`).
- **Concatenated (drillhole) storage:**
  - Layout:
    - `Concatenated object IDs`
    - `Concatenated Data/{Attributes | Attributes Jsons}`
    - `Concatenated Data/Index/<field>` (compound `Object ID`, `Data ID`, `Start index`, `Size`)
    - payloads stored under `Concatenated Data/` or `Concatenated Data/Data/`
  - geoh5 2.0 uses the `Attributes` label and 2.1 uses `Attributes Jsons` (`gv/io/hierarchy.py:231-259`; `README.md:410-413`).
  - Names in concatenated storage use U+2044 FRACTION SLASH in place of `/` (`gv/io/selective.py:231,255`; `gv/io/hierarchy.py:95`).
  - Some writers leave `Index` entries without a payload array, which makes geoh5py 0.13 crash. geoh5vista monkey-patches `H5Reader.fetch_concatenated_values` at import time to work around this (`gv/compat.py:31-59`, called at `gv/__init__.py:19`). The selective reader does the same thing natively (`gv/io/selective.py:183-193`).
  - The Drillhole object-type record can be missing from `Types/Object types`. In that case the registered type is used and a warning is issued (`gv/io/hierarchy.py:262-284`; `CHANGELOG.md:75-80`).
- **Data channels:** each channel is a `Data/{uid}` group with:
  - attributes `Association` (`Vertex`/`Cell`/`Object`) and `Name`;
  - a `Data` dataset and a `Type` subgroup;
  - data-type attributes `Primitive type` (Float/Integer/Referenced/Text/Boolean/…), `Mapping`, `Number of bins`, `Scale`, `Transparent no data` and `Units`.

### Display info
- **Object colour** is the `<Colour>` integer inside the "Visual Parameters" XML text data channel. It is stored as a little-endian packed int, so the bytes come out B,G,R.
  - The correct RGB decode is `gv/io/hierarchy.py:111-121`.
  - `gv/utilities.py:112` notes that "Colour order was BGR before geoh5py 0.12.1".
  - **Gotcha:** `geoh5-mapmaker/src/geoh5_mapmaker/mapmaker_vector.py:61-73` (`RGB_from_GA`) returns bytes 0..2, which is actually BGR. Do not copy it.
- **Visibility** is the entity's own stored `Visible` attribute. A missing value means visible. Per-view `Visible` datasets in `Visualisation/` must be ignored because geoh5py overwrites `.visible` from them (`gv/visibility.py:20-38,41-56`; `README.md:233-248`; `CHANGELOG.md:113-126`).
- **Colour maps** are stored on the data type as dataset `Types/Data types/{uid}/Color map`, with compound `(Value f8, Red, Green, Blue, Alpha u1)` and attribute `File name`. This was found in `assets/block_model/bm_reference_values_test.geoh5`. **geoh5vista does not read colour maps.**
- **Value maps** (referenced data) are stored as `Types/Data types/{uid}/Value map`, compound `(Key u4, Value str)`. geoh5vista uses `data.value_map.map_values()` to add a `<name>_names` string array (`gv/converters/arrays.py:74-78,182-184`; `gv/converters/drillholes.py:270-280`). Key 0 is "Unknown" by convention (`gv/converters/arrays.py:227-239`).

### No-data handling
- geoh5py sentinels are `FLOAT_NDV = 1.17549435e-38` and `INTEGER_NDV = -2147483648` (`gpy/shared/__init__.py:28-29`).
- geoh5py converts float NDV to NaN, and so does the raw reader (`gv/io/selective.py:125-126,227-228`).
- The drillhole path builds `__valid` arrays from `isfinite` (floats) or `!= data.nan_value` (ints/text). Booleans are always valid (`gv/converters/drillholes.py:245-253`; `README.md:494-499`).
- The generic converter silently **skips BooleanData** and anything that is not Float, Integer or Referenced, such as Text (`gv/converters/arrays.py:64-82`). It also skips the names listed in `DATASKIP` (`gv/constants.py:42-52`).

### Object types and geometry
- **Registry** (`gv/converters/registry.py:6-18`): Points, Curve, Surface, Grid2D, BlockModel, Drillhole, DrillholeGroup, ConcatenatorDrillholeGroup, ConcatenatedDrillhole, Slicer, Label.
- **GeoImage, Octree, DrapeModel** and the survey types are explicitly unsupported (`gv/constants.py:13-40`). The archive stubs are empty (`archive/geoh5vista_old/geoimage.py:15-16`, `octree.py:35-39`).

| Type | Geometry rule in geoh5vista |
|---|---|
| Points | `vertices (n,3)` (`gv/converters/points.py:33-50`) |
| Curve | `cells (m,2)` are segments. Polylines/parts are recovered as connected components, giving the `Line Index` array (`gv/converters/curve.py:34-58`). |
| Surface | `vertices (n,3)` plus `cells (m,3)` triangles. Validation covers range, finiteness and non-degeneracy, because GA writes placeholder/degenerate surfaces (`gv/converters/surface.py:35-73`). |
| Grid2D | Values are **cell-centred** in U-fastest order. The origin is the first *corner*. The rotation matrix is `xy_rotation(rotation) @ yz_rotation(dip)`, `Vertical=1` means dip=90°, and cell sizes may be negative (sign is kept in the direction matrix). Centres match `grid.centroids` (`gv/converters/grid2d.py:35-86`; `README.md:160-169`; `CHANGELOG.md:42-53`; tests `tests/test_grid2d.py:20-70`). |
| BlockModel | Uses `origin` plus `u/v/z_cell_delimiters`, with horizontal rotation only. Delimiters can be descending (negative cells). Values are stored as `reshape((nv,nu,nz),'C').transpose(1,0,2)`, so the stored order is z-fastest, then u, then v (`gv/converters/arrays.py:170-176`; `gv/converters/blockmodel.py:57-128,177-229`). |
| Drillhole | The collar plus surveys `(depth, azimuth, dip)` are desurveyed by `hole.desurvey(depths)`, which is geoh5py's minimum-curvature method. The sampling densifier uses each interval's radius, bounding chord error to `curve_tolerance` with a step of at most 5 units (`gv/converters/drillholes.py:170-199`). Edge cases: an end of hole of 0 or None means "unknown", so the extent is inferred; with no stored survey, geoh5py synthesises a vertical one and a warning is issued (`gv/converters/drillholes.py:393-421,454-459`; `README.md:469-487`). Modern holes use `Depth table`/`Interval table` property groups; legacy holes use `from_/to_/depths` (`gv/converters/drillholes.py:87-155`). Rows with from ≥ to are dropped with a warning (`gv/converters/drillholes.py:292-308`). |
| Label | Label and target positions are read as raw attributes named `"Label position"` and `"Target position"`, because geoh5py does not model them (`gv/converters/label.py:36-64`). The label text is the `<Text>` element in the Visual Parameters XML (`gv/converters/label.py:67-103`). |
| Slicer | Position and orientation are parsed from the `<Position>{…}` and `<Orientation>{…}` text in the Visual Parameters XML (`gv/converters/slicer.py:162-202`). |

### Units, CRS and precision
- **Units:** only `workspace.distance_unit` is carried, and only into drillhole metadata (`gv/converters/drillholes.py:436`; `gv/io/selective.py:332`). Per-data `Units` attributes are ignored.
- **CRS: geoh5vista ignores it entirely**, although the file does carry it.
  - Object `Metadata` JSON holds `{"Coordinate Reference System": {"Current": {"Code": "EPSG:26914", "Name": "NAD83 / UTM zone 14N"}}}`. This was found in `assets/surface/surface_b.geoh5`.
  - Most other assets have no CRS. The `grid2d_examples.geoh5` Metadata is just `{}`.
  - geoh5py exposes it as `entity.coordinate_reference_system`, defaulting to `{"Code":"Unknown","Name":"Unknown"}` (`gpy/shared/entity.py:40,196-227`).
- **Precision:** coordinates are UTM/mine-grid scale. float32 loses about 0.5 m at a northing of 5.5 Mm, so keep everything float64 (`README.md:607-615`; `CHANGELOG.md:60-67`).
- **Text:** VTK rejects non-ASCII text, so geoh5vista raises an error (`README.md:503-505`). QGIS has no such limit.

---

## 2. geoh5py or raw h5py? Dependencies and lessons

**geoh5vista uses both.**
- **Full reads go through geoh5py `Workspace`:** `with Workspace(path, mode="r")`, then `fetch_children(recursively=True)` (`gv/io/readers.py:79-80`). A read-only default was only added in 0.11.0 (`CHANGELOG.md:40`).
- **Metadata (catalog/hierarchy) uses raw h5py plus a few `H5Reader` static helpers:** `fetch_type_attributes`, `fetch_type`, `fetch_concatenated_attributes`, plus `TYPE_UID_TO_CLASS`. The reasons are given in `gv/io/hierarchy.py:1-8` and `README.md:122-130`:
  - **"opening a geoh5py Workspace eagerly loads geometry arrays"**;
  - `H5Reader.fetch_children` misses hierarchy-only entities.
- **The selective reader** (`gv/io/selective.py:282-433`) opens h5py read-only and reads only the selected objects and payload slices. It rebuilds them in an in-memory geoh5py `Workspace(version=…, distance_unit=…)` so that the existing converters can be reused. The README calls this a prototype with overhead (`README.md:368-374,424-436`).
- **One global monkey-patch of geoh5py is required** for real-world drillhole files (`gv/compat.py`).

**geoh5vista declared dependencies:** `geoh5py>=0.13.1`, `h5py>=3.15`, `numpy>=2.4`, `pyvista>=0.49.2`, Python ≥3.12 (`pyproject.toml:6,26-31`; `README.md:17-19`).

**geoh5py 0.13.1 actual metadata** (`…/envs/geoh5vista/Lib/site-packages/geoh5py-0.13.1.dist-info/METADATA:14,28-32`):
`Requires-Python >=3.12,<3.15`; `Pillow >=12.2,<13`; `h5py >=3.15,<3.16`; `numpy >=2.4,<2.5`; `psutil >=7.2.2,<7.3`; `pydantic >=2.12,<2.13`. The pins are very tight. Other local envs have 0.13.1, and `geoh5-fault-blocks` has 0.13.0.

**Python bundled in local QGIS 4.2.3** (`C:\Program Files\QGIS 4.2.3\apps\Python312\Lib\site-packages`):
- Python 3.12.15, numpy 2.4.6 and Pillow 12.3.0. These satisfy geoh5py.
- **pydantic 2.13.5**, which *violates* geoh5py's `<2.13` pin.
- **No h5py and no psutil.**
- Also present: GDAL 3.13.3 with the **HDF5/HDF5Image drivers**, `hdf5.dll` in `bin\`, pyproj 3.8, shapely 2.1.2, scipy 1.18.1.

**Implications:**
- **Depending on geoh5py inside QGIS** means pip-installing h5py and psutil into QGIS Python and overriding pydantic. pip will refuse that unless `--no-deps`, or pydantic must be downgraded, which may break other plugins. The h5py wheel also ships its own HDF5 DLL next to QGIS's `hdf5.dll`, a possible DLL clash that is unverified.
- **h5py alone** needs a single extra dependency (h5py 3.15 wheels for cp312 / numpy 2.x). Everything geoh5vista proved about the layout (section 1) is enough to read Points, Curve, Surface, Grid2D, BlockModel, Label, CRS, colours and value maps with plain h5py plus numpy.
- **The hard parts without geoh5py:**
  - desurveying (geoh5py `Drillhole.desurvey`, minimum curvature);
  - concatenated drillhole storage;
  - the type-UID→class table, which can be copied as a constant dict.
- **A zero-dependency option worth prototyping:** GDAL's HDF5 driver and multidimensional API (`gdal.OpenEx(..., gdal.OF_MULTIDIM_RASTER)`) can read groups, attributes and datasets with no pip install, though compound/vlen-string handling would need testing.

**Lessons:**
- Always open read-only (`CHANGELOG.md:40`). GA may hold the file open, and nothing in the repo addresses HDF5 file locking or SWMR, so this is untested.
- GA-version differences: geoh5 2.0 vs 2.1 concatenation labels, split/absent `/Root`, missing type records, `Index` entries without data, and the BGR/RGB colour change around geoh5py 0.12.1.

---

## 3. Mapping geoh5 objects to QGIS layers

| geoh5 | Proposed QGIS layer | What translates from geoh5vista |
|---|---|---|
| Points | Memory/GPKG **PointZ** vector. One feature per vertex; VERTEX data becomes attributes. | `vertices` read (`gv/converters/points.py:47`); `add_data_to_vtk` field loop becomes attribute columns (`gv/converters/arrays.py:43-86`) |
| Curve | **MultiLineStringZ** (or LineStringZ per part). CELL data stays per segment; options are per-segment features or aggregating per part. | Segment cells plus connected-component "Line Index" (`gv/converters/curve.py:34-58`). In QGIS, chaining segments into polylines must be done in numpy, or with `shapely.line_merge`. |
| Surface | 2D map: **PolygonZ** per triangle (TIN-like vector), outline/footprint polygons, or a **QgsMeshLayer** (QGIS's native TIN; supports vertex/face datasets, Z, and 3D view). The mesh layer needs a 2DM/UGRID file or a memory mesh provider. | `_validate_surface_geometry` (`gv/converters/surface.py:35-55`). Triangle-coordinate helper `generate_triangles` (`geoh5-mapmaker/.../mapmaker_vector.py:42-46`). |
| Grid2D (horizontal, dip=0) | **Raster** via a GDAL MEM/GeoTIFF dataset, one band per channel. Use a rotated geotransform `(x0, du·cosθ, -dv·sinθ, y0, du·sinθ, dv·cosθ)`. Rows are V; flip if dv>0 so north-up conventions hold. NoData=NaN. | Rotation/sign/origin rules and the U-fastest cell order (`gv/converters/grid2d.py:58-84`; `README.md:160-169`). Port the matrix maths (geoh5py `xy_rotation_matrix`/`yz_rotation_matrix`). |
| Grid2D (dipping/vertical section) | Cannot be a north-up raster. Options: PolygonZ cells (true 3D footprints) or skip with a message. | Same geometry function |
| GeoImage | **Raster** via GDAL. geoh5py stores the image (PIL) plus 4 corner vertices and TIFF tags 33550/33922 (`gpy/objects/geo_image.py:265-341,521-560`). | Nothing in geoh5vista (stub only) |
| BlockModel | 2D options: a level-slice raster per z-index (when unrotated or rotated-uniform), or PolygonZ/PointZ centroids with attributes. QGIS has no voxel layer. | Index/reshape order (`gv/converters/arrays.py:170-176`), rotation and delimiter rules (`gv/converters/blockmodel.py:57-229`) |
| Drillhole / group | Collars as **PointZ**; traces as **LineStringZ** (desurveyed, plan view); intervals as **LineStringZ** per interval with attributes (from/to/values); depth samples as **PointZ**. | Composite collar/trace/intervals/points design and validity arrays (`gv/converters/drillholes.py:286-479`; `README.md:454-461`). Densification `_sample_depths` (`:170-192`). This needs a desurvey; see section 5. |
| Label | Point layer with a `text` attribute plus label/target leader **LineString** | `gv/converters/label.py:36-129` |
| Slicer | Probably skip, or show as a line/plane trace | `gv/converters/slicer.py:162-202` |
| Octree, DrapeModel, survey types | Out of scope initially | Unsupported in geoh5vista as well |
| Groups/hierarchy | QGIS **layer tree groups** that mirror the GA tree. Visibility is the stored checkbox; colour is used as the default single-symbol colour. | `read_workspace_hierarchy` / `WorkspaceHierarchy` (`gv/io/hierarchy.py:520-543`; `gv/models.py:36-75`). This is directly usable for a browser/dock tree and reads no geometry. |

**Styling:**
- **Referenced data** maps to a `QgsCategorizedSymbolRenderer` built from the value map (with key 0 as Unknown).
- **Numeric data** maps to a graduated renderer, or a pseudocolour raster renderer, built from the type's `Color map` (Value→RGBA).
- **Default colour** comes from Visual Parameters `<Colour>`.

---

## 4. Reusable code, fixtures, and conventions

### Port or adapt
Strip PyVista from these; keep the numpy logic.
- `gv/io/_hdf5.py:1-41`: tiny h5py helpers (`project_group`, `text`, `uid`, `group`, `dataset`). Copy as-is.
- `gv/visibility.py:20-38` (`stored_visibility`): copy as-is.
- `gv/io/hierarchy.py:111-134`: colour decode (`_colour`, `_object_colour`). Copy as-is.
- `gv/io/hierarchy.py`, the rest: root/rootless/hierarchy-only discovery. This is the most battle-tested piece. Its only geoh5py ties are `H5Reader.fetch_type_attributes`/`fetch_type`/`fetch_concatenated_attributes` and `TYPE_UID_TO_CLASS`, which can be replaced by reading the `Type` attributes directly plus a copied UID→name table.
- `gv/models.py:17-192`: frozen, slotted dataclasses `EntityMetadata`, `HierarchyEntity` and `WorkspaceHierarchy`, with `children`, `descendants`, `select` and `effectively_visible`. These are directly usable for a QGIS tree model.
- `gv/converters/grid2d.py:35-86`: Grid2D frame maths. Convert it to a GDAL geotransform plus a "dip ≠ 0 means unsupported as raster" check.
- `gv/converters/blockmodel.py:57-229` and `gv/converters/arrays.py:170-176`: block model ordering and rotation.
- `gv/converters/surface.py:35-55`: surface validation.
- `gv/converters/label.py:40-103`: label attributes and text.
- `gv/converters/drillholes.py:49-155,170-192,224-284`: sampling-table normalisation, densification, and validity/value-map provenance. This depends on geoh5py `Drillhole` objects, so it is reusable only if geoh5py is a dependency.
- `gv/compat.py:31-59`: needed only if geoh5py is used.

**Depend on geoh5vista directly?** Not recommended inside QGIS:
- it hard-depends on pyvista/VTK (`pyproject.toml:30`);
- its metadata modules still import geoh5py (`gv/io/hierarchy.py:22-26`), even though `README.md:67-70` says they don't import VTK.

### Test fixtures (`geoh5vista/assets/`)

| Path | Size | Contents |
|---|---|---|
| `points/pts_test_a.geoh5` | 109 KB | |
| `points/pts_test_b.geoh5` | 834 KB | |
| `surface/surface_a.geoh5` | 765 KB | |
| `surface/surface_b.geoh5` | 1.8 MB | **has CRS EPSG:26914 in Metadata** |
| `surface/open_pit.geoh5` | 9.4 MB | |
| `grid2d/grid2d_examples.geoh5` | 367 KB | 8 grids, incl. dip −35 |
| `grid2d/grid2d_test_a.geoh5` | 462 KB | |
| `block_model/bm_reference_values_test.geoh5` | 183 KB | **has Color map and Value map** |
| `block_model/rotated_bm_test.geoh5` | 235 KB | |
| `block_model/test_bm.geoh5` | 6.1 MB | |
| `drillholes/dh_test_a.geoh5` | 1.9 MB | |
| `drillholes/dh_test_b.geoh5` | 361 KB | |
| `slicer/test_slicer.geoh5` | 886 KB | |
| `workspace/workspace_test_file.geoh5` | 13.4 MB | GA 4.3.0; Surfaces, Curve, Points, Block Model; has `LICENSE.md` |
| `workspace/workspace_complex.geoh5` | 7.8 MB | |
| `workspace/round_trip_test_file.geoh5` | 5.8 MB | |
| `other formats/EM072_clipped.geoh5` | 197 KB | non-GA writer, no `Visualisation` group, `GA Version "1"` |

- The tests themselves mostly **generate fixtures with geoh5py in `tmp_path`** (e.g. `tests/test_points.py:30-41`; `tests/test_grid2d.py:20-70`). `conftest.py`'s `sample_data_path` (`tests/conftest.py:5-9`) is essentially unused.
- For a plugin that avoids depending on geoh5py at runtime, geoh5py can stay a **test-only** dependency for generating fixtures and serving as the oracle (e.g. `grid.centroids`).

### Conventions
- **Build and layout:** hatchling build, src layout, `[tool.hatch.envs.default]`/`test` with `test = "pytest"` and `test-cov` scripts (`pyproject.toml:42-78`). Pytest uses `pythonpath=["src"]`, `-ra -q --strict-markers` and `testpaths=["tests"]` (`pyproject.toml:75-79`).
- **Tests:** flat `tests/test_<module>.py` files with section-comment banners (`tests/README.md`; `tests/test_points.py:16-55`).
- **Ruff:** there is no `[tool.ruff]` section, so ruff defaults apply (`.ruff_cache` shows ruff 0.15–0.17). An `[tool.isort]` line length of 100 is left over (`pyproject.toml:100-104`).
- **Style:**
  - Mostly functional: module-level functions, `__all__`, `Final` constants, frozen dataclasses for records (`gv/models.py:17-75`) and a module registry dict for dispatch (`gv/converters/registry.py:6-18`).
  - Docstrings are **NumPy-style**, which conflicts with the user's CLAUDE.md (Google style). The new project should use Google style.
- **Other:** a CHANGELOG with dated versions, README tables of supported entities, and a conda env per project (`.vscode/settings.json`). Mamba envs are named after the project.

---

## 5. Gaps a 2D GIS plugin must fill

1. **CRS.** Read `Metadata` → `Coordinate Reference System.Current.Code` (e.g. `"EPSG:26914"`) per object and pass it to `QgsCoordinateReferenceSystem`. It is often `"Unknown"` or absent, so fall back to the project CRS or ask the user, and keep a per-file override. Mine grids have no EPSG code; the user's `prj_to_wkt` (`Dev/prj_to_wkt/prj_to_wkt.py:7-11`) hints at local-grid WKT/.prj workflows. Nothing reprojects; geoh5 is a local Cartesian frame.
2. **Distance units.** The project `Distance unit` (`"metres"`/`"meter"`/`"feet"`?) needs mapping to QGIS units. Per-data `Units` should go into field comments/aliases.
3. **2D projection of 3D data.**
   - QGIS stores Z, but rendering is plan view.
   - Dipping or vertical Grid2D, block models, steep surfaces and drillholes need explicit choices: plan footprint, level slice, section view, or skip.
   - There are no section, profile or level-plan tools in geoh5vista. `geoh5-mapmaker` only shows a matplotlib plan render of surfaces.
4. **Attribute tables.**
   - geoh5 associations (VERTEX, CELL, OBJECT) must become feature attributes. Data on vertices versus on segments/triangles determines feature granularity.
   - OBJECT data should become layer metadata or a single-row attribute.
   - Field-name sanitisation and dedup are needed for GPKG/shape limits; geoh5vista dedups with `" [uid]"` (`gv/converters/drillholes.py:158-167`).
   - Text and Boolean data must not be dropped, unlike `gv/converters/arrays.py:72,82`.
   - NDV becomes NULL, not NaN.
5. **Colour maps and styling.** Data-type `Color map` and `Mapping`/`Scale`/`Number of bins` attributes are unread anywhere. Building QGIS renderers from them is new work.
6. **GeoImage and Octree** are unimplemented. For GeoImage, geoh5py's TIFF-tag georeferencing (`gpy/objects/geo_image.py:265-341`) is the reference.
7. **Drillholes without geoh5py.** A minimum-curvature desurvey must be ported, and concatenated-storage slicing (`gv/io/selective.py:130-279` shows the index/slice mechanics). `geoh5-desurvey-drillholes` is a 30-line stub that calls geoh5py, so there is nothing to reuse there.
8. **File locking and refresh.** GA keeps files open. The behaviour of h5py read-only open with HDF5 file locking on Windows (`HDF5_USE_FILE_LOCKING`) is untested in geoh5vista. A plugin also needs reload-on-change.
9. **Large files and performance.** Use lazy per-object loading, which the catalog→select pattern already enables (`README.md:260-313`), plus background `QgsTask` loading. Nothing bounds memory today (`README.md:433-435`).
10. **Writing back** (QGIS → geoh5) is out of scope for v1. geoh5vista's write path is VTK-centric.

---

# Decisions and smoke-test results (2026-11-05)

## Scope decisions

- Target **QGIS 4.x only**. This supersedes the "3.44 LTR + 4.x" recommendation above.
- Initial object types: **Points, Curves, Surfaces, Grid2D**.
- Layers are **in-memory**; users save them to disk themselves. geoh5 files stay the source
  of truth.
- Goal UX: browse `.geoh5` files in the **Browser panel**, expand them to see objects, and
  add selected objects to the project.
- **Runtime reader: GDAL's multidimensional API** (`osgeo.gdal`), not h5py or geoh5py.
  geoh5py stays a test/fixture-only dependency.

## Smoke test on installed QGIS 4.2.3 [local]

Environment: QGIS 4.2.3 standalone, Python 3.12, GDAL 3.13.3, bundled HDF5 1.14.6
(version read from `hdf5.dll` via `H5get_libversion`).

**QGIS loads `hdf5.dll` early.** `GetModuleHandleW("hdf5.dll")` is already non-null right
after `QgsApplication.initQgis()`. A plugin's `import h5py` therefore always happens after
QGIS's HDF5 has loaded.

**The pip h5py wheels behave differently depending on their bundled HDF5:**

| pip wheel | Bundled HDF5 | Import after QGIS |
|---|---|---|
| h5py 3.16.0 | 2.0.0 | **Fails**: `ImportError: DLL load failed while importing _errors: The specified procedure could not be found.` |
| h5py 3.15.1 | 1.14.6 | Works |
| h5py 3.14.0 | 1.14.6 | Not run; same bundled HDF5 as 3.15.1 |

Importing h5py 3.16.0 *before* QGIS works, but a plugin can't control that order. This makes
h5py fragile: the wheel must match QGIS's HDF5 version exactly.

**GDAL multidim reads geoh5 with no extra install.** It was tested on `pts_test_a.geoh5`,
`surface_a.geoh5` and `surface_b.geoh5`:

- `gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER)` opens the file. Navigate with
  `GetRootGroup().OpenGroup("GEOSCIENCE").OpenGroup("Objects")`.
- Group attributes such as `Name`, `Visible` and `Association` come back as Python values
  via `GetAttributes()`.
- Compound `Vertices` becomes a numpy structured array with fields `x`, `y`, `z` (f8), via
  `ReadAsArray()`.
- `Cells` becomes an `(n, 3)` int32 array. Data `Data` arrays come back as numpy arrays.
- The string `Metadata` dataset fails with `ReadAsArray()`, but works with `MDArray.Read()`,
  which returns a list of JSON strings. The CRS code is readable this way (`EPSG:26914` in
  `surface_b.geoh5`).
- The `Visible` compound dataset `(ViewID, Visible)` fails with `ReadAsArray()`. It is not
  needed for the initial scope.

Smoke-test scripts are kept in the session scratchpad and are not part of the repo.

## Follow-ups

- Check how GDAL holds the file handle, so the reader can close it promptly and GEOSCIENCE
  ANALYST can still write to the file.
- ~~Check Grid2D attributes through `GetAttributes()`~~ Done; see below.
- ~~Check that Curve `Cells` read the same way~~ Done; see below.

## GDAL follow-up: Grid2D, Curve, value maps, colour maps [local]

Tested with QGIS 4.2.3's Python on `grid2d_examples.geoh5` (8 grids), `grid2d_test_a.geoh5`,
`workspace_complex.geoh5`, `workspace_test_file.geoh5` (1 Curve) and
`bm_reference_values_test.geoh5`.

**Object type.** Read the `ID` attribute of the object's `Type` subgroup:

- Grid2D: `{48f5054a-1c5c-4ca4-9048-80f36dc60a06}`
- Curve: `{6a057fdc-b355-11e3-95be-fd84a7ffcb88}`

**Grid2D:**

- Most frame attributes come back as plain Python values from `GetAttributes()`: `Rotation`,
  `Dip`, `Vertical` (0/1), `U Size`, `V Size`, `U Count`, `V Count`, and `Clipping IDs` (a
  list of UUID strings).
- **`Origin` is a compound attribute.** `Read()` returns **24 raw bytes**. Its datatype
  components are `x`, `y`, `z` at offsets 0, 8 and 16, each Float64. Decode it with
  `np.frombuffer(raw, "<f8")`. Example: `airborne data` → `[315532.25, 6065072.56, 172.0]`.
- Sample grids include `Dip` -35 and -90 with `Vertical` 0, and `Rotation` 20. Only grids
  with dip 0 can become north-up rasters with a rotated geotransform.
- Cell data has `Association=Cell`, and its length is `U Count × V Count` (132 × 356 =
  46 992). It is float32.

**Curve:**

- `Vertices` is a compound `(x, y, z)` f8 array.
- `Cells` is an `(n, 2)` **uint32** array of segments. (Surface `Cells` were `(n, 3)` int32.)
- Cell data has length `n_cells` (8583), so it is per segment. Vertex data is per vertex.
- No `Parts` dataset was present. Connected polylines must be derived from segment
  connectivity.

**No-data.** No data type carried a `No data value` attribute. The float data contained
`1.1754943508222875e-38`, which is geoh5py's `FLOAT_NDV` (see Part B, no-data handling).
The reader must convert it to NaN/NULL, and `-2147483648` likewise for integers.

**Value maps and colour maps:**

- **Value map** (`Types/Data types/{uid}/Value map`) is a compound `(Key u4, Value string)`.
  Whole-array `ReadAsArray()` and `Read()` both **fail** because of the string field. Read it
  per field with `GetView('["Key"]').ReadAsArray()` (numpy uint32) and
  `GetView('["Value"]').Read()` (a list of str). Key 0 = "Unknown" when present.
- **Color map** is a compound `(Value f8, Red u1, Green u1, Blue u1, Alpha u1)`. It is all
  numeric, so `ReadAsArray()` returns a numpy structured array directly.
  - Alpha is 255 in `bm_reference_values_test.geoh5` but **1** in `workspace_test_file.geoh5`.
    The meaning is unclear, so treat it with care (perhaps as a visibility flag).
  - Colour maps are present on both referenced data (one row per key) and float data (256
    rows: a continuous ramp).

**Not readable through GDAL's Python bindings:**

- **Unreadable by any method:** the object `Visible` dataset, a compound
  `(ViewID string, Visible u1)`. It also fails per field and is not needed.
- **Readable with `Read()` but not `ReadAsArray()`:** the `Metadata` and Text data
  (`Visual Parameters` XML, which includes the object `Colour` as an ARGB integer).
