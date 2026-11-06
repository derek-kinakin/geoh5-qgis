"""QGIS plugin entry point: registers the geoh5 Browser integration."""

from qgis.core import QgsApplication
from qgis.gui import QgsGui

from geoh5_qgis.browser import (
    Geoh5DataItemGuiProvider,
    Geoh5DataItemProvider,
    Geoh5DropHandler,
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

    def unload(self) -> None:
        # The registries own (and delete) the providers on removal.
        if self.item_provider is not None:
            QgsApplication.dataItemProviderRegistry().removeProvider(self.item_provider)
        if self.gui_provider is not None:
            QgsGui.dataItemGuiProviderRegistry().removeProvider(self.gui_provider)
        if self.drop_handler is not None:
            self.iface.unregisterCustomDropHandler(self.drop_handler)
        self.item_provider = self.gui_provider = self.drop_handler = None
