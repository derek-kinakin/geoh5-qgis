"""Tests for building QGIS layers from geoh5 objects (needs QGIS via pytest-qgis)."""

from pathlib import Path

import numpy as np
import pytest
from geoh5py.workspace import Workspace as Geoh5pyWorkspace
from osgeo import gdal
from qgis.core import (
    NULL,
    QgsMesh,
    QgsMeshDatasetGroupMetadata,
    QgsMeshDatasetIndex,
    QgsMeshLayer,
    QgsPointXY,
    QgsRasterLayer,
    QgsReadWriteContext,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtXml import QDomDocument

from conftest import (
    CURVE_CELLS,
    CURVE_VERTICES,
    POINT_INTS,
    POINT_ROCK_KEYS,
    POINT_TEXT,
    POINT_VERTICES,
    SHADED_VERTEX_VALUES,
    SURFACE_CELLS,
    SURFACE_VERTICES,
    TYPED_FLAGS,
    TYPED_UNITS,
)
from geoh5_qgis.layers import (
    ALL_CHANNELS,
    _finish,
    channel_columns,
    curve_layer,
    curve_parts,
    load_object_layer,
    load_object_layers,
    mesh_dataset_name,
    mesh_display_name,
    vertex_parts,
)
from geoh5_qgis.reader import DataChannel, Grid2DFrame, list_objects, open_workspace
from geoh5_qgis.tempfiles import is_temp_file

ASSETS = Path(r"C:\Users\dkinakin\Dev\geoh5vista\assets")


def uid_of(path: Path, name: str) -> str:
    with open_workspace(path) as ws:
        return next(o.uid for o in list_objects(ws) if o.name == name)


def attribute_rows(layer: QgsVectorLayer) -> list[dict]:
    names = layer.fields().names()
    return [dict(zip(names, f.attributes(), strict=True)) for f in layer.getFeatures()]


def test_points_layer(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "pts"))
    assert isinstance(layer, QgsVectorLayer)
    assert layer.isValid()
    assert layer.name() == "pts"
    assert layer.wkbType() == QgsWkbTypes.Type.PointZ
    assert layer.crs().authid() == "EPSG:26914"
    assert layer.featureCount() == len(POINT_VERTICES)

    feats = list(layer.getFeatures())
    xyz = [
        (
            f.geometry().constGet().x(),
            f.geometry().constGet().y(),
            f.geometry().constGet().z(),
        )
        for f in feats
    ]
    np.testing.assert_allclose(xyz, POINT_VERTICES)

    rows = attribute_rows(layer)
    assert [r["grade"] for r in rows] == [1.5, NULL, 3.5, 4.5]
    assert [r["count"] for r in rows] == POINT_INTS.tolist()
    assert [r["rock"] for r in rows] == POINT_ROCK_KEYS.tolist()
    labels = ["granite", "basalt", "granite", "Unknown"]
    assert [r["rock_label"] for r in rows] == labels
    assert [r["note"] for r in rows] == POINT_TEXT


def test_points_fields_match_geoh5_primitive_types(channels_file: Path) -> None:
    layer = load_object_layer(channels_file, uid_of(channels_file, "typed"))
    fields = layer.fields()
    types = {f.name(): f.type() for f in fields}
    assert types == {
        "au": QMetaType.Type.Double,
        "count": QMetaType.Type.LongLong,
        "rock": QMetaType.Type.LongLong,
        "rock_label": QMetaType.Type.QString,
        "flag": QMetaType.Type.Bool,
        "when": QMetaType.Type.QDateTime,
        "note": QMetaType.Type.QString,
    }
    assert fields.field("au").alias() == f"au ({TYPED_UNITS})"
    assert fields.field("count").alias() == ""


def test_points_values_round_trip_with_no_data_as_null(channels_file: Path) -> None:
    layer = load_object_layer(channels_file, uid_of(channels_file, "typed"))
    rows = attribute_rows(layer)
    assert [r["au"] for r in rows] == [0.25, NULL, 2.0, 8.0]
    assert [r["count"] for r in rows] == [5, NULL, 7, 0]
    assert [r["flag"] for r in rows] == TYPED_FLAGS.tolist()
    assert [r["rock"] for r in rows] == POINT_ROCK_KEYS.tolist()
    assert [r["note"] for r in rows] == POINT_TEXT
    dates = [r["when"] for r in rows]
    assert dates[2] == NULL
    assert [d.toString(Qt.DateFormat.ISODate) for d in dates if d != NULL] == [
        "2024-01-02T03:04:05",
        "2024-02-03T00:00:00",
        "2024-03-04T10:00:00",  # +02:00 converted to UTC
    ]


