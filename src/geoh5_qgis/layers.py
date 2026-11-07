"""Build QGIS layers from geoh5 objects.

Vector objects become memory-provider layers. Surfaces with vertex data become
PLY mesh files (``mdal`` provider) instead. Grid2D and GeoImage objects become
GeoTIFF/VRT files in the session temp folder (see tempfiles.py), opened with the
``gdal`` provider. Each layer records its source file, object UID and part as
custom properties, so the geoh5 file stays the source of truth.
"""

import itertools
from dataclasses import dataclass
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
    QgsMeshDatasetGroupMetadata,
    QgsMeshLayer,
    QgsRasterLayer,
    QgsReadWriteContext,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, QMetaType
from qgis.PyQt.QtXml import QDomDocument

from geoh5_qgis.drillholes import (
    DepthTable,
    DrillholeGroupData,
    IntervalTable,
    interval_path,
    positions_at,
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
    DataChannel,
    DataInfo,
    Grid2DFrame,
    Workspace,
    find_object,
    is_horizontal,
    open_workspace,
    raster_channels,
    read_cells,
    read_channel,
    read_channels,
    read_crs,
    read_geoimage,
    read_grid2d_frame,
    read_object_colour,
    read_vertices,
)
from geoh5_qgis.sources import register_generated
from geoh5_qgis.styles import (
    apply_default_style,
    apply_interpolated_style,
    apply_mesh_style,
    apply_raster_style,
)
from geoh5_qgis.styles3d import apply_3d_style, qgis_3d
from geoh5_qgis.tempfiles import is_temp_file, new_path

PATH_PROPERTY = "geoh5/path"
UID_PROPERTY = "geoh5/uid"
ROLE_PROPERTY = "geoh5/role"
# Identifies each of an object's layers (main, vertices, collars, traces,
# intervals/<table>), so a saved project can be matched to rebuilt layers.
PART_PROPERTY = "geoh5/part"
MAIN_PART = "main"
POLYGONS_PART = "polygons"  # a surface loaded as triangle polygons
# Part of a Grid2D channel raster: CHANNEL_PART_PREFIX + data UID.
CHANNEL_PART_PREFIX = "channel/"
# Pass as ``channels`` to load every raster channel of a Grid2D.
ALL_CHANNELS = ("*",)
VERTEX_ROLE = "vertices"
VERTEX_LAYER_SUFFIX = " vertices"
MESH_PROVIDER = "mdal"
# Suffix of the text column holding a referenced channel's names.
LABEL_SUFFIX = "_label"
MESH_3D_RENDERER = "mesh"
NBSP = " "
# GeoImages longer than this (pixels) without overviews get them built.
OVERVIEW_MIN_SIZE = 2048
# Smallest overview built (longest side, pixels).
OVERVIEW_SMALLEST = 256
# ISO WKB geometry type codes (Z variants).
WKB_POINT_Z = 1001
WKB_LINESTRING_Z = 1002
WKB_POLYGON_Z = 1003


