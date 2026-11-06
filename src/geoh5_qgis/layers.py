"""Build in-memory QGIS layers from geoh5 objects.

Vector objects become memory-provider layers. Surfaces with vertex data become
``mesh_memory`` mesh layers instead. Grid2D objects become a GeoTIFF in
GDAL's in-memory filesystem (``/vsimem``) opened with the ``gdal`` provider. Each
layer records its source file and object UID as custom properties, so the geoh5
file stays the source of truth.
"""

import itertools
from pathlib import Path

import numpy as np
from osgeo import gdal, osr
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayer,
    QgsMeshLayer,
    QgsRasterLayer,
    QgsReadWriteContext,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtXml import QDomDocument

from geoh5_qgis.drillholes import (
    DrillholeGroupData,
    IntervalTable,
    interval_path,
    read_drillholes,
)
from geoh5_qgis.hdf5_blob import read_blob
from geoh5_qgis.reader import (
    CURVE,
    DRILLHOLES,
    GEOIMAGE,
    GRID2D,
    INTEGER_NDV,
    POINTS,
    SURFACE,
    Grid2DFrame,
    Workspace,
    find_object,
    is_horizontal,
    open_workspace,
    read_attributes,
    read_cells,
    read_crs,
    read_geoimage,
    read_grid2d_frame,
    read_vertices,
)

PATH_PROPERTY = "geoh5/path"
UID_PROPERTY = "geoh5/uid"
ROLE_PROPERTY = "geoh5/role"
VERTEX_ROLE = "vertices"
VERTEX_LAYER_SUFFIX = " vertices"
VSIMEM_DIR = "/vsimem/geoh5_qgis"
MESH_MEMORY_PROVIDER = "mesh_memory"
MESH_3D_RENDERER = "mesh"
ELEVATION_DATASET = "Z"
NBSP = " "
# ISO WKB geometry type codes (Z variants).
WKB_POINT_Z = 1001
WKB_LINESTRING_Z = 1002
WKB_POLYGON_Z = 1003
WKB_MULTIPOLYGON_Z = 1006

_raster_counter = itertools.count()


def load_object_layers(
    path: str | Path, object_uid: str, crs: str | None = None
) -> list[QgsMapLayer]:
    """Build the QGIS layers for one geoh5 object.

    Most objects give one layer. A curve with vertex data also gives a PointZ
    "<name> vertices" layer (custom property ``geoh5/role`` = ``"vertices"``).

    Args:
        path: Path to the .geoh5 file.
        object_uid: Object group name, as reported by reader.list_objects().
        crs: CRS to assign (e.g. ``"EPSG:26914"``). Defaults to the CRS stored in
            the object's metadata, if any.

    Returns:
        The main layer first: a QgsVectorLayer (Points, Curve, Surface), a
        QgsMeshLayer (Surface with vertex data) or a QgsRasterLayer (Grid2D).

    Raises:
        KeyError: If the object does not exist.
        ValueError: If the object kind is unsupported or cannot be mapped.
    """
    with open_workspace(path) as workspace:
        info = find_object(workspace, object_uid)
        if info is None:
            raise KeyError(f"No object {object_uid!r} in {path}")
        crs = crs or read_crs(workspace, object_uid)
        layers = _build_layers(workspace, path, info.kind, info.name, object_uid, crs)
    for layer in layers:
        layer.setCustomProperty(PATH_PROPERTY, str(path))
        layer.setCustomProperty(UID_PROPERTY, object_uid)
    return layers


def load_object_layer(
    path: str | Path, object_uid: str, crs: str | None = None
) -> QgsMapLayer:
    """Build the main QGIS layer for one geoh5 object.

    Args:
        path: Path to the .geoh5 file.
        object_uid: Object group name, as reported by reader.list_objects().
        crs: CRS to assign; defaults to the object's stored CRS, if any.

    Returns:
        The first layer from load_object_layers().

    Raises:
        KeyError: If the object does not exist.
        ValueError: If the object kind is unsupported or cannot be mapped.
    """
    return load_object_layers(path, object_uid, crs)[0]


