"""Browser panel integration: expand .geoh5 files and add their objects.

QGIS requires subclasses for data items and providers, so this module has a few
thin classes. The work they trigger is done by plain functions
(``add_objects``, ``object_uri``, ``parse_object_uri``).

Interaction:
- A .geoh5 file expands to one item per object.
- Double-click an object, drag it onto the map, or use the context menu to add
  it to the project. The file's context menu can add all supported objects.
"""

from pathlib import Path

from qgis.core import (
    Qgis,
    QgsDataCollectionItem,
    QgsDataItem,
    QgsDataItemProvider,
    QgsErrorItem,
    QgsLayerItem,
    QgsMapLayer,
    QgsMimeDataUtils,
    QgsProject,
)
from qgis.gui import QgsCustomDropHandler, QgsDataItemGuiProvider
from qgis.PyQt.QtGui import QAction

from geoh5_qgis.layers import load_object_layers
from geoh5_qgis.reader import (
    CURVE,
    GRID2D,
    POINTS,
    SURFACE,
    UNSUPPORTED,
    ObjectInfo,
    list_objects,
    open_workspace,
)

PROVIDER_KEY = "geoh5"
URI_SEPARATOR = "|"
MESSAGE_TITLE = "geoh5"

_KIND_ICONS = {
    POINTS: QgsLayerItem.iconPoint,
    CURVE: QgsLayerItem.iconLine,
    SURFACE: QgsLayerItem.iconPolygon,
    GRID2D: QgsLayerItem.iconRaster,
}


def add_objects(
    path: str | Path, uids: list[str], project: QgsProject
) -> tuple[list[QgsMapLayer], list[str]]:
    """Load geoh5 objects as layers and add them to a project.

    Args:
        path: Path to the .geoh5 file.
        uids: Object group names to load.
        project: Project to add the layers to.

    Returns:
        The added layers (an object can give more than one, e.g. a curve and
        its vertices), and one error message per object that failed.
    """
    results = [_try_load(path, uid) for uid in uids]
    layers = [layer for r in results if isinstance(r, list) for layer in r]
    errors = [r for r in results if isinstance(r, str)]
    project.addMapLayers(layers)
    return layers, errors


def object_uri(path: str | Path, uid: str) -> str:
    """URI identifying one object in a geoh5 file (``<path>|<uid>``).

    Args:
        path: Path to the .geoh5 file.
        uid: Object group name.

    Returns:
        The URI string.
    """
    return f"{path}{URI_SEPARATOR}{uid}"


def parse_object_uri(uri: str) -> tuple[str, str]:
    """Split an object URI back into file path and object UID.

    Args:
        uri: A URI made by object_uri().

    Returns:
        The file path and object UID.
    """
    path, _, uid = uri.rpartition(URI_SEPARATOR)
    return path, uid


def _try_load(path: str | Path, uid: str) -> list[QgsMapLayer] | str:
    try:
        return load_object_layers(path, uid)
    except (KeyError, ValueError, RuntimeError, FileNotFoundError) as exc:
        return f"{Path(path).name} {uid}: {exc}"


def _report(errors: list[str], message_bar) -> None:
    if errors and message_bar is not None:
        message_bar.pushWarning(MESSAGE_TITLE, "; ".join(errors))


class Geoh5DataItemProvider(QgsDataItemProvider):
    """Creates a Browser item for each .geoh5 file in a directory."""

    def name(self) -> str:
        return "geoh5"

    def dataProviderKey(self) -> str:
        return PROVIDER_KEY

    def capabilities(self) -> Qgis.DataItemProviderCapability:
        return Qgis.DataItemProviderCapability.Files

    def createDataItem(self, path: str, parentItem: QgsDataItem | None):
        if not path or not path.lower().endswith(".geoh5"):
            return None
        return Geoh5FileItem(parentItem, Path(path).name, path)