def test_aliases_follow_renamed_channel_columns() -> None:
    # A curve channel named like the fixed "part" column is renamed "part_2";
    # its alias and style must land on that field, not on the fixed one.
    channel = DataChannel(
        "{u}", "part", "Cell", "Float", np.arange(3.0), units="m", visible=True
    )
    data = channel_columns([channel])
    layer = curve_layer("c", CURVE_VERTICES, CURVE_CELLS, data.columns, None)
    _finish(layer, data, None)
    assert layer.fields().names() == ["part", "part_2"]
    assert layer.fields().field("part").alias() == ""
    assert layer.fields().field("part_2").alias() == "part (m)"
    assert layer.renderer().classAttribute() == "part_2"


def test_referenced_grid2d_channel_is_an_int32_raster(channels_file: Path) -> None:
    layer = load_object_layer(channels_file, uid_of(channels_file, "zones"))
    assert isinstance(layer, QgsRasterLayer)
    assert layer.name() == "zones: zone"
    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(layer.source())
        band = dataset.GetRasterBand(1)
        assert band.GetDescription() == "zone"
        assert band.DataType == gdal.GDT_Int32
        assert set(np.unique(dataset.ReadAsArray())) == {1, 2}


def test_layer_records_its_geoh5_source(geoh5_file: Path) -> None:
    uid = uid_of(geoh5_file, "pts")
    layer = load_object_layer(geoh5_file, uid)
    assert layer.customProperty("geoh5/path") == str(geoh5_file)
    assert layer.customProperty("geoh5/uid") == uid


@pytest.mark.parametrize("name", ["topo", "line", "grid"])
def test_missing_crs_is_left_unset(geoh5_file: Path, name: str) -> None:
    # The memory provider would otherwise default to EPSG:4326.
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, name))
    assert not layer.crs().isValid()


def test_crs_override(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "topo"), crs="EPSG:32611")
    assert layer.crs().authid() == "EPSG:32611"


def test_curve_parts_groups_connected_segments() -> None:
    np.testing.assert_array_equal(curve_parts(CURVE_CELLS), [0, 0, 1])


def test_curve_layer(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "line"))
    assert layer.wkbType() == QgsWkbTypes.Type.LineStringZ
    assert layer.featureCount() == len(CURVE_CELLS)
    rows = attribute_rows(layer)
    assert [r["part"] for r in rows] == [0, 0, 1]
    assert [r["seg_id"] for r in rows] == [1.0, 2.0, 3.0]
    first = next(layer.getFeatures()).geometry().constGet()
    assert [(p.x(), p.y(), p.z()) for p in first.vertices()] == [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
    ]


def ring_coords(polygon) -> list[tuple[float, float, float]]:
    return [(p.x(), p.y(), p.z()) for p in polygon.exteriorRing().vertices()]


FIRST_TRIANGLE = [tuple(SURFACE_VERTICES[i]) for i in [0, 1, 2, 0]]


@pytest.mark.parametrize("name", ["topo", "zoned"])
def test_every_surface_is_a_mesh_by_default(geoh5_file: Path, name: str) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, name))
    assert isinstance(layer, QgsMeshLayer)
    assert layer.isValid()
    groups = mesh_groups(layer)
    assert next(iter(groups)) == "Bed Elevation"  # default colouring
    if name == "zoned":  # triangle data becomes a face dataset
        on_faces = QgsMeshDatasetGroupMetadata.DataType.DataOnFaces
        assert groups["zone"] == (on_faces, [7.0, 8.0])


