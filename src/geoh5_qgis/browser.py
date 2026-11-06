"""Browser panel integration: expand .geoh5 files and add their objects.

QGIS requires subclasses for data items and providers, so this module has a few
thin classes. The work they trigger is done by plain functions
(``add_objects``, ``object_uri``, ``parse_object_uri``).

Interaction:
- A .geoh5 file expands to its GEOSCIENCE ANALYST group folders and objects.
- Double-click an object, drag it onto the map, or use the context menu to add
  it to the project. Group and file context menus add every loadable object
  below them.
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
    QgsTask,
)
from qgis.gui import QgsCustomDropHandler, QgsDataItemGuiProvider
from qgis.PyQt.QtGui import QAction

from geoh5_qgis.reader import (
    CURVE,
    DRILLHOLES,
    GEOIMAGE,
    GRID2D,
    GROUP,
    POINTS,
    SURFACE,
    ObjectInfo,
    TreeNode,
    list_objects,
    load_issue,
    open_workspace,
    read_tree,
    tree_objects,
)
from geoh5_qgis.tasks import LoadResult, load_in_background, try_load

PROVIDER_KEY = "geoh5"
URI_SEPARATOR = "|"
MESSAGE_TITLE = "geoh5"

_KIND_ICONS = {
    POINTS: QgsLayerItem.iconPoint,
    CURVE: QgsLayerItem.iconLine,
    SURFACE: QgsLayerItem.iconPolygon,
    GRID2D: QgsLayerItem.iconRaster,
    GEOIMAGE: QgsLayerItem.iconRaster,
    DRILLHOLES: QgsLayerItem.iconLine,
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
    results = [try_load(path, uid) for uid in uids]
    layers = [layer for r in results if isinstance(r, list) for layer in r]
    errors = [r for r in results if isinstance(r, str)]
    project.addMapLayers(layers)
    return layers, errors


def add_objects_in_background(
    path: str | Path,
    uids: list[str],
    project: QgsProject,
    message_bar=None,
) -> QgsTask:
    """Load geoh5 objects in a background task, then add them to a project.

    Layers are built in a worker thread (shown in QGIS's task bar, with
    progress and cancel) and moved to the main thread; they are added to the
    project when the task finishes. Errors go to ``message_bar`` if given.

    Args:
        path: Path to the .geoh5 file.
        uids: Object group names to load.
        project: Project to add the layers to.
        message_bar: A QgsMessageBar for errors, or None.

    Returns:
        The started task.
    """

    def loaded(results: list[LoadResult]) -> None:
        project.addMapLayers(
            [layer for *_, r in results if not isinstance(r, str) for layer in r]
        )
        _report([r for *_, r in results if isinstance(r, str)], message_bar)

    count = f"{len(uids)} object{'s' if len(uids) != 1 else ''}"
    return load_in_background(
        f"Loading {count} from {Path(path).name}",
        [(str(path), uid) for uid in uids],
        loaded,
        lambda message: _report([f"{Path(path).name}: {message}"], message_bar),
    )


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
            nodes, issues = read_file_contents(self.file_path)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            return [QgsErrorItem(self, f"Cannot read file: {exc}", f"{self.path()}/!")]
        return _child_items(self, nodes, issues, self.file_path)


class Geoh5GroupItem(QgsDataCollectionItem):
    """A GEOSCIENCE ANALYST group (folder); children come from the file's tree."""

    def __init__(
        self,
        parent: QgsDataItem,
        node: TreeNode,
        file_path: str,
        issues: dict[str, str | None],
    ) -> None:
        super().__init__(parent, node.name, f"{parent.path()}/{node.uid}", PROVIDER_KEY)
        self.file_path = file_path
        self.node = node
        self.issues = issues
        # Children are already in memory, so expanding is cheap.
        self.setCapabilitiesV2(
            Qgis.BrowserItemCapability.Fertile | Qgis.BrowserItemCapability.Fast
        )
        if not node.children:
            self.setState(Qgis.BrowserItemState.Populated)
        count = len(tree_objects(node.children))
        self.setToolTip(f"Group ({count} objects)\n{node.uid}")

    def createChildren(self) -> list[QgsDataItem]:
        return _child_items(self, self.node.children, self.issues, self.file_path)

    def loadable_uids(self) -> list[str]:
        """UIDs of the loadable objects anywhere below this group."""
        return [
            n.uid
            for n in tree_objects(self.node.children)
            if not self.issues.get(n.uid)
        ]


