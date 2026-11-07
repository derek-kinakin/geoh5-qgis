"""Tests for rebuilding geoh5 layers when a saved project is reopened."""

import re
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from geoh5py.workspace import Workspace as Geoh5pyWorkspace
from qgis.core import QgsProject
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QColor

from geoh5_qgis import tempfiles
from geoh5_qgis.layers import PART_PROPERTY, load_object_layers
from geoh5_qgis.reader import list_objects, open_workspace
from geoh5_qgis.restore import restore_project, restore_project_in_background

NAMES = ["pts", "line", "grid", "topo", "shaded"]


@pytest.fixture
def project(qgis_new_project) -> QgsProject:
    return QgsProject.instance()


@pytest.fixture
def geoh5_copy(geoh5_file: Path, tmp_path: Path) -> Path:
    """A private copy of the fixture file that tests may edit or delete."""
    return Path(shutil.copy(geoh5_file, tmp_path / "copy.geoh5"))


def uids(path: Path) -> dict[str, str]:
    with open_workspace(path) as ws:
        return {o.name: o.uid for o in list_objects(ws)}


def save_and_reopen(project: QgsProject, path: Path, tmp_path: Path) -> None:
    """Load layers, save, then reopen as after a restart (in-memory data gone)."""
    for name in NAMES:
        project.addMapLayers(load_object_layers(path, uids(path)[name]))
    project.mapLayersByName("pts")[0].renderer().symbol().setColor(QColor("#123456"))
    qgz = str(tmp_path / "saved.qgz")
    assert project.write(qgz)
    project.clear()
    tempfiles.cleanup_session()  # what a restart would do
    assert project.read(qgz)


def layer(project: QgsProject, name: str):
    (found,) = project.mapLayersByName(name)
    return found


def test_reopened_project_starts_empty(geoh5_copy, project, tmp_path) -> None:
    save_and_reopen(project, geoh5_copy, tmp_path)
    assert layer(project, "pts").featureCount() == 0
    assert not layer(project, "grid: elev").isValid()
    assert layer(project, "line vertices").customProperty(PART_PROPERTY) == "vertices"


def test_restore_refills_the_same_layers(geoh5_copy, project, tmp_path) -> None:
    save_and_reopen(project, geoh5_copy, tmp_path)
    ids_before = {lyr.name(): lyr.id() for lyr in project.mapLayers().values()}

    assert restore_project(project) == []

    assert {lyr.name(): lyr.id() for lyr in project.mapLayers().values()} == ids_before
    pts = layer(project, "pts")
    assert pts.featureCount() == 4
    assert pts.renderer().symbol().color().name() == "#123456"  # style kept
    assert layer(project, "line").featureCount() == 3
    assert layer(project, "line vertices").featureCount() == 5
    assert layer(project, "topo").isValid()  # mesh by default
    grid = layer(project, "grid: elev")
    assert grid.isValid()
    assert grid.bandCount() == 1
    mesh = layer(project, "shaded")
    assert mesh.isValid()
    assert mesh.datasetGroupCount() == 3  # Z, Residual mag, zone
    assert mesh.renderer3D() is not None


def test_restore_picks_up_edits_made_in_the_geoh5(
    geoh5_copy, project, tmp_path
) -> None:
    save_and_reopen(project, geoh5_copy, tmp_path)
    with Geoh5pyWorkspace(geoh5_copy, mode="r+") as ws:
        ws.get_entity("pts")[0].add_data({"added": {"values": np.arange(4.0)}})

    assert restore_project(project) == []

    pts = layer(project, "pts")
    assert "added" in pts.fields().names()
    assert [f["added"] for f in pts.getFeatures()] == [0.0, 1.0, 2.0, 3.0]


def test_restore_reports_a_missing_file(geoh5_copy, project, tmp_path) -> None:
    save_and_reopen(project, geoh5_copy, tmp_path)
    geoh5_copy.unlink()

    errors = restore_project(project)

    assert errors and all("copy.geoh5" in e for e in errors)
    assert layer(project, "pts").featureCount() == 0  # left as it was


def test_restore_in_background(geoh5_copy, project, tmp_path) -> None:
    save_and_reopen(project, geoh5_copy, tmp_path)
    bar = MagicMock()
    task = restore_project_in_background(project, bar)
    assert task is not None
    deadline = time.monotonic() + 60
    while layer(project, "pts").featureCount() == 0 and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    QCoreApplication.processEvents()
    assert layer(project, "pts").featureCount() == 4
    assert layer(project, "grid: elev").isValid()
    bar.pushWarning.assert_not_called()


def test_nothing_to_restore(project) -> None:
    assert restore_project_in_background(project, None) is None
    assert restore_project(project) == []


@pytest.fixture
def source_hooks():
    """Install the plugin's path writer/preprocessor, as initGui does."""
    from qgis.core import QgsPathResolver

    from geoh5_qgis.restore import preprocess_source, write_source

    writer = QgsPathResolver.setPathWriter(write_source)
    preprocessor = QgsPathResolver.setPathPreprocessor(preprocess_source)
    yield
    QgsPathResolver.removePathWriter(writer)
    QgsPathResolver.removePathPreprocessor(preprocessor)


