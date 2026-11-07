"""Default QGIS styles taken from GEOSCIENCE ANALYST display settings.

A vector layer is coloured by the data channel GA was showing (its ``Visible``
flag), using that channel's GA colour map:

- Referenced and Boolean channels: a categorised renderer, one category per key
  present, coloured from the colour map (keys are the map's values).
- Float and Integer channels: a graduated renderer of GRADUATED_CLASSES classes.
  GA's colour-map values are data values already spaced by its linear or
  equal-area mapping, so class breaks are taken at evenly spaced colour-map
  entries. Without a colour map, an equal-area map is made from the data.

With no shown channel the layer gets GA's object colour, if known. Styles apply
only to freshly loaded layers; restoring a project keeps the saved style.
"""

import numpy as np
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsGraduatedSymbolRenderer,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from geoh5_qgis.reader import INTEGER_NDV, DataChannel

GRADUATED_CLASSES = 10
CATEGORY_PRIMITIVES = frozenset({"Referenced", "Boolean"})
GRADUATED_PRIMITIVES = frozenset({"Float", "Integer"})
# For keys missing from a colour map (GA draws "Unknown" in grey).
MISSING_COLOUR = (128, 128, 128, 255)
# Ramp for numeric channels without a colour map, low values blue.
DEFAULT_RAMP = "Spectral"
DEFAULT_MAP_STOPS = 256

Colour = tuple[int, int, int, int]


def apply_default_style(
    layer: QgsVectorLayer,
    field_name: str | None,
    channel: DataChannel | None,
    object_colour: Colour | None,
) -> None:
    """Colour a vector layer the way GEOSCIENCE ANALYST shows its object.

    Args:
        layer: The layer to style.
        field_name: The layer field holding ``channel``'s values.
        channel: The channel GA was showing, or None.
        object_colour: GA's object colour, used when no channel is styled.
    """
    renderer = None
    if channel is not None and field_name is not None:
        if channel.primitive_type in CATEGORY_PRIMITIVES:
            renderer = _categorized(layer, field_name, channel)
        elif channel.primitive_type in GRADUATED_PRIMITIVES:
            renderer = _graduated(layer, field_name, channel)
    if renderer is None and object_colour is not None:
        renderer = QgsSingleSymbolRenderer(_symbol(layer, object_colour))
    if renderer is not None:
        layer.setRenderer(renderer)


def graduated_classes(
    color_map: np.ndarray, values: np.ndarray, n_classes: int = GRADUATED_CLASSES
) -> list[tuple[float, float, Colour]]:
    """Class ranges and colours that follow a GA colour map.

    Breaks are the colour-map values at evenly spaced entries, so an equal-area
    map gives quantile-like classes as in GA. The outer classes are widened to
    the data range (GA clamps values beyond the map to its end colours). A map
    whose values are all equal keeps its colours, spread over the data range.

    Args:
        color_map: (n, 5) rows of value, red, green, blue, alpha.
        values: The channel's values; NaN is ignored.
        n_classes: Number of classes wanted (fewer if breaks coincide).

    Returns:
        (lower, upper, colour) per class, ascending; empty if no finite values.
    """
    data = values[np.isfinite(values)]
    if data.size == 0 or len(color_map) == 0:
        return []
    stops = color_map[np.argsort(color_map[:, 0], kind="stable")]
    levels = stops[:, 0]
    if np.ptp(levels) == 0:
        levels = np.linspace(data.min(), data.max(), len(stops))
    entries = np.round(np.linspace(0, len(stops) - 1, n_classes + 1)).astype(int)
    breaks = levels[entries]
    breaks[0] = min(breaks[0], data.min())
    breaks[-1] = max(breaks[-1], data.max())
    classes = []
    for lower, upper, first, last in zip(
        breaks[:-1], breaks[1:], entries[:-1], entries[1:], strict=True
    ):
        if upper <= lower:
            continue  # coinciding breaks
        colour = stops[(first + last) // 2, 1:]
        classes.append((float(lower), float(upper), _rgba(colour)))
    return classes


def _categorized(
    layer: QgsVectorLayer, field_name: str, channel: DataChannel
) -> QgsCategorizedSymbolRenderer | None:
    values = channel.values
    keys = np.unique(values if values.dtype == bool else values[values != INTEGER_NDV])
    if keys.size == 0:
        return None
    colours = {
        int(row[0]): _rgba(row[1:])
        for row in (channel.color_map if channel.color_map is not None else [])
    }
    names = channel.value_map or {}
    categories = [
        QgsRendererCategory(
            key.item(),
            _symbol(layer, colours.get(int(key), MISSING_COLOUR)),
            names.get(int(key), str(key.item())),
        )
        for key in keys
    ]
    return QgsCategorizedSymbolRenderer(field_name, categories)


def _graduated(
    layer: QgsVectorLayer, field_name: str, channel: DataChannel
) -> QgsGraduatedSymbolRenderer | None:
    values = channel.values.astype(np.float64)
    if channel.values.dtype.kind in "iu":
        values[channel.values == INTEGER_NDV] = np.nan
    color_map = channel.color_map
    if color_map is None or len(color_map) == 0:
        color_map = _default_color_map(values)
    classes = graduated_classes(color_map, values)
    if not classes:
        return None
    ranges = [
        QgsRendererRange(lower, upper, _symbol(layer, colour), f"{lower:g} - {upper:g}")
        for lower, upper, colour in classes
    ]
    renderer = QgsGraduatedSymbolRenderer(field_name, ranges)
    # "Classify" in Layer Properties then reuses GA's colours.
    renderer.setSourceColorRamp(_color_ramp(color_map))
    return renderer


def _default_color_map(values: np.ndarray) -> np.ndarray:
    """An equal-area map (GA's default mapping) over a QGIS colour ramp."""
    data = values[np.isfinite(values)]
    if data.size == 0:
        return np.empty((0, 5))
    ramp = QgsStyle.defaultStyle().colorRamp(DEFAULT_RAMP)
    ramp.invert()
    positions = np.linspace(0.0, 1.0, DEFAULT_MAP_STOPS)
    colours = [ramp.color(float(t)).getRgb() for t in positions]
    return np.column_stack([np.quantile(data, positions), np.array(colours)])


def _color_ramp(color_map: np.ndarray) -> QgsGradientColorRamp:
    """A gradient through every colour-map entry, evenly spaced as GA shows it."""
    colours = [QColor(*_rgba(row[1:])) for row in color_map]
    positions = np.linspace(0.0, 1.0, len(colours))
    stops = [
        QgsGradientStop(float(t), c)
        for t, c in zip(positions[1:-1], colours[1:-1], strict=True)
    ]
    return QgsGradientColorRamp(colours[0], colours[-1], False, stops)


def _symbol(layer: QgsVectorLayer, colour: Colour) -> QgsSymbol:
    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    symbol.setColor(QColor(*colour))
    return symbol


def _rgba(row: np.ndarray) -> Colour:
    red, green, blue, alpha = (int(v) for v in row)
    return red, green, blue, alpha