def load_object_layers(
    path: str | Path,
    object_uid: str,
    crs: str | None = None,
    as_polygons: bool = False,
    channels: tuple[str, ...] | None = None,
) -> list[QgsMapLayer]:
    """Build the QGIS layers for one geoh5 object.

    Most objects give one layer. A curve with vertex data also gives a PointZ
    "<name> vertices" layer (custom property ``geoh5/role`` = ``"vertices"``).
    Surfaces load as meshes, or with ``as_polygons`` as one PolygonZ per
    triangle (part ``"polygons"``). A Grid2D gives one raster per channel,
    named "<grid>: <channel>" (part ``"channel/<data uid>"``).

    Args:
        path: Path to the .geoh5 file.
        object_uid: Object group name, as reported by reader.list_objects().
        crs: CRS to assign (e.g. ``"EPSG:26914"``). Defaults to the CRS stored in
            the object's metadata, if any.
        as_polygons: Load a surface as triangle polygons instead of a mesh.
            Ignored for other object kinds.
        channels: Data UIDs of the Grid2D channels to load, or ALL_CHANNELS.
            None loads the channel GEOSCIENCE ANALYST shows (else the first).
            Ignored for other object kinds.

    Returns:
        The main layer first: a QgsVectorLayer (Points, Curve, drillholes,
        surface polygons), a QgsMeshLayer (Surface) or a QgsRasterLayer
        (Grid2D, GeoImage).

    Raises:
        KeyError: If the object does not exist.
        ValueError: If the object kind is unsupported or cannot be mapped.
    """
    with open_workspace(path) as workspace:
        info = find_object(workspace, object_uid)
        if info is None:
            raise KeyError(f"No object {object_uid!r} in {path}")
        crs = crs or read_crs(workspace, object_uid)
        layers = _build_layers(
            workspace,
            path,
            info.kind,
            info.name,
            object_uid,
            crs,
            as_polygons,
            channels,
        )
    for layer in layers:
        layer.setCustomProperty(PATH_PROPERTY, str(path))
        layer.setCustomProperty(UID_PROPERTY, object_uid)
        if layer.customProperty(PART_PROPERTY) is None:
            role = layer.customProperty(ROLE_PROPERTY)
            layer.setCustomProperty(PART_PROPERTY, role or MAIN_PART)
        if isinstance(layer, QgsVectorLayer) and layer.renderer3D() is None:
            apply_3d_style(layer)  # draw at true Z, not draped on the terrain
        if is_temp_file(layer.source()):
            part = str(layer.customProperty(PART_PROPERTY))
            register_generated(layer.source(), str(path), object_uid, part)
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
    as_polygons: bool = False,
    channels: tuple[str, ...] | None = None,
) -> list[QgsMapLayer]:
    if kind == POINTS:
        vertices = read_vertices(workspace, uid)
        data = channel_columns(read_channels(workspace, uid, "Vertex", len(vertices)))
        colour = read_object_colour(workspace, uid)
        return [_finish(points_layer(name, vertices, data.columns, crs), data, colour)]
    if kind == CURVE:
        vertices = read_vertices(workspace, uid)
        cells = read_cells(workspace, uid)
        colour = read_object_colour(workspace, uid)
        cell_data = channel_columns(read_channels(workspace, uid, "Cell", len(cells)))
        vertex_channels = read_channels(workspace, uid, "Vertex", len(vertices))
        vertex_data = channel_columns(vertex_channels)
        if not cell_data.columns and not vertex_data.columns:
            lines = curve_lines_layer(name, vertices, cells, crs)
            return [_finish(lines, cell_data, colour)]
        ends, end_channels = vertex_end_columns(
            vertex_channels, cells, set(cell_data.columns)
        )
        data = _concat(cell_data, ends)
        curve = _finish(
            curve_layer(name, vertices, cells, data.columns, crs), data, colour
        )
        _style_by_shown_vertex_channel(
            curve, data, len(cell_data.aliases), end_channels
        )
        layers = [curve]
        if vertex_data.columns:
            vertex_layer = curve_vertex_layer(
                name, vertices, cells, vertex_data.columns, crs
            )
            layers.append(_finish(vertex_layer, vertex_data, colour))
        return layers
    if kind == SURFACE:
        vertices = read_vertices(workspace, uid)
        cells = read_cells(workspace, uid)
        cell_data = channel_columns(read_channels(workspace, uid, "Cell", len(cells)))
        if as_polygons:
            polygons = surface_polygons_layer(
                name, vertices, cells, cell_data.columns, crs
            )
            colour = read_object_colour(workspace, uid)
            return [_finish(polygons, cell_data, colour)]
        vertex_data = channel_columns(
            read_channels(workspace, uid, "Vertex", len(vertices))
        )
        mesh = surface_mesh_layer(
            name,
            vertices,
            cells,
            _numeric(vertex_data.columns),
            _numeric(cell_data.columns),
            crs,
            shown=_shown_column(vertex_data) or _shown_column(cell_data),
        )
        return [mesh]
    if kind == GRID2D:
        frame = read_grid2d_frame(workspace, uid)
        infos = select_channels(raster_channels(workspace, uid), channels)
        if not infos:
            raise ValueError(f"Grid2D {name!r} has no matching numeric cell data")
        return [
            grid2d_layer(
                f"{name}: {info.name}", frame, read_channel(workspace, info), crs
            )
            for info in infos
        ]
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