def _build_layers(
    workspace: Workspace,
    path: str | Path,
    kind: str,
    name: str,
    uid: str,
    crs: str | None,
) -> list[QgsMapLayer]:
    if kind == POINTS:
        vertices = read_vertices(workspace, uid)
        attributes = read_attributes(workspace, uid, "Vertex", len(vertices))
        return [points_layer(name, vertices, attributes, crs)]
    if kind == CURVE:
        vertices = read_vertices(workspace, uid)
        cells = read_cells(workspace, uid)
        cell_data = read_attributes(workspace, uid, "Cell", len(cells))
        vertex_data = read_attributes(workspace, uid, "Vertex", len(vertices))
        layers = [curve_layer(name, vertices, cells, cell_data, crs)]
        if vertex_data:
            layers.append(curve_vertex_layer(name, vertices, cells, vertex_data, crs))
        return layers
    if kind == SURFACE:
        vertices = read_vertices(workspace, uid)
        cells = read_cells(workspace, uid)
        cell_data = read_attributes(workspace, uid, "Cell", len(cells))
        vertex_data = _numeric(read_attributes(workspace, uid, "Vertex", len(vertices)))
        if vertex_data:
            mesh = surface_mesh_layer(
                name, vertices, cells, vertex_data, _numeric(cell_data), crs
            )
            return [mesh]
        return [surface_layer(name, vertices, cells, cell_data, crs)]
    if kind == GRID2D:
        frame = read_grid2d_frame(workspace, uid)
        n_cells = frame.u_count * frame.v_count
        bands = _numeric(read_attributes(workspace, uid, "Cell", n_cells))
        return [grid2d_layer(name, uid, frame, bands, crs)]
    if kind == DRILLHOLES:
        return drillhole_layers(name, read_drillholes(workspace, uid), crs)
    if kind == GEOIMAGE:
        image = read_geoimage(workspace, uid)
        if image.blob_path is None:
            raise ValueError(f"GeoImage {name!r} has no embedded image")
        image_bytes = read_blob(path, image.blob_path)
        suffix = Path(image.file_name or "").suffix or ".img"
        return [geoimage_layer(name, uid, image.corners, image_bytes, suffix, crs)]
    raise ValueError(f"Object {name!r} has unsupported kind {kind!r}")


def points_layer(
    name: str,
    vertices: np.ndarray,
    attributes: dict[str, np.ndarray],
    crs: str | None,
) -> QgsVectorLayer:
    """Build a PointZ memory layer with one feature per vertex.

    Args:
        name: Layer name.
        vertices: (n, 3) x, y, z array.
        attributes: Column name to length-n array.
        crs: CRS string, or None to leave it unset.

    Returns:
        The memory layer.
    """
    geometries = _wkb_geometries(WKB_POINT_Z, (), vertices[:, None, :])
    return _vector_layer("PointZ", name, crs, geometries, attributes)


def curve_layer(
    name: str,
    vertices: np.ndarray,
    cells: np.ndarray,
    attributes: dict[str, np.ndarray],
    crs: str | None,
) -> QgsVectorLayer:
    """Build a LineStringZ memory layer with one feature per curve segment.

    A ``part`` column numbers the connected polylines, so segments can be
    dissolved into whole lines.

    Args:
        name: Layer name.
        vertices: (n, 3) x, y, z array.
        cells: (m, 2) vertex indices of each segment.
        attributes: Column name to length-m (segment) array.
        crs: CRS string, or None to leave it unset.

    Returns:
        The memory layer.
    """
    geometries = _wkb_geometries(WKB_LINESTRING_Z, (2,), vertices[cells])
    columns = _prepend({"part": curve_parts(cells)}, attributes)
    return _vector_layer("LineStringZ", name, crs, geometries, columns)


def curve_vertex_layer(
    name: str,
    vertices: np.ndarray,
    cells: np.ndarray,
    attributes: dict[str, np.ndarray],
    crs: str | None,
) -> QgsVectorLayer:
    """Build a PointZ layer of a curve's vertices, carrying its vertex data.

    ``vertex`` (index) and ``part`` columns link each point back to the curve's
    segments. The layer is named "<name> vertices" and has custom property
    ``geoh5/role`` = ``"vertices"``.

    Args:
        name: Curve name.
        vertices: (n, 3) x, y, z array.
        cells: (m, 2) vertex indices of each segment.
        attributes: Column name to length-n (vertex) array.
        crs: CRS string, or None to leave it unset.

    Returns:
        The memory layer.
    """
    index_columns = {
        "vertex": np.arange(len(vertices), dtype=np.int64),
        "part": vertex_parts(cells, len(vertices)),
    }
    layer = points_layer(
        f"{name}{VERTEX_LAYER_SUFFIX}",
        vertices,
        _prepend(index_columns, attributes),
        crs,
    )
    layer.setCustomProperty(ROLE_PROPERTY, VERTEX_ROLE)
    return layer


