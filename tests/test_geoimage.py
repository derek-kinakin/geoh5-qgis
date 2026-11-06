"""Tests for GeoImage support (reader, HDF5 blob access and raster layers)."""

from pathlib import Path

import numpy as np
import pytest
from osgeo import gdal
from qgis.core import QgsRasterLayer

from conftest import IMAGE_CORNERS, IMAGE_PIXELS
from geoh5_qgis.hdf5_blob import read_blob
from geoh5_qgis.layers import geoimage_geotransform, load_object_layer
from geoh5_qgis.reader import (
    GEOIMAGE,
    list_objects,
    load_issue,
    open_workspace,
    read_geoimage,
)

ASSETS = Path(r"C:\Users\dkinakin\Dev\geoh5vista\assets")


def object_named(path: Path, name: str):
    with open_workspace(path) as ws:
        return next(o for o in list_objects(ws) if o.name == name)


def test_geoimage_is_identified(image_file: Path) -> None:
    assert object_named(image_file, "photo").kind == GEOIMAGE


def test_read_geoimage_corners_and_blob(image_file: Path) -> None:
    uid = object_named(image_file, "photo").uid
    with open_workspace(image_file) as ws:
        image = read_geoimage(ws, uid)
    np.testing.assert_allclose(image.corners, IMAGE_CORNERS)
    assert image.blob_path is not None
    blob = read_blob(image_file, image.blob_path)
    gdal.FileFromMemBuffer("/vsimem/test_blob", blob)
    try:
        with gdal.ExceptionMgr(useExceptions=True):
            decoded = gdal.Open("/vsimem/test_blob").ReadAsArray()
    finally:
        gdal.Unlink("/vsimem/test_blob")
    np.testing.assert_array_equal(np.moveaxis(decoded, 0, -1)[..., :3], IMAGE_PIXELS)


def test_geoimage_geotransform_from_corners() -> None:
    gt = geoimage_geotransform(IMAGE_CORNERS, width=4, height=3)
    assert gt == pytest.approx((100.0, 10.0, 0.0, 530.0, 0.0, -10.0))


def test_geoimage_layer_places_pixels(image_file: Path) -> None:
    layer = load_object_layer(image_file, object_named(image_file, "photo").uid)
    assert isinstance(layer, QgsRasterLayer)
    assert layer.isValid()
    assert layer.bandCount() >= 3
    extent = layer.extent()
    assert (extent.xMinimum(), extent.xMaximum()) == pytest.approx((100.0, 140.0))
    assert (extent.yMinimum(), extent.yMaximum()) == pytest.approx((500.0, 530.0))
    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(layer.source())
    rgb = np.moveaxis(dataset.ReadAsArray(), 0, -1)[..., :3]
    np.testing.assert_array_equal(rgb, IMAGE_PIXELS)


def test_tilted_geoimage_has_issue(image_file: Path) -> None:
    info = object_named(image_file, "tilted")
    with open_workspace(image_file) as ws:
        issue = load_issue(ws, info)
        flat = load_issue(ws, object_named(image_file, "photo"))
    assert issue is not None and "tilted" in issue
    assert flat is None


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_ga_geoimage() -> None:
    path = ASSETS / "workspace" / "workspace_complex.geoh5"
    info = object_named(path, "FlinFlon_geology")
    assert info.kind == GEOIMAGE
    with open_workspace(path) as ws:
        image = read_geoimage(ws, info.uid)
        assert load_issue(ws, info) is None
    assert read_blob(path, image.blob_path)[:3] == b"\xff\xd8\xff"  # JPEG
    layer = load_object_layer(path, info.uid)
    assert layer.isValid()
    assert (layer.width(), layer.height()) == (832, 1112)
    extent = layer.extent()
    assert extent.xMinimum() == pytest.approx(311436.27, abs=0.01)
    assert extent.yMaximum() == pytest.approx(6076690.47, abs=0.01)
