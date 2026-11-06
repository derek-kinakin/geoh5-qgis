"""QGIS plugin entry point: Browser integration and project-reopen rebuilding."""

from qgis.core import QgsApplication, QgsPathResolver, QgsProject
from qgis.gui import QgsGui

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


class Geoh5QgisPlugin:
    """Plugin object QGIS creates via classFactory(); required to be a class."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.item_provider: Geoh5DataItemProvider | None = None
        self.gui_provider: Geoh5DataItemGuiProvider | None = None
        self.drop_handler: Geoh5DropHandler | None = None

    def initGui(self) -> None:  # noqa: N802 (QGIS API name)
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
        QgsProject.instance().readProject.connect(self.handle_project_read)

    def unload(self) -> None:
        QgsProject.instance().readProject.disconnect(self.handle_project_read)
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

    def handle_project_read(self, _document) -> None:
        """Rebuild geoh5 layers from their files after a project is opened."""
        restore_project_in_background(QgsProject.instance(), self.iface.messageBar())
