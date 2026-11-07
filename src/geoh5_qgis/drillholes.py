"""Drillhole groups stored in GEOSCIENCE ANALYST's concatenated format.

A drillhole group keeps all of its holes and their data in shared arrays:

- ``Concatenated object IDs``: the hole UIDs.
- ``Concatenated Data/Attributes Jsons`` (``Attributes`` in geoh5 2.0): one JSON
  record per hole, data channel and property group.
- ``Concatenated Data/Index/<channel>``: compound (Start index, Size, Object
  ID, Data ID) locating each hole's slice of a channel's payload array, stored
  in ``Concatenated Data/Data/<channel>`` (or directly under
  ``Concatenated Data`` for Trace, TraceDepth, Surveys, Property Group IDs).

GA stores each hole's desurveyed path (``Trace`` + ``TraceDepth``); files written
by geoh5py do not, so holes without a trace are desurveyed here with the
minimum-curvature method from their surveys (Depth, Azimuth, Dip; dip negative
downwards).

Reads only via GDAL; no QGIS imports.
"""

import json
from dataclasses import dataclass, field

import numpy as np
from osgeo import gdal

from geoh5_qgis.reader import (
    CONCATENATED_IDS,
    INTEGER_NDV,
    DataChannel,
    DataInfo,
    Workspace,
    _array,
    _attrs,
    _groups,
    _indexed,
    _is_drillhole_group,
    find_data_type,
    make_channel,
)

CONCATENATED_DATA = "Concatenated Data"
INTERVAL_TABLE = "Interval table"
MAX_STEP = 10.0  # metres between desurveyed trace points
SURVEY_SPACING = 50.0  # long survey intervals get stations this far apart

_ROOT_CHANNELS = ("Trace", "TraceDepth", "Surveys", "Property Group IDs")


@dataclass(frozen=True)
class Hole:
    """One drillhole: collar, path (trace) and the surveys it came from."""

    uid: str
    name: str
    collar: np.ndarray
    end_of_hole: float | None
    planning: str | None
    trace: np.ndarray  # (n, 3) x, y, z along the hole
    trace_depth: np.ndarray  # (n,) depth of each trace point
    ga_trace: bool  # True if the trace is GA's stored one, not desurveyed here
    surveys: np.ndarray | None = None  # (m, 3) depth, azimuth, dip


@dataclass(frozen=True)
class IntervalTable:
    """From-to interval data of one property group, across all holes.

    Each channel has one value per row; rows of holes without the channel hold
    no-data (NaN, INTEGER_NDV, NaT or None, by type; False for Boolean).
    """

    name: str
    hole_index: np.ndarray  # row -> index into DrillholeGroupData.holes
    start: np.ndarray
    end: np.ndarray
    channels: tuple[DataChannel, ...]


@dataclass(frozen=True)
class DrillholeGroupData:
    """Everything loaded from one drillhole group."""

    holes: tuple[Hole, ...]
    intervals: tuple[IntervalTable, ...]


def read_drillholes(workspace: Workspace, group_uid: str) -> DrillholeGroupData:
    """Read the holes and interval tables of a drillhole group.

    Args:
        workspace: Project group from reader.open_workspace().
        group_uid: The drillhole group's UID.

    Returns:
        Holes (in stored order) with their paths, and one IntervalTable per
        interval property group, its channels typed as reader.DataChannel.

    Raises:
        KeyError: If the group does not exist or is not a drillhole group.
    """
    group = _drillhole_group(workspace, group_uid)
    store = _open_store(group.OpenGroup(CONCATENATED_DATA))
    records = {_norm(r["ID"]): r for r in _records(store)}
    hole_ids = _hole_ids(group)
    intervals_by_hole = {
        uid: _hole_interval_groups(store, records, uid) for uid in hole_ids
    }
    holes = tuple(
        _read_hole(
            store,
            records.get(uid, {}),
            uid,
            _deepest_interval(store, records, uid, intervals_by_hole[uid]),
        )
        for uid in hole_ids
    )
    tables = _interval_tables(workspace, store, records, hole_ids, intervals_by_hole)
    return DrillholeGroupData(holes, tables)