def surface_layer(
    name: str,
    vertices: np.ndarray,
    cells: np.ndarray,
    attributes: dict[str, np.ndarray],
    crs: str | None,
) -> QgsVectorLayer:
    """Build a memory layer of a triangulated surface.

    With per-triangle attributes, each triangle is a PolygonZ feature. Without
    them, the whole surface is a single MultiPolygonZ feature.

    Args:
        name: Layer name.
        vertices: (n, 3) x, y, z array.
        cells: (m, 3) vertex indices of each triangle.
        attributes: Column name to length-m (triangle) array.
        crs: CRS string, or None to leave it unset.

    Returns:
        The memory layer.
    """
    rings = vertices[np.column_stack([cells, cells[:, 0]])]  # closed: (m, 4, 3)
    if not attributes:
        # One feature for the whole surface: far faster and lighter than one
        # feature per triangle, and nothing per-triangle to show anyway.
        multi = _from_wkb(_wkb_multi(WKB_MULTIPOLYGON_Z, WKB_POLYGON_Z, (1, 4), rings))
        return _vector_layer("MultiPolygonZ", name, crs, [multi], {})
    geometries = _wkb_geometries(WKB_POLYGON_Z, (1, 4), rings)
    return _vector_layer("PolygonZ", name, crs, geometries, attributes)


def surface_mesh_layer(
    name: str,
    vertices: np.ndarray,
    cells: np.ndarray,
    vertex_data: dict[str, np.ndarray],
    face_data: dict[str, np.ndarray],
    crs: str | None,
) -> QgsMeshLayer:
    """Build an in-memory mesh layer for a surface with vertex data.

    The ``mesh_memory`` provider stores 2D vertices only, so elevation is added
    as the first vertex dataset (ELEVATION_DATASET), which is also the active
    colouring and can drive elevation in 3D views.

    Args:
        name: Layer name.
        vertices: (n, 3) x, y, z array.
        cells: (m, 3) vertex indices of each triangle.
        vertex_data: Dataset name to length-n numeric array.
        face_data: Dataset name to length-m numeric array.
        crs: CRS string, or None to leave it unset.

    Returns:
        The mesh layer.

    Raises:
        RuntimeError: If QGIS rejects the mesh or a dataset.
    """
    layer = QgsMeshLayer(_mesh_uri(vertices, cells), name, MESH_MEMORY_PROVIDER)
    if not layer.isValid():
        raise RuntimeError(f"QGIS could not build a mesh for surface {name!r}")
    layer.setCrs(
        QgsCoordinateReferenceSystem(crs) if crs else QgsCoordinateReferenceSystem()
    )
    datasets = [("vertex", ELEVATION_DATASET, vertices[:, 2])]
    datasets += [("vertex", k, v) for k, v in vertex_data.items()]
    datasets += [("face", k, v) for k, v in face_data.items()]
    taken: set[str] = set()
    for location, dataset_name, values in datasets:
        unique = _unique(dataset_name, taken)
        uri = _mesh_dataset_uri(location, unique, values)
        if not layer.dataProvider().addDataset(uri):
            raise RuntimeError(f"QGIS rejected dataset {unique!r} on {name!r}")
    settings = layer.rendererSettings()
    settings.setActiveScalarDatasetGroup(0)  # elevation
    # Dense triangle wireframes swamp the colours; users can turn edges back on.
    native = settings.nativeMeshSettings()
    native.setEnabled(False)
    settings.setNativeMeshSettings(native)
    triangular = settings.triangularMeshSettings()
    triangular.setEnabled(False)
    settings.setTriangularMeshSettings(triangular)
    layer.setRendererSettings(settings)
    _set_3d_height_from_dataset(layer, 0)  # 0 = elevation
    return layer


