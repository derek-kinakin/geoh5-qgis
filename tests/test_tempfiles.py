"""Tests for temp-file backed rasters: location, GDAL tools, cleanup."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from qgis.core import QgsProject
from qgis.PyQt.QtCore import QCoreApplication

import geoh5_qgis
from geoh5_qgis import tempfiles
from geoh5_qgis.layers import load_object_layer
from geoh5_qgis.reader import list_objects, open_workspace


def uid_of(path: Path, name: str) -> str:
    with open_workspace(path) as ws:
        return next(o.uid for o in list_objects(ws) if o.name == name)


def test_rasters_are_files_in_the_session_folder(geoh5_file, image_file) -> None:
    grid = load_object_layer(geoh5_file, uid_of(geoh5_file, "grid"))
    photo = load_object_layer(image_file, uid_of(image_file, "photo"))
    for layer in (grid, photo):
        assert tempfiles.is_temp_file(layer.source())
        assert Path(layer.source()).is_file()
    # A GeoImage is its image file plus the VRT the layer opens.
    suffixes = sorted(p.suffix for p in tempfiles.files_of(photo.source()))
    assert len(suffixes) == 2 and ".vrt" in suffixes


def test_gdal_command_line_tools_can_read_the_raster(geoh5_file, tmp_path) -> None:
    from processing.core.Processing import Processing

    Processing.initialize()
    import processing

    grid = load_object_layer(geoh5_file, uid_of(geoh5_file, "grid"))
    out = tmp_path / "translated.tif"
    processing.run("gdal:translate", {"INPUT": grid, "OUTPUT": str(out)})
    assert out.is_file()


def test_removing_a_layer_deletes_its_files(geoh5_file, qgis_new_project) -> None:
    plugin = geoh5_qgis.classFactory(MagicMock())
    plugin.initGui()
    try:
        project = QgsProject.instance()
        grid = load_object_layer(geoh5_file, uid_of(geoh5_file, "grid"))
        project.addMapLayer(grid)
        files = tempfiles.files_of(grid.source())
        assert files and all(f.exists() for f in files)
        project.removeMapLayer(grid.id())
        deadline = time.monotonic() + 10
        while any(f.exists() for f in files) and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.05)
        assert not any(f.exists() for f in files)
    finally:
        plugin.unload()


def test_sweep_removes_only_old_folders(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tempfiles.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / f"{tempfiles.PREFIX}old"
    recent = tmp_path / f"{tempfiles.PREFIX}recent"
    unrelated = tmp_path / "something_else"
    for folder in (old, recent, unrelated):
        folder.mkdir()
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    os.utime(unrelated, (week_ago, week_ago))
    tempfiles.sweep_stale()
    assert not old.exists()
    assert recent.exists()  # may belong to another running QGIS
    assert unrelated.exists()


@pytest.mark.parametrize("source", ["", "C:/elsewhere/x.tif", "/vsimem/x.tif"])
def test_other_sources_are_not_temp_files(source) -> None:
    assert not tempfiles.is_temp_file(source)
    assert tempfiles.files_of(source) == []
