"""Read geoh5 (GEOSCIENCE ANALYST) files with GDAL's multidimensional API.

Uses only ``osgeo.gdal`` and numpy, both shipped with QGIS, so the plugin needs no
extra installs. Nothing here imports PyQGIS, so it can be tested on its own.

Layout used (geoh5 v2): one project group (normally ``GEOSCIENCE``) holding
``Objects/{uid}``, ``Data/{uid}`` and ``Types/...``. Each object group has a
``Type`` subgroup whose ``ID`` attribute identifies the object kind.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from osgeo import gdal

Workspace = gdal.Group  # the project group yielded by open_workspace()

POINTS = "Points"
CURVE = "Curve"
SURFACE = "Surface"
GRID2D = "Grid2D"
UNSUPPORTED = "Unsupported"

OBJECT_KINDS = {
    "{202c5db1-a56d-4004-9cad-baafd8899406}": POINTS,
    "{6a057fdc-b355-11e3-95be-fd84a7ffcb88}": CURVE,
    "{f26feba3-aded-494b-b9e9-b2bbcbe298e1}": SURFACE,
    "{48f5054a-1c5c-4ca4-9048-80f36dc60a06}": GRID2D,
}

# No-data sentinels used by GEOSCIENCE ANALYST / geoh5py (geoh5py/shared/__init__.py).
FLOAT_NDV = 1.17549435e-38
INTEGER_NDV = -2147483648

# Data primitive types that map to attribute columns.
ATTRIBUTE_PRIMITIVES = frozenset({"Float", "Integer", "Referenced", "Text", "Boolean"})
# Primitive types that become numeric raster bands / mesh datasets.
NUMERIC_PRIMITIVES = frozenset({"Float", "Integer", "Boolean"})
# Grids with |dip| below this (degrees) count as horizontal (absorbs float noise).
DIP_TOLERANCE = 1e-6

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
class DataInfo:
    """A data channel attached to an object."""

    uid: str
    name: str
    association: str
    primitive_type: str


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


def read_attributes(
    workspace: Workspace, object_uid: str, association: str, length: int
) -> dict[str, np.ndarray]:
    """Read an object's data channels as attribute columns.

    Referenced (categorical) keys are replaced by their value-map names. Channels
    with an unsupported primitive type or the wrong length are skipped. Duplicate
    channel names get a numeric suffix.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.
        association: Which channels to read, e.g. ``"Vertex"`` or ``"Cell"``.
        length: Expected number of values (vertices or cells).

    Returns:
        Column name to 1-D array, in file order.

    Raises:
        KeyError: If the object does not exist.
    """
    columns: dict[str, np.ndarray] = {}
    for info in list_data(workspace, object_uid):
        if info.association != association:
            continue
        if info.primitive_type not in ATTRIBUTE_PRIMITIVES:
            continue
        values = read_data_values(workspace, info.uid)
        if values.shape != (length,):
            continue
        if info.primitive_type == "Referenced":
            values = _map_values(values, read_value_map(workspace, info.uid) or {})
        columns[_unique_name(info.name, columns)] = values
    return columns


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


def read_crs(workspace: Workspace, object_uid: str) -> str | None:
    """Read an object's coordinate reference system code from its metadata.

    Args:
        workspace: Project group from open_workspace().
        object_uid: Object group name.

    Returns:
        A code such as ``"EPSG:26914"``, or None if absent or "Unknown".

    Raises:
        KeyError: If the object does not exist.
    """
    obj = _object(workspace, object_uid)
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


def _map_values(keys: np.ndarray, value_map: dict[int, str]) -> np.ndarray:
    return np.array([value_map.get(int(k), str(k)) for k in keys], dtype=object)


def _unique_name(name: str, taken: dict[str, object]) -> str:
    candidates = (name if i == 1 else f"{name}_{i}" for i in range(1, len(taken) + 2))
    return next(c for c in candidates if c not in taken)


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
    )


def _object(workspace: Workspace, uid: str) -> gdal.Group:
    return _child_group(_child_group(workspace, "Objects"), uid)


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
