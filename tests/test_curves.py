"""Tests for curve layers: whole lines, segments with vertex values, styling."""

from pathlib import Path

import h5py
import numpy as np
import pytest
from qgis.core import NULL, QgsSingleSymbolRenderer, QgsWkbTypes

from conftest import CURVE_CELLS, CURVE_VERTEX_DEPTHS, CURVE_VERTICES
from geoh5_qgis.layers import curve_chains, load_object_layers
from geoh5_qgis.reader import list_objects, open_workspace


def uid_of(path: Path, name: str) -> str:
    with open_workspace(path) as ws:
        return next(o.uid for o in list_objects(ws) if o.name == name)


def chains(cells: list[list[int]]) -> list[list[int]]:
    return [c.tolist() for c in curve_chains(np.array(cells, dtype=np.int64))]


def coords(layer) -> list[list[tuple[float, float, float]]]:
    return [
        [(p.x(), p.y(), p.z()) for p in f.geometry().constGet().vertices()]
        for f in layer.getFeatures()
    ]


def test_chains_follow_segments_between_ends() -> None:
    assert chains(CURVE_CELLS.tolist()) == [[0, 1, 2], [3, 4]]


def test_chains_split_at_junctions() -> None:
    # A "T": vertex 1 joins three segments.
    assert chains([[0, 1], [1, 2], [1, 3]]) == [[0, 1], [1, 2], [1, 3]]


def test_closed_loop_is_one_chain_ending_where_it_starts() -> None:
    assert chains([[0, 1], [1, 2], [2, 0]]) == [[0, 1, 2, 0]]


def test_chains_join_segments_stored_backwards() -> None:
    assert chains([[1, 0], [2, 1]]) == [[0, 1, 2]]


def test_no_segments_no_chains() -> None:
    assert chains(np.zeros((0, 2), dtype=np.int64).tolist()) == []


def test_curve_without_data_loads_as_whole_lines(geoh5_file: Path) -> None:
    (layer,) = load_object_layers(geoh5_file, uid_of(geoh5_file, "bare_line"))
    assert layer.wkbType() == QgsWkbTypes.Type.LineStringZ
    assert layer.featureCount() == 2
    assert [f["part"] for f in layer.getFeatures()] == [0, 1]
    expected = [CURVE_VERTICES[[0, 1, 2]], CURVE_VERTICES[[3, 4]]]
    assert coords(layer) == [[tuple(v) for v in line] for line in expected]


def test_curve_with_data_keeps_segments_with_vertex_end_values(
    geoh5_file: Path,
) -> None:
    curve, vertices = load_object_layers(geoh5_file, uid_of(geoh5_file, "line"))
    assert curve.featureCount() == len(CURVE_CELLS)
    names = curve.fields().names()
    assert names == ["part", "seg_id", "depth_start", "depth_end"]
    rows = [dict(zip(names, f.attributes(), strict=True)) for f in curve.getFeatures()]
    expected_start = CURVE_VERTEX_DEPTHS[CURVE_CELLS[:, 0]]
    expected_end = CURVE_VERTEX_DEPTHS[CURVE_CELLS[:, 1]]
    nulls = [None if np.isnan(v) else v for v in expected_end]
    assert [r["depth_start"] for r in rows] == expected_start.tolist()
    assert [None if r["depth_end"] == NULL else r["depth_end"] for r in rows] == nulls
    assert vertices.name() == "line vertices"  # still there for vertex data


@pytest.fixture
def shown_vertex_curve(geoh5_file: Path, tmp_path: Path) -> Path:
    """The fixture file with the 'line' curve's vertex channel marked as shown."""
    import shutil

    path = Path(shutil.copy(geoh5_file, tmp_path / "shown.geoh5"))
    with h5py.File(path, "r+") as f:
        data = f["GEOSCIENCE"]["Data"]
        depth = next(k for k in data if data[k].attrs["Name"] == "depth")
        data[depth].attrs["Visible"] = np.int8(1)
    return path


def test_shown_vertex_channel_shades_segments_continuously(
    shown_vertex_curve: Path,
) -> None:
    curve, vertices = load_object_layers(
        shown_vertex_curve, uid_of(shown_vertex_curve, "line")
    )
    renderer = curve.renderer()
    assert isinstance(renderer, QgsSingleSymbolRenderer)
    line = renderer.symbol().symbolLayer(0)
    assert line.layerType() == "InterpolatedLine"
    # 3D colours segments by class of their mean value.
    rules = curve.renderer3D().rootRule().children()
    assert rules and all("depth_start" in r.filterExpression() for r in rules)
    # The vertex points are styled by the same channel.
    assert vertices.renderer().classAttribute() == "depth"
