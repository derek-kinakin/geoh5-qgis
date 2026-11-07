"""Default QGIS styles taken from GEOSCIENCE ANALYST display settings.

Layers are coloured by a data channel using that channel's GA colour map. GA's
colour-map values are data values already spaced by its linear or equal-area
mapping (keys for Referenced and Boolean data). Without a colour map, an
equal-area map is made from the data over a QGIS ramp.

Vector layers are coloured by the channel GA was showing (its ``Visible`` flag):

- Referenced and Boolean channels: a categorised renderer, one category per key
  present, coloured from the colour map.
- Float and Integer channels: a graduated renderer of GRADUATED_CLASSES classes
  with breaks at evenly spaced colour-map entries.
- No shown channel: GA's object colour, if known.

Raster layers (one Grid2D channel each):

- Referenced and Boolean channels: a paletted renderer with the same categories.
- Float and Integer channels: a pseudocolour renderer interpolating through every
  colour-map entry, so it matches GA's colouring and shows a continuous legend.

Styles apply only to freshly loaded layers; restoring a project keeps the saved
style.
"""

import numpy as np
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsColorRampShader,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsGraduatedSymbolRenderer,
    QgsPalettedRasterRenderer,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleBandPseudoColorRenderer,
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
# (key, colour, label) of one category.
Category = tuple[int | bool, Colour, str]


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


def apply_raster_style(layer: QgsRasterLayer, channel: DataChannel) -> None:
    """Colour a single-band raster of one channel the way GA colours it.

    Args:
        layer: A raster whose band 1 holds ``channel``'s values.
        channel: The channel.
    """
    if channel.primitive_type in CATEGORY_PRIMITIVES:
        renderer = _paletted(layer, channel)
    elif channel.primitive_type in GRADUATED_PRIMITIVES:
        renderer = _pseudocolor(layer, channel)
    else:
        renderer = None
    if renderer is not None:
        layer.setRenderer(renderer)


def colour_stops(color_map: np.ndarray, values: np.ndarray) -> np.ndarray:
    """A GA colour map's entries in value order, ready to colour ``values``.

    A map whose values are all equal (GA writes some like that) keeps its
    colours, spread evenly over the data range.

    Args:
        color_map: (n, 5) rows of value, red, green, blue, alpha.
        values: The channel's values; NaN is ignored.

    Returns:
        (n, 5) rows sorted by value; empty if there are no finite values.
    """
    data = values[np.isfinite(values)]
    if data.size == 0 or len(color_map) == 0:
        return np.empty((0, 5))
    stops = color_map[np.argsort(color_map[:, 0], kind="stable")].astype(np.float64)
    if np.ptp(stops[:, 0]) == 0:
        stops[:, 0] = np.linspace(data.min(), data.max(), len(stops))
    return stops


def graduated_classes(
    color_map: np.ndarray, values: np.ndarray, n_classes: int = GRADUATED_CLASSES
) -> list[tuple[float, float, Colour]]:
    """Class ranges and colours that follow a GA colour map.

    Breaks are the colour-map values at evenly spaced entries, so an equal-area
    map gives quantile-like classes as in GA. The outer classes are widened to
    the data range (GA clamps values beyond the map to its end colours).

    Args:
        color_map: (n, 5) rows of value, red, green, blue, alpha.
        values: The channel's values; NaN is ignored.
        n_classes: Number of classes wanted (fewer if breaks coincide).

    Returns:
        (lower, upper, colour) per class, ascending; empty if no finite values.
    """
    stops = colour_stops(color_map, values)
    if len(stops) == 0:
        return []
    data = values[np.isfinite(values)]
    entries = np.round(np.linspace(0, len(stops) - 1, n_classes + 1)).astype(int)
    breaks = stops[entries, 0]
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
    categories = _categories(channel)
    if not categories:
        return None
    return QgsCategorizedSymbolRenderer(
        field_name,
        [
            QgsRendererCategory(key, _symbol(layer, colour), label)
            for key, colour, label in categories
        ],
    )


def _paletted(
    layer: QgsRasterLayer, channel: DataChannel
) -> QgsPalettedRasterRenderer | None:
    categories = _categories(channel)
    if not categories:
        return None
    classes = [
        QgsPalettedRasterRenderer.Class(float(key), QColor(*colour), label)
        for key, colour, label in categories
    ]
    return QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes)


def _categories(channel: DataChannel) -> list[Category]:
    """One category per key present, coloured and named from the GA maps."""
    values = channel.values
    keys = np.unique(values if values.dtype == bool else values[values != INTEGER_NDV])
    colours = {
        int(row[0]): _rgba(row[1:])
        for row in (channel.color_map if channel.color_map is not None else [])
    }
    names = channel.value_map or {}
    return [
        (
            key.item(),
            colours.get(int(key), MISSING_COLOUR),
            names.get(int(key), str(key.item())),
        )
        for key in keys
    ]


def _graduated(
    layer: QgsVectorLayer, field_name: str, channel: DataChannel
) -> QgsGraduatedSymbolRenderer | None:
    values = _float_values(channel)
    color_map = _color_map(channel, values)
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


def _pseudocolor(
    layer: QgsRasterLayer, channel: DataChannel
) -> QgsSingleBandPseudoColorRenderer | None:
    values = _float_values(channel)
    color_map = _color_map(channel, values)
    stops = colour_stops(color_map, values)
    if len(stops) == 0:
        return None
    low, high = float(stops[0, 0]), float(stops[-1, 0])
    ramp_shader = QgsColorRampShader(low, high)
    ramp_shader.setColorRampType(Qgis.ShaderInterpolationMethod.Linear)
    ramp_shader.setSourceColorRamp(_color_ramp(color_map))
    ramp_shader.setColorRampItemList(
        [
            QgsColorRampShader.ColorRampItem(
                float(row[0]), QColor(*_rgba(row[1:])), f"{row[0]:g}"
            )
            for row in stops
        ]
    )
    shader = QgsRasterShader(low, high)
    shader.setRasterShaderFunction(ramp_shader)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(low)
    renderer.setClassificationMax(high)
    return renderer


def _float_values(channel: DataChannel) -> np.ndarray:
    """Numeric values as float64 with integer no-data as NaN."""
    values = channel.values.astype(np.float64)
    if channel.values.dtype.kind in "iu":
        values[channel.values == INTEGER_NDV] = np.nan
    return values


def _color_map(channel: DataChannel, values: np.ndarray) -> np.ndarray:
    """The channel's GA colour map, or an equal-area default."""
    if channel.color_map is not None and len(channel.color_map):
        return channel.color_map
    return _default_color_map(values)


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