def _set_3d_height_from_dataset(layer: QgsMeshLayer, group_index: int) -> None:
    """Make QGIS 3D views take vertex heights from a dataset group.

    QgsMeshLayer3DRenderer is not exposed to Python in QGIS 4.2, so the renderer
    is built from XML through the 3D renderer registry. Skipped if the 3D
    module is unavailable.
    """
    try:
        import qgis._3d as q3d
    except ImportError:
        return
    registry = QgsApplication.renderer3DRegistry()
    if MESH_3D_RENDERER not in registry.renderersList():
        q3d.Qgs3D.initialize()  # already done by the desktop app
    symbol = q3d.QgsMesh3DSymbol()
    symbol.setVerticalDatasetGroupIndex(group_index)
    symbol.setIsVerticalMagnitudeRelative(False)  # dataset value is the height
    symbol.setVerticalScale(1.0)
    symbol.setSmoothedTriangles(True)
    symbol.setRenderingStyle(q3d.QgsMesh3DSymbol.RenderingStyle.ColorRamp2DRendering)

    doc = QDomDocument()
    context = QgsReadWriteContext()
    renderer_elem = doc.createElement("renderer-3d")
    renderer_elem.setAttribute("layer", layer.id())
    symbol_elem = doc.createElement("symbol")
    symbol_elem.setAttribute("type", symbol.type())
    symbol.writeXml(symbol_elem, context)
    renderer_elem.appendChild(symbol_elem)
    metadata = registry.rendererMetadata(MESH_3D_RENDERER)
    layer.setRenderer3D(metadata.createRenderer(renderer_elem, context))


def mesh_dataset_name(name: str) -> str:
    """Encode a name for a mesh_memory dataset URI.

    The provider splits its type line on ASCII spaces, so spaces become
    non-breaking spaces (which display the same). Newlines are flattened.

    Args:
        name: Display name.

    Returns:
        The encoded name.
    """
    return " ".join(name.split()).replace(" ", NBSP)


def mesh_display_name(name: str) -> str:
    """Reverse mesh_dataset_name() for display or comparison.

    Args:
        name: A dataset group name as reported by QGIS.

    Returns:
        The name with non-breaking spaces turned back into spaces.
    """
    return name.replace(NBSP, " ")


def grid2d_layer(
    name: str,
    uid: str,
    frame: Grid2DFrame,
    bands: dict[str, np.ndarray],
    crs: str | None,
) -> QgsRasterLayer:
    """Build a raster layer for a horizontal Grid2D, one band per data channel.

    Args:
        name: Layer name.
        uid: Object UID, used to name the in-memory GeoTIFF.
        frame: The grid's placement.
        bands: Band name to cell values (U fastest, length u_count * v_count).
        crs: CRS string, or None to leave it unset.

    Returns:
        A raster layer backed by a GeoTIFF in /vsimem.

    Raises:
        ValueError: If the grid is dipping/vertical or has no numeric data.
    """
    if not is_horizontal(frame):
        raise ValueError(
            f"Grid2D {name!r} has dip {frame.dip} (vertical={frame.vertical}); "
            "only horizontal grids can be shown as map rasters"
        )
    if not bands:
        raise ValueError(f"Grid2D {name!r} has no numeric cell data to display")
    path = f"{VSIMEM_DIR}/{uid.strip('{}')}_{next(_raster_counter)}.tif"
    _write_grid_tiff(path, frame, bands, crs)
    layer = QgsRasterLayer(path, name, "gdal")
    if crs:
        layer.setCrs(QgsCoordinateReferenceSystem(crs))
    return layer


def drillhole_layers(
    name: str, group: DrillholeGroupData, crs: str | None
) -> list[QgsVectorLayer]:
    """Build collar, trace and interval layers for a drillhole group.

    - "<name> collars": PointZ per hole (hole, end_of_hole, planning).
    - "<name> traces": LineStringZ per hole along its path.
    - "<name> <table>": LineStringZ per interval of each interval table, following
      the hole between its from and to depths, with the table's channels.

    Args:
        name: Drillhole group name.
        group: Holes and interval tables from drillholes.read_drillholes().
        crs: CRS string, or None to leave it unset.

    Returns:
        The layers, collars first.
    """
    holes = group.holes
    hole_names = np.array([h.name for h in holes], dtype=object)
    collars = points_layer(
        f"{name} collars",
        np.array([h.collar for h in holes]).reshape(-1, 3),
        {
            "hole": hole_names,
            "end_of_hole": np.array(
                [np.nan if h.end_of_hole is None else h.end_of_hole for h in holes]
            ),
            "planning": np.array([h.planning for h in holes], dtype=object),
        },
        crs,
    )
    traces = _vector_layer(
        "LineStringZ",
        f"{name} traces",
        crs,
        [_line_geometry(h.trace) for h in holes],
        {"hole": hole_names},
    )
    layers = [collars, traces] + [
        _interval_layer(f"{name} {table.name}", holes, table, crs)
        for table in group.intervals
    ]
    for layer, role in zip(layers, ["collars", "traces"], strict=False):
        layer.setCustomProperty(ROLE_PROPERTY, role)
    return layers