@dataclass(frozen=True)
class ChannelColumns:
    """Attribute columns made from data channels (see channel_columns)."""

    columns: dict[str, np.ndarray]
    # Per column, in order: its alias (None for none) and the channel whose
    # stored values it holds (None for label columns).
    aliases: tuple[str | None, ...]
    sources: tuple[DataChannel | None, ...]


def channel_columns(channels: list[DataChannel]) -> ChannelColumns:
    """Turn data channels into attribute columns.

    Each channel gives a column of its stored values under its name (made unique
    with ``_2``, ``_3``...). A referenced channel keeps its integer keys and also
    gives a ``<name>_label`` text column of the value-map names. A channel with
    units is aliased ``"<name> (<units>)"``.

    Args:
        channels: Channels from reader.read_channels().

    Returns:
        The columns with their aliases and source channels.
    """
    taken: set[str] = set()
    columns: dict[str, np.ndarray] = {}
    aliases: list[str | None] = []
    sources: list[DataChannel | None] = []
    for channel in channels:
        columns[_unique(channel.name, taken)] = channel.values
        aliases.append(f"{channel.name} ({channel.units})" if channel.units else None)
        sources.append(channel)
        if channel.value_map is not None:
            label = _unique(f"{channel.name}{LABEL_SUFFIX}", taken)
            columns[label] = _labels(channel.values, channel.value_map)
            aliases.append(None)
            sources.append(None)
    return ChannelColumns(columns, tuple(aliases), tuple(sources))


def _labels(keys: np.ndarray, value_map: dict[int, str]) -> np.ndarray:
    """Value-map names of referenced keys; unmapped keys show as their number."""
    return np.array([value_map.get(int(k), str(k)) for k in keys], dtype=object)


def _finish(
    layer: QgsVectorLayer,
    data: ChannelColumns,
    object_colour: tuple[int, int, int, int] | None,
) -> QgsVectorLayer:
    """Alias the channel fields and apply GA's colouring.

    Builders put channel columns last, so they are found counting from the end,
    even when a builder renamed one to avoid one of its own fixed columns.
    """
    first = layer.fields().count() - len(data.aliases)
    for offset, alias in enumerate(data.aliases):
        if alias:
            layer.setFieldAlias(first + offset, alias)
    shown = next(
        (i for i, c in enumerate(data.sources) if c is not None and c.visible), None
    )
    field_name = None if shown is None else layer.fields().at(first + shown).name()
    channel = None if shown is None else data.sources[shown]
    apply_default_style(layer, field_name, channel, object_colour)
    return layer


def vertex_end_columns(
    channels: list[DataChannel], cells: np.ndarray, taken: set[str]
) -> tuple[ChannelColumns, list[DataChannel]]:
    """Per-segment columns of numeric vertex channels at each segment's ends.

    Each Float or Integer channel gives ``<name>_start`` and ``<name>_end``
    (values at the segment's first and second vertex), so segments can be
    shaded along their length. The columns have no source channel, so they are
    not picked up as a shown channel by _finish.

    Args:
        channels: Vertex channels from reader.read_channels().
        cells: (m, 2) vertex indices of each segment.
        taken: Column names already in use (made unique against these).

    Returns:
        The columns, and the channels they came from (two columns each, in
        order).
    """
    numeric = [c for c in channels if c.primitive_type in ("Float", "Integer")]
    taken = set(taken)
    columns: dict[str, np.ndarray] = {}
    aliases: list[str | None] = []
    for channel in numeric:
        for suffix, ends in (("_start", cells[:, 0]), ("_end", cells[:, 1])):
            columns[_unique(f"{channel.name}{suffix}", taken)] = channel.values[ends]
            units = f" ({channel.units})" if channel.units else ""
            aliases.append(f"{channel.name}{suffix}{units}" if units else None)
    return ChannelColumns(columns, tuple(aliases), (None,) * len(aliases)), numeric


def _concat(first: ChannelColumns, second: ChannelColumns) -> ChannelColumns:
    return ChannelColumns(
        first.columns | second.columns,
        first.aliases + second.aliases,
        first.sources + second.sources,
    )