class Geoh5ObjectItem(QgsDataItem):
    """One object in a .geoh5 file. Adding it loads a layer.

    ``issue`` (from reader.load_issue) explains why the object cannot be
    loaded; such items show the reason and offer no add/drag actions.
    """

    def __init__(
        self,
        parent: QgsDataItem,
        info: TreeNode,
        file_path: str,
        issue: str | None = None,
    ) -> None:
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
        self.issue = issue
        self.setState(Qgis.BrowserItemState.Populated)  # no children
        icon = _KIND_ICONS.get(info.kind, QgsLayerItem.iconDefault)
        self.setIcon(icon())
        reason = f"\nCannot be added: {issue}" if issue else ""
        self.setToolTip(f"{info.kind}{reason}\n{info.uid}")

    def hasDragEnabled(self) -> bool:
        return self.issue is None

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
        if item.issue:
            _report([f"{item.name()}: {item.issue}"], context.messageBar())
            return True
        add_objects_in_background(
            item.file_path, [item.uid], QgsProject.instance(), context.messageBar()
        )
        return True

    def populateContextMenu(self, item, menu, selectedItems, context) -> None:
        objects = [
            i
            for i in selectedItems
            if isinstance(i, Geoh5ObjectItem) and i.issue is None
        ]
        if isinstance(item, Geoh5ObjectItem) and objects:
            label = (
                "Add Selected Objects to Project"
                if len(objects) > 1
                else ("Add Object to Project")
            )
            action = QAction(label, menu)
            action.triggered.connect(lambda: _add_items(objects, context))
            menu.addAction(action)
        elif isinstance(item, Geoh5GroupItem) and item.loadable_uids():
            action = QAction("Add All Objects in Group to Project", menu)
            action.triggered.connect(lambda: _add_group(item, context))
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
        add_objects_in_background(
            path, [uid], QgsProject.instance(), _main_message_bar()
        )


def _add_items(items: list[Geoh5ObjectItem], context) -> None:
    by_file: dict[str, list[str]] = {}
    for item in items:
        by_file.setdefault(item.file_path, []).append(item.uid)
    for path, uids in by_file.items():
        add_objects_in_background(
            path, uids, QgsProject.instance(), context.messageBar()
        )


def read_file_contents(
    path: str | Path,
) -> tuple[tuple[TreeNode, ...], dict[str, str | None]]:
    """Read a file's tree and why each object can or cannot be loaded.

    Falls back to a flat object list if the group tree cannot be read.

    Args:
        path: Path to the .geoh5 file.

    Returns:
        The top-level tree nodes, and object UID to load issue (None if the
        object can be loaded).

    Raises:
        RuntimeError: If GDAL cannot open the file.
        ValueError: If the file is not a geoh5 project.
        FileNotFoundError: If the file does not exist.
    """
    with open_workspace(path) as workspace:
        try:
            nodes = tuple(read_tree(workspace))
        except (KeyError, RuntimeError, ValueError):
            objects = sorted(list_objects(workspace), key=lambda o: o.name.lower())
            nodes = tuple(TreeNode(o.uid, o.name, o.kind) for o in objects)
        issues = {n.uid: _safe_issue(workspace, n) for n in tree_objects(nodes)}
    return nodes, issues


def loadable_object_uids(path: str | Path) -> list[str]:
    """UIDs of the objects shown in a file's tree that can be loaded as layers.

    Args:
        path: Path to the .geoh5 file.

    Returns:
        UIDs of objects with no load issue, in tree order.
    """
    nodes, issues = read_file_contents(path)
    return [n.uid for n in tree_objects(nodes) if issues[n.uid] is None]


def _child_items(
    parent: QgsDataItem,
    nodes: tuple[TreeNode, ...],
    issues: dict[str, str | None],
    file_path: str,
) -> list[QgsDataItem]:
    return [
        Geoh5GroupItem(parent, node, file_path, issues)
        if node.kind == GROUP
        else Geoh5ObjectItem(parent, node, file_path, issues.get(node.uid))
        for node in nodes
    ]


def _safe_issue(workspace, node: TreeNode) -> str | None:
    """load_issue(), turning a malformed object into an issue instead of failing."""
    info = ObjectInfo(node.uid, node.name, node.kind, type_uid="")
    try:
        return load_issue(workspace, info)
    except (KeyError, RuntimeError, ValueError) as exc:
        return f"cannot read object: {exc}"


def _add_group(item: "Geoh5GroupItem", context) -> None:
    add_objects_in_background(
        item.file_path,
        item.loadable_uids(),
        QgsProject.instance(),
        context.messageBar(),
    )


def _main_message_bar():
    """QGIS's main message bar when running in the desktop app, else None."""
    try:
        from qgis.utils import iface
    except ImportError:
        return None
    return iface.messageBar() if iface is not None else None


def _add_file(path: str, context) -> None:
    try:
        uids = loadable_object_uids(path)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        _report([f"{Path(path).name}: {exc}"], context.messageBar())
        return
    add_objects_in_background(path, uids, QgsProject.instance(), context.messageBar())
