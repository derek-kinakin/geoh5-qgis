"""Shared fixtures: geoh5 files written with geoh5py (test-only dependency)."""

from pathlib import Path

import h5py
import numpy as np
import pytest
from geoh5py.groups import ContainerGroup
from geoh5py.objects import Curve, GeoImage, Grid2D, Points, Surface
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
# 3 rows x 4 columns RGB; pixel (row, col) = (10*row, 10*col, 200).
IMAGE_PIXELS = np.array(
    [[[10 * r, 10 * c, 200] for c in range(4)] for r in range(3)], dtype=np.uint8
)
# Corners: top-left, top-right, bottom-right, bottom-left (geoh5py order).
IMAGE_CORNERS = np.array(
    [[100.0, 530.0, 5.0], [140.0, 530.0, 5.0], [140.0, 500.0, 5.0], [100.0, 500.0, 5.0]]
)
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


def write_tree_file(path: Path) -> Path:
    """Root: Empty/, Folder A/{Sub/{line_b}, pts_a}, top_pts."""
    with Workspace.create(path) as ws:
        folder = ContainerGroup.create(ws, name="Folder A")
        sub = ContainerGroup.create(ws, name="Sub", parent=folder)
        ContainerGroup.create(ws, name="Empty")
        Points.create(ws, name="pts_a", vertices=POINT_VERTICES, parent=folder)
        Curve.create(
            ws, name="line_b", vertices=CURVE_VERTICES, cells=CURVE_CELLS, parent=sub
        )
        Points.create(ws, name="top_pts", vertices=POINT_VERTICES)
    return path


@pytest.fixture(scope="session")
def tree_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A geoh5 file with nested container groups."""
    return write_tree_file(tmp_path_factory.mktemp("geoh5") / "tree.geoh5")


@pytest.fixture
def orphan_tree_file(tmp_path: Path) -> Path:
    """tree file where top_pts is unlinked from the root (as after a GA delete)."""
    path = write_tree_file(tmp_path / "orphan.geoh5")
    with h5py.File(path, "r+") as f:
        project = f["GEOSCIENCE"]
        root_objects = project["Root"]["Objects"]
        top = next(
            k for k in root_objects if root_objects[k].attrs["Name"] == "top_pts"
        )
        del root_objects[top]  # still in the flat /Objects index
    return path


@pytest.fixture
def rootless_tree_file(tmp_path: Path) -> Path:
    """tree file with /Root and the indexed root group removed (a forest)."""
    path = write_tree_file(tmp_path / "rootless.geoh5")
    with h5py.File(path, "r+") as f:
        project = f["GEOSCIENCE"]
        root_id = project["Root"].attrs["ID"]
        root_id = root_id.decode() if isinstance(root_id, bytes) else root_id
        del project["Root"]
        del project["Groups"][root_id]
    return path


@pytest.fixture(scope="session")
def image_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """GeoImages: 'photo' (flat, 4x3 RGB) and 'tilted' (corners at varying z)."""
    path = tmp_path_factory.mktemp("geoh5") / "images.geoh5"
    with Workspace.create(path) as ws:
        GeoImage.create(ws, name="photo", image=IMAGE_PIXELS, vertices=IMAGE_CORNERS)
        tilted = IMAGE_CORNERS.copy()
        tilted[:2, 2] += 50.0  # top edge raised
        GeoImage.create(ws, name="tilted", image=IMAGE_PIXELS, vertices=tilted)
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