def _style_by_shown_vertex_channel(
    layer: QgsVectorLayer,
    data: ChannelColumns,
    n_cell_columns: int,
    end_channels: list[DataChannel],
) -> None:
    """Shade segments by the vertex channel GA shows, if it has end columns."""
    shown = next((i for i, c in enumerate(end_channels) if c.visible), None)
    if shown is None:
        return
    first = layer.fields().count() - len(data.aliases) + n_cell_columns + 2 * shown
    start, end = (layer.fields().at(first + i).name() for i in (0, 1))
    apply_interpolated_style(layer, start, end, end_channels[shown])


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


def curve_lines_layer(
    name: str, vertices: np.ndarray, cells: np.ndarray, crs: str | None
) -> QgsVectorLayer:
    """Build a LineStringZ memory layer with one feature per curve chain.

    Used for curves without data, where splitting into segments only gets in
    the way of selecting, measuring and profiling. See curve_chains().

    Args:
        name: Layer name.
        vertices: (n, 3) x, y, z array.
        cells: (m, 2) vertex indices of each segment.
        crs: CRS string, or None to leave it unset.

    Returns:
        The memory layer, with a ``part`` column numbering connected polylines.
    """
    chains = curve_chains(cells)
    parts = vertex_parts(cells, len(vertices))
    return _vector_layer(
        "LineStringZ",
        name,
        crs,
        [_line_geometry(vertices[chain]) for chain in chains],
        {"part": np.array([parts[chain[0]] for chain in chains], dtype=np.int64)},
    )


def curve_chains(cells: np.ndarray) -> list[np.ndarray]:
    """Split a curve's segments into chains: runs of vertices joined end to end.

    A chain starts and stops at an end or a junction (a vertex used by one or
    by three or more segments) and follows the segments in between, so each
    chain is one LineString. A closed loop of segments gives a chain that ends
    on its first vertex. Chains start at the vertex that appears first in
    ``cells``, so a curve stored in drawing order keeps its direction.

    Args:
        cells: (m, 2) vertex indices of each segment.

    Returns:
        One int64 array of vertex indices per chain; every segment is used once.
    """
    links: dict[int, list[int]] = {}
    for index, (a, b) in enumerate(cells.tolist()):
        links.setdefault(a, []).append(index)
        links.setdefault(b, []).append(index)
    used = [False] * len(cells)

    def walk(vertex: int, segment: int) -> np.ndarray:
        chain = [vertex]
        while True:
            used[segment] = True
            a, b = cells[segment].tolist()
            vertex = b if a == vertex else a
            chain.append(vertex)
            if len(links[vertex]) != 2:
                break  # an end or a junction
            segment = next((s for s in links[vertex] if not used[s]), -1)
            if segment < 0:
                break  # back where a loop started
        return np.array(chain, dtype=np.int64)

    order = dict.fromkeys(cells.ravel().tolist())
    chains = [
        walk(vertex, segment)
        for vertex in order
        if len(links[vertex]) != 2
        for segment in links[vertex]
        if not used[segment]
    ]
    for segment in range(len(cells)):  # what is left is closed loops
        if not used[segment]:
            chains.append(walk(int(cells[segment, 0]), segment))
    return chains


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


