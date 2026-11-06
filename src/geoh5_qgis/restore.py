"""Rebuild geoh5 layers from their geoh5 files when a saved project is reopened.

QGIS does not keep the data of memory layers or files in GDAL's /vsimem in a
project, so after reopening, geoh5 vector layers are empty and rasters are
invalid. Every geoh5 layer records its file, object UID and part (custom
properties), so its data is rebuilt from the geoh5 file, the source of truth,
and transplanted into the layer QGIS restored. Layer ids, styling and position
in the layer tree are kept; edits made in GEOSCIENCE ANALYST since the project
was saved are picked up.
"""

from pathlib import Path

from qgis.core import (
    QgsDataProvider,
    QgsMapLayer,
    QgsProject,
    QgsTask,
    QgsVectorLayer,
)

from geoh5_qgis.layers import (
    MAIN_PART,
    PART_PROPERTY,
    PATH_PROPERTY,
    POLYGONS_PART,
    ROLE_PROPERTY,
    UID_PROPERTY,
)
from geoh5_qgis.sources import parse_token, token_for
from geoh5_qgis.tasks import LoadResult, load_in_background, try_load
from geoh5_qgis.tempfiles import files_of, remove_files

MESSAGE_TITLE = "geoh5"

# Sources rebuilt by preprocess_source while the current project was read.
_FRESH_SOURCES: set[str] = set()

# (geoh5 path, object UID, as_polygons) -> the project's layers built from it.
Targets = dict[tuple[str, str, bool], list[QgsMapLayer]]


def restore_project(project: QgsProject) -> list[str]:
    """Rebuild every geoh5 layer in a project from its geoh5 file (blocking).

    Args:
        project: The project whose geoh5 layers to rebuild.

    Returns:
        One message per object or layer that could not be rebuilt.
    """
    targets = geoh5_layers(project)
    results = [(*key, try_load(*key)) for key in targets]
    return _apply(targets, results)


def restore_project_in_background(
    project: QgsProject, message_bar=None
) -> QgsTask | None:
    """Rebuild every geoh5 layer in a project in a background task.

    Args:
        project: The project whose geoh5 layers to rebuild.
        message_bar: A QgsMessageBar for problems, or None.

    Returns:
        The started task, or None if the project has no geoh5 layers.
    """
    targets = geoh5_layers(project)
    if not targets:
        return None

    def loaded(results: list[LoadResult]) -> None:
        _report(_apply(targets, results), message_bar)

    count = f"{len(targets)} geoh5 object{'s' if len(targets) != 1 else ''}"
    return load_in_background(
        f"Reloading {count}",
        list(targets),
        loaded,
        lambda message: _report([message], message_bar),
    )


def write_source(source: str) -> str:
    """Path writer: save generated sources as tokens naming their geoh5 origin.

    Install with QgsPathResolver.setPathWriter().

    Args:
        source: A data source about to be written to a project.

    Returns:
        A ``geoh5:`` token for plugin-generated sources, else ``source``.
    """
    return token_for(source) or source


def preprocess_source(source: str) -> str:
    """Path preprocessor: rebuild a tokenised source while a project is read.

    Runs before QGIS's bad-layer handling, so the layer opens valid instead of
    being reported as unavailable. If the geoh5 file or object is gone, the
    token is returned unchanged and QGIS reports the layer as usual. Install
    with QgsPathResolver.setPathPreprocessor().

    Args:
        source: A data source read from a project.

    Returns:
        A freshly generated source, or ``source`` unchanged.
    """
    origin = parse_token(source)
    if origin is None:
        return source
    path, uid, part = origin
    if not Path(path).is_file():
        return source
    rebuilt = try_load(path, uid)
    if isinstance(rebuilt, str):
        return source
    match = next((layer for layer in rebuilt if _part(layer) == part), None)
    if match is None:
        return source
    _FRESH_SOURCES.add(match.source())
    return match.source()