def minimum_curvature(
    collar: np.ndarray,
    surveys: np.ndarray,
    end_of_hole: float | None = None,
    max_step: float = MAX_STEP,
) -> tuple[np.ndarray, np.ndarray]:
    """Desurvey a hole with the minimum-curvature method.

    Between survey stations the path follows a circular arc tangent to both
    station directions. Before the first station the first direction is used;
    past the last station the hole continues straight to ``end_of_hole``.
    Survey intervals of SURVEY_SPACING or more are first densified with
    linearly interpolated azimuth/dip, as GEOSCIENCE ANALYST does.

    Args:
        collar: (3,) collar x, y, z.
        surveys: (m, 3) depth, azimuth (degrees clockwise from north) and dip
            (degrees, negative downwards). Empty means vertical down.
        end_of_hole: Depth to extend the path to, if deeper than the surveys.
        max_step: Largest depth step between returned points.

    Returns:
        (n, 3) path coordinates and (n,) their depths, starting at the collar.
    """
    stations = _stations(np.asarray(surveys, dtype=float), end_of_hole)
    depths, directions = stations[:, 0], _direction(stations[:, 1], stations[:, 2])
    points = [np.asarray(collar, dtype=float)[None, :]]
    sample_depths = [depths[:1]]
    start = points[0][0]
    for i in range(len(depths) - 1):
        length = depths[i + 1] - depths[i]
        if length <= 0:
            continue
        steps = max(1, int(np.ceil(length / max_step)))
        fractions = np.linspace(0.0, 1.0, steps + 1)[1:]
        offsets = _arc_offsets(directions[i], directions[i + 1], length, fractions)
        points.append(start + offsets)
        sample_depths.append(depths[i] + fractions * length)
        start = start + offsets[-1]
    return np.vstack(points), np.concatenate(sample_depths)


def positions_at(
    trace: np.ndarray, trace_depth: np.ndarray, depths: np.ndarray | list[float]
) -> np.ndarray:
    """Positions at given depths along a hole path (linear between points).

    Depths outside the path are clamped to its ends.

    Args:
        trace: (n, 3) path coordinates.
        trace_depth: (n,) increasing depths of the path points.
        depths: Depths to locate.

    Returns:
        (k, 3) positions.
    """
    depths = np.asarray(depths, dtype=float)
    return np.column_stack(
        [np.interp(depths, trace_depth, trace[:, axis]) for axis in range(3)]
    )


def interval_path(
    trace: np.ndarray, trace_depth: np.ndarray, start: float, end: float
) -> np.ndarray:
    """The part of a hole path between two depths, following its bends.

    Args:
        trace: (n, 3) path coordinates.
        trace_depth: (n,) increasing depths of the path points.
        start: From depth.
        end: To depth.

    Returns:
        (k, 3) points: the start position, path points strictly inside the
        interval, and the end position.
    """
    inside = (trace_depth > start) & (trace_depth < end)
    ends = positions_at(trace, trace_depth, [start, end])
    return np.vstack([ends[:1], trace[inside], ends[1:]])


# --- holes ----------------------------------------------------------------------


def _read_hole(store: "_Store", record: dict, uid: str, deepest: float) -> Hole:
    collar = _parse_collar(record.get("Collar"))
    end_of_hole = record.get("End of hole")
    surveys = _hole_surveys(store, uid)
    trace = _slice(store, "Trace", uid)
    trace_depth = _slice(store, "TraceDepth", uid)
    if trace is not None and trace_depth is not None and len(trace) >= 2:
        xyz = np.column_stack([trace["x"], trace["y"], trace["z"]]).astype(float)
        return Hole(
            uid,
            str(record.get("Name", "")),
            collar,
            end_of_hole,
            record.get("Planning"),
            xyz,
            trace_depth.astype(float),
            True,
            surveys,
        )
    depth = max(float(end_of_hole or 0.0), deepest)
    xyz, depths = minimum_curvature(
        collar, surveys if surveys is not None else np.empty((0, 3)), depth
    )
    return Hole(
        uid,
        str(record.get("Name", "")),
        collar,
        end_of_hole,
        record.get("Planning"),
        xyz,
        depths,
        False,
        surveys,
    )


