"""Tests for drillhole groups stored in GA's concatenated format."""

from pathlib import Path

import numpy as np
import pytest
from geoh5py.workspace import Workspace as Geoh5pyWorkspace
from qgis.core import NULL, QgsWkbTypes

from conftest import (
    H1_AU,
    H1_COLLAR,
    H1_FROM_TO,
    H1_LITH_KEYS,
    H1_LITH_MAP,
    H1_SURVEYS,
    SAMPLE_FILE,
)
from geoh5_qgis.drillholes import (
    interval_path,
    minimum_curvature,
    positions_at,
    read_drillholes,
)
from geoh5_qgis.layers import load_object_layers
from geoh5_qgis.reader import (
    DRILLHOLES,
    load_issue,
    open_workspace,
    read_tree,
    tree_objects,
)

# The GA sample's two drillhole groups.
GEOLOGY_HOLES = "Drillholes"  # 121 holes, a "geology" interval table
LOGGED_HOLES = "Drillholes_descriptions"  # 13 holes, text and category intervals


def drillhole_node(path: Path, name: str | None = None):
    """The file's drillhole group named ``name`` (default: the first one)."""
    with open_workspace(path) as ws:
        return next(
            n
            for n in tree_objects(read_tree(ws))
            if n.kind == DRILLHOLES and name in (None, n.name)
        )


def sample_layers(name: str) -> dict:
    """The GA sample's drillhole group ``name`` loaded as layers, by layer name."""
    uid = drillhole_node(SAMPLE_FILE, name).uid
    return {layer.name(): layer for layer in load_object_layers(SAMPLE_FILE, uid)}


def geoh5py_positions(path: Path, hole: str, depths: np.ndarray) -> np.ndarray:
    with Geoh5pyWorkspace(path, mode="r") as ws:
        return ws.get_entity(hole)[0].desurvey(depths)


# --- minimum curvature ---------------------------------------------------------


def test_straight_inclined_hole() -> None:
    xyz, depth = minimum_curvature(
        np.r_[0.0, 0.0, 0.0], np.c_[[0.0], [90.0], [-45.0]], end_of_hole=100.0
    )
    assert depth[0] == 0.0 and depth[-1] == 100.0
    s = 100.0 / np.sqrt(2.0)
    np.testing.assert_allclose(xyz[-1], [s, 0.0, -s], atol=1e-9)


def test_vertical_default_without_surveys() -> None:
    xyz, _ = minimum_curvature(np.r_[5.0, 6.0, 7.0], np.empty((0, 3)), 30.0)
    np.testing.assert_allclose(xyz[-1], [5.0, 6.0, -23.0], atol=1e-9)


def test_minimum_curvature_matches_geoh5py(drillhole_file: Path) -> None:
    depths = np.linspace(0.0, 200.0, 21)
    xyz, trace_depth = minimum_curvature(H1_COLLAR, H1_SURVEYS, 200.0)
    ours = positions_at(xyz, trace_depth, depths)
    theirs = geoh5py_positions(drillhole_file, "H1", depths)
    np.testing.assert_allclose(ours, theirs, atol=0.05)


def test_positions_and_interval_path() -> None:
    trace = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, -10.0], [10.0, 0.0, -10.0]])
    depth = np.array([0.0, 10.0, 20.0])
    np.testing.assert_allclose(positions_at(trace, depth, [5.0]), [[0, 0, -5]])
    path = interval_path(trace, depth, 5.0, 15.0)
    np.testing.assert_allclose(path, [[0, 0, -5], [0, 0, -10], [5, 0, -10]])


# --- reading ------------------------------------------------------------------


def test_drillhole_group_is_a_loadable_tree_node(drillhole_file: Path) -> None:
    node = drillhole_node(drillhole_file)
    assert (node.name, node.kind, node.children) == ("DH", DRILLHOLES, ())
    with open_workspace(drillhole_file) as ws:
        assert load_issue(ws, node) is None


