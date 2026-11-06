# SPDX-FileCopyrightText: 2026-present Derek Kinakin
#
# SPDX-License-Identifier: MIT
"""geoh5-qgis: browse geoh5 files in QGIS and load their objects as layers."""


def classFactory(iface):  # noqa: N802 (QGIS plugin API name)
    """Create the plugin object; called by QGIS when the plugin loads.

    Args:
        iface: The QGIS interface (QgisInterface).

    Returns:
        The plugin instance.
    """
    from geoh5_qgis.plugin import Geoh5QgisPlugin

    return Geoh5QgisPlugin(iface)
