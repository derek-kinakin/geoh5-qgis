"""Temporary files backing generated layers (rasters, later meshes).

Files live in one folder per QGIS session (``geoh5_qgis_*`` in the system temp
folder). They are deleted when their layer is removed from the project and
when the plugin unloads; folders left behind (e.g. after a crash, or files
still locked on Windows) are swept on a later start once a week old. Real files,
unlike GDAL's /vsimem, are visible to GDAL command-line tools that QGIS
Processing runs as separate processes.
"""

import contextlib
import itertools
import os
import shutil
import tempfile
import time
from pathlib import Path

PREFIX = "geoh5_qgis_"
STALE_AGE_DAYS = 7.0

_session_dir: Path | None = None
_counter = itertools.count()


def new_path(stem: str, suffix: str) -> Path:
    """A fresh, unused file path in this session's temp folder.

    Args:
        stem: Readable start of the file name (e.g. an object UID).
        suffix: File extension including the dot (e.g. ``.tif``).

    Returns:
        The path (the file is not created).
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:60]
    return session_dir() / f"{safe}_{next(_counter)}{suffix}"


def session_dir() -> Path:
    """This session's temp folder, created on first use."""
    global _session_dir  # noqa: PLW0603 (one folder per process)
    if _session_dir is None or not _session_dir.is_dir():
        _session_dir = Path(tempfile.mkdtemp(prefix=PREFIX))
    return _session_dir


def is_temp_file(source: str) -> bool:
    """Whether a data source is a file in this session's temp folder.

    Args:
        source: A layer data source.

    Returns:
        True if it is one of this session's generated files.
    """
    if _session_dir is None or not source:
        return False
    try:
        return Path(source).resolve().is_relative_to(_session_dir.resolve())
    except (OSError, ValueError):
        return False


def files_of(source: str) -> list[Path]:
    """All generated files belonging to a source (they share its name stem).

    A GeoImage, for example, is an image file plus the VRT that the layer opens,
    and for large images the VRT's overviews (``<stem>.vrt.ovr``).

    Args:
        source: A generated data source (see is_temp_file).

    Returns:
        The files, or an empty list if ``source`` is not a generated file.
    """
    if not is_temp_file(source):
        return []
    path = Path(source)
    # Generated stems are unique and have no dots (new_path), so this cannot
    # match another source's files.
    return list(path.parent.glob(f"{path.stem}.*"))


def remove_files(paths: list[str | Path]) -> None:
    """Delete generated files, ignoring ones that are already gone or locked.

    Args:
        paths: Files in the session temp folder.
    """
    for path in paths:
        # Locked (Windows) or already gone: swept later.
        with contextlib.suppress(OSError):
            os.remove(path)


def cleanup_session() -> None:
    """Delete this session's temp folder (best effort)."""
    global _session_dir  # noqa: PLW0603
    if _session_dir is not None:
        shutil.rmtree(_session_dir, ignore_errors=True)
    _session_dir = None


def sweep_stale(max_age_days: float = STALE_AGE_DAYS) -> None:
    """Delete temp folders left by earlier sessions (best effort).

    Only folders unchanged for ``max_age_days`` are removed, so another QGIS
    instance running at the same time keeps its files (process ids cannot be
    probed safely on Windows).

    Args:
        max_age_days: Minimum age, in days, of folders to delete.
    """
    cutoff = time.time() - max_age_days * 86400
    for folder in Path(tempfile.gettempdir()).glob(f"{PREFIX}*"):
        try:
            stale = folder.is_dir() and folder.stat().st_mtime < cutoff
        except OSError:
            continue
        if stale and folder != _session_dir:
            shutil.rmtree(folder, ignore_errors=True)
