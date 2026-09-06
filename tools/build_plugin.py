"""Build a reproducible ZIP importable through Krita's Python plugin importer."""

import ast
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def _version(root: Path) -> str:
    module = ast.parse((root / "plugin/krita6_bridge/protocol.py").read_text())
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PLUGIN_VERSION"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and all(part.isdecimal() for part in value.split(".")):
                return value
    raise ValueError("Expected a literal plugin release version in protocol.py")


def build(destination: Path | None = None) -> Path:
    root = Path(__file__).resolve().parents[1]
    destination = destination or root / "dist" / f"krita6-bridge-{_version(root)}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = root / "plugin"
    paths = [
        source / "krita6_bridge.desktop",
        *sorted((source / "krita6_bridge").glob("*.py")),
        source / "krita6_bridge/manual.html",
    ]
    payloads = {path.relative_to(source).as_posix(): path.read_bytes() for path in paths}
    payloads["krita6_bridge/LICENSE"] = (root / "LICENSE").read_bytes()
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        # Krita's importer searches for an explicit module directory entry.
        directory = ZipInfo("krita6_bridge/", date_time=(2026, 1, 1, 0, 0, 0))
        directory.create_system = 3
        directory.external_attr = (0o40755 << 16) | 0x10
        archive.writestr(directory, b"")
        for name, data in sorted(payloads.items()):
            entry = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return destination


if __name__ == "__main__":
    print(build())