def surface_polygons_layer(
    name: str,
    vertices: np.ndarray,
    cells: np.ndarray,
    attributes: dict[str, np.ndarray],
    crs: str | None,
) -> QgsVectorLayer:
    """Build a memory layer with one PolygonZ feature per surface triangle.

    Each triangle gets ``z_min``, ``z_max`` and ``z_mean`` of its corners (for
    graduated styling by elevation) followed by its triangle data. The layer's
    part is ``"polygons"``, so a reopened project rebuilds it as polygons.

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
    corner_z = vertices[cells][:, :, 2]
    elevation = {
        "z_min": corner_z.min(axis=1),
        "z_max": corner_z.max(axis=1),
        "z_mean": corner_z.mean(axis=1),
    }
    geometries = _wkb_geometries(WKB_POLYGON_Z, (1, 4), rings)
    layer = _vector_layer(
        "PolygonZ", name, crs, geometries, _prepend(elevation, attributes)
    )
    layer.setCustomProperty(PART_PROPERTY, POLYGONS_PART)
    return layer


def surface_mesh_layer(
    name: str,
    vertices: np.ndarray,
    cells: np.ndarray,
    vertex_data: dict[str, np.ndarray],
    face_data: dict[str, np.ndarray],
    crs: str | None,
    shown: tuple[str, DataChannel] | None = None,
) -> QgsMeshLayer:
    """Build a mesh layer for a surface with vertex data.

    The surface is written as a binary PLY file in the session temp folder and
    opened with the ``mdal`` provider, so vertices keep their real Z (3D views,
    elevation profiles and exports use it). MDAL exposes Z as the first dataset
    group, "Bed Elevation", which is the default colouring; the data channels
    follow as vertex and face datasets.

    Args:
        name: Layer name.
        vertices: (n, 3) x, y, z array.
        cells: (m, 3) vertex indices of each triangle.
        vertex_data: Dataset name to length-n numeric array.
        face_data: Dataset name to length-m numeric array.
        crs: CRS string, or None to leave it unset.
        shown: (column of ``vertex_data`` or ``face_data``, its channel) for the
            channel GEOSCIENCE ANALYST shows; that dataset is made active and
            styled from the channel's colour map. None keeps elevation.

    Returns:
        The mesh layer.

    Raises:
        RuntimeError: If QGIS cannot open the written mesh.
    """
    path = new_path(name, ".ply")
    vertex_names, face_names = _write_ply(path, vertices, cells, vertex_data, face_data)
    layer = QgsMeshLayer(str(path), name, MESH_PROVIDER)
    if not layer.isValid():
        raise RuntimeError(f"QGIS could not open the mesh written for {name!r}")
    layer.setCrs(QgsCoordinateReferenceSystem(crs or ""))  # "" -> unset CRS
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
    if shown is not None:
        column, channel = shown
        on_vertices = column in vertex_names
        names = vertex_names if on_vertices else face_names
        group = _dataset_group(layer, names[column], on_vertices)
        if group is not None:
            apply_mesh_style(layer, group, channel)
    _set_3d_height_from_dataset(layer, 0)  # 0 = elevation
    return layer


def _shown_column(data: ChannelColumns) -> tuple[str, DataChannel] | None:
    """The numeric column holding the channel GA shows, with that channel."""
    return next(
        (
            (column, source)
            for column, source in zip(data.columns, data.sources, strict=True)
            if source is not None
            and source.visible
            and data.columns[column].dtype.kind in "fiub"
        ),
        None,
    )


def _dataset_group(layer: QgsMeshLayer, ply_name: str, on_vertices: bool) -> int | None:
    """Index of the dataset group MDAL made from a PLY property."""
    provider = layer.dataProvider()
    wanted = (
        QgsMeshDatasetGroupMetadata.DataType.DataOnVertices
        if on_vertices
        else QgsMeshDatasetGroupMetadata.DataType.DataOnFaces
    )
    return next(
        (
            index
            for index in range(provider.datasetGroupCount())
            if provider.datasetGroupMetadata(index).name()
            == mesh_dataset_name(ply_name)
            and provider.datasetGroupMetadata(index).dataType() == wanted
        ),
        None,
    )


def _set_3d_height_from_dataset(layer: QgsMeshLayer, group_index: int) -> None:
    """Make QGIS 3D views take vertex heights from a dataset group.

    QgsMeshLayer3DRenderer is not exposed to Python in QGIS 4.2, so the renderer
    is built from XML through the 3D renderer registry. Skipped if the 3D
    module is unavailable.
    """
    q3d = qgis_3d()
    if q3d is None:
        return
    registry = QgsApplication.renderer3DRegistry()
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
    """Encode a name for a mesh dataset (a PLY property name).

    PLY headers separate tokens with ASCII whitespace, so spaces become
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


def select_channels(
    infos: list[DataInfo], channels: tuple[str, ...] | None
) -> list[DataInfo]:
    """Pick the channels to load from a Grid2D's raster channels.

    Args:
        infos: The grid's raster channels (reader.raster_channels()).
        channels: Data UIDs to pick, ALL_CHANNELS, or None for the channel
            GEOSCIENCE ANALYST shows (else the first).

    Returns:
        The picked channels, in file order; empty if none match.
    """
    if channels == ALL_CHANNELS:
        return list(infos)
    if channels is None:
        shown = next((i for i in infos if i.visible), None)
        return [shown or infos[0]] if infos else []
    wanted = {c.lower() for c in channels}
    return [i for i in infos if i.uid.lower() in wanted]