def geoh5_layers(project: QgsProject) -> Targets:
    """The project's layers that were built from geoh5 objects.

    Args:
        project: A QGIS project.

    Returns:
        (geoh5 path, object UID, as_polygons) to the layers built from it.
    """
    targets: Targets = {}
    for layer in project.mapLayers().values():
        path = layer.customProperty(PATH_PROPERTY)
        uid = layer.customProperty(UID_PROPERTY)
        if path and uid:
            as_polygons = _part(layer) == POLYGONS_PART
            key = (str(path), str(uid), as_polygons)
            targets.setdefault(key, []).append(layer)
    # Objects whose layers were all just rebuilt by preprocess_source are current.
    fresh = {
        key
        for key, layers in targets.items()
        if all(layer.source() in _FRESH_SOURCES for layer in layers)
    }
    _FRESH_SOURCES.clear()
    return {key: layers for key, layers in targets.items() if key not in fresh}


def transplant(target: QgsMapLayer, source: QgsMapLayer) -> str | None:
    """Give ``target`` the data of a freshly built ``source`` layer.

    Vector layers get the source's fields and features; rasters and meshes get
    its data source. The target keeps its id, name, CRS and styling.

    Args:
        target: The layer QGIS restored from the project.
        source: A layer rebuilt from the geoh5 file.

    Returns:
        A message if the data could not be transplanted, else None.
    """
    if type(target) is not type(source) or (
        isinstance(target, QgsVectorLayer) and target.wkbType() != source.wkbType()
    ):
        return (
            f"{target.name()}: its geometry type changed in the geoh5 file; "
            "remove it and add it again from the Browser"
        )
    if isinstance(target, QgsVectorLayer):
        _replace_features(target, source)
    else:
        _replace_source(target, source)
    target.triggerRepaint()
    return None if target.isValid() else f"{target.name()}: rebuilt layer is invalid"


def _apply(targets: Targets, results: list[LoadResult]) -> list[str]:
    errors: list[str] = []
    for path, uid, as_polygons, rebuilt in results:
        if isinstance(rebuilt, str):
            errors.append(rebuilt)
            continue
        by_part = {_part(layer): layer for layer in rebuilt}
        by_name = {layer.name(): layer for layer in rebuilt}
        for target in targets.get((path, uid, as_polygons), []):
            # Projects saved before parts existed: fall back to the layer name.
            source = by_part.get(_part(target)) or by_name.get(target.name())
            error = (
                transplant(target, source)
                if source is not None
                else f"{target.name()}: no longer in {Path(path).name}"
            )
            if error:
                errors.append(error)
    return errors


def _part(layer: QgsMapLayer) -> str:
    """The layer's part; projects saved before parts existed fall back to role."""
    return str(
        layer.customProperty(PART_PROPERTY)
        or layer.customProperty(ROLE_PROPERTY)
        or MAIN_PART
    )


def _replace_features(target: QgsVectorLayer, source: QgsVectorLayer) -> None:
    provider = target.dataProvider()
    provider.truncate()
    if _field_signature(target) != _field_signature(source):
        provider.deleteAttributes(list(range(len(target.fields()))))
        provider.addAttributes(source.fields().toList())
        target.updateFields()
    provider.addFeatures(list(source.getFeatures()))
    target.updateExtents()


def _field_signature(layer: QgsVectorLayer) -> list[tuple[str, int]]:
    return [(f.name(), int(f.type())) for f in layer.fields()]


def _replace_source(target: QgsMapLayer, source: QgsMapLayer) -> None:
    crs = target.crs()
    old_files = files_of(target.source())
    target.setDataSource(
        source.source(),
        target.name(),
        source.providerType(),
        QgsDataProvider.ProviderOptions(),
    )
    if crs.isValid():
        target.setCrs(crs)
    provider = source.dataProvider()
    for uri in provider.extraDatasets() if hasattr(provider, "extraDatasets") else []:
        target.dataProvider().addDataset(uri)  # QGIS skips ones already present
    if source.renderer3D() is not None and target.renderer3D() is None:
        target.setRenderer3D(source.renderer3D().clone())
    remove_files(old_files)  # the old provider has released them


def _report(errors: list[str], message_bar) -> None:
    if errors and message_bar is not None:
        message_bar.pushWarning(MESSAGE_TITLE, "; ".join(errors))