def _interval_layer(
    name: str, holes: tuple, table: IntervalTable, crs: str | None
) -> QgsVectorLayer:
    geometries = [
        _line_geometry(interval_path(holes[i].trace, holes[i].trace_depth, s, e))
        for i, s, e in zip(table.hole_index, table.start, table.end, strict=True)
    ]
    fixed = {
        "hole": np.array([holes[i].name for i in table.hole_index], dtype=object),
        "from": table.start,
        "to": table.end,
    }
    layer = _vector_layer(
        "LineStringZ", name, crs, geometries, _prepend(fixed, table.columns)
    )
    layer.setCustomProperty(ROLE_PROPERTY, "intervals")
    return layer


def _line_geometry(points: np.ndarray) -> QgsGeometry:
    """LineStringZ from an (n, 3) array, via ISO WKB."""
    header = (
        np.array([1], "u1").tobytes()
        + np.array([WKB_LINESTRING_Z, len(points)], "<u4").tobytes()
    )
    return _from_wkb(header + np.ascontiguousarray(points, dtype="<f8").tobytes())


def geoimage_layer(
    name: str,
    uid: str,
    corners: np.ndarray,
    image_bytes: bytes,
    suffix: str,
    crs: str | None,
) -> QgsRasterLayer:
    """Build a raster layer for a horizontal GeoImage.

    The embedded image file is placed in /vsimem and wrapped in a VRT that
    carries the corner-derived geotransform and CRS, so GDAL decodes the
    original JPEG/PNG/TIFF on demand.

    Args:
        name: Layer name.
        uid: Object UID, used to name the in-memory files.
        corners: (4, 3) corners: top-left, top-right, bottom-right, bottom-left.
        image_bytes: The embedded image file's bytes.
        suffix: File extension of the embedded image (e.g. ``.jpg``).
        crs: CRS string, or None to leave it unset.

    Returns:
        A raster layer backed by a VRT in /vsimem.

    Raises:
        RuntimeError: If GDAL cannot decode the image.
    """
    stem = f"{VSIMEM_DIR}/{uid.strip('{}')}_{next(_raster_counter)}"
    image_path, vrt_path = f"{stem}{suffix}", f"{stem}.vrt"
    with gdal.ExceptionMgr(useExceptions=True):
        gdal.FileFromMemBuffer(image_path, image_bytes)
        source = gdal.Open(image_path)
        vrt = gdal.Translate(vrt_path, source, format="VRT")
        vrt.SetGeoTransform(
            geoimage_geotransform(corners, source.RasterXSize, source.RasterYSize)
        )
        if crs:
            srs = osr.SpatialReference()
            srs.SetFromUserInput(crs)
            vrt.SetProjection(srs.ExportToWkt())
        vrt.FlushCache()
        del vrt, source
    layer = QgsRasterLayer(vrt_path, name, "gdal")
    if crs:
        layer.setCrs(QgsCoordinateReferenceSystem(crs))
    return layer


def geoimage_geotransform(
    corners: np.ndarray, width: int, height: int
) -> tuple[float, ...]:
    """GDAL geotransform mapping image pixels onto a GeoImage's corners.

    Args:
        corners: (4, 3) corners: top-left, top-right, bottom-right, bottom-left
            (geoh5py's order; pixel (0, 0) is the top-left of the image).
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        The six geotransform coefficients.
    """
    top_left, top_right, _, bottom_left = corners[:, :2].astype(float)
    column_step = (top_right - top_left) / width
    row_step = (bottom_left - top_left) / height
    return (
        float(top_left[0]),
        float(column_step[0]),
        float(row_step[0]),
        float(top_left[1]),
        float(column_step[1]),
        float(row_step[1]),
    )


