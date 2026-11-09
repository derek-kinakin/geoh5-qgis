"""Tests for scripts/build_plugin_zip.py."""

import importlib.util
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_plugin_zip.py"
_spec = importlib.util.spec_from_file_location("build_plugin_zip", SCRIPT)
build_plugin_zip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_plugin_zip)


def test_zip_has_plugin_folder_at_top_and_no_caches(tmp_path: Path) -> None:
    plugin = tmp_path / "src" / "my_plugin"
    (plugin / "__pycache__").mkdir(parents=True)
    (plugin / "metadata.txt").write_text("[general]\nname=x\nversion=1.2.3\n")
    (plugin / "__init__.py").write_text("")
    (plugin / "module.py").write_text("")
    (plugin / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"")
    (plugin / "stray.pyc").write_bytes(b"")
    license_file = tmp_path / "LICENSE.txt"
    license_file.write_bytes(b"MIT License\n")

    target = build_plugin_zip.build_zip(plugin, tmp_path / "dist", license_file)

    assert target.name == "my_plugin-1.2.3.zip"
    with zipfile.ZipFile(target) as archive:
        names = sorted(archive.namelist())
        licence_text = archive.read("my_plugin/LICENSE")
    assert names == [
        "my_plugin/LICENSE",
        "my_plugin/__init__.py",
        "my_plugin/metadata.txt",
        "my_plugin/module.py",
    ]
    assert licence_text == b"MIT License\n"


def test_real_plugin_zip_contains_entry_point_and_metadata(tmp_path: Path) -> None:
    target = build_plugin_zip.build_zip(out_dir=tmp_path)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    assert {
        "geoh5_qgis/__init__.py",
        "geoh5_qgis/metadata.txt",
        "geoh5_qgis/LICENSE",
    } <= names
    assert all(n.startswith("geoh5_qgis/") for n in names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
