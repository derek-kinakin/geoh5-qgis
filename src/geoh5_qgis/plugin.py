"""QGIS plugin entry point: Browser integration, project reopening, temp files."""

from qgis.core import QgsApplication, QgsPathResolver, QgsProject
from qgis.gui import QgsGui
from qgis.PyQt.QtCore import QTimer

from geoh5_qgis import tempfiles
from geoh5_qgis.browser import (
    Geoh5DataItemGuiProvider,
    Geoh5DataItemProvider,
    Geoh5DropHandler,
)
from geoh5_qgis.restore import (
    preprocess_source,
    restore_project_in_background,
    write_source,
)

# Delay before deleting a removed layer's files, so QGIS has released them.
FILE_RELEASE_DELAY_MS = 1000


class Geoh5QgisPlugin:
    """Plugin object QGIS creates via classFactory(); required to be a class."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.item_provider: Geoh5DataItemProvider | None = None
        self.gui_provider: Geoh5DataItemGuiProvider | None = None
        self.drop_handler: Geoh5DropHandler | None = None
        self.files_to_remove: list = []

    def initGui(self) -> None:  # noqa: N802 (QGIS API name)
        tempfiles.sweep_stale()
        self.item_provider = Geoh5DataItemProvider()
        QgsApplication.dataItemProviderRegistry().addProvider(self.item_provider)
        self.gui_provider = Geoh5DataItemGuiProvider()
        QgsGui.dataItemGuiProviderRegistry().addProvider(self.gui_provider)
        self.drop_handler = Geoh5DropHandler()
        self.iface.registerCustomDropHandler(self.drop_handler)
        # Generated sources are saved as geoh5 tokens and rebuilt on reading,
        # before QGIS's "unavailable layers" check.
        self.path_writer = QgsPathResolver.setPathWriter(write_source)
        self.path_preprocessor = QgsPathResolver.setPathPreprocessor(preprocess_source)
        project = QgsProject.instance()
        project.readProject.connect(self.handle_project_read)
        project.layersWillBeRemoved.connect(self.handle_layers_will_be_removed)

    def unload(self) -> None:
        project = QgsProject.instance()
        project.readProject.disconnect(self.handle_project_read)
        project.layersWillBeRemoved.disconnect(self.handle_layers_will_be_removed)
        QgsPathResolver.removePathWriter(self.path_writer)
        QgsPathResolver.removePathPreprocessor(self.path_preprocessor)
        # The registries own (and delete) the providers on removal.
        if self.item_provider is not None:
            QgsApplication.dataItemProviderRegistry().removeProvider(self.item_provider)
        if self.gui_provider is not None:
            QgsGui.dataItemGuiProviderRegistry().removeProvider(self.gui_provider)
        if self.drop_handler is not None:
            self.iface.unregisterCustomDropHandler(self.drop_handler)
        self.item_provider = self.gui_provider = self.drop_handler = None
        tempfiles.cleanup_session()

    def handle_project_read(self, _document) -> None:
        """Rebuild geoh5 layers from their files after a project is opened."""
        restore_project_in_background(QgsProject.instance(), self.iface.messageBar())

    def handle_layers_will_be_removed(self, layer_ids: list[str]) -> None:
        """Delete removed layers' generated files once QGIS has released them."""
        project = QgsProject.instance()
        layers = [project.mapLayer(layer_id) for layer_id in layer_ids]
        files = [
            f
            for lyr in layers
            if lyr is not None
            for f in tempfiles.files_of(lyr.source())
        ]
        if files:
            self.files_to_remove.extend(files)
            QTimer.singleShot(FILE_RELEASE_DELAY_MS, self.remove_released_files)

    def remove_released_files(self) -> None:
        """Delete files queued by handle_layers_will_be_removed."""
        files, self.files_to_remove = self.files_to_remove, []
        tempfiles.remove_files(files)