def test_read_drillholes(drillhole_file: Path) -> None:
    with open_workspace(drillhole_file) as ws:
        group = read_drillholes(ws, drillhole_node(drillhole_file).uid)
    holes = {h.name: h for h in group.holes}
    assert set(holes) == {"H1", "V2"}
    np.testing.assert_allclose(holes["H1"].collar, H1_COLLAR)
    np.testing.assert_allclose(holes["H1"].trace[0], H1_COLLAR)
    assert not holes["H1"].ga_trace  # geoh5py writes no GA trace
    # The path reaches the deepest interval (250 m), past the last survey.
    assert holes["H1"].trace_depth[-1] == pytest.approx(250.0)

    (table,) = group.intervals
    assert table.name == "assay"
    assert [group.holes[i].name for i in table.hole_index] == ["H1"] * 3
    np.testing.assert_allclose(np.c_[table.start, table.end], H1_FROM_TO)
    channels = {c.name: c for c in table.channels}
    lith = channels["LITH"]
    assert lith.primitive_type == "Referenced"
    assert lith.values.tolist() == H1_LITH_KEYS.tolist()
    assert {k: lith.value_map[k] for k in (1, 2)} == H1_LITH_MAP
    np.testing.assert_array_equal(channels["AU"].values, H1_AU)


# --- layers -------------------------------------------------------------------


def test_drillhole_layers(drillhole_file: Path) -> None:
    layers = {
        layer.name(): layer
        for layer in load_object_layers(
            drillhole_file, drillhole_node(drillhole_file).uid
        )
    }
    assert list(layers) == ["DH collars", "DH traces", "DH assay"]

    collars = layers["DH collars"]
    assert collars.wkbType() == QgsWkbTypes.Type.PointZ
    by_hole = {f["hole"]: f for f in collars.getFeatures()}
    point = by_hole["H1"].geometry().constGet()
    np.testing.assert_allclose([point.x(), point.y(), point.z()], H1_COLLAR)

    traces = layers["DH traces"]
    assert traces.wkbType() == QgsWkbTypes.Type.LineStringZ
    assert sorted(f["hole"] for f in traces.getFeatures()) == ["H1", "V2"]

    intervals = layers["DH assay"]
    assert intervals.wkbType() == QgsWkbTypes.Type.LineStringZ
    rows = sorted(intervals.getFeatures(), key=lambda f: f["from"])
    assert [(f["hole"], f["from"], f["to"]) for f in rows] == [
        ("H1", 0.0, 50.0),
        ("H1", 50.0, 120.0),
        ("H1", 120.0, 250.0),
    ]
    assert [f["LITH"] for f in rows] == H1_LITH_KEYS.tolist()
    assert [f["LITH_label"] for f in rows] == ["granite", "basalt", "granite"]
    assert [f["AU"] for f in rows] == [0.5, 1.5, NULL]
    # The second interval runs from 50 m to 120 m down the hole.
    line = rows[1].geometry().constGet()
    start, end = line.startPoint(), line.endPoint()
    expected = geoh5py_positions(drillhole_file, "H1", np.r_[50.0, 120.0])
    np.testing.assert_allclose(
        [[start.x(), start.y(), start.z()], [end.x(), end.y(), end.z()]],
        expected,
        atol=0.05,
    )


# --- the GEOSCIENCE ANALYST sample file ----------------------------------------------


def test_real_ga_drillholes_use_the_stored_trace() -> None:
    with open_workspace(SAMPLE_FILE) as ws:
        group = read_drillholes(ws, drillhole_node(SAMPLE_FILE, GEOLOGY_HOLES).uid)
    assert len(group.holes) == 121
    with_trace = [h for h in group.holes if h.ga_trace]
    assert len(with_trace) == 121
    for hole in with_trace:
        np.testing.assert_allclose(hole.trace[0], hole.collar, atol=1e-3)
    (geology,) = group.intervals
    assert geology.name == "geology"
    assert len(geology.start) == 363
    assert (geology.end >= geology.start).all()
    (channel,) = geology.channels
    assert channel.name == "GEOLOGY"
    assert channel.primitive_type == "Referenced"
    assert channel.visible  # the channel GA shows
    assert set(channel.values.tolist()) <= set(channel.value_map)
    assert channel.color_map is not None


