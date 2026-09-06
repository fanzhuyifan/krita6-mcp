"""Check the plugin ZIP using an installed Krita importer and a temporary resource directory."""

import argparse
import builtins
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from build_plugin import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--importer",
        type=Path,
        default=Path("/usr/share/krita/pykrita/plugin_importer/plugin_importer.py"),
        help="Path to Krita's installed plugin_importer.py; this trusted Python module is executed",
    )
    args = parser.parse_args()
    if not args.importer.is_file():
        parser.error("Locate Krita's installed plugin_importer.py and pass --importer.")
    source = Path(__file__).resolve().parents[1]
    package = build()
    spec = importlib.util.spec_from_file_location("installed_krita_importer", args.importer)
    module = importlib.util.module_from_spec(spec)
    # The importer only needs Krita's translation function; no GUI/API is mocked.
    with patch.object(builtins, "i18n", lambda text: text, create=True):
        spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="krita6-plugin-import-") as scratch:
        importer = module.PluginImporter(str(package), scratch, lambda plugin: False)
        try:
            imported = importer.import_all()
            assert len(imported) == 1 and imported[0]["name"] == "krita6_bridge"
            installed = Path(scratch) / "pykrita/krita6_bridge"
            assert (installed / "LICENSE").read_bytes() == (source / "LICENSE").read_bytes()
            for path in (source / "plugin/krita6_bridge").glob("*.py"):
                assert (installed / path.name).read_bytes() == path.read_bytes()
            assert (installed / "manual.html").is_file()
        finally:
            importer.archive.close()
    print(json.dumps({"passed": True, "plugin": "krita6_bridge", "scratch_only": True}))


if __name__ == "__main__":
    main()
