"""Tests for default styles taken from GEOSCIENCE ANALYST display settings."""

from pathlib import Path

import numpy as np
import pytest
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer,
    QgsPalettedRasterRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
)

from conftest import FLAG_COLOURS, TYPED_COLOR_MAP
from geoh5_qgis.layers import load_object_layer, load_object_layers
from geoh5_qgis.reader import list_objects, open_workspace, read_object_colour
from geoh5_qgis.styles import graduated_classes

ASSETS = Path(r"C:\Users\dkinakin\Dev\geoh5vista\assets")
needs_assets = pytest.mark.skipif(not ASSETS.is_dir(), reason="sample files missing")


def uid_of(path: Path, name: str) -> str:
    with open_workspace(path) as ws:
        return next(o.uid for o in list_objects(ws) if o.name == name)


def colour_map(levels: list[float]) -> np.ndarray:
    """A map with the given values and a distinct red level per entry."""
    n = len(levels)
    return np.column_stack(
        [levels, np.arange(n), np.zeros(n), np.zeros(n), np.full(n, 255)]
    )


def rgba(symbol) -> tuple[int, int, int, int]:
    return symbol.color().getRgb()


def test_classes_break_at_evenly_spaced_colour_map_entries() -> None:
    # Uneven values, as GA writes for an equal-area mapping.
    levels = [0.0, 1.0, 2.0, 10.0, 100.0]
    classes = graduated_classes(colour_map(levels), np.array(levels), n_classes=2)
    assert [(lo, hi) for lo, hi, _ in classes] == [(0.0, 2.0), (2.0, 100.0)]
    # Each class takes the colour of its middle entry.
    assert [c[0] for _, _, c in classes] == [1, 3]


def test_outer_classes_cover_the_data_range() -> None:
    classes = graduated_classes(colour_map([0.0, 5.0, 10.0]), np.array([-3.0, 12.0]))
    assert classes[0][0] == -3.0
    assert classes[-1][1] == 12.0


def test_flat_colour_map_is_spread_over_the_data() -> None:
    # GA wrote all 39 entries of the 'dip' map at 30.5; data runs 35-85.
    classes = graduated_classes(
        colour_map([30.5] * 5), np.array([35.0, 60.0, 85.0]), n_classes=4
    )
    assert [(lo, hi) for lo, hi, _ in classes] == [
        (35.0, 47.5),
        (47.5, 60.0),
        (60.0, 72.5),
        (72.5, 85.0),
    ]


def test_coinciding_breaks_merge_classes() -> None:
    classes = graduated_classes(
        colour_map([0.0, 0.0, 0.0, 1.0]), np.array([0.0, 1.0]), n_classes=3
    )
    assert [(lo, hi) for lo, hi, _ in classes] == [(0.0, 1.0)]


def test_no_finite_values_gives_no_classes() -> None:
    assert graduated_classes(colour_map([0.0, 1.0]), np.array([np.nan])) == []


def test_shown_numeric_channel_is_graduated_with_ga_colours(
    channels_file: Path,
) -> None:
    layer = load_object_layer(channels_file, uid_of(channels_file, "typed"))
    renderer = layer.renderer()
    assert isinstance(renderer, QgsGraduatedSymbolRenderer)
    assert renderer.classAttribute() == "au"
    ranges = renderer.ranges()
    assert ranges[0].lowerValue() == 0.0
    assert ranges[-1].upperValue() == 8.0
    colours = {rgba(r.symbol()) for r in ranges}
    assert colours <= {tuple(int(v) for v in row[1:]) for row in TYPED_COLOR_MAP}
    # Reclassifying in QGIS reuses the GA colours.
    ramp = renderer.sourceColorRamp()
    assert ramp.color1().getRgb() == tuple(int(v) for v in TYPED_COLOR_MAP[0, 1:])


def test_shown_boolean_channel_is_categorised(channels_file: Path) -> None:
    layer = load_object_layer(channels_file, uid_of(channels_file, "flagged"))
    renderer = layer.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer)
    assert renderer.classAttribute() == "flag"
    categories = {c.value(): rgba(c.symbol()) for c in renderer.categories()}
    assert categories == {False: FLAG_COLOURS[0], True: FLAG_COLOURS[1]}


def test_layer_without_a_shown_channel_keeps_the_qgis_default(
    geoh5_file: Path,
) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "pts"))
    assert isinstance(layer.renderer(), QgsSingleSymbolRenderer)


@needs_assets
def test_real_referenced_channel_uses_ga_category_colours() -> None:
    path = ASSETS / "points" / "pts_test_b.geoh5"
    layer = load_object_layer(path, uid_of(path, "surface samples"))
    renderer = layer.renderer()
    assert isinstance(renderer, QgsCategorizedSymbolRenderer)
    assert renderer.classAttribute() == "LithoCode3"
    keys = {f["LithoCode3"] for f in layer.getFeatures()}
    assert {c.value() for c in renderer.categories()} == keys
    assert all(c.label() for c in renderer.categories())


@needs_assets
def test_real_object_colour() -> None:
    path = ASSETS / "points" / "pts_test_b.geoh5"
    with open_workspace(path) as ws:
        uid = next(o.uid for o in list_objects(ws) if o.name == "surface samples")
        # <Colour>4278211967</Colour> is ARGB 0xFF00557F.
        assert read_object_colour(ws, uid) == (0, 85, 127, 255)


@needs_assets
def test_real_object_without_shown_channel_gets_ga_colour() -> None:
    path = ASSETS / "workspace" / "workspace_complex.geoh5"
    with open_workspace(path) as ws:
        uid = next(o.uid for o in list_objects(ws) if o.name == "Mississippi")
        expected = read_object_colour(ws, uid)
    # A surface with no data; as polygons it is a styled vector layer.
    layer = load_object_layers(path, uid, as_polygons=True)[0]
    assert expected is not None
    assert isinstance(layer.renderer(), QgsSingleSymbolRenderer)
    assert rgba(layer.renderer().symbol()) == expected


def test_referenced_grid_channel_is_paletted(channels_file: Path) -> None:
    layer = load_object_layer(channels_file, uid_of(channels_file, "zones"))
    renderer = layer.renderer()
    assert isinstance(renderer, QgsPalettedRasterRenderer)
    assert [(c.value, c.label) for c in renderer.classes()] == [
        (1.0, "A"),
        (2.0, "B"),
    ]


def test_float_grid_without_colour_map_gets_a_default_ramp(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "grid"))
    renderer = layer.renderer()
    assert isinstance(renderer, QgsSingleBandPseudoColorRenderer)
    assert (renderer.classificationMin(), renderer.classificationMax()) == (0.0, 11.0)


@needs_assets
def test_real_float_grid_interpolates_through_the_ga_colour_map() -> None:
    path = ASSETS / "grid2d" / "grid2d_test_a.geoh5"
    layer = load_object_layer(path, uid_of(path, "airborne data"))
    renderer = layer.renderer()
    assert isinstance(renderer, QgsSingleBandPseudoColorRenderer)
    items = renderer.shader().rasterShaderFunction().colorRampItemList()
    assert len(items) == 256  # every GA colour-map entry
    assert renderer.classificationMin() == pytest.approx(-252.67, abs=0.01)
    assert renderer.classificationMax() == pytest.approx(1227.81, abs=0.01)
    # Equal-area spacing is kept: the entries are not evenly spaced.
    gaps = np.diff([item.value for item in items])
    assert gaps.max() > 10 * gaps.min()
