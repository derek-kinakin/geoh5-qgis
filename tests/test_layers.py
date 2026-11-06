"""Tests for building QGIS layers from geoh5 objects (needs QGIS via pytest-qgis)."""

from pathlib import Path

import numpy as np
import pytest
from geoh5py.workspace import Workspace as Geoh5pyWorkspace
from osgeo import gdal
from qgis.core import NULL, QgsPointXY, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes

from conftest import (
    CURVE_CELLS,
    POINT_INTS,
    POINT_TEXT,
    POINT_VERTICES,
    SURFACE_CELLS,
    SURFACE_VERTICES,
)
from geoh5_qgis.layers import curve_parts, load_object_layer
from geoh5_qgis.reader import Grid2DFrame, list_objects, open_workspace

ASSETS = Path(r"C:\Users\dkinakin\Dev\geoh5vista\assets")


def uid_of(path: Path, name: str) -> str:
    with open_workspace(path) as ws:
        return next(o.uid for o in list_objects(ws) if o.name == name)


def attribute_rows(layer: QgsVectorLayer) -> list[dict]:
    names = layer.fields().names()
    return [dict(zip(names, f.attributes(), strict=True)) for f in layer.getFeatures()]


def test_points_layer(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "pts"))
    assert isinstance(layer, QgsVectorLayer)
    assert layer.isValid()
    assert layer.name() == "pts"
    assert layer.wkbType() == QgsWkbTypes.Type.PointZ
    assert layer.crs().authid() == "EPSG:26914"
    assert layer.featureCount() == len(POINT_VERTICES)

    feats = list(layer.getFeatures())
    xyz = [
        (
            f.geometry().constGet().x(),
            f.geometry().constGet().y(),
            f.geometry().constGet().z(),
        )
        for f in feats
    ]
    np.testing.assert_allclose(xyz, POINT_VERTICES)

    rows = attribute_rows(layer)
    assert [r["grade"] for r in rows] == [1.5, NULL, 3.5, 4.5]
    assert [r["count"] for r in rows] == POINT_INTS.tolist()
    assert [r["rock"] for r in rows] == ["granite", "basalt", "granite", "Unknown"]
    assert [r["note"] for r in rows] == POINT_TEXT


def test_layer_records_its_geoh5_source(geoh5_file: Path) -> None:
    uid = uid_of(geoh5_file, "pts")
    layer = load_object_layer(geoh5_file, uid)
    assert layer.customProperty("geoh5/path") == str(geoh5_file)
    assert layer.customProperty("geoh5/uid") == uid


@pytest.mark.parametrize("name", ["topo", "line", "grid"])
def test_missing_crs_is_left_unset(geoh5_file: Path, name: str) -> None:
    # The memory provider would otherwise default to EPSG:4326.
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, name))
    assert not layer.crs().isValid()


def test_crs_override(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "topo"), crs="EPSG:32611")
    assert layer.crs().authid() == "EPSG:32611"


def test_curve_parts_groups_connected_segments() -> None:
    np.testing.assert_array_equal(curve_parts(CURVE_CELLS), [0, 0, 1])


def test_curve_layer(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "line"))
    assert layer.wkbType() == QgsWkbTypes.Type.LineStringZ
    assert layer.featureCount() == len(CURVE_CELLS)
    rows = attribute_rows(layer)
    assert [r["part"] for r in rows] == [0, 0, 1]
    assert [r["seg_id"] for r in rows] == [1.0, 2.0, 3.0]
    first = next(layer.getFeatures()).geometry().constGet()
    assert [(p.x(), p.y(), p.z()) for p in first.vertices()] == [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
    ]


def test_surface_layer(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "topo"))
    assert layer.wkbType() == QgsWkbTypes.Type.PolygonZ
    assert layer.featureCount() == len(SURFACE_CELLS)
    ring = next(layer.getFeatures()).geometry().constGet().exteriorRing()
    coords = [(p.x(), p.y(), p.z()) for p in ring.vertices()]
    expected = [tuple(SURFACE_VERTICES[i]) for i in [0, 1, 2, 0]]
    assert coords == expected


def assert_raster_matches_geoh5py(path: Path, name: str, data_name: str) -> None:
    """geoh5py's cell centroids must hit the matching pixel of the written raster.

    Checked with GDAL on the raster file itself: QGIS displays rotated rasters
    through an on-the-fly north-up warp, which resamples cells near edges.
    """
    with Geoh5pyWorkspace(path, mode="r") as ws:
        grid = ws.get_entity(name)[0]
        centroids = grid.centroids.copy()
        values = grid.get_data(data_name)[0].values.copy()
    layer = load_object_layer(path, uid_of(path, name))
    assert isinstance(layer, QgsRasterLayer)
    assert layer.isValid()
    assert all(layer.extent().contains(QgsPointXY(x, y)) for x, y, _ in centroids)

    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(layer.source())
    band = next(
        dataset.GetRasterBand(b)
        for b in range(1, dataset.RasterCount + 1)
        if dataset.GetRasterBand(b).GetDescription() == data_name
    )
    pixels = band.ReadAsArray()
    inverse = gdal.InvGeoTransform(dataset.GetGeoTransform())
    cols_rows = [gdal.ApplyGeoTransform(inverse, x, y) for x, y, _ in centroids]
    sampled = [pixels[int(row), int(col)] for col, row in cols_rows]
    np.testing.assert_allclose(sampled, values, rtol=1e-6, equal_nan=True)


def test_grid2d_layer_values_land_on_cell_centres(geoh5_file: Path) -> None:
    assert_raster_matches_geoh5py(geoh5_file, "grid", "elev")


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_rotated_grid2d() -> None:
    path = ASSETS / "grid2d" / "grid2d_test_a.geoh5"
    assert_raster_matches_geoh5py(path, "airborne data", "Bouguer Gravity")


def test_dipping_grid2d_is_rejected() -> None:
    from geoh5_qgis.layers import grid2d_layer

    frame = Grid2DFrame((0.0, 0.0, 0.0), 1.0, 1.0, 2, 2, 0.0, -35.0, False)
    with pytest.raises(ValueError, match="dip"):
        grid2d_layer("g", "{x}", frame, {"v": np.zeros(4)}, None)
