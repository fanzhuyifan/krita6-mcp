"""Distribution contracts needed by Krita's plugin importer and open-source licensing."""

from configparser import ConfigParser
import importlib.util
from pathlib import Path
from zipfile import ZipFile


def test_plugin_zip_is_discoverable_licensed_and_reproducible(tmp_path):
    root = Path(__file__).parents[2]
    spec = importlib.util.spec_from_file_location("plugin_builder", root / "tools/build_plugin.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    first = builder.build(tmp_path / "first.zip")
    second = builder.build(tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        desktop = ConfigParser()
        desktop.read_string(archive.read("krita6_bridge.desktop").decode())
        module = desktop["Desktop Entry"]["X-KDE-Library"]
        # A directory implicit in filenames is insufficient for Krita's importer.
        assert archive.getinfo(f"{module}/").is_dir()
        assert archive.read(f"{module}/__init__.py")
        manual = desktop["Desktop Entry"]["X-Krita-Manual"]
        assert archive.read(f"{module}/{manual}").startswith(b"<!doctype html>")
        assert archive.read(f"{module}/LICENSE") == (root / "LICENSE").read_bytes()
        assert archive.read(f"{module}/protocol.py")
        assert not any(
            name.startswith(("tests/", "tools/", "ai_diffusion/"))
            or "__pycache__" in name
            or name.endswith((".kra", ".pyc"))
            for name in archive.namelist()
        )
