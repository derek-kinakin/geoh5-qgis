"""Build an installable QGIS plugin ZIP from src/geoh5_qgis.

The ZIP has the plugin folder at its top level (as QGIS's "Install from ZIP"
expects) and leaves out Python caches. Standard library only, so any Python
(including QGIS's own) can run it:

    python scripts/build_plugin_zip.py              # -> dist/geoh5_qgis-<version>.zip
    python scripts/build_plugin_zip.py --out other/folder
"""

import argparse
import configparser
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "src" / "geoh5_qgis"
DEFAULT_OUT_DIR = REPO / "dist"
LICENSE_FILE = REPO / "LICENSE.txt"
EXCLUDED_DIRS = frozenset({"__pycache__"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


def build_zip(
    plugin_dir: Path = PLUGIN_DIR,
    out_dir: Path = DEFAULT_OUT_DIR,
    license_file: Path = LICENSE_FILE,
) -> Path:
    """Zip a plugin folder for QGIS's "Install from ZIP".

    plugins.qgis.org requires a plain-text ``LICENSE`` (no extension) in the
    plugin folder, so ``license_file`` is added there under that name.

    Args:
        plugin_dir: The plugin package folder (contains metadata.txt).
        out_dir: Folder to write the ZIP into (created if needed).
        license_file: License text to ship as ``<folder>/LICENSE``.

    Returns:
        Path of the written ZIP, named ``<folder>-<version>.zip``.

    Raises:
        FileNotFoundError: If ``plugin_dir`` has no metadata.txt or
            ``license_file`` does not exist.
    """
    version = plugin_version(plugin_dir)
    if not license_file.is_file():
        raise FileNotFoundError(f"License file not found: {license_file}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{plugin_dir.name}-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in plugin_files(plugin_dir):
            archive.write(path, Path(plugin_dir.name) / path.relative_to(plugin_dir))
        archive.write(license_file, Path(plugin_dir.name) / "LICENSE")
    return target


def plugin_version(plugin_dir: Path) -> str:
    """Read the plugin version from metadata.txt.

    Args:
        plugin_dir: The plugin package folder.

    Returns:
        The ``version`` value from the ``[general]`` section.

    Raises:
        FileNotFoundError: If metadata.txt is missing.
    """
    metadata_path = plugin_dir / "metadata.txt"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"No metadata.txt in {plugin_dir}")
    metadata = configparser.ConfigParser()
    metadata.read(metadata_path, encoding="utf-8")
    return metadata["general"]["version"]


def plugin_files(plugin_dir: Path) -> list[Path]:
    """Files to ship: everything except Python caches.

    Args:
        plugin_dir: The plugin package folder.

    Returns:
        Sorted file paths.
    """
    return sorted(
        path
        for path in plugin_dir.rglob("*")
        if path.is_file()
        and path.suffix not in EXCLUDED_SUFFIXES
        and not EXCLUDED_DIRS.intersection(path.relative_to(plugin_dir).parts)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT_DIR, help="output folder"
    )
    print(build_zip(out_dir=parser.parse_args().out))
