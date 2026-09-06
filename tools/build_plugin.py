"""Build a reproducible ZIP importable through Krita's Python plugin importer."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def build(destination: Path | None = None) -> Path:
    root = Path(__file__).resolve().parents[1]
    destination = destination or root / "dist" / "krita6-bridge-0.1.0.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = root / "plugin"
    paths = [source / "krita6_bridge.desktop", *sorted((source / "krita6_bridge").glob("*.py"))]
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            entry = ZipInfo(path.relative_to(source).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    return destination


if __name__ == "__main__":
    print(build())
