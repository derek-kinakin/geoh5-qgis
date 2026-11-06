"""Tests for plugin registration (needs QGIS via pytest-qgis)."""

from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock

from qgis.core import QgsApplication
from qgis.gui import QgsGui

import geoh5_qgis
from geoh5_qgis.__about__ import __version__

PLUGIN_DIR = Path(geoh5_qgis.__file__).parent


def provider_names() -> tuple[list[str], list[str]]:
    items = [p.name() for p in QgsApplication.dataItemProviderRegistry().providers()]
    guis = [p.name() for p in QgsGui.dataItemGuiProviderRegistry().providers()]
    return items, guis


def test_init_gui_registers_and_unload_removes() -> None:
    iface = MagicMock()
    plugin = geoh5_qgis.classFactory(iface)

    plugin.initGui()
    items, guis = provider_names()
    assert "geoh5" in items
    assert "geoh5" in guis
    iface.registerCustomDropHandler.assert_called_once_with(plugin.drop_handler)
    handler = plugin.drop_handler

    plugin.unload()
    items, guis = provider_names()
    assert "geoh5" not in items
    assert "geoh5" not in guis
    iface.unregisterCustomDropHandler.assert_called_once_with(handler)


def test_metadata_matches_package_version() -> None:
    metadata = ConfigParser()
    metadata.read(PLUGIN_DIR / "metadata.txt", encoding="utf-8")
    general = metadata["general"]
    assert general["version"] == __version__
    assert general["qgisMinimumVersion"].startswith("4.")
    for key in ("name", "description", "about", "author", "email", "repository"):
        assert general[key]


def test_reopening_a_project_rebuilds_geoh5_layers(
    geoh5_file, tmp_path, qgis_new_project
) -> None:
    import time

    from qgis.core import QgsProject
    from qgis.PyQt.QtCore import QCoreApplication

    from geoh5_qgis.layers import load_object_layers
    from geoh5_qgis.reader import list_objects, open_workspace

    project = QgsProject.instance()
    with open_workspace(geoh5_file) as ws:
        uid = next(o.uid for o in list_objects(ws) if o.name == "pts")
    project.addMapLayers(load_object_layers(geoh5_file, uid))
    qgz = str(tmp_path / "p.qgz")
    project.write(qgz)
    project.clear()

    iface = MagicMock()
    plugin = geoh5_qgis.classFactory(iface)
    plugin.initGui()
    try:
        project.read(qgz)
        (pts,) = project.mapLayersByName("pts")
        deadline = time.monotonic() + 60
        while pts.featureCount() == 0 and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        assert pts.featureCount() == 4
    finally:
        plugin.unload()