def test_surface_as_polygons_has_elevation_fields(geoh5_file: Path) -> None:
    (layer,) = load_object_layers(
        geoh5_file, uid_of(geoh5_file, "zoned"), as_polygons=True
    )
    assert layer.wkbType() == QgsWkbTypes.Type.PolygonZ
    assert layer.featureCount() == len(SURFACE_CELLS)
    assert layer.customProperty("geoh5/part") == "polygons"
    rows = attribute_rows(layer)
    z = SURFACE_VERTICES[:, 2][SURFACE_CELLS]  # per-triangle corner elevations
    assert [r["z_min"] for r in rows] == pytest.approx(z.min(axis=1).tolist())
    assert [r["z_max"] for r in rows] == pytest.approx(z.max(axis=1).tolist())
    assert [r["z_mean"] for r in rows] == pytest.approx(z.mean(axis=1).tolist())
    assert [r["zone"] for r in rows] == [7.0, 8.0]
    assert ring_coords(next(layer.getFeatures()).geometry().constGet()) == (
        FIRST_TRIANGLE
    )


def test_polygons_option_only_changes_surfaces(geoh5_file: Path) -> None:
    (points,) = load_object_layers(
        geoh5_file, uid_of(geoh5_file, "pts"), as_polygons=True
    )
    assert points.wkbType() == QgsWkbTypes.Type.PointZ


def assert_raster_matches_geoh5py(path: Path, name: str, data_name: str) -> None:
    """geoh5py's cell centroids must hit the matching pixel of the written raster.

    Checked with GDAL on the raster file itself: QGIS displays rotated rasters
    through an on-the-fly north-up warp, which resamples cells near edges.
    """
    with Geoh5pyWorkspace(path, mode="r") as ws:
        grid = ws.get_entity(name)[0]
        centroids = grid.centroids.copy()
        values = grid.get_data(data_name)[0].values.copy()
    layers = load_object_layers(path, uid_of(path, name), channels=ALL_CHANNELS)
    layer = next(lyr for lyr in layers if lyr.name() == f"{name}: {data_name}")
    assert isinstance(layer, QgsRasterLayer)
    assert layer.isValid()
    assert all(layer.extent().contains(QgsPointXY(x, y)) for x, y, _ in centroids)

    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(layer.source())
    assert dataset.RasterCount == 1
    band = dataset.GetRasterBand(1)
    assert band.GetDescription() == data_name
    pixels = band.ReadAsArray()
    inverse = gdal.InvGeoTransform(dataset.GetGeoTransform())
    cols_rows = [gdal.ApplyGeoTransform(inverse, x, y) for x, y, _ in centroids]
    sampled = [pixels[int(row), int(col)] for col, row in cols_rows]
    np.testing.assert_allclose(sampled, values, rtol=1e-6, equal_nan=True)


def test_grid2d_layer_values_land_on_cell_centres(geoh5_file: Path) -> None:
    assert_raster_matches_geoh5py(geoh5_file, "grid", "elev")


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_rotated_grid2d() -> None:
    path = ASSETS / "grid2d" / "grid2d_test_a.geoh5"
    assert_raster_matches_geoh5py(path, "airborne data", "Bouguer Gravity")


def test_dipping_grid2d_is_rejected() -> None:
    from geoh5_qgis.layers import grid2d_layer

    frame = Grid2DFrame((0.0, 0.0, 0.0), 1.0, 1.0, 2, 2, 0.0, -35.0, False)
    channel = DataChannel("{x}", "v", "Cell", "Float", np.zeros(4))
    with pytest.raises(ValueError, match="dip"):
        grid2d_layer("g", frame, channel, None)


def mesh_groups(layer: QgsMeshLayer) -> dict[str, tuple[int, list[float]]]:
    """Dataset group name -> (data type, values), with display names unescaped."""
    provider = layer.dataProvider()
    groups = {}
    for group in range(provider.datasetGroupCount()):
        meta = provider.datasetGroupMetadata(group)
        index = QgsMeshDatasetIndex(group, 0)
        count = (
            provider.vertexCount()
            if meta.dataType() == QgsMeshDatasetGroupMetadata.DataType.DataOnVertices
            else provider.faceCount()
        )
        values = [provider.datasetValue(index, i).scalar() for i in range(count)]
        groups[mesh_display_name(meta.name())] = (meta.dataType(), values)
    return groups


