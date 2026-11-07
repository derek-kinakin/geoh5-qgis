"""Read geoh5 (GEOSCIENCE ANALYST) files with GDAL's multidimensional API.

Uses only ``osgeo.gdal`` and numpy, both shipped with QGIS, so the plugin needs no
extra installs. Nothing here imports PyQGIS, so it can be tested on its own.

Layout used (geoh5 v2): one project group (normally ``GEOSCIENCE``) holding
``Objects/{uid}``, ``Data/{uid}`` and ``Types/...``. Each object group has a
``Type`` subgroup whose ``ID`` attribute identifies the object kind.
"""

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from osgeo import gdal

Workspace = gdal.Group  # the project group yielded by open_workspace()

POINTS = "Points"
CURVE = "Curve"
SURFACE = "Surface"
GRID2D = "Grid2D"
GEOIMAGE = "GeoImage"
DRILLHOLES = "Drillholes"
UNSUPPORTED = "Unsupported"
# Array marking a drillhole group whose holes are in concatenated storage.
CONCATENATED_IDS = "Concatenated object IDs"
GROUP = "Group"
# RootGroup shares the NoType group type UID (geoh5py groups/notype.py).
NO_TYPE_GROUP_UID = "{dd99b610-be92-48c0-873c-5b5946ea2840}"

OBJECT_KINDS = {
    "{202c5db1-a56d-4004-9cad-baafd8899406}": POINTS,
    "{6a057fdc-b355-11e3-95be-fd84a7ffcb88}": CURVE,
    "{f26feba3-aded-494b-b9e9-b2bbcbe298e1}": SURFACE,
    "{48f5054a-1c5c-4ca4-9048-80f36dc60a06}": GRID2D,
    "{77ac043c-fe8d-4d14-8167-75e300fb835a}": GEOIMAGE,
}
GEOIMAGE_DATA_NAME = "GeoImageMesh_Image"
# Object-level Text data holding GA's display settings (XML).
VISUAL_PARAMETERS_NAME = "Visual Parameters"

# No-data sentinels used by GEOSCIENCE ANALYST / geoh5py (geoh5py/shared/__init__.py).
FLOAT_NDV = 1.17549435e-38
INTEGER_NDV = -2147483648

# Data primitive types that map to attribute columns.
ATTRIBUTE_PRIMITIVES = frozenset(
    {"Float", "Integer", "Referenced", "Text", "Boolean", "DateTime"}
)
# Primitive types that become numeric raster bands / mesh datasets.
NUMERIC_PRIMITIVES = frozenset({"Float", "Integer", "Referenced", "Boolean"})
# Fields of a data type's "Color map" compound, in column order.
COLOR_MAP_FIELDS = ("Value", "Red", "Green", "Blue", "Alpha")
# Grids with |dip| below this (degrees) count as horizontal (absorbs float noise).
DIP_TOLERANCE = 1e-6
# GeoImage corner checks, relative to the image's largest horizontal extent.
CORNER_TOLERANCE = 1e-6

_COMPOUND_FORMATS = {
    gdal.GDT_Float64: "<f8",
    gdal.GDT_Float32: "<f4",
    gdal.GDT_Int32: "<i4",
    gdal.GDT_UInt32: "<u4",
}


@dataclass(frozen=True)
class ObjectInfo:
    """An object stored in a geoh5 file."""

    uid: str
    name: str
    kind: str
    type_uid: str


@dataclass(frozen=True)
class GeoImageInfo:
    """A GeoImage: four corners (TL, TR, BR, BL) and where its image file is."""

    corners: np.ndarray
    blob_path: str | None
    file_name: str | None


@dataclass(frozen=True)
class TreeNode:
    """A group or object in the file's tree; ``kind`` is GROUP for groups."""

    uid: str
    name: str
    kind: str
    children: tuple["TreeNode", ...] = ()


@dataclass(frozen=True)
class DataInfo:
    """A data channel attached to an object."""

    uid: str
    name: str
    association: str
    primitive_type: str
    # The channel GEOSCIENCE ANALYST was colouring the object by.
    visible: bool = False