def channel_part(data_uid: str) -> str:
    """The part of the raster layer built from one Grid2D channel.

    Args:
        data_uid: The channel's data UID.

    Returns:
        ``"channel/<data uid>"``.
    """
    return f"{CHANNEL_PART_PREFIX}{data_uid}"


def grid2d_layer(
    name: str, frame: Grid2DFrame, channel: DataChannel, crs: str | None
) -> QgsRasterLayer:
    """Build a single-band raster layer for one channel of a horizontal Grid2D.

    Float data is written as Float32 (NaN no-data), Integer and Referenced as
    Int32 (INTEGER_NDV no-data) and Boolean as Byte. The layer is styled from
    the channel's GA colour map (see styles.apply_raster_style).

    Args:
        name: Layer name.
        frame: The grid's placement.
        channel: A cell channel (length u_count * v_count, U fastest).
        crs: CRS string, or None to leave it unset.

    Returns:
        A raster layer backed by a GeoTIFF in the session temp folder, with
        part ``"channel/<data uid>"``.

    Raises:
        ValueError: If the grid is dipping/vertical or the channel has the
            wrong number of values.
    """
    if not is_horizontal(frame):
        raise ValueError(
            f"Grid2D {name!r} has dip {frame.dip} (vertical={frame.vertical}); "
            "only horizontal grids can be shown as map rasters"
        )
    if channel.values.shape != (frame.u_count * frame.v_count,):
        raise ValueError(f"{name!r} does not have one value per grid cell")
    path = str(new_path(channel.uid.strip("{}"), ".tif"))
    _write_grid_tiff(path, frame, channel, crs)
    layer = QgsRasterLayer(path, name, "gdal")
    if crs:
        layer.setCrs(QgsCoordinateReferenceSystem(crs))
    layer.setCustomProperty(PART_PROPERTY, channel_part(channel.uid))
    apply_raster_style(layer, channel)
    return layer


def drillhole_layers(
    name: str, group: DrillholeGroupData, crs: str | None
) -> list[QgsVectorLayer]:
    """Build collar, trace and interval layers for a drillhole group.

    - "<name> collars": PointZ per hole (hole, end_of_hole, planning).
    - "<name> traces": LineStringZ per hole along its path.
    - "<name> <table>": LineStringZ per interval of each interval table, following
      the hole between its from and to depths, with the table's channels.
    - "<name> <table>": PointZ per sample of each depth table, on the hole at
      its depth, with the table's channels.

    Args:
        name: Drillhole group name.
        group: Holes and tables from drillholes.read_drillholes().
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
    layers = (
        [collars, traces]
        + [
            _interval_layer(f"{name} {table.name}", holes, table, crs)
            for table in group.intervals
        ]
        + [
            _depth_layer(f"{name} {table.name}", holes, table, crs)
            for table in group.depths
        ]
    )
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
    data = channel_columns(list(table.channels))
    layer = _vector_layer(
        "LineStringZ", name, crs, geometries, _prepend(fixed, data.columns)
    )
    _finish(layer, data, None)  # typed fields, unit aliases, GA styling
    layer.setCustomProperty(ROLE_PROPERTY, "intervals")
    layer.setCustomProperty(PART_PROPERTY, f"intervals/{table.name}")
    return layer


def _depth_layer(
    name: str, holes: tuple, table: DepthTable, crs: str | None
) -> QgsVectorLayer:
    xyz = np.full((len(table.depth), 3), np.nan)
    for index in np.unique(table.hole_index):
        rows = table.hole_index == index
        hole = holes[index]
        xyz[rows] = positions_at(hole.trace, hole.trace_depth, table.depth[rows])
    fixed = {
        "hole": np.array([holes[i].name for i in table.hole_index], dtype=object),
        "depth": table.depth,
    }
    data = channel_columns(list(table.channels))
    layer = points_layer(name, xyz, _prepend(fixed, data.columns), crs)
    _finish(layer, data, None)
    layer.setCustomProperty(ROLE_PROPERTY, "depths")
    layer.setCustomProperty(PART_PROPERTY, f"depths/{table.name}")
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

    The embedded image file is written to the session temp folder and wrapped
    in a VRT that carries the corner-derived geotransform and CRS, so GDAL
    decodes the original JPEG/PNG/TIFF on demand.

    Args:
        name: Layer name.
        uid: Object UID, used to name the in-memory files.
        corners: (4, 3) corners: top-left, top-right, bottom-right, bottom-left.
        image_bytes: The embedded image file's bytes.
        suffix: File extension of the embedded image (e.g. ``.jpg``).
        crs: CRS string, or None to leave it unset.

    Returns:
        A raster layer backed by a VRT in the session temp folder.

    Raises:
        RuntimeError: If GDAL cannot decode the image.
    """
    image_path = new_path(uid.strip("{}"), suffix)
    vrt_path = str(image_path.with_suffix(".vrt"))
    image_path.write_bytes(image_bytes)
    with gdal.ExceptionMgr(useExceptions=True):
        source = gdal.Open(str(image_path))
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
    build_overviews(vrt_path)
    layer = QgsRasterLayer(vrt_path, name, "gdal")
    if crs:
        layer.setCrs(QgsCoordinateReferenceSystem(crs))
    return layer


