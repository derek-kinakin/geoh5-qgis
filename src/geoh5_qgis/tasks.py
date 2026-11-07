"""Build geoh5 layers in a QGIS background task (shared by Browser and restore).

Layers are built in a worker thread, moved to the main thread, and handed to a
callback that runs on the main thread when the task finishes.
"""

from collections.abc import Callable
from pathlib import Path

from qgis.core import QgsApplication, QgsMapLayer, QgsTask
from qgis.PyQt.QtCore import QCoreApplication

from geoh5_qgis.layers import load_object_layers

# What to load: (geoh5 path, object UID, surface as polygons instead of a mesh,
# Grid2D channel data UIDs or None; see layers.load_object_layers).
LoadRequest = tuple[str, str, bool, tuple[str, ...] | None]
# (path, uid, as_polygons, layers or error message) for each request.
LoadResult = tuple[str, str, bool, list[QgsMapLayer] | str]

_RUNNING_TASKS: set[QgsTask] = set()


def load_in_background(
    description: str,
    requests: list[LoadRequest],
    on_loaded: Callable[[list[LoadResult]], None],
    on_error: Callable[[str], None] | None = None,
) -> QgsTask:
    """Build the layers of several geoh5 objects in a background task.

    Args:
        description: Task name shown in QGIS's task bar.
        requests: (geoh5 path, object UID, as_polygons, channels) to load.
        on_loaded: Called on the main thread with one LoadResult per request
            (not called if the task is cancelled).
        on_error: Called on the main thread with a message if the task fails.

    Returns:
        The started task (progress and cancel are shown in the task bar).
    """

    def finished(exception, result=None) -> None:
        _RUNNING_TASKS.discard(task)
        if exception is not None:
            if on_error is not None:
                on_error(str(exception))
            return
        if result is not None:  # None: cancelled
            on_loaded(result)

    task = QgsTask.fromFunction(
        description, _load_all, list(requests), on_finished=finished
    )
    _RUNNING_TASKS.add(task)  # keep the Python wrapper alive until it finishes
    QgsApplication.taskManager().addTask(task)
    return task


def try_load(
    path: str | Path,
    uid: str,
    as_polygons: bool = False,
    channels: tuple[str, ...] | None = None,
) -> list[QgsMapLayer] | str:
    """Build an object's layers, or return a user-facing error message.

    Args:
        path: Path to the .geoh5 file.
        uid: Object UID.
        as_polygons: Load a surface as triangle polygons instead of a mesh.
        channels: Grid2D channels to load (see layers.load_object_layers).

    Returns:
        The layers, or an error message if the object could not be loaded.
    """
    try:
        return load_object_layers(path, uid, as_polygons=as_polygons, channels=channels)
    except (KeyError, ValueError, RuntimeError, FileNotFoundError) as exc:
        return f"{Path(path).name} {uid}: {exc}"


def _load_all(task: QgsTask, requests: list[LoadRequest]) -> list[LoadResult] | None:
    """Task body (worker thread)."""
    main_thread = QCoreApplication.instance().thread()
    results: list[LoadResult] = []
    for index, (path, uid, as_polygons, channels) in enumerate(requests, start=1):
        if task.isCanceled():
            return None
        loaded = try_load(path, uid, as_polygons, channels)
        if not isinstance(loaded, str):
            for layer in loaded:
                layer.moveToThread(main_thread)
        results.append((path, uid, as_polygons, loaded))
        task.setProgress(100.0 * index / len(requests))
    return results
