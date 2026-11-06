"""Tests for the GDAL-based geoh5 reader."""

from pathlib import Path

import numpy as np
import pytest

from conftest import (
    CURVE_CELLS,
    CURVE_VERTICES,
    GRID_KWARGS,
    POINT_FLOATS,
    POINT_INTS,
    POINT_ROCK_KEYS,
    POINT_TEXT,
    POINT_VERTICES,
    ROCK_VALUE_MAP,
    SURFACE_CELLS,
    SURFACE_VERTICES,
    TYPED_COLOR_MAP,
    TYPED_FLAGS,
    TYPED_FLOATS,
    TYPED_INTS,
    TYPED_UNITS,
)
from geoh5_qgis.reader import (
    Grid2DFrame,
    is_horizontal,
    list_data,
    list_objects,
    load_issue,
    open_workspace,
    read_cells,
    read_channels,
    read_crs,
    read_data_values,
    read_grid2d_frame,
    read_tree,
    read_value_map,
    read_vertices,
    tree_objects,
)

ASSETS = Path(r"C:\Users\dkinakin\Dev\geoh5vista\assets")


def object_by_name(workspace, name: str):
    return next(o for o in list_objects(workspace) if o.name == name)


def data_by_name(workspace, object_uid: str, name: str):
    return next(d for d in list_data(workspace, object_uid) if d.name == name)