def test_surface_with_vertex_data_is_a_mesh(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "shaded"))
    assert isinstance(layer, QgsMeshLayer)
    assert layer.isValid()
    assert layer.name() == "shaded"
    assert layer.dataProvider().vertexCount() == len(SURFACE_VERTICES)
    assert layer.dataProvider().faceCount() == len(SURFACE_CELLS)
    assert not layer.crs().isValid()
    assert layer.customProperty("geoh5/uid") == uid_of(geoh5_file, "shaded")

    # A real mesh file in the session temp folder, with true vertex elevations.
    assert layer.providerType() == "mdal"
    assert is_temp_file(layer.source()) and layer.source().endswith(".ply")
    native = QgsMesh()
    layer.dataProvider().populateMesh(native)
    assert [native.vertex(i).z() for i in range(native.vertexCount())] == (
        SURFACE_VERTICES[:, 2].tolist()
    )

    on_vertices = QgsMeshDatasetGroupMetadata.DataType.DataOnVertices
    on_faces = QgsMeshDatasetGroupMetadata.DataType.DataOnFaces
    groups = mesh_groups(layer)
    # Text data cannot be a mesh dataset and is skipped. MDAL exposes vertex Z
    # as "Bed Elevation".
    assert list(groups) == ["Bed Elevation", "Residual mag", "zone"]
    assert groups["Bed Elevation"] == (on_vertices, SURFACE_VERTICES[:, 2].tolist())
    kind, values = groups["Residual mag"]
    assert kind == on_vertices
    np.testing.assert_array_equal(values, SHADED_VERTEX_VALUES)  # NaN kept
    assert groups["zone"] == (on_faces, [7.0, 8.0])


def test_mesh_colours_by_elevation_by_default(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "shaded"))
    settings = layer.rendererSettings()
    assert settings.activeScalarDatasetGroup() == 0
    assert not settings.nativeMeshSettings().isEnabled()
    assert not settings.triangularMeshSettings().isEnabled()


def test_mesh_group_names_with_spaces_survive() -> None:
    assert mesh_display_name(mesh_dataset_name("Residual Magnetic")) == (
        "Residual Magnetic"
    )
    assert " " not in mesh_dataset_name("Residual Magnetic")


def test_curve_with_vertex_data_adds_a_vertex_point_layer(geoh5_file: Path) -> None:
    uid = uid_of(geoh5_file, "line")
    segments, vertices = load_object_layers(geoh5_file, uid)
    assert segments.name() == "line"
    assert segments.wkbType() == QgsWkbTypes.Type.LineStringZ
    assert "depth" not in segments.fields().names()

    assert vertices.name() == "line vertices"
    assert vertices.wkbType() == QgsWkbTypes.Type.PointZ
    assert vertices.featureCount() == len(CURVE_VERTICES)
    xyz = [
        (g.x(), g.y(), g.z())
        for g in (f.geometry().constGet() for f in vertices.getFeatures())
    ]
    np.testing.assert_allclose(xyz, CURVE_VERTICES)
    rows = attribute_rows(vertices)
    assert [r["vertex"] for r in rows] == [0, 1, 2, 3, 4]
    assert [r["part"] for r in rows] == [0, 0, 0, 1, 1]
    assert [r["depth"] for r in rows] == [0.0, 1.5, NULL, 4.0, 5.5]
    assert vertices.customProperty("geoh5/uid") == uid
    assert vertices.customProperty("geoh5/role") == "vertices"
    assert segments.customProperty("geoh5/role") is None


def test_curve_without_vertex_data_is_one_layer(geoh5_file: Path) -> None:
    layers = load_object_layers(geoh5_file, uid_of(geoh5_file, "bare_line"))
    assert [layer.name() for layer in layers] == ["bare_line"]


def test_vertex_parts_follow_segment_parts() -> None:
    np.testing.assert_array_equal(vertex_parts(CURVE_CELLS, 5), [0, 0, 0, 1, 1])
    # A vertex used by no segment gets part -1.
    np.testing.assert_array_equal(vertex_parts(np.array([[0, 1]]), 3), [0, 0, -1])


def test_nearly_flat_grid_loads_as_raster(grid_issues_file: Path) -> None:
    layer = load_object_layer(grid_issues_file, uid_of(grid_issues_file, "nearly_flat"))
    assert isinstance(layer, QgsRasterLayer)
    assert layer.isValid()


def renderer_3d_xml(layer) -> str:
    doc = QDomDocument()
    elem = doc.createElement("renderer-3d")
    layer.renderer3D().writeXml(elem, QgsReadWriteContext())
    doc.appendChild(elem)
    return doc.toString()