def _hole_surveys(store: "_Store", uid: str) -> np.ndarray | None:
    columns = [
        _slice(store, "Surveys", uid, field=f) for f in ("Depth", "Azimuth", "Dip")
    ]
    if any(c is None for c in columns):
        return None
    return np.column_stack(columns).astype(float)


def _parse_collar(text: str | None) -> np.ndarray:
    if not text:
        return np.zeros(3)
    return np.array([float(v) for v in str(text).strip("{} ").split(",")])


def _stations(surveys: np.ndarray, end_of_hole: float | None) -> np.ndarray:
    """Survey stations sorted by depth, starting at 0 and reaching end_of_hole."""
    if surveys.size == 0:
        surveys = np.array([[0.0, 0.0, -90.0]])
    surveys = surveys[np.argsort(surveys[:, 0], kind="stable")]
    if surveys[0, 0] > 0:
        surveys = np.vstack([[0.0, *surveys[0, 1:]], surveys])
    if end_of_hole is not None and end_of_hole > surveys[-1, 0]:
        surveys = np.vstack([surveys, [end_of_hole, *surveys[-1, 1:]]])
    return _densify(surveys)


def _densify(stations: np.ndarray) -> np.ndarray:
    """Insert stations every SURVEY_SPACING m in long survey intervals.

    Azimuth and dip are interpolated linearly in depth, the convention
    GEOSCIENCE ANALYST and geoh5py use before fitting arcs.
    """
    rows = [stations[:1]]
    for upper, lower in zip(stations[:-1], stations[1:], strict=True):
        length = lower[0] - upper[0]
        if length >= SURVEY_SPACING:
            extra = np.arange(SURVEY_SPACING, length, SURVEY_SPACING)
            fractions = (extra / length)[:, None]
            rows.append(upper + fractions * (lower - upper))
        rows.append(lower[None, :])
    return np.vstack(rows)


def _direction(azimuth: np.ndarray, dip: np.ndarray) -> np.ndarray:
    """Unit vectors (east, north, up) for azimuth/dip in degrees."""
    azimuth, dip = np.deg2rad(azimuth), np.deg2rad(dip)
    return np.column_stack(
        [np.cos(dip) * np.sin(azimuth), np.cos(dip) * np.cos(azimuth), np.sin(dip)]
    )


def _arc_offsets(
    t1: np.ndarray, t2: np.ndarray, length: float, fractions: np.ndarray
) -> np.ndarray:
    """Offsets along a circular arc of ``length`` from direction t1 to t2."""
    cos_beta = float(np.clip(np.dot(t1, t2), -1.0, 1.0))
    beta = np.arccos(cos_beta)
    if beta < 1e-9:
        return np.outer(fractions * length, t1)
    radius = length / beta
    normal = (t2 - cos_beta * t1) / np.sin(beta)
    angles = fractions * beta
    return radius * (
        np.outer(np.sin(angles), t1) + np.outer(1 - np.cos(angles), normal)
    )


# --- interval tables ------------------------------------------------------------


def _deepest_interval(
    store: "_Store", records: dict, uid: str, groups: list[dict]
) -> float:
    ends = [_table_from_to(store, records, uid, g)[1] for g in groups]
    return max((float(e.max()) for e in ends if e.size), default=0.0)


def _hole_interval_groups(store: "_Store", records: dict, uid: str) -> list[dict]:
    ids = _slice(store, "Property Group IDs", uid)
    groups = [records.get(_norm(i)) for i in (ids if ids is not None else [])]
    return [
        g
        for g in groups
        if g
        and g.get("Property Group Type") == INTERVAL_TABLE
        and len(g.get("Properties", [])) >= 2
    ]