def build_overviews(raster_path: str) -> list[int]:
    """Give a large image overviews so QGIS draws it quickly when zoomed out.

    Images whose longest side is over OVERVIEW_MIN_SIZE pixels and that have no
    overviews of their own (JPEGs do) get an external ``<file>.ovr``. Images with
    a colour table are reduced with nearest-neighbour sampling, others averaged.

    Args:
        raster_path: A raster file (e.g. a GeoImage's VRT).

    Returns:
        The overview reduction factors built; empty if none were needed.
    """
    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(raster_path)
        band = dataset.GetRasterBand(1)
        size = max(dataset.RasterXSize, dataset.RasterYSize)
        if size <= OVERVIEW_MIN_SIZE or band.GetOverviewCount() > 0:
            return []
        levels = overview_levels(size)
        resampling = "NEAREST" if band.GetColorTable() is not None else "AVERAGE"
        with gdal.config_options({"COMPRESS_OVERVIEW": "DEFLATE"}):
            dataset.BuildOverviews(resampling, levels)
        del band, dataset
    return levels


def overview_levels(size: int) -> list[int]:
    """Overview reduction factors (2, 4, 8...) down to OVERVIEW_SMALLEST pixels.

    Args:
        size: The image's longest side in pixels.

    Returns:
        Factors whose overview's longest side is at least OVERVIEW_SMALLEST.
    """
    return list(
        itertools.takewhile(
            lambda factor: size / factor >= OVERVIEW_SMALLEST,
            (2**k for k in itertools.count(1)),
        )
    )


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


# numpy dtype kind to QGIS field type; anything else becomes a string field.
FIELD_TYPES = {
    "f": QMetaType.Type.Double,
    "i": QMetaType.Type.LongLong,
    "u": QMetaType.Type.LongLong,
    "b": QMetaType.Type.Bool,
    "M": QMetaType.Type.QDateTime,
}


def _field_type(values: np.ndarray) -> QMetaType.Type:
    return FIELD_TYPES.get(values.dtype.kind, QMetaType.Type.QString)


def _python_values(values: np.ndarray) -> list:
    """Convert a column to Python values, with no-data as None (QGIS NULL)."""
    if values.dtype.kind == "f":
        return [None if np.isnan(v) else v for v in values.tolist()]
    if values.dtype.kind in "iu":
        return [None if v == INTEGER_NDV else v for v in values.tolist()]
    if values.dtype.kind == "M":
        # The memory provider rejects Python datetimes; NaT comes back as None.
        moments = values.astype("datetime64[ms]").tolist()
        return [None if m is None else QDateTime(m) for m in moments]
    return values.tolist()