@pytest.mark.parametrize(
    ("name", "typical", "worst"),
    [(GEOLOGY_HOLES, 0.1, 2.0), (LOGGED_HOLES, 0.05, 0.05)],
)
def test_minimum_curvature_reproduces_ga_traces(
    name: str, typical: float, worst: float
) -> None:
    """Our desurvey of GA's own surveys lands on GA's stored trace.

    Per hole, the largest deviation is a few cm (median). A few holes in
    the 121-hole group deviate up to ~1.7 m near the bottom or across large azimuth
    jumps, where GA evidently treats stations slightly differently. GA's own
    trace is always used when present; this desurvey is only the fallback.
    """
    with open_workspace(SAMPLE_FILE) as ws:
        group = read_drillholes(ws, drillhole_node(SAMPLE_FILE, name).uid)
    deviations = []
    for hole in group.holes:
        if hole.surveys is None or len(hole.surveys) < 2:
            continue
        xyz, depth = minimum_curvature(hole.collar, hole.surveys, hole.trace_depth[-1])
        ours = positions_at(xyz, depth, hole.trace_depth)
        deviations.append(float(np.linalg.norm(ours - hole.trace, axis=1).max()))
    assert np.median(deviations) < typical
    assert max(deviations) < worst


def test_real_ga_text_and_category_intervals() -> None:
    layers = sample_layers(LOGGED_HOLES)
    litho = layers["Drillholes_descriptions litho"]
    assert litho.isValid()
    fields = litho.fields().names()
    assert {"hole", "from", "to", "Logged Lithology", "Geologist Description"} <= set(
        fields
    )
    assert layers["Drillholes_descriptions collars"].featureCount() == 13


def test_real_ga_intervals_are_coloured_like_ga() -> None:
    from qgis.core import QgsCategorizedSymbolRenderer

    geology = sample_layers(GEOLOGY_HOLES)["Drillholes geology"]
    renderer = geology.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer)
    assert renderer.classAttribute() == "GEOLOGY"  # the channel GA shows
    labels = {f["GEOLOGY_label"] for f in geology.getFeatures()}
    assert {c.label() for c in renderer.categories()} == labels
    assert geology.renderer3D() is not None  # drawn down the holes in 3D


