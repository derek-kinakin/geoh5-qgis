"""Shared fixtures: geoh5 files written with geoh5py (test-only dependency)."""

from pathlib import Path

import numpy as np
import pytest
from geoh5py.objects import Curve, Grid2D, Points, Surface
from geoh5py.workspace import Workspace

CRS_METADATA = {
    "Coordinate Reference System": {
        "Current": {"Code": "EPSG:26914", "Name": "NAD83 / UTM zone 14N"}
    }
}
POINT_VERTICES = np.array(
    [[0.0, 0.0, 10.0], [1.0, 2.0, 11.0], [2.0, 4.0, 12.0], [3.0, 6.0, 13.0]]
)
POINT_FLOATS = np.array([1.5, np.nan, 3.5, 4.5])
POINT_INTS = np.array([10, 20, 30, 40], dtype=np.int32)
POINT_ROCK_KEYS = np.array([1, 2, 1, 0], dtype=np.uint32)
POINT_TEXT = ["a", "b", "", "d"]
ROCK_VALUE_MAP = {0: "Unknown", 1: "granite", 2: "basalt"}
# Two separate polylines: 0-1-2 and 3-4.
CURVE_VERTICES = np.array(
    [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [2.0, 1.0, 0.0],
        [5.0, 5.0, 1.0],
        [6.0, 5.0, 1.0],
    ]
)
CURVE_CELLS = np.array([[0, 1], [1, 2], [3, 4]], dtype=np.uint32)
CURVE_VERTEX_DEPTHS = np.array([0.0, 1.5, np.nan, 4.0, 5.5])
SURFACE_VERTICES = np.array(
    [[0.0, 0.0, 0.0], [1.0, 0.0, 0.5], [0.0, 1.0, 1.0], [1.0, 1.0, 1.5]]
)
SURFACE_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.uint32)
SHADED_VERTEX_VALUES = np.array([1.0, np.nan, 3.0, 4.0])
GRID_KWARGS = {
    "origin": [1000.0, 2000.0, 50.0],
    "u_cell_size": 10.0,
    "v_cell_size": 20.0,
    "u_count": 4,
    "v_count": 3,
    "rotation": 30.0,
}


@pytest.fixture(scope="session")
def geoh5_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A geoh5 file with one Points, Curve, Surface and Grid2D object."""
    path = tmp_path_factory.mktemp("geoh5") / "fixture.geoh5"
    with Workspace.create(path) as ws:
        points = Points.create(ws, name="pts", vertices=POINT_VERTICES)
        points.metadata = CRS_METADATA
        points.add_data(
            {
                "note": {"type": "text", "values": np.array(POINT_TEXT)},
                "grade": {"values": POINT_FLOATS},
                "count": {"values": POINT_INTS},
                "rock": {
                    "type": "referenced",
                    "values": POINT_ROCK_KEYS,
                    "value_map": ROCK_VALUE_MAP,
                },
            }
        )
        curve = Curve.create(
            ws, name="line", vertices=CURVE_VERTICES, cells=CURVE_CELLS
        )
        curve.add_data(
            {
                "seg_id": {"values": np.array([1.0, 2.0, 3.0]), "association": "CELL"},
                "depth": {"values": CURVE_VERTEX_DEPTHS},
            }
        )
        Curve.create(ws, name="bare_line", vertices=CURVE_VERTICES, cells=CURVE_CELLS)
        Surface.create(ws, name="topo", vertices=SURFACE_VERTICES, cells=SURFACE_CELLS)
        zoned = Surface.create(
            ws, name="zoned", vertices=SURFACE_VERTICES, cells=SURFACE_CELLS
        )
        zoned.add_data(
            {"zone": {"values": np.array([7.0, 8.0]), "association": "CELL"}}
        )
        shaded = Surface.create(
            ws, name="shaded", vertices=SURFACE_VERTICES, cells=SURFACE_CELLS
        )
        shaded.add_data(
            {
                "Residual mag": {"values": SHADED_VERTEX_VALUES},
                "zone": {"values": np.array([7.0, 8.0]), "association": "CELL"},
                "label": {"type": "text", "values": np.array(["a", "b", "c", "d"])},
            }
        )
        grid = Grid2D.create(ws, name="grid", **GRID_KWARGS)
        grid.add_data(
            {
                "elev": {
                    "values": np.arange(12, dtype=np.float64),
                    "association": "CELL",
                }
            }
        )
    return path


@pytest.fixture(scope="session")
def grid_issues_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Grid2D objects that cannot (or can only just) be map rasters."""
    path = tmp_path_factory.mktemp("geoh5") / "grid_issues.geoh5"
    cell_data = {
        "v": {"values": np.arange(12, dtype=np.float64), "association": "CELL"}
    }
    with Workspace.create(path) as ws:
        for name, extra in [
            ("dipping", {"dip": -35.0}),
            ("vertical", {"vertical": True}),
            ("nearly_flat", {"dip": 1e-9}),
            ("empty", {}),
            ("text_only", {}),
        ]:
            grid = Grid2D.create(ws, name=name, **GRID_KWARGS, **extra)
            if name in ("dipping", "vertical", "nearly_flat"):
                grid.add_data(cell_data)
            if name == "text_only":
                grid.add_data(
                    {
                        "note": {
                            "type": "text",
                            "values": np.array(["x"] * 12),
                            "association": "CELL",
                        }
                    }
                )
    return path