@dataclass(frozen=True)
class DataChannel:
    """A data channel's values and the display hints stored on its data type.

    ``values`` by primitive type: Float as float64 with NaN for no-data; Integer
    and Referenced as stored (INTEGER_NDV marks integer no-data); Boolean as
    bool; Text as str objects; DateTime as datetime64[ms] with NaT for no-data.
    """

    uid: str
    name: str
    association: str
    primitive_type: str
    values: np.ndarray
    units: str | None = None
    # Key to name, for Referenced data only.
    value_map: dict[int, str] | None = None
    # (n, 5) rows of value, red, green, blue, alpha (colours 0-255). Values are
    # data values (GA's linear/equal-area mapping is already applied) or, for
    # Referenced and Boolean data, keys.
    color_map: np.ndarray | None = None
    visible: bool = False


@dataclass(frozen=True)
class Grid2DFrame:
    """Placement of a Grid2D: corner origin, cell sizes/counts and orientation."""

    origin: tuple[float, float, float]
    u_size: float
    v_size: float
    u_count: int
    v_count: int
    rotation: float
    dip: float
    vertical: bool


@contextmanager
def open_workspace(path: str | Path) -> Iterator[Workspace]:
    """Open a geoh5 file read-only and yield its project group.

    The file is released when the block exits, so other applications (e.g.
    GEOSCIENCE ANALYST) are not blocked longer than necessary.

    Args:
        path: Path to a .geoh5 file.

    Yields:
        The project group, to pass to the other reader functions.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file does not contain exactly one project group.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"geoh5 file not found: {path}")
    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.OpenEx(str(path), gdal.OF_MULTIDIM_RASTER | gdal.OF_READONLY)
        root = dataset.GetRootGroup()
        names = root.GetGroupNames() or []
        if len(names) != 1:
            raise ValueError(f"{path}: expected one geoh5 project group, got {names}")
        try:
            yield root.OpenGroup(names[0])
        finally:
            del root
            del dataset


def list_objects(workspace: Workspace) -> list[ObjectInfo]:
    """List every object in the file (flat; group hierarchy not yet resolved).

    Args:
        workspace: Project group from open_workspace().

    Returns:
        One ObjectInfo per object. Kinds outside the supported set are
        reported as UNSUPPORTED.
    """
    if "Objects" not in _groups(workspace):
        return []  # e.g. files holding only (drillhole) groups
    objects = workspace.OpenGroup("Objects")
    return [_object_info(uid, objects.OpenGroup(uid)) for uid in _groups(objects)]


def read_vertices(workspace: Workspace, object_uid: str) -> np.ndarray:
    """Read an object's vertices.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name, e.g. ``{2393...}``.

    Returns:
        An (n, 3) float64 array of x, y, z.

    Raises:
        KeyError: If the object or its Vertices dataset does not exist.
    """
    vertices = _array(_object(workspace, object_uid), "Vertices").ReadAsArray()
    return np.column_stack([vertices["x"], vertices["y"], vertices["z"]]).astype(
        np.float64
    )


def read_cells(workspace: Workspace, object_uid: str) -> np.ndarray:
    """Read an object's cells (Curve segments or Surface triangles).

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.

    Returns:
        An (n, 2) array for curves or (n, 3) for surfaces, as int64 vertex indices.

    Raises:
        KeyError: If the object or its Cells dataset does not exist.
    """
    cells = _array(_object(workspace, object_uid), "Cells").ReadAsArray()
    return cells.astype(np.int64)


def read_grid2d_frame(workspace: Workspace, object_uid: str) -> Grid2DFrame:
    """Read the placement of a Grid2D object.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name of a Grid2D.

    Returns:
        The grid's frame. Origin is the grid corner; data values are cell-centred.

    Raises:
        KeyError: If the object does not exist.
    """
    obj = _object(workspace, object_uid)
    attrs = _attrs(obj)
    return Grid2DFrame(
        origin=_read_compound_attr(obj, "Origin"),
        u_size=float(attrs["U Size"]),
        v_size=float(attrs["V Size"]),
        u_count=int(attrs["U Count"]),
        v_count=int(attrs["V Count"]),
        rotation=float(attrs.get("Rotation", 0.0)),
        dip=float(attrs.get("Dip", 0.0)) + 0.0,  # + 0.0 normalises -0.0
        vertical=bool(attrs.get("Vertical", 0)),
    )


def list_data(workspace: Workspace, object_uid: str) -> list[DataInfo]:
    """List the data channels of an object that store values.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.

    Returns:
        One DataInfo per channel with a ``Data`` array. Channels computed on the
        fly by GEOSCIENCE ANALYST (no stored values) are skipped.

    Raises:
        KeyError: If the object does not exist.
    """
    obj = _object(workspace, object_uid)
    if "Data" not in _groups(obj):
        return []
    data_root = obj.OpenGroup("Data")
    infos = [_data_info(uid, data_root.OpenGroup(uid)) for uid in _groups(data_root)]
    return [info for info in infos if info is not None]


def read_data_values(workspace: Workspace, data_uid: str) -> np.ndarray:
    """Read a data channel's values.

    Float no-data values (FLOAT_NDV) become NaN and are returned as float64.
    Integer and referenced values are returned unchanged; callers should treat
    INTEGER_NDV as missing. Text values are returned as an object array of str.

    Args:
        workspace: Project group from open_workspace().
        data_uid: Data group name.

    Returns:
        A 1-D array of values.

    Raises:
        KeyError: If the data channel does not exist or stores no values.
    """
    array = _array(_data(workspace, data_uid), "Data")
    if array.GetDataType().GetClass() == gdal.GEDTC_STRING:
        # GDAL's ReadAsArray() cannot return strings; Read() gives a list.
        return np.array(array.Read(), dtype=object)
    values = array.ReadAsArray()
    if values.dtype.kind != "f":
        return values
    values = values.astype(np.float64)
    values[np.isclose(values, FLOAT_NDV, rtol=1e-6, atol=0.0)] = np.nan
    return values


def read_value_map(workspace: Workspace, data_uid: str) -> dict[int, str] | None:
    """Read the key-to-name lookup of referenced (categorical) data.

    Args:
        workspace: Project group from open_workspace().
        data_uid: Data group name.

    Returns:
        A dict of key to name, or None if the data type has no value map.

    Raises:
        KeyError: If the data channel does not exist.
    """
    data_type = _data(workspace, data_uid).OpenGroup("Type")
    if "Value map" not in (data_type.GetMDArrayNames() or []):
        return None
    value_map = data_type.OpenMDArray("Value map")
    # The compound mixes u4 and string fields, which GDAL's Python bindings can
    # only read one field at a time.
    keys = value_map.GetView('["Key"]').ReadAsArray()
    names = value_map.GetView('["Value"]').Read()
    return {int(k): str(n) for k, n in zip(keys, names, strict=True)}


def read_channels(
    workspace: Workspace, object_uid: str, association: str, length: int
) -> list[DataChannel]:
    """Read an object's data channels that can become attribute columns.

    Channels with an unsupported primitive type or the wrong length are skipped.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.
        association: Which channels to read, e.g. ``"Vertex"`` or ``"Cell"``.
        length: Expected number of values (vertices or cells).

    Returns:
        The channels, in file order.

    Raises:
        KeyError: If the object does not exist.
    """
    infos = [
        info
        for info in list_data(workspace, object_uid)
        if info.association == association
        and info.primitive_type in ATTRIBUTE_PRIMITIVES
    ]
    channels = [read_channel(workspace, info) for info in infos]
    return [c for c in channels if c.values.shape == (length,)]


def raster_channels(workspace: Workspace, object_uid: str) -> list[DataInfo]:
    """List the cell channels of a Grid2D that can become raster layers.

    Reads only attributes, never values.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name of a Grid2D.

    Returns:
        The numeric (Float, Integer, Referenced, Boolean) cell channels, in file
        order.

    Raises:
        KeyError: If the object does not exist.
    """
    return [
        info
        for info in list_data(workspace, object_uid)
        if info.association == "Cell" and info.primitive_type in NUMERIC_PRIMITIVES
    ]


def read_channel(workspace: Workspace, info: DataInfo) -> DataChannel:
    """Read one data channel with its units, value map and colour map.

    Args:
        workspace: Project group from open_workspace().
        info: The channel, from list_data().

    Returns:
        The channel (see DataChannel for how values are typed).

    Raises:
        KeyError: If the data channel does not exist or stores no values.
    """
    data = _data(workspace, info.uid)
    data_type = data.OpenGroup("Type") if "Type" in _groups(data) else None
    units = str(_attrs(data_type).get("Units") or "") if data_type else ""
    referenced = info.primitive_type == "Referenced"
    return DataChannel(
        uid=info.uid,
        name=info.name,
        association=info.association,
        primitive_type=info.primitive_type,
        values=_typed_values(read_data_values(workspace, info.uid), info),
        units=units or None,
        value_map=read_value_map(workspace, info.uid) if referenced else None,
        color_map=_read_color_map(data_type) if data_type else None,
        visible=info.visible,
    )


def read_object_colour(
    workspace: Workspace, object_uid: str
) -> tuple[int, int, int, int] | None:
    """Read the colour GEOSCIENCE ANALYST draws an object in.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.

    Returns:
        Red, green, blue, alpha (0-255) from the object's "Visual Parameters",
        or None if it has none or they cannot be parsed.

    Raises:
        KeyError: If the object does not exist.
    """
    info = next(
        (
            d
            for d in list_data(workspace, object_uid)
            if d.name == VISUAL_PARAMETERS_NAME and d.association == "Object"
        ),
        None,
    )
    if info is None:
        return None
    values = read_data_values(workspace, info.uid)
    try:
        colour = ET.fromstring(str(values[0])).findtext("Colour")
        argb = int(colour or "")
    except (ET.ParseError, ValueError, IndexError):
        return None
    return (argb >> 16) & 255, (argb >> 8) & 255, argb & 255, (argb >> 24) & 255


def read_tree(workspace: Workspace) -> list[TreeNode]:
    """Read the group/object tree as GEOSCIENCE ANALYST shows it.

    The root group's children are returned (the root itself is not). The root
    is the indexed copy of ``/Root`` when present. Without ``/Root`` the file
    is read as a forest of the indexed entities no group links to; a single
    top-level NoType group is then taken to be the root. Entities only in the
    flat index and not reachable from the root (e.g. deleted in GA) are left
    out. The walk is lenient: each entity appears once, so repeated links and
    cycles are skipped. Groups come before objects, each sorted by name.

    Args:
        workspace: Project group from open_workspace().

    Returns:
        Top-level tree nodes.
    """
    seen: set[str] = set()
    if "Root" in _groups(workspace):
        root = workspace.OpenGroup("Root")
        root_uid = str(_attrs(root).get("ID", "")).lower()
        indexed = _indexed(workspace, "Groups", root_uid)
        seen.add(root_uid)
        return _child_nodes(workspace, indexed or root, seen)
    tops = _forest_tops(workspace)
    if len(tops) == 1 and tops[0][0] == "Groups":
        family, uid = tops[0]
        group = _indexed(workspace, family, uid)
        if _type_uid(group) == NO_TYPE_GROUP_UID:
            seen.add(uid)
            return _child_nodes(workspace, group, seen)
    nodes = [
        _node(workspace, f, uid, _indexed(workspace, f, uid), seen) for f, uid in tops
    ]
    return _sorted_nodes([n for n in nodes if n is not None])


def tree_objects(nodes: list[TreeNode] | tuple[TreeNode, ...]) -> list[TreeNode]:
    """All object nodes in a tree, depth first in display order.

    Args:
        nodes: Nodes from read_tree() (or a group's children).

    Returns:
        The object (non-group) nodes.
    """
    return [
        found
        for node in nodes
        for found in (tree_objects(node.children) if node.kind == GROUP else [node])
    ]


def read_geoimage(workspace: Workspace, object_uid: str) -> GeoImageInfo:
    """Read a GeoImage's corners and locate its embedded image file.

    The image bytes are an opaque HDF5 dataset that GDAL cannot read; pass
    ``blob_path`` to hdf5_blob.read_blob().

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name of a GeoImage.

    Returns:
        Corners as a (4, 3) array (top-left, top-right, bottom-right,
        bottom-left) and the image dataset's HDF5 path and file name, which
        are None if the object has no image.

    Raises:
        KeyError: If the object or its vertices do not exist.
    """
    corners = read_vertices(workspace, object_uid)
    image = next(
        (
            d
            for d in list_data(workspace, object_uid)
            if d.name == GEOIMAGE_DATA_NAME and d.primitive_type == "Filename"
        ),
        None,
    )
    if image is None:
        return GeoImageInfo(corners, None, None)
    data = _data(workspace, image.uid)
    names = _array(data, "Data").Read()
    file_name = str(names[0] if isinstance(names, list) else names)
    if file_name not in (data.GetMDArrayNames() or []):
        return GeoImageInfo(corners, None, file_name)
    blob_path = f"{workspace.GetFullName()}/Data/{image.uid}/{file_name}"
    return GeoImageInfo(corners, blob_path, file_name)


def is_horizontal(frame: Grid2DFrame) -> bool:
    """Whether a Grid2D lies flat, so it can be shown as a map raster.

    Args:
        frame: The grid's placement.

    Returns:
        True if not vertical and |dip| is within DIP_TOLERANCE degrees.
    """
    return not frame.vertical and abs(frame.dip) < DIP_TOLERANCE


def load_issue(workspace: Workspace, info: ObjectInfo) -> str | None:
    """Explain why an object cannot be loaded as a layer, if it cannot.

    Reads only attributes and the data list, never data values, so it is cheap
    enough to call for every object when listing a file.

    Args:
        workspace: Project group from open_workspace().
        info: The object, from list_objects().

    Returns:
        A short user-facing reason, or None if the object can be loaded.
    """
    if info.kind == UNSUPPORTED:
        return "object type not supported yet"
    if info.kind == GEOIMAGE:
        return _geoimage_issue(read_geoimage(workspace, info.uid))
    if info.kind == DRILLHOLES:
        group = _child_group(_child_group(workspace, "Groups"), info.uid)
        ids = _array(group, CONCATENATED_IDS).Read()
        return None if ids else "drillhole group has no holes"
    if info.kind != GRID2D:
        return None
    frame = read_grid2d_frame(workspace, info.uid)
    if not is_horizontal(frame):
        orientation = "vertical" if frame.vertical else f"dipping ({frame.dip:g}°)"
        return f"{orientation} Grid2D: only horizontal grids can be shown on the map"
    data = list_data(workspace, info.uid)
    if not any(
        d.association == "Cell" and d.primitive_type in NUMERIC_PRIMITIVES for d in data
    ):
        return "Grid2D has no numeric cell data to display"
    return None


def find_object(workspace: Workspace, uid: str) -> ObjectInfo | None:
    """Find a loadable entity by UID: an object, or a drillhole group.

    Args:
        workspace: Project group from open_workspace().
        uid: Object or group name.

    Returns:
        Its ObjectInfo (kind DRILLHOLES for a drillhole group), or None.
    """
    found = next((o for o in list_objects(workspace) if o.uid == uid), None)
    if found is not None:
        return found
    group = _indexed(workspace, "Groups", uid)
    if group is None or not _is_drillhole_group(group):
        return None
    return ObjectInfo(
        uid, str(_attrs(group).get("Name", "")), DRILLHOLES, _type_uid(group)
    )


def read_crs(workspace: Workspace, object_uid: str) -> str | None:
    """Read an object's coordinate reference system code from its metadata.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.

    Returns:
        A code such as ``"EPSG:26914"``, or None if absent or "Unknown".

    Raises:
        KeyError: If the object (or group) does not exist.
    """
    obj = _entity(workspace, object_uid)
    if "Metadata" not in (obj.GetMDArrayNames() or []):
        return None
    raw = obj.OpenMDArray("Metadata").Read()
    text = raw[0] if isinstance(raw, list) else raw
    try:
        metadata = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return None
    crs = metadata.get("Coordinate Reference System", {}).get("Current", {})
    code = crs.get("Code") if isinstance(crs, dict) else None
    return code if code and code.lower() != "unknown" else None


def _geoimage_issue(image: GeoImageInfo) -> str | None:
    corners = image.corners
    if image.blob_path is None:
        return "GeoImage has no embedded image"
    if corners.shape != (4, 3):
        return f"GeoImage has {len(corners)} corners, expected 4"
    size = float(np.ptp(corners[:, :2], axis=0).max()) or 1.0
    if np.ptp(corners[:, 2]) > CORNER_TOLERANCE * size:
        return "tilted GeoImage: only horizontal images can be shown on the map"
    top_left, top_right, bottom_right, bottom_left = corners[:, :2]
    if not np.allclose(
        bottom_right, top_right + bottom_left - top_left, atol=CORNER_TOLERANCE * size
    ):
        return "GeoImage corners do not form a parallelogram (warped image)"
    return None


def _child_nodes(
    workspace: Workspace, parent: gdal.Group, seen: set[str]
) -> tuple[TreeNode, ...]:
    nodes = [
        _node(
            workspace, family, key, _link_target(workspace, parent, family, key), seen
        )
        for family in ("Groups", "Objects")
        if family in _groups(parent)
        for key in _groups(parent.OpenGroup(family))
    ]
    return _sorted_nodes([n for n in nodes if n is not None])


def _node(
    workspace: Workspace,
    family: str,
    key: str,
    entity: gdal.Group | None,
    seen: set[str],
) -> TreeNode | None:
    uid = key.lower()
    if entity is None or uid in seen:
        return None
    seen.add(uid)
    if family == "Groups":
        name = str(_attrs(entity).get("Name", ""))
        if _is_drillhole_group(entity):
            # Holes live in concatenated storage: the group loads as one item.
            return TreeNode(key, name, DRILLHOLES)
        return TreeNode(key, name, GROUP, _child_nodes(workspace, entity, seen))
    info = _object_info(key, entity)
    return TreeNode(key, info.name, info.kind)


def _link_target(
    workspace: Workspace, parent: gdal.Group, family: str, key: str
) -> gdal.Group | None:
    """The indexed entity for a child link, else the linked group itself."""
    indexed = _indexed(workspace, family, key)
    if indexed is not None:
        return indexed
    try:
        return parent.OpenGroup(family).OpenGroup(key)
    except RuntimeError:
        return None


def _indexed(workspace: Workspace, family: str, key: str) -> gdal.Group | None:
    if family not in _groups(workspace):
        return None
    index = workspace.OpenGroup(family)
    match = next((k for k in _groups(index) if k.lower() == key.lower()), None)
    return index.OpenGroup(match) if match else None


def _forest_tops(workspace: Workspace) -> list[tuple[str, str]]:
    """(family, key) of indexed entities that no indexed group links to."""
    families = [f for f in ("Groups", "Objects") if f in _groups(workspace)]
    entities = [(f, k) for f in families for k in _groups(workspace.OpenGroup(f))]
    linked = {
        child.lower()
        for family, key in entities
        if family == "Groups"
        for child_family in ("Groups", "Objects")
        for child in _child_keys(
            workspace.OpenGroup(family).OpenGroup(key), child_family
        )
    }
    return [(f, k) for f, k in entities if k.lower() not in linked]


def _child_keys(group: gdal.Group, family: str) -> list[str]:
    return _groups(group.OpenGroup(family)) if family in _groups(group) else []


def _type_uid(entity: gdal.Group | None) -> str:
    if entity is None or "Type" not in _groups(entity):
        return ""
    return str(_attrs(entity.OpenGroup("Type")).get("ID", "")).lower()


def _sorted_nodes(nodes: list[TreeNode]) -> tuple[TreeNode, ...]:
    return tuple(sorted(nodes, key=lambda n: (n.kind != GROUP, n.name.lower(), n.uid)))


def _typed_values(values: np.ndarray, info: DataInfo) -> np.ndarray:
    if info.primitive_type == "Boolean":
        return values.astype(bool)
    if info.primitive_type == "DateTime":
        return np.array([_parse_datetime(v) for v in values], dtype="datetime64[ms]")
    return values


def _parse_datetime(text: object) -> np.datetime64:
    """ISO 8601 text as naive UTC; NaT if empty or not a date."""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return np.datetime64("NaT", "ms")
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return np.datetime64(moment, "ms")


def _read_color_map(data_type: gdal.Group) -> np.ndarray | None:
    if "Color map" not in (data_type.GetMDArrayNames() or []):
        return None
    color_map = data_type.OpenMDArray("Color map")
    columns = [color_map.GetView(f'["{f}"]').ReadAsArray() for f in COLOR_MAP_FIELDS]
    return np.column_stack(columns).astype(np.float64)


def _object_info(uid: str, obj: gdal.Group) -> ObjectInfo:
    type_uid = str(_attrs(obj.OpenGroup("Type")).get("ID", "")).lower()
    return ObjectInfo(
        uid=uid,
        name=str(_attrs(obj).get("Name", "")),
        kind=OBJECT_KINDS.get(type_uid, UNSUPPORTED),
        type_uid=type_uid,
    )


def _data_info(uid: str, data: gdal.Group) -> DataInfo | None:
    if "Data" not in (data.GetMDArrayNames() or []):
        return None
    type_attrs = _attrs(data.OpenGroup("Type")) if "Type" in _groups(data) else {}
    attrs = _attrs(data)
    return DataInfo(
        uid=uid,
        name=str(attrs.get("Name", "")),
        association=str(attrs.get("Association", "")),
        primitive_type=str(type_attrs.get("Primitive type", "")),
        visible=bool(attrs.get("Visible", 0)),
    )


def _object(workspace: Workspace, uid: str) -> gdal.Group:
    return _child_group(_child_group(workspace, "Objects"), uid)


def _entity(workspace: Workspace, uid: str) -> gdal.Group:
    """An object, or failing that a group, by UID."""
    group = _indexed(workspace, "Objects", uid) or _indexed(workspace, "Groups", uid)
    if group is None:
        raise KeyError(f"No object or group {uid!r}")
    return group


def _is_drillhole_group(group: gdal.Group) -> bool:
    return CONCATENATED_IDS in (group.GetMDArrayNames() or [])


def _data(workspace: Workspace, uid: str) -> gdal.Group:
    return _child_group(_child_group(workspace, "Data"), uid)


def _child_group(parent: gdal.Group, name: str) -> gdal.Group:
    if name not in _groups(parent):
        raise KeyError(f"No group {name!r} in {parent.GetFullName()}")
    return parent.OpenGroup(name)


def _array(group: gdal.Group, name: str) -> gdal.MDArray:
    if name not in (group.GetMDArrayNames() or []):
        raise KeyError(f"No dataset {name!r} in {group.GetFullName()}")
    return group.OpenMDArray(name)


def _groups(group: gdal.Group) -> list[str]:
    return group.GetGroupNames() or []


def _attrs(item: gdal.Group | gdal.MDArray) -> dict[str, object]:
    return {attr.GetName(): attr.Read() for attr in item.GetAttributes() or []}


def _read_compound_attr(item: gdal.Group, name: str) -> tuple[float, ...]:
    """Decode a numeric compound attribute, which GDAL returns as raw bytes."""
    attr = item.GetAttribute(name)
    if attr is None:
        raise KeyError(f"No attribute {name!r} on {item.GetFullName()}")
    data_type = attr.GetDataType()
    components = data_type.GetComponents()
    dtype = np.dtype(
        {
            "names": [c.GetName() for c in components],
            "formats": [
                _COMPOUND_FORMATS[c.GetType().GetNumericDataType()] for c in components
            ],
            "offsets": [c.GetOffset() for c in components],
            "itemsize": data_type.GetSize(),
        }
    )
    record = np.frombuffer(bytes(attr.Read()), dtype=dtype)[0]
    return tuple(float(value) for value in record)
