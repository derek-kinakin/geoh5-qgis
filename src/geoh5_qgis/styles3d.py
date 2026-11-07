"""3D styles: draw geoh5 vector layers at their true elevation in 3D views.

QGIS drapes a vector layer that has no 3D renderer onto the terrain, so geoh5
points and lines, which carry real Z, look flat. Each vector layer gets a
rule-based 3D renderer with absolute altitude clamping, coloured like its 2D
style: one rule per category or class of a categorised or graduated renderer,
else a single colour. Points are screen-sized billboards and lines are simple
screen-width lines, so neither depends on the map's units.

The 3D classes live in ``qgis._3d``; if it is missing, layers are left as they
are.
"""

from types import ModuleType

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsExpression,
    QgsFeatureRenderer,
    QgsGraduatedSymbolRenderer,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor

VECTOR_3D_RENDERER = "rulebased"
LINE_WIDTH_PX = 2.0
POINT_SIZE_MM = 2.0
DEFAULT_COLOUR = QColor(128, 128, 128)

# (filter expression, colour) of one 3D rule; "" matches every feature.
Rule3D = tuple[str, QColor]


def qgis_3d() -> ModuleType | None:
    """The QGIS 3D module, initialised; None if this QGIS has no 3D support.

    Returns:
        ``qgis._3d``, or None.
    """
    try:
        import qgis._3d as q3d
    except ImportError:
        return None
    if VECTOR_3D_RENDERER not in QgsApplication.renderer3DRegistry().renderersList():
        q3d.Qgs3D.initialize()  # already done by the desktop app
    return q3d


def apply_3d_style(layer: QgsVectorLayer, rules: list[Rule3D] | None = None) -> None:
    """Give a vector layer a 3D renderer at its true Z.

    Args:
        layer: The layer, already given its 2D style.
        rules: The 3D rules; by default derived from the layer's 2D renderer.
    """
    q3d = qgis_3d()
    if q3d is None:
        return
    rules = rules if rules is not None else rules_from_2d(layer.renderer())
    geometry = QgsWkbTypes.geometryType(layer.wkbType())
    root = q3d.QgsRuleBased3DRenderer.Rule(None)
    for expression, colour in rules:
        if colour.alpha() == 0:
            continue  # hidden in 2D (e.g. GA's transparent "False")
        symbol = _symbol(q3d, geometry, colour)
        root.appendChild(q3d.QgsRuleBased3DRenderer.Rule(symbol, expression))
    layer.setRenderer3D(q3d.QgsRuleBased3DRenderer(root))


def rules_from_2d(renderer: QgsFeatureRenderer | None) -> list[Rule3D]:
    """3D rules matching a 2D renderer's categories, classes or single colour.

    Args:
        renderer: A layer's 2D renderer.

    Returns:
        One rule per shown category or class, else one rule for everything.
    """
    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        field = renderer.classAttribute()
        return [
            (
                QgsExpression.createFieldEqualityExpression(field, c.value()),
                c.symbol().color(),
            )
            for c in renderer.categories()
            if c.renderState()
        ]
    if isinstance(renderer, QgsGraduatedSymbolRenderer):
        field = QgsExpression.quotedColumnRef(renderer.classAttribute())
        return [
            (
                range_expression(field, r.lowerValue(), r.upperValue()),
                r.symbol().color(),
            )
            for r in renderer.ranges()
            if r.renderState()
        ]
    if isinstance(renderer, QgsSingleSymbolRenderer):
        return [("", renderer.symbol().color())]
    return [("", DEFAULT_COLOUR)]


def range_expression(value: str, lower: float, upper: float) -> str:
    """A filter for ``lower <= value <= upper``.

    Args:
        value: An expression, e.g. a quoted field name.
        lower: Lower bound.
        upper: Upper bound.

    Returns:
        The QGIS expression.
    """
    return f"{value} >= {lower!r} AND {value} <= {upper!r}"


def _symbol(q3d: ModuleType, geometry: Qgis.GeometryType, colour: QColor):
    if geometry == Qgis.GeometryType.Point:
        symbol = q3d.QgsPoint3DSymbol()
        symbol.setShape(Qgis.Point3DShape.Billboard)
        # Edit the built-in marker: setBillboardSymbol() takes ownership in C++
        # but not in PyQGIS 4.2, so Python and QGIS both delete it (crash).
        marker = symbol.billboardSymbol()
        marker.setSize(POINT_SIZE_MM)
        marker.setColor(colour)
    elif geometry == Qgis.GeometryType.Line:
        symbol = q3d.QgsLine3DSymbol()
        symbol.setRenderAsSimpleLines(True)
        symbol.setWidth(LINE_WIDTH_PX)
        material = q3d.QgsSimpleLineMaterialSettings()
        material.setAmbient(colour)
        symbol.setMaterialSettings(material)
    else:
        symbol = q3d.QgsPolygon3DSymbol()
        material = q3d.QgsPhongMaterialSettings()
        material.setDiffuse(colour)
        material.setAmbient(colour.darker(200))
        symbol.setMaterialSettings(material)
    symbol.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
    return symbol