def test_list_objects_identifies_kinds(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        kinds = {o.name: o.kind for o in list_objects(ws)}
    assert kinds == {
        "pts": "Points",
        "line": "Curve",
        "bare_line": "Curve",
        "topo": "Surface",
        "zoned": "Surface",
        "shaded": "Surface",
        "grid": "Grid2D",
    }


def test_read_vertices_returns_n_by_3_float(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts = read_vertices(ws, object_by_name(ws, "pts").uid)
        curve = read_vertices(ws, object_by_name(ws, "line").uid)
    assert pts.dtype == np.float64
    np.testing.assert_allclose(pts, POINT_VERTICES)
    np.testing.assert_allclose(curve, CURVE_VERTICES)


@pytest.mark.parametrize(
    ("name", "expected"), [("line", CURVE_CELLS), ("topo", SURFACE_CELLS)]
)
def test_read_cells(geoh5_file: Path, name: str, expected: np.ndarray) -> None:
    with open_workspace(geoh5_file) as ws:
        cells = read_cells(ws, object_by_name(ws, name).uid)
    assert cells.dtype == np.int64
    np.testing.assert_array_equal(cells, expected)


def test_surface_vertices(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        verts = read_vertices(ws, object_by_name(ws, "topo").uid)
    np.testing.assert_allclose(verts, SURFACE_VERTICES)


def test_read_grid2d_frame(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        frame = read_grid2d_frame(ws, object_by_name(ws, "grid").uid)
    assert frame.origin == tuple(GRID_KWARGS["origin"])
    assert (frame.u_size, frame.v_size) == (10.0, 20.0)
    assert (frame.u_count, frame.v_count) == (4, 3)
    assert frame.rotation == 30.0
    assert frame.dip == 0.0
    assert frame.vertical is False


def test_list_data_reports_association_and_type(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts_uid = object_by_name(ws, "pts").uid
        info = {
            d.name: (d.association, d.primitive_type) for d in list_data(ws, pts_uid)
        }
    assert info["grade"] == ("Vertex", "Float")
    assert info["count"] == ("Vertex", "Integer")
    assert info["rock"] == ("Vertex", "Referenced")


def test_float_no_data_becomes_nan(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts_uid = object_by_name(ws, "pts").uid
        values = read_data_values(ws, data_by_name(ws, pts_uid, "grade").uid)
    np.testing.assert_array_equal(values, POINT_FLOATS)  # NaN == NaN here


def test_integer_and_referenced_values(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts_uid = object_by_name(ws, "pts").uid
        ints = read_data_values(ws, data_by_name(ws, pts_uid, "count").uid)
        keys = read_data_values(ws, data_by_name(ws, pts_uid, "rock").uid)
    np.testing.assert_array_equal(ints, POINT_INTS)
    np.testing.assert_array_equal(keys, POINT_ROCK_KEYS)


def test_read_value_map(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts_uid = object_by_name(ws, "pts").uid
        value_map = read_value_map(ws, data_by_name(ws, pts_uid, "rock").uid)
    assert value_map == ROCK_VALUE_MAP


def test_value_map_is_none_for_float_data(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts_uid = object_by_name(ws, "pts").uid
        assert read_value_map(ws, data_by_name(ws, pts_uid, "grade").uid) is None


def test_grid_cell_data_length(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        grid_uid = object_by_name(ws, "grid").uid
        elev = data_by_name(ws, grid_uid, "elev")
        values = read_data_values(ws, elev.uid)
    assert elev.association == "Cell"
    np.testing.assert_array_equal(values, np.arange(12))


def test_read_crs(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        assert read_crs(ws, object_by_name(ws, "pts").uid) == "EPSG:26914"
        assert read_crs(ws, object_by_name(ws, "topo").uid) is None


def test_text_values_are_strings(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        pts_uid = object_by_name(ws, "pts").uid
        note = data_by_name(ws, pts_uid, "note")
        values = read_data_values(ws, note.uid)
    assert note.primitive_type == "Text"
    assert values.dtype == object
    assert list(values) == POINT_TEXT


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_file_without_objects_group_lists_nothing() -> None:
    # A GEOSCIENCE ANALYST drillhole file holding only groups: no /Objects at all.
    with open_workspace(ASSETS / "drillholes" / "dh_test_a.geoh5") as ws:
        assert list_objects(ws) == []
        with pytest.raises(KeyError):
            read_vertices(ws, "{00000000-0000-0000-0000-000000000000}")


def test_unknown_uid_raises(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws, pytest.raises(KeyError):
        read_vertices(ws, "{00000000-0000-0000-0000-000000000000}")


def test_open_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError), open_workspace(tmp_path / "nope.geoh5"):
        pass


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_grid2d_origin_from_geoscience_analyst() -> None:
    path = ASSETS / "grid2d" / "grid2d_test_a.geoh5"
    with open_workspace(path) as ws:
        frame = read_grid2d_frame(ws, object_by_name(ws, "airborne data").uid)
    np.testing.assert_allclose(frame.origin, (315532.25, 6065072.56, 172.0), atol=0.01)
    assert (frame.u_count, frame.v_count, frame.rotation) == (132, 356, 20.0)


def issues_by_name(path: Path) -> dict[str, str | None]:
    with open_workspace(path) as ws:
        return {o.name: load_issue(ws, o) for o in list_objects(ws)}


def test_loadable_objects_have_no_issue(geoh5_file: Path) -> None:
    assert set(issues_by_name(geoh5_file).values()) == {None}


def test_grid_issues(grid_issues_file: Path) -> None:
    issues = issues_by_name(grid_issues_file)
    assert "dipping" in issues["dipping"] and "-35" in issues["dipping"]
    assert "vertical" in issues["vertical"]
    assert issues["nearly_flat"] is None  # within DIP_TOLERANCE
    assert "no numeric cell data" in issues["empty"]
    assert "no numeric cell data" in issues["text_only"]


def test_is_horizontal_tolerance() -> None:
    def frame(dip: float, vertical: bool = False) -> Grid2DFrame:
        return Grid2DFrame((0.0, 0.0, 0.0), 1.0, 1.0, 2, 2, 0.0, dip, vertical)

    assert is_horizontal(frame(0.0))
    assert is_horizontal(frame(-1e-9))
    assert not is_horizontal(frame(-35.0))
    assert not is_horizontal(frame(0.0, vertical=True))


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_grid_issues() -> None:
    issues = issues_by_name(ASSETS / "grid2d" / "grid2d_examples.geoh5")
    assert "dipping" in issues["i_grid"]
    assert "dipping" in issues["v_grid"]
    assert "no numeric cell data" in issues["h_grid"]
    real = issues_by_name(ASSETS / "grid2d" / "grid2d_test_a.geoh5")
    assert real["airborne data"] is None  # dip stored as -0.0
    blocks = issues_by_name(ASSETS / "block_model" / "test_bm.geoh5")
    assert all("not supported" in issue for issue in blocks.values())


def outline(nodes) -> list:
    """Nested (name, kind[, children]) tuples for easy comparison."""
    return [
        (n.name, n.kind, outline(n.children)) if n.kind == "Group" else (n.name, n.kind)
        for n in nodes
    ]


TREE_OUTLINE = [
    ("Empty", "Group", []),
    (
        "Folder A",
        "Group",
        [("Sub", "Group", [("line_b", "Curve")]), ("pts_a", "Points")],
    ),
    ("top_pts", "Points"),
]


def test_read_tree_mirrors_groups(tree_file: Path) -> None:
    with open_workspace(tree_file) as ws:
        tree = read_tree(ws)
        flat = {o.uid: o for o in list_objects(ws)}
    assert outline(tree) == TREE_OUTLINE
    pts_a = tree[1].children[1]
    assert flat[pts_a.uid].name == "pts_a"


def test_tree_objects_flattens_in_tree_order(tree_file: Path) -> None:
    with open_workspace(tree_file) as ws:
        assert [n.name for n in tree_objects(read_tree(ws))] == [
            "line_b",
            "pts_a",
            "top_pts",
        ]


def test_tree_hides_objects_unlinked_from_root(orphan_tree_file: Path) -> None:
    with open_workspace(orphan_tree_file) as ws:
        assert outline(read_tree(ws)) == TREE_OUTLINE[:2]
        # The flat index still has it; the tree is what GA shows.
        assert "top_pts" in {o.name for o in list_objects(ws)}


def test_rootless_file_is_read_as_a_forest(rootless_tree_file: Path) -> None:
    with open_workspace(rootless_tree_file) as ws:
        assert outline(read_tree(ws)) == TREE_OUTLINE


def test_flat_file_tree_lists_objects(geoh5_file: Path) -> None:
    with open_workspace(geoh5_file) as ws:
        names = [n.name for n in read_tree(ws)]
    assert names == sorted(names, key=str.lower)
    assert "pts" in names


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_ga_tree() -> None:
    with open_workspace(ASSETS / "workspace" / "workspace_complex.geoh5") as ws:
        tree = read_tree(ws)
        flat = {o.uid.lower() for o in list_objects(ws)}
    assert [n.name for n in tree] == [
        "FlinFlon Canada",
        "GemPy Implicit Modelling",
        "Tools",
    ]
    flinflon = tree[0]
    geophysics = next(n for n in flinflon.children if n.name == "Geophysics data")
    assert [(n.name, n.kind) for n in geophysics.children] == [
        ("airborne data", "Grid2D")
    ]
    # Every indexed object is reachable from the root in this GA file; the tree
    # also lists its two drillhole groups as loadable items.
    loadable = tree_objects(tree)
    assert {n.uid.lower() for n in loadable if n.kind != "Drillholes"} == flat
    assert sorted(n.name for n in loadable if n.kind == "Drillholes") == [
        "Drillholes",
        "Drillholes_descriptions",
    ]


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_drillhole_group_is_one_loadable_item() -> None:
    # Holes live in concatenated storage; the group loads as a whole.
    with open_workspace(ASSETS / "drillholes" / "dh_test_a.geoh5") as ws:
        assert [(n.name, n.kind, n.children) for n in read_tree(ws)] == [
            ("Drillholes", "Drillholes", ())
        ]


def typed_channels(path: Path) -> dict:
    with open_workspace(path) as ws:
        uid = object_by_name(ws, "typed").uid
        return {c.name: c for c in read_channels(ws, uid, "Vertex", 4)}


def test_read_channels_types_each_primitive(channels_file: Path) -> None:
    channels = typed_channels(channels_file)
    np.testing.assert_array_equal(channels["au"].values, TYPED_FLOATS)
    assert channels["count"].values.tolist() == TYPED_INTS.tolist()
    assert channels["rock"].values.tolist() == POINT_ROCK_KEYS.tolist()
    assert channels["flag"].values.dtype == bool
    assert channels["flag"].values.tolist() == TYPED_FLAGS.tolist()
    assert channels["note"].values.tolist() == POINT_TEXT
    expected_dates = np.array(
        ["2024-01-02T03:04:05", "2024-02-03", "NaT", "2024-03-04T10:00:00"],
        dtype="datetime64[ms]",
    )
    np.testing.assert_array_equal(channels["when"].values, expected_dates)


def test_read_channels_type_metadata(channels_file: Path) -> None:
    channels = typed_channels(channels_file)
    assert channels["au"].units == TYPED_UNITS
    assert channels["count"].units is None
    np.testing.assert_array_equal(channels["au"].color_map, TYPED_COLOR_MAP)
    assert channels["count"].color_map is None
    assert channels["rock"].value_map == ROCK_VALUE_MAP
    # Booleans carry a False/True value map in the file; only Referenced uses it.
    assert channels["flag"].value_map is None


def test_read_channels_skips_other_associations(channels_file: Path) -> None:
    with open_workspace(channels_file) as ws:
        uid = object_by_name(ws, "typed").uid
        assert read_channels(ws, uid, "Cell", 4) == []