class Geoh5FileItem(QgsDataCollectionItem):
    """A .geoh5 file; its children are the file's objects."""

    def __init__(self, parent: QgsDataItem | None, name: str, path: str) -> None:
        super().__init__(parent, name, path, PROVIDER_KEY)
        self.file_path = path
        self.setCapabilitiesV2(
            Qgis.BrowserItemCapability.Fertile
            | Qgis.BrowserItemCapability.ItemRepresentsFile
        )
        self.setToolTip(path)

    def createChildren(self) -> list[QgsDataItem]:
        try:
            with open_workspace(self.file_path) as workspace:
                objects = list_objects(workspace)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            return [QgsErrorItem(self, f"Cannot read file: {exc}", f"{self.path()}/!")]
        ordered = sorted(objects, key=lambda o: (o.name.lower(), o.uid))
        return [Geoh5ObjectItem(self, info, self.file_path) for info in ordered]


class Geoh5ObjectItem(QgsDataItem):
    """One object in a .geoh5 file. Adding it loads a layer."""

    def __init__(self, parent: QgsDataItem, info: ObjectInfo, file_path: str) -> None:
        super().__init__(
            Qgis.BrowserItemType.Custom,
            parent,
            info.name,
            f"{parent.path()}/{info.uid}",
            PROVIDER_KEY,
        )
        self.file_path = file_path
        self.uid = info.uid
        self.kind = info.kind
        self.setState(Qgis.BrowserItemState.Populated)  # no children
        icon = _KIND_ICONS.get(info.kind, QgsLayerItem.iconDefault)
        self.setIcon(icon())
        note = " (not supported yet)" if info.kind == UNSUPPORTED else ""
        self.setToolTip(f"{info.kind}{note}\n{info.uid}")

    def hasDragEnabled(self) -> bool:
        return self.kind != UNSUPPORTED

    def mimeUris(self) -> list[QgsMimeDataUtils.Uri]:
        uri = QgsMimeDataUtils.Uri()
        uri.layerType = "custom"
        uri.providerKey = PROVIDER_KEY
        uri.name = self.name()
        uri.uri = object_uri(self.file_path, self.uid)
        return [uri]


class Geoh5DataItemGuiProvider(QgsDataItemGuiProvider):
    """Double-click and context-menu actions for geoh5 Browser items."""

    def name(self) -> str:
        return "geoh5"

    def handleDoubleClick(self, item, context) -> bool:
        if not isinstance(item, Geoh5ObjectItem):
            return False
        _, errors = add_objects(item.file_path, [item.uid], QgsProject.instance())
        _report(errors, context.messageBar())
        return True

    def populateContextMenu(self, item, menu, selectedItems, context) -> None:
        objects = [i for i in selectedItems if isinstance(i, Geoh5ObjectItem)]
        if isinstance(item, Geoh5ObjectItem) and objects:
            label = (
                "Add Selected Objects to Project"
                if len(objects) > 1
                else ("Add Object to Project")
            )
            action = QAction(label, menu)
            action.triggered.connect(lambda: _add_items(objects, context))
            menu.addAction(action)
        elif isinstance(item, Geoh5FileItem):
            action = QAction("Add All Supported Objects to Project", menu)
            action.triggered.connect(lambda: _add_file(item.file_path, context))
            menu.addAction(action)


class Geoh5DropHandler(QgsCustomDropHandler):
    """Loads geoh5 objects dragged from the Browser onto the map or layer tree."""

    def customUriProviderKey(self) -> str:
        return PROVIDER_KEY

    def handleCustomUriDrop(self, uri: QgsMimeDataUtils.Uri) -> None:
        path, uid = parse_object_uri(uri.uri)
        add_objects(path, [uid], QgsProject.instance())


def _add_items(items: list[Geoh5ObjectItem], context) -> None:
    by_file: dict[str, list[str]] = {}
    for item in items:
        by_file.setdefault(item.file_path, []).append(item.uid)
    errors = [
        error
        for path, uids in by_file.items()
        for error in add_objects(path, uids, QgsProject.instance())[1]
    ]
    _report(errors, context.messageBar())


def _add_file(path: str, context) -> None:
    try:
        with open_workspace(path) as workspace:
            uids = [o.uid for o in list_objects(workspace) if o.kind != UNSUPPORTED]
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        _report([f"{Path(path).name}: {exc}"], context.messageBar())
        return
    _, errors = add_objects(path, uids, QgsProject.instance())
    _report(errors, context.messageBar())
