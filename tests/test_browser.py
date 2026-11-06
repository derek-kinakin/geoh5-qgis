"""Tests for the Browser panel integration (needs QGIS via pytest-qgis)."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from qgis.core import Qgis, QgsProject, QgsTask
from qgis.gui import QgsDataItemGuiContext
from qgis.PyQt.QtCore import QCoreApplication

from geoh5_qgis.browser import (
    PROVIDER_KEY,
    Geoh5DataItemGuiProvider,
    Geoh5DataItemProvider,
    Geoh5DropHandler,
    Geoh5FileItem,
    Geoh5GroupItem,
    Geoh5ObjectItem,
    add_objects,
    add_objects_in_background,
    loadable_object_uids,
    parse_object_uri,
)
from geoh5_qgis.reader import list_objects, open_workspace


@pytest.fixture
def project(qgis_new_project) -> QgsProject:
    return QgsProject.instance()


def file_item(path: Path) -> Geoh5FileItem:
    item = Geoh5DataItemProvider().createDataItem(str(path), None)
    assert isinstance(item, Geoh5FileItem)
    return item


def object_items(path: Path) -> dict[str, Geoh5ObjectItem]:
    return {child.name(): child for child in file_item(path).createChildren()}


def uid_of(path: Path, name: str) -> str:
    with open_workspace(path) as ws:
        return next(o.uid for o in list_objects(ws) if o.name == name)


def test_provider_only_handles_geoh5_files(geoh5_file: Path, tmp_path: Path) -> None:
    provider = Geoh5DataItemProvider()
    assert provider.capabilities() == Qgis.DataItemProviderCapability.Files
    assert provider.createDataItem(str(tmp_path / "x.tif"), None) is None
    assert isinstance(provider.createDataItem(str(geoh5_file), None), Geoh5FileItem)


def test_file_item_lists_objects_sorted_by_name(geoh5_file: Path) -> None:
    items = object_items(geoh5_file)
    assert list(items) == [
        "bare_line",
        "grid",
        "line",
        "pts",
        "shaded",
        "topo",
        "zoned",
    ]
    assert all(isinstance(i, Geoh5ObjectItem) for i in items.values())
    assert items["pts"].kind == "Points"
    assert items["pts"].uid == uid_of(geoh5_file, "pts")


def test_unreadable_file_shows_error_item(tmp_path: Path) -> None:
    bad = tmp_path / "broken.geoh5"
    bad.write_bytes(b"not an hdf5 file")
    children = file_item(bad).createChildren()
    assert len(children) == 1
    assert children[0].type() == Qgis.BrowserItemType.Error


def test_object_item_mime_uri_round_trips(geoh5_file: Path) -> None:
    item = object_items(geoh5_file)["line"]
    assert item.hasDragEnabled()
    (uri,) = item.mimeUris()
    assert uri.providerKey == PROVIDER_KEY
    assert uri.layerType == "custom"
    assert uri.name == "line"
    assert parse_object_uri(uri.uri) == (str(geoh5_file), item.uid)


def test_add_objects_adds_layers_and_reports_failures(
    geoh5_file: Path, project: QgsProject
) -> None:
    uids = [uid_of(geoh5_file, "pts"), "{00000000-0000-0000-0000-000000000000}"]
    layers, errors = add_objects(geoh5_file, uids, project)
    assert [layer.name() for layer in layers] == ["pts"]
    assert len(errors) == 1
    assert [layer.name() for layer in project.mapLayers().values()] == ["pts"]


def test_double_click_adds_object_layer(geoh5_file: Path, project: QgsProject) -> None:
    item = object_items(geoh5_file)["topo"]
    handled = Geoh5DataItemGuiProvider().handleDoubleClick(
        item, QgsDataItemGuiContext()
    )
    assert handled is True
    assert layer_names_when_loaded(project, 1) == ["topo"]


def test_double_click_ignores_other_items(geoh5_file: Path) -> None:
    provider = Geoh5DataItemGuiProvider()
    context = QgsDataItemGuiContext()
    # False lets QGIS do its default (expand/collapse) for the file item.
    assert provider.handleDoubleClick(file_item(geoh5_file), context) is False
    assert provider.handleDoubleClick(None, QgsDataItemGuiContext()) is False


def test_drop_handler_adds_layer(geoh5_file: Path, project: QgsProject) -> None:
    (uri,) = object_items(geoh5_file)["grid"].mimeUris()
    handler = Geoh5DropHandler()
    assert handler.customUriProviderKey() == PROVIDER_KEY
    handler.handleCustomUriDrop(uri)
    assert layer_names_when_loaded(project, 1) == ["grid"]


def test_add_objects_adds_every_layer_of_an_object(
    geoh5_file: Path, project: QgsProject
) -> None:
    layers, errors = add_objects(geoh5_file, [uid_of(geoh5_file, "line")], project)
    assert errors == []
    assert [layer.name() for layer in layers] == ["line", "line vertices"]
    assert len(project.mapLayers()) == 2


def test_blocked_object_items_explain_why(grid_issues_file: Path) -> None:
    items = object_items(grid_issues_file)
    dipping = items["dipping"]
    assert dipping.issue is not None
    assert "dipping" in dipping.toolTip()
    assert not dipping.hasDragEnabled()
    assert items["nearly_flat"].issue is None
    assert items["nearly_flat"].hasDragEnabled()


def test_double_click_on_blocked_item_reports_and_adds_nothing(
    grid_issues_file: Path, project: QgsProject
) -> None:
    item = object_items(grid_issues_file)["vertical"]
    context = MagicMock()
    assert Geoh5DataItemGuiProvider().handleDoubleClick(item, context) is True
    assert project.mapLayers() == {}
    message = context.messageBar().pushWarning.call_args.args[1]
    assert "vertical" in message


def test_loadable_object_uids_skips_blocked_objects(grid_issues_file: Path) -> None:
    assert loadable_object_uids(grid_issues_file) == [
        uid_of(grid_issues_file, "nearly_flat")
    ]


def children_outline(item) -> list:
    return [
        (c.name(), "Group", children_outline(c))
        if isinstance(c, Geoh5GroupItem)
        else (c.name(), c.kind)
        for c in item.createChildren()
    ]


def test_file_item_shows_ga_group_folders(tree_file: Path) -> None:
    assert children_outline(file_item(tree_file)) == [
        ("Empty", "Group", []),
        (
            "Folder A",
            "Group",
            [("Sub", "Group", [("line_b", "Curve")]), ("pts_a", "Points")],
        ),
        ("top_pts", "Points"),
    ]


def test_group_item_paths_are_unique(tree_file: Path) -> None:
    folder = next(
        c for c in file_item(tree_file).createChildren() if c.name() == "Folder A"
    )
    paths = [c.path() for c in folder.createChildren()]
    assert len(set(paths)) == len(paths)
    assert all(p.startswith(folder.path() + "/") for p in paths)


def test_group_item_lists_its_loadable_objects(tree_file: Path) -> None:
    folder = next(
        c for c in file_item(tree_file).createChildren() if c.name() == "Folder A"
    )
    assert folder.loadable_uids() == [
        uid_of(tree_file, "line_b"),
        uid_of(tree_file, "pts_a"),
    ]


def test_add_all_uses_the_tree_not_the_flat_index(orphan_tree_file: Path) -> None:
    uids = loadable_object_uids(orphan_tree_file)
    assert uid_of(orphan_tree_file, "top_pts") not in uids
    assert len(uids) == 2


def layer_names_when_loaded(
    project: QgsProject, count: int, timeout: float = 60.0
) -> list[str]:
    """Process Qt events until the project has ``count`` layers (background load)."""
    deadline = time.monotonic() + timeout
    while len(project.mapLayers()) < count and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return sorted(layer.name() for layer in project.mapLayers().values())


def test_background_load_adds_layers_and_reports_errors(
    geoh5_file: Path, project: QgsProject
) -> None:
    bar = MagicMock()
    uids = [uid_of(geoh5_file, "line"), "{00000000-0000-0000-0000-000000000000}"]
    task = add_objects_in_background(geoh5_file, uids, project, bar)
    assert isinstance(task, QgsTask)
    assert layer_names_when_loaded(project, 2) == ["line", "line vertices"]
    deadline = time.monotonic() + 10
    while not bar.pushWarning.called and time.monotonic() < deadline:
        QCoreApplication.processEvents()
    assert "0000" in bar.pushWarning.call_args.args[1]
    main = QCoreApplication.instance().thread()
    assert all(layer.thread() is main for layer in project.mapLayers().values())


def test_background_load_of_a_mesh_and_raster(
    geoh5_file: Path, project: QgsProject
) -> None:
    uids = [uid_of(geoh5_file, "shaded"), uid_of(geoh5_file, "grid")]
    add_objects_in_background(geoh5_file, uids, project)
    assert layer_names_when_loaded(project, 2) == ["grid", "shaded"]
    assert all(layer.isValid() for layer in project.mapLayers().values())
    (mesh,) = project.mapLayersByName("shaded")
    assert mesh.renderer3D() is not None  # survives the worker-thread build
