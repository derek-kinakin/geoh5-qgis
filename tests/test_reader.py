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
)
from geoh5_qgis.reader import (
    list_data,
    list_objects,
    open_workspace,
    read_cells,
    read_crs,
    read_data_values,
    read_grid2d_frame,
    read_value_map,
    read_vertices,
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