def grid2d_geotransform(frame: Grid2DFrame) -> tuple[float, ...]:
    """GDAL geotransform for a horizontal Grid2D with row 0 at the far-V edge.

    geoh5 places the grid corner at ``origin`` with U along the rotated x axis and
    V along the rotated y axis (rotation counter-clockwise in degrees). Rasters
    start at their top row, so the transform starts from the corner at v_count.

    Args:
        frame: The grid's placement.

    Returns:
        The six geotransform coefficients.
    """
    angle = np.deg2rad(frame.rotation)
    cos, sin = float(np.cos(angle)), float(np.sin(angle))
    x0, y0, _ = frame.origin
    v_extent = frame.v_count * frame.v_size
    return (
        x0 - v_extent * sin,
        frame.u_size * cos,
        frame.v_size * sin,
        y0 + v_extent * cos,
        frame.u_size * sin,
        -frame.v_size * cos,
    )


def curve_parts(cells: np.ndarray) -> np.ndarray:
    """Number the connected polylines of a curve, per segment.

    Args:
        cells: (m, 2) vertex indices of each segment.

    Returns:
        Length-m int array of part numbers, in order of first appearance.
    """
    if len(cells) == 0:
        return np.zeros(0, dtype=np.int64)
    parent = list(range(int(cells.max()) + 1))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in cells.tolist():
        parent[find(a)] = find(b)
    roots = [find(a) for a, _ in cells.tolist()]
    numbering = {root: n for n, root in enumerate(dict.fromkeys(roots))}
    return np.array([numbering[root] for root in roots], dtype=np.int64)


def vertex_parts(cells: np.ndarray, n_vertices: int) -> np.ndarray:
    """Part number of each curve vertex (see curve_parts).

    Args:
        cells: (m, 2) vertex indices of each segment.
        n_vertices: Number of vertices in the curve.

    Returns:
        Length-n int array; -1 for vertices used by no segment.
    """
    parts = np.full(n_vertices, -1, dtype=np.int64)
    segment_parts = curve_parts(cells)
    parts[cells[:, 0]] = segment_parts
    parts[cells[:, 1]] = segment_parts
    return parts


def _prepend(
    fixed: dict[str, np.ndarray], data: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """Fixed columns first; data columns renamed (``name_2``) if they clash."""
    taken = set(fixed)
    return fixed | {_unique(k, taken): v for k, v in data.items()}


def _vector_layer(
    geometry_type: str,
    name: str,
    crs: str | None,
    geometries: list,
    columns: dict[str, np.ndarray],
) -> QgsVectorLayer:
    uri = f"{geometry_type}?crs={crs}" if crs else geometry_type
    layer = QgsVectorLayer(uri, name, "memory")
    if not crs:
        # The memory provider defaults to EPSG:4326; leave it unset instead so
        # QGIS applies its "layer has no CRS" handling.
        layer.setCrs(QgsCoordinateReferenceSystem())
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(k, _field_type(v)) for k, v in columns.items()])
    layer.updateFields()
    rows = zip(*(_python_values(v) for v in columns.values()), strict=True)
    features = [
        _feature(layer, geometry, list(row))
        for geometry, row in zip(
            geometries, rows if columns else itertools.repeat(()), strict=False
        )
    ]
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _feature(
    layer: QgsVectorLayer, geometry: QgsGeometry, attributes: list
) -> QgsFeature:
    feature = QgsFeature(layer.fields())
    feature.setGeometry(geometry)
    feature.setAttributes(attributes)
    return feature


def _wkb_geometries(
    type_code: int, counts: tuple[int, ...], coords: np.ndarray
) -> list[QgsGeometry]:
    """Build one geometry per row of ``coords`` from ISO WKB generated in numpy.

    About 3x faster than constructing QgsPoint/QgsLineString/QgsPolygon objects.

    Args:
        type_code: ISO WKB geometry type (e.g. 1003 for PolygonZ).
        counts: Fixed header counts after the type (ring and/or point counts).
        coords: (n, k, 3) coordinates, k points per geometry.

    Returns:
        n geometries.
    """
    raw = _wkb_records(type_code, counts, coords)
    size = len(raw) // len(coords) if len(coords) else 0
    return [_from_wkb(raw[i * size : (i + 1) * size]) for i in range(len(coords))]


def _wkb_multi(
    multi_code: int, type_code: int, counts: tuple[int, ...], coords: np.ndarray
) -> bytes:
    """ISO WKB of a multi-geometry whose parts are the rows of ``coords``."""
    header = (
        np.array([1], "u1").tobytes()
        + np.array([multi_code, len(coords)], "<u4").tobytes()
    )
    return header + _wkb_records(type_code, counts, coords)