def test_generated_rasters_reopen_valid_without_a_bad_layer_prompt(
    geoh5_copy, project, tmp_path, source_hooks
) -> None:
    import zipfile

    save_and_reopen(project, geoh5_copy, tmp_path)
    with zipfile.ZipFile(tmp_path / "saved.qgz") as archive:
        xml = archive.read(next(n for n in archive.namelist() if n.endswith(".qgs")))
    assert b"geoh5:" in xml  # token saved instead of the temp file path
    # No layer data source points at a temp file (labels such as a mesh dataset
    # group's description may still mention one; they do not affect loading).
    sources = re.findall(rb"<datasource>(.*?)</datasource>", xml)
    assert sources and not any(tempfiles.PREFIX.encode() in s for s in sources)
    # Valid straight after reading, before any background rebuild.
    grid = layer(project, "grid: elev")
    assert grid.isValid()
    assert grid.bandCount() == 1


def test_missing_geoh5_leaves_raster_for_qgis_to_report(
    geoh5_copy, project, tmp_path, source_hooks
) -> None:
    for name in NAMES:
        project.addMapLayers(load_object_layers(geoh5_copy, uids(geoh5_copy)[name]))
    qgz = str(tmp_path / "saved.qgz")
    project.write(qgz)
    project.clear()
    geoh5_copy.unlink()
    project.read(qgz)
    assert not layer(project, "grid: elev").isValid()
    assert layer(project, "grid: elev").source().startswith("geoh5:")


def test_old_projects_without_parts_match_interval_layers_by_name(
    drillhole_file, project, tmp_path
) -> None:
    from geoh5_qgis.reader import DRILLHOLES, read_tree, tree_objects

    with open_workspace(drillhole_file) as ws:
        uid = next(n.uid for n in tree_objects(read_tree(ws)) if n.kind == DRILLHOLES)
    layers = load_object_layers(drillhole_file, uid)
    for built in layers:
        built.removeCustomProperty(PART_PROPERTY)  # as saved before parts existed
    project.addMapLayers(layers)
    qgz = str(tmp_path / "old.qgz")
    project.write(qgz)
    project.clear()
    project.read(qgz)
    assert restore_project(project) == []
    assert layer(project, "DH assay").featureCount() == 3


def test_polygon_surfaces_are_restored_as_polygons(
    geoh5_copy, project, tmp_path
) -> None:
    project.addMapLayers(
        load_object_layers(geoh5_copy, uids(geoh5_copy)["zoned"], as_polygons=True)
    )
    qgz = str(tmp_path / "polygons.qgz")
    project.write(qgz)
    project.clear()
    project.read(qgz)
    assert restore_project(project) == []
    zoned = layer(project, "zoned")
    assert zoned.featureCount() == 2
    assert "z_mean" in zoned.fields().names()


def two_channel_grid(path: Path) -> Path:
    from geoh5py.objects import Grid2D

    from conftest import GRID_KWARGS

    with Geoh5pyWorkspace.create(path) as ws:
        grid = Grid2D.create(ws, name="g2", **GRID_KWARGS)
        grid.add_data(
            {
                "first": {"values": np.arange(12.0), "association": "CELL"},
                "second": {"values": np.arange(12.0) + 100, "association": "CELL"},
            }
        )
    return path


def band_description(layer) -> str:
    from osgeo import gdal

    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(layer.source())  # keep: bands die with it
        return dataset.GetRasterBand(1).GetDescription()


def test_each_saved_grid_channel_is_rebuilt_as_itself(
    project, tmp_path, source_hooks
) -> None:
    from geoh5_qgis.layers import ALL_CHANNELS

    path = two_channel_grid(tmp_path / "g2.geoh5")
    project.addMapLayers(
        load_object_layers(path, uids(path)["g2"], channels=ALL_CHANNELS)
    )
    qgz = str(tmp_path / "grids.qgz")
    project.write(qgz)
    project.clear()
    tempfiles.cleanup_session()
    project.read(qgz)
    assert restore_project(project) == []
    for name in ("first", "second"):
        raster = layer(project, f"g2: {name}")
        assert raster.isValid()
        assert band_description(raster) == name


def test_old_single_band_grid_layer_is_restored(geoh5_copy, project, tmp_path) -> None:
    (grid,) = load_object_layers(geoh5_copy, uids(geoh5_copy)["grid"])
    grid.setCustomProperty(PART_PROPERTY, "main")  # as saved before channels
    grid.setName("grid")
    project.addMapLayer(grid)
    qgz = str(tmp_path / "old_grid.qgz")
    project.write(qgz)
    project.clear()
    tempfiles.cleanup_session()
    project.read(qgz)
    assert restore_project(project) == []
    assert layer(project, "grid").isValid()