@pytest.fixture(scope="module")
def two_hole_table(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Holes A and B share table 'assay'; only A has CU (float) and CNT (int)."""
    from geoh5py.groups import DrillholeGroup
    from geoh5py.objects import Drillhole
    from geoh5py.workspace import Workspace

    path = tmp_path_factory.mktemp("geoh5") / "two_holes.geoh5"
    from_to = np.c_[[0.0, 10.0], [10.0, 20.0]]
    surveys = np.c_[[0.0, 30.0], [0.0, 0.0], [-90.0, -90.0]]
    with Workspace.create(path) as ws:
        group = DrillholeGroup.create(ws, name="DH2")
        a = Drillhole.create(
            ws, name="A", parent=group, collar=np.r_[0.0, 0.0, 0.0], surveys=surveys
        )
        a.add_data(
            {
                "ROCK": {
                    "values": np.array([1, 2], dtype=np.int32),
                    "from-to": from_to,
                    "type": "referenced",
                    "value_map": {1: "x", 2: "y"},
                },
                "CU": {"values": np.array([0.5, 0.7]), "from-to": from_to},
                "CNT": {"values": np.array([3, 4], dtype=np.int32), "from-to": from_to},
            },
            property_group="assay",
        )
        b = Drillhole.create(
            ws, name="B", parent=group, collar=np.r_[50.0, 0.0, 0.0], surveys=surveys
        )
        b.add_data(
            {
                "ROCK": {
                    "values": np.array([2, 1], dtype=np.int32),
                    "from-to": from_to,
                    "type": "referenced",
                    "value_map": {1: "x", 2: "y"},
                }
            },
            property_group="assay",
        )
    return path


def test_holes_missing_a_channel_get_null_of_the_right_type(
    two_hole_table: Path,
) -> None:
    from qgis.PyQt.QtCore import QMetaType

    layers = {
        layer.name(): layer
        for layer in load_object_layers(
            two_hole_table, drillhole_node(two_hole_table).uid
        )
    }
    assay = layers["DH2 assay"]
    types = {f.name(): f.type() for f in assay.fields()}
    assert types["CNT"] == QMetaType.Type.LongLong  # not turned into a float
    assert types["CU"] == QMetaType.Type.Double
    rows = {(f["hole"], f["from"]): f for f in assay.getFeatures()}
    assert rows[("A", 0.0)]["CNT"] == 3
    assert rows[("B", 0.0)]["CNT"] == NULL
    assert rows[("B", 0.0)]["CU"] == NULL
    assert rows[("B", 0.0)]["ROCK_label"] == "y"


@pytest.fixture(scope="module")
def depth_sample_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Hole H with depth table 'mag' (MAGSUS at 10, 40, 90 m), curved surveys."""
    from geoh5py.groups import DrillholeGroup
    from geoh5py.objects import Drillhole
    from geoh5py.workspace import Workspace

    path = tmp_path_factory.mktemp("geoh5") / "depth_samples.geoh5"
    with Workspace.create(path) as ws:
        group = DrillholeGroup.create(ws, name="DHD")
        hole = Drillhole.create(
            ws, name="H", parent=group, collar=H1_COLLAR, surveys=H1_SURVEYS
        )
        hole.add_data(
            {
                "MAGSUS": {
                    "values": np.array([0.1, 0.2, 0.3]),
                    "depth": np.array([10.0, 40.0, 90.0]),
                }
            },
            property_group="mag",
        )
    return path


def test_depth_table_is_read(depth_sample_file: Path) -> None:
    with open_workspace(depth_sample_file) as ws:
        group = read_drillholes(ws, drillhole_node(depth_sample_file).uid)
    assert group.intervals == ()
    (table,) = group.depths
    assert table.name == "mag"
    np.testing.assert_allclose(table.depth, [10.0, 40.0, 90.0])
    (channel,) = table.channels
    np.testing.assert_allclose(channel.values, [0.1, 0.2, 0.3])


def test_depth_samples_sit_on_the_hole_at_their_depth(depth_sample_file: Path) -> None:
    layers = {
        layer.name(): layer
        for layer in load_object_layers(
            depth_sample_file, drillhole_node(depth_sample_file).uid
        )
    }
    mag = layers["DHD mag"]
    assert mag.wkbType() == QgsWkbTypes.Type.PointZ
    assert mag.customProperty("geoh5/part") == "depths/mag"
    rows = sorted(mag.getFeatures(), key=lambda f: f["depth"])
    assert [(f["hole"], f["depth"], f["MAGSUS"]) for f in rows] == [
        ("H", 10.0, pytest.approx(0.1)),
        ("H", 40.0, pytest.approx(0.2)),
        ("H", 90.0, pytest.approx(0.3)),
    ]
    points = [r.geometry().constGet() for r in rows]
    expected = geoh5py_positions(depth_sample_file, "H", np.r_[10.0, 40.0, 90.0])
    np.testing.assert_allclose(
        [[p.x(), p.y(), p.z()] for p in points], expected, atol=0.05
    )


def test_real_ga_depth_table() -> None:
    layers = sample_layers(LOGGED_HOLES)
    # GA keeps the surveys as a "Static-Survey" depth table too.
    survey = layers["Drillholes_descriptions Static-Survey"]
    assert survey.featureCount() == 844
    assert {"hole", "depth", "Azimuth", "Dip"} <= set(survey.fields().names())
    assert survey.renderer3D() is not None