def _table_from_to(
    store: "_Store", records: dict, uid: str, group: dict
) -> tuple[np.ndarray, np.ndarray]:
    from_id, to_id = (_norm(p) for p in group["Properties"][:2])
    return (
        _float_slice(store, records, uid, from_id),
        _float_slice(store, records, uid, to_id),
    )


def _interval_tables(
    workspace: Workspace,
    store: "_Store",
    records: dict,
    hole_ids: list[str],
    groups_by_hole: dict[str, list[dict]],
) -> tuple[IntervalTable, ...]:
    names = list(
        dict.fromkeys(
            g.get("Group Name", "")
            for groups in groups_by_hole.values()
            for g in groups
        )
    )
    tables = []
    for table_name in names:
        rows = []  # (hole index, from, to, {channel name: stored values})
        channel_records: dict[str, dict] = {}  # first record of each channel
        shown: set[str] = set()
        for index, uid in enumerate(hole_ids):
            for group in groups_by_hole[uid]:
                if group.get("Group Name", "") != table_name:
                    continue
                start, end = _table_from_to(store, records, uid, group)
                stored = {}
                for prop in group["Properties"][2:]:
                    record = records.get(_norm(prop), {})
                    name = str(record.get("Name", prop))
                    values = _stored_values(store, record, uid)
                    if values is None or len(values) != len(start):
                        continue
                    stored[name] = values
                    channel_records.setdefault(name, record)
                    if record.get("Visible"):
                        shown.add(name)
                rows.append((index, start, end, stored))
        if rows:
            channels = tuple(
                _table_channel(
                    workspace,
                    name,
                    record,
                    name in shown,
                    [(len(s), stored.get(name)) for _, s, _, stored in rows],
                )
                for name, record in channel_records.items()
            )
            tables.append(_stack_rows(table_name, rows, channels))
    return tuple(tables)


def _stack_rows(
    name: str, rows: list, channels: tuple[DataChannel, ...]
) -> IntervalTable:
    hole_index = np.concatenate([np.full(len(s), i) for i, s, _, _ in rows])
    start = np.concatenate([s for _, s, _, _ in rows])
    end = np.concatenate([e for _, _, e, _ in rows])
    return IntervalTable(name, hole_index, start, end, channels)


def _table_channel(
    workspace: Workspace,
    name: str,
    record: dict,
    visible: bool,
    parts: list[tuple[int, np.ndarray | None]],
) -> DataChannel:
    """One channel of an interval table, its holes' values stacked in row order."""
    data_type = find_data_type(workspace, record.get("Type ID", ""))
    primitive = str(_attrs(data_type).get("Primitive type", "")) if data_type else ""
    info = DataInfo(
        _norm(record.get("ID", "")),
        name,
        str(record.get("Association", "")),
        primitive,
        visible,
    )
    return make_channel(info, _stack_column(parts, primitive), data_type)


def _stack_column(
    parts: list[tuple[int, np.ndarray | None]], primitive: str
) -> np.ndarray:
    """Concatenate holes' stored values, filling holes without them with no-data.

    Integers are widened to int64 so INTEGER_NDV fits even for unsigned keys.
    """
    present = [p for _, p in parts if p is not None]
    kind = present[0].dtype.kind if present else "f"
    if kind in "OSU":
        dtype, fill = object, None
    elif kind == "f":
        dtype, fill = np.float64, np.nan
    elif primitive == "Boolean":
        dtype, fill = np.int64, 0
    else:
        dtype, fill = np.int64, INTEGER_NDV
    return np.concatenate(
        [
            np.asarray(p).astype(dtype) if p is not None else np.full(n, fill, dtype)
            for n, p in parts
        ]
    )


def _stored_values(store: "_Store", record: dict, uid: str) -> np.ndarray | None:
    """A hole's stored values of one channel, or None if it has none."""
    name, data_id = record.get("Name"), _norm(record.get("ID", ""))
    values = _slice(store, name, uid, data_id=data_id) if name else None
    if values is None:
        return None
    return np.array(values, dtype=object) if isinstance(values, list) else values