def test_mesh_3d_view_takes_height_from_elevation_dataset(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "shaded"))
    renderer = layer.renderer3D()
    assert renderer is not None
    assert renderer.type() == "mesh"
    xml = renderer_3d_xml(layer)
    # Absolute height from dataset group 0 ("Z"), unscaled, smoothed.
    assert 'vertical-group-index="0"' in xml
    assert 'vertical-relative="0"' in xml
    assert 'vertical-scale="1"' in xml
    assert 'smoothed-triangle="1"' in xml
    assert f'layer="{layer.id()}"' in xml


def test_mesh_channels_named_like_coordinates_do_not_clash() -> None:
    from geoh5_qgis.layers import surface_mesh_layer

    layer = surface_mesh_layer(
        "m",
        SURFACE_VERTICES,
        SURFACE_CELLS.astype(np.int64),
        {"z": np.array([1.0, 2.0, 3.0, 4.0])},
        {},
        None,
    )
    native = QgsMesh()
    layer.dataProvider().populateMesh(native)
    assert native.vertex(0).z() == SURFACE_VERTICES[0, 2]  # coordinates intact
    assert "z_2" in mesh_groups(layer)


def surface_profile_heights(layer) -> list[float]:
    """Heights from QGIS's elevation profile engine along y=0.5 across the surface."""
    from qgis.core import (
        Qgis,
        QgsGeometry,
        QgsLineString,
        QgsPoint,
        QgsProfileRequest,
    )

    request = QgsProfileRequest(QgsLineString([QgsPoint(0.1, 0.5), QgsPoint(0.9, 0.5)]))
    request.setCrs(layer.crs())
    request.setTolerance(0.01)
    generator = layer.createProfileGenerator(request)
    assert generator.generateProfile()
    results = generator.takeResults()
    if isinstance(layer, QgsMeshLayer):
        return list(results.distanceToHeightMap().values())
    features = results.asFeatures(Qgis.ProfileExportType.Features3D)
    return [p.z() for f in features for p in QgsGeometry(f.geometry).vertices()]


@pytest.mark.parametrize("as_polygons", [False, True])
def test_surfaces_work_with_the_elevation_profile_tool(
    geoh5_file: Path, as_polygons: bool
) -> None:
    (layer,) = load_object_layers(
        geoh5_file,
        uid_of(geoh5_file, "topo"),
        crs="EPSG:32611",
        as_polygons=as_polygons,
    )
    heights = surface_profile_heights(layer)
    assert heights
    # The fixture surface rises from z=0 to z=1.5; along y=0.5, x=0.1..0.9 the
    # surface z is 0.5*x + y, i.e. between 0.55 and 0.95.
    assert min(heights) == pytest.approx(0.55, abs=0.05)
    assert max(heights) == pytest.approx(0.95, abs=0.05)


def test_float_grid_channel_is_a_float32_raster(geoh5_file: Path) -> None:
    layer = load_object_layer(geoh5_file, uid_of(geoh5_file, "grid"))
    assert layer.name() == "grid: elev"
    with gdal.ExceptionMgr(useExceptions=True):
        dataset = gdal.Open(layer.source())  # keep: bands die with it
        assert dataset.GetRasterBand(1).DataType == gdal.GDT_Float32


def test_select_channels() -> None:
    from geoh5_qgis.layers import select_channels
    from geoh5_qgis.reader import DataInfo

    a = DataInfo("{A}", "a", "Cell", "Float")
    b = DataInfo("{B}", "b", "Cell", "Float", visible=True)
    assert select_channels([a, b], None) == [b]  # the one GA shows
    assert select_channels([a], None) == [a]  # else the first
    assert select_channels([], None) == []
    assert select_channels([a, b], ("{b}",)) == [b]  # UIDs ignore case
    assert select_channels([a, b], ALL_CHANNELS) == [a, b]


@pytest.mark.skipif(not ASSETS.exists(), reason="geoh5vista assets not available")
def test_real_grid_loads_the_channel_ga_shows() -> None:
    path = ASSETS / "grid2d" / "grid2d_test_a.geoh5"
    (layer,) = load_object_layers(path, uid_of(path, "airborne data"))
    assert layer.name() == "airborne data: Residual Magnetic"
    assert layer.customProperty("geoh5/part").startswith("channel/{")
    every = load_object_layers(
        path, uid_of(path, "airborne data"), channels=ALL_CHANNELS
    )
    assert [lyr.name().split(": ")[1] for lyr in every] == [
        "Residual Magnetic",
        "Bouguer Gravity",
        "Elevation",
    ]