def _wkb_records(type_code: int, counts: tuple[int, ...], coords: np.ndarray) -> bytes:
    """Concatenated little-endian ISO WKB, one geometry per row of ``coords``."""
    n, k, _ = coords.shape
    dtype = np.dtype(
        [("order", "u1"), ("type", "<u4")]
        + [(f"count{i}", "<u4") for i in range(len(counts))]
        + [("xyz", "<f8", (k, 3))]
    )
    records = np.empty(n, dtype=dtype)
    records["order"], records["type"] = 1, type_code  # 1 = little-endian
    for i, count in enumerate(counts):
        records[f"count{i}"] = count
    records["xyz"] = coords
    return records.tobytes()


def _from_wkb(wkb: bytes) -> QgsGeometry:
    geometry = QgsGeometry()
    geometry.fromWkb(wkb)
    return geometry


def _field_type(values: np.ndarray) -> QMetaType.Type:
    if values.dtype.kind == "f":
        return QMetaType.Type.Double
    if values.dtype.kind in "iub":
        return QMetaType.Type.LongLong
    return QMetaType.Type.QString


def _python_values(values: np.ndarray) -> list:
    """Convert a column to Python values, with no-data as None (QGIS NULL)."""
    if values.dtype.kind == "f":
        return [None if np.isnan(v) else v for v in values.tolist()]
    if values.dtype.kind in "iu":
        return [None if v == INTEGER_NDV else v for v in values.tolist()]
    return values.tolist()


def _write_grid_tiff(
    path: str, frame: Grid2DFrame, bands: dict[str, np.ndarray], crs: str | None
) -> None:
    with gdal.ExceptionMgr(useExceptions=True):
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(
            path, frame.u_count, frame.v_count, len(bands), gdal.GDT_Float64
        )
        dataset.SetGeoTransform(grid2d_geotransform(frame))
        if crs:
            srs = osr.SpatialReference()
            srs.SetFromUserInput(crs)
            dataset.SetProjection(srs.ExportToWkt())
        for index, (band_name, values) in enumerate(bands.items(), start=1):
            band = dataset.GetRasterBand(index)
            band.WriteArray(_grid_rows(values, frame))
            band.SetNoDataValue(float("nan"))
            band.SetDescription(band_name)
        dataset.FlushCache()
        del dataset


def _grid_rows(values: np.ndarray, frame: Grid2DFrame) -> np.ndarray:
    """Reshape U-fastest cell values to raster rows, top (far-V) row first."""
    return _as_float(values).reshape(frame.v_count, frame.u_count)[::-1]


def _as_float(values: np.ndarray) -> np.ndarray:
    """Numeric values as float64, with integer no-data as NaN."""
    result = values.astype(np.float64)
    if values.dtype.kind in "iu":
        result[values == INTEGER_NDV] = np.nan
    return result


def _numeric(columns: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Drop text and value-mapped (string) columns."""
    return {k: v for k, v in columns.items() if v.dtype != object}


def _unique(name: str, taken: set[str]) -> str:
    candidates = (name if i == 1 else f"{name}_{i}" for i in itertools.count(1))
    unique = next(c for c in candidates if c not in taken)
    taken.add(unique)
    return unique


def _mesh_uri(vertices: np.ndarray, cells: np.ndarray) -> str:
    """mesh_memory URI: ``x,y`` lines, ``---``, then ``i,j,k`` face lines."""
    return f"{_csv_lines(vertices[:, :2])}\n---\n{_csv_lines(cells)}"


def _mesh_dataset_uri(location: str, name: str, values: np.ndarray) -> str:
    """mesh_memory dataset URI: type line, metadata, then time and values.

    Sections are separated by ``---``; an empty metadata section would be
    dropped by the provider's parser, so one metadata line is always written.
    """
    header = f"{location} scalar {mesh_dataset_name(name)}"
    return f"{header}\n---\nsource: geoh5\n---\n0\n{_csv_lines(_as_float(values))}"


def _csv_lines(array: np.ndarray) -> str:
    """Comma-separated rows; floats use NumPy's shortest round-trip repr."""
    text = np.asarray(array).astype(str)
    if text.ndim == 1:
        return "\n".join(text)
    return "\n".join(",".join(row) for row in text)