def _float_slice(store: "_Store", records: dict, uid: str, data_id: str) -> np.ndarray:
    record = records.get(data_id, {})
    values = _slice(store, record.get("Name", ""), uid, data_id=data_id)
    return np.asarray(values if values is not None else [], dtype=float)


# --- concatenated storage ------------------------------------------------------


def _drillhole_group(workspace: Workspace, group_uid: str) -> gdal.Group:
    group = _indexed(workspace, "Groups", group_uid)
    if group is None or not _is_drillhole_group(group):
        raise KeyError(f"No drillhole group {group_uid!r}")
    return group


def _hole_ids(group: gdal.Group) -> list[str]:
    ids = _array(group, CONCATENATED_IDS).Read()
    return [_norm(i) for i in (ids if isinstance(ids, list) else [ids])]


def _norm(uid: object) -> str:
    return "{" + str(uid).strip().strip("{}").lower() + "}"


@dataclass(frozen=True)
class _Store:
    """One group's concatenated-data handles plus a read cache (data only)."""

    data: gdal.Group
    payloads: gdal.Group | None
    index: gdal.Group | None
    cache: dict = field(default_factory=dict)


def _open_store(data: gdal.Group) -> _Store:
    def child(name: str) -> gdal.Group | None:
        return data.OpenGroup(name) if name in _groups(data) else None

    return _Store(data, child("Data"), child("Index"))


def _records(store: _Store) -> list[dict]:
    names = store.data.GetMDArrayNames() or []
    label = next((n for n in ("Attributes Jsons", "Attributes") if n in names), None)
    if label is None:
        return []
    texts = store.data.OpenMDArray(label).Read()
    return [json.loads(t) for t in (texts if isinstance(texts, list) else [texts])]


def _slice(
    store: _Store,
    channel: str,
    uid: str,
    field: str | None = None,
    data_id: str | None = None,
):
    """One hole's part of a channel's payload, or None if absent."""
    location = _channel_index(store, channel).get((uid, data_id) if data_id else uid)
    if location is None:
        return None
    values = _payload(store, channel, field)
    start, size = location
    return None if values is None else values[start : start + size]


def _channel_index(store: _Store, channel: str) -> dict:
    """(object, data) and object -> (start, size), read once per channel."""
    key = ("index", channel)
    if key not in store.cache:
        store.cache[key] = _read_index(store.index, channel)
    return store.cache[key]


def _read_index(index_group: gdal.Group | None, channel: str) -> dict:
    if index_group is None or channel not in (index_group.GetMDArrayNames() or []):
        return {}
    index = index_group.OpenMDArray(channel)
    starts = index.GetView('["Start index"]').ReadAsArray()
    sizes = index.GetView('["Size"]').ReadAsArray()
    objects = [_norm(o) for o in index.GetView('["Object ID"]').Read()]
    data_ids = [_norm(d) for d in index.GetView('["Data ID"]').Read()]
    lookup: dict = {}
    for obj, data_id, start, size in zip(objects, data_ids, starts, sizes, strict=True):
        location = (int(start), int(size))
        lookup[(obj, data_id)] = location
        lookup.setdefault(obj, location)
    return lookup


def _payload(store: _Store, channel: str, field: str | None):
    """A channel's whole payload array (or one compound field), read once."""
    key = ("payload", channel, field)
    if key not in store.cache:
        store.cache[key] = _read_payload(store, channel, field)
    return store.cache[key]


def _read_payload(store: _Store, channel: str, field: str | None):
    holders = (
        [store.data, store.payloads]
        if channel in _ROOT_CHANNELS
        else [
            store.payloads,
            store.data,
        ]
    )
    holder = next(
        (
            h
            for h in holders
            if h is not None and channel in (h.GetMDArrayNames() or [])
        ),
        None,
    )
    if holder is None:
        return None
    array = holder.OpenMDArray(channel)
    if field is not None:
        array = array.GetView(f'["{field}"]')
    if array.GetDataType().GetClass() == gdal.GEDTC_STRING:
        return np.array(array.Read(), dtype=object)
    return array.ReadAsArray()
