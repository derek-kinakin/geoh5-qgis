"""Build in-memory QGIS layers from geoh5 objects.

Vector objects become memory-provider layers. Grid2D objects become a GeoTIFF in
GDAL's in-memory filesystem (``/vsimem``) opened with the ``gdal`` provider. Each
layer records its source file and object UID as custom properties, so the geoh5
file stays the source of truth.
"""

import itertools
from pathlib import Path

import numpy as np
from osgeo import gdal, osr
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayer,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from geoh5_qgis.reader import (
    CURVE,
    GRID2D,
    INTEGER_NDV,
    POINTS,
    SURFACE,
    Grid2DFrame,
    Workspace,
    list_objects,
    open_workspace,
    read_attributes,
    read_cells,
    read_crs,
    read_grid2d_frame,
    read_vertices,
)

PATH_PROPERTY = "geoh5/path"
UID_PROPERTY = "geoh5/uid"
VSIMEM_DIR = "/vsimem/geoh5_qgis"
# ISO WKB geometry type codes (Z variants).
WKB_POINT_Z = 1001
WKB_LINESTRING_Z = 1002
WKB_POLYGON_Z = 1003
WKB_MULTIPOLYGON_Z = 1006

_raster_counter = itertools.count()


def load_object_layer(
    path: str | Path, object_uid: str, crs: str | None = None
) -> QgsMapLayer:
    """Build a QGIS layer for one geoh5 object.

    Args:
        path: Path to the .geoh5 file.
        object_uid: Object group name, as reported by reader.list_objects().
        crs: CRS to assign (e.g. ``"EPSG:26914"``). Defaults to the CRS stored in
            the object's metadata, if any.

    Returns:
        A QgsVectorLayer (Points, Curve, Surface) or QgsRasterLayer (Grid2D).

    Raises:
        KeyError: If the object does not exist.
        ValueError: If the object kind is unsupported or cannot be mapped.
    """
    with open_workspace(path) as workspace:
        info = next((o for o in list_objects(workspace) if o.uid == object_uid), None)
        if info is None:
            raise KeyError(f"No object {object_uid!r} in {path}")
        crs = crs or read_crs(workspace, object_uid)
        layer = _build_layer(workspace, info.kind, info.name, object_uid, crs)
    layer.setCustomProperty(PATH_PROPERTY, str(path))
    layer.setCustomProperty(UID_PROPERTY, object_uid)
    return layer


def _build_layer(
    workspace: Workspace, kind: str, name: str, uid: str, crs: str | None
) -> QgsMapLayer:
    if kind == POINTS:
        vertices = read_vertices(workspace, uid)
        attributes = read_attributes(workspace, uid, "Vertex", len(vertices))
        return points_layer(name, vertices, attributes, crs)
    if kind in (CURVE, SURFACE):
        vertices = read_vertices(workspace, uid)
        cells = read_cells(workspace, uid)
        attributes = read_attributes(workspace, uid, "Cell", len(cells))
        build = curve_layer if kind == CURVE else surface_layer
        return build(name, vertices, cells, attributes, crs)
    if kind == GRID2D:
        frame = read_grid2d_frame(workspace, uid)
        n_cells = frame.u_count * frame.v_count
        attributes = read_attributes(workspace, uid, "Cell", n_cells)
        bands = {k: v for k, v in attributes.items() if v.dtype != object}
        return grid2d_layer(name, uid, frame, bands, crs)
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
    columns = {"part": curve_parts(cells)} | attributes
    return _vector_layer("LineStringZ", name, crs, geometries, columns)


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
    if frame.vertical or frame.dip != 0.0:
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
    grid = values.astype(np.float64)
    if values.dtype.kind in "iu":
        grid[values == INTEGER_NDV] = np.nan
    return grid.reshape(frame.v_count, frame.u_count)[::-1]