def _write_grid_tiff(
    path: str, frame: Grid2DFrame, channel: DataChannel, crs: str | None
) -> None:
    data_type, no_data = _GRID_BAND_TYPES.get(
        channel.primitive_type, (gdal.GDT_Int32, INTEGER_NDV)
    )
    with gdal.ExceptionMgr(useExceptions=True):
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(path, frame.u_count, frame.v_count, 1, data_type)
        dataset.SetGeoTransform(grid2d_geotransform(frame))
        if crs:
            srs = osr.SpatialReference()
            srs.SetFromUserInput(crs)
            dataset.SetProjection(srs.ExportToWkt())
        band = dataset.GetRasterBand(1)
        band.WriteArray(_grid_rows(channel.values, frame))
        if no_data is not None:
            band.SetNoDataValue(no_data)
        band.SetDescription(channel.name)
        dataset.FlushCache()
        del dataset


# GDAL band type and no-data value per primitive type (Integer/Referenced: else).
_GRID_BAND_TYPES = {
    "Float": (gdal.GDT_Float32, float("nan")),
    "Boolean": (gdal.GDT_Byte, None),
}


def _grid_rows(values: np.ndarray, frame: Grid2DFrame) -> np.ndarray:
    """Reshape U-fastest cell values to raster rows, top (far-V) row first."""
    rows = values.reshape(frame.v_count, frame.u_count)[::-1]
    return rows.astype(np.uint8) if rows.dtype == bool else rows


def _as_float(values: np.ndarray) -> np.ndarray:
    """Numeric values as float64, with integer no-data as NaN."""
    result = values.astype(np.float64)
    if values.dtype.kind in "iu":
        result[values == INTEGER_NDV] = np.nan
    return result


def _numeric(columns: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Keep number and boolean columns; drop text, labels and dates."""
    return {k: v for k, v in columns.items() if v.dtype.kind in "fiub"}


def _unique(name: str, taken: set[str]) -> str:
    candidates = (name if i == 1 else f"{name}_{i}" for i in itertools.count(1))
    unique = next(c for c in candidates if c not in taken)
    taken.add(unique)
    return unique


def _write_ply(
    path: Path,
    vertices: np.ndarray,
    cells: np.ndarray,
    vertex_data: dict[str, np.ndarray],
    face_data: dict[str, np.ndarray],
) -> tuple[dict[str, str], dict[str, str]]:
    """Write a triangle mesh with vertex/face properties as binary PLY.

    MDAL reads vertex properties other than x, y, z as vertex datasets and face
    properties as face datasets. Property names are whitespace-separated in
    the header, so they go through mesh_dataset_name(); channels named like the
    coordinates get a suffix so they cannot replace them.

    Returns:
        The property name each vertex and each face column was written as.
    """
    taken = {"x", "y", "z"}
    vertex_names = {k: _unique(k, taken) for k in vertex_data}
    face_taken: set[str] = set()
    face_names = {k: _unique(k, face_taken) for k in face_data}
    vertex_columns = {"x": vertices[:, 0], "y": vertices[:, 1], "z": vertices[:, 2]}
    vertex_columns |= {vertex_names[k]: _as_float(v) for k, v in vertex_data.items()}
    face_columns = {face_names[k]: _as_float(v) for k, v in face_data.items()}
    header = [
        "ply",
        "format binary_little_endian 1.0",
        f"element vertex {len(vertices)}",
        *(f"property double {mesh_dataset_name(n)}" for n in vertex_columns),
        f"element face {len(cells)}",
        "property list uchar int vertex_indices",
        *(f"property double {mesh_dataset_name(n)}" for n in face_columns),
        "end_header",
    ]
    vertex_records = np.empty(
        len(vertices), [(f"v{i}", "<f8") for i in range(len(vertex_columns))]
    )
    for i, values in enumerate(vertex_columns.values()):
        vertex_records[f"v{i}"] = values
    face_records = np.empty(
        len(cells),
        [("count", "u1"), ("indices", "<i4", (3,))]
        + [(f"f{i}", "<f8") for i in range(len(face_columns))],
    )
    face_records["count"], face_records["indices"] = 3, cells
    for i, values in enumerate(face_columns.values()):
        face_records[f"f{i}"] = values
    with open(path, "wb") as file:
        file.write(("\n".join(header) + "\n").encode("utf-8"))
        file.write(vertex_records.tobytes())
        file.write(face_records.tobytes())
    return vertex_names, face_names
