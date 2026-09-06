"""Standard-library checks for bounded reads under configured input directories."""

import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

from .protocol_validation import BridgeError

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_KRA_ENTRIES = 4096
MAX_KRA_EXPANDED_BYTES = 256 * 1024 * 1024


def _unique_roots(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate root name")
        result[name] = value
    return result


def parse_input_roots(value):
    """Parse KRITA6_MCP_INPUT_ROOTS once; no roots are available by default."""
    try:
        roots = json.loads(value or "{}", object_pairs_hook=_unique_roots)
    except (ValueError, TypeError):
        raise BridgeError(
            "INVALID_CONFIGURATION", "KRITA6_MCP_INPUT_ROOTS must be a JSON object."
        ) from None
    if not isinstance(roots, dict) or len(roots) > 32:
        raise BridgeError("INVALID_CONFIGURATION", "Configure at most 32 named input roots.")
    result = {}
    for name, path in roots.items():
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None
            or not isinstance(path, str)
            or not path
            or "\x00" in path
            or not Path(path).is_absolute()
        ):
            raise BridgeError(
                "INVALID_CONFIGURATION", "Input roots need simple names and absolute directories."
            )
        try:
            directory = Path(path).resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("Not a directory")
        except (OSError, ValueError, RuntimeError):
            raise BridgeError(
                "INVALID_CONFIGURATION", "Each input root must be an existing directory."
            ) from None
        result[name] = directory
    return result


def _check_kra_archive(path):
    """Bound ZIP directory allocation and declared uncompressed Krita data."""
    try:
        # Check the ordinary end record before ZipFile allocates one object per
        # member. ZIP64 is unnecessary within these file/entry limits and rejected.
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 65557))
            tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ValueError("Missing ZIP directory")
        end = struct.unpack_from("<4s4H2IH", tail, offset)
        (
            _,
            disk,
            directory_disk,
            disk_entries,
            entries,
            directory_size,
            directory_offset,
            comment,
        ) = end
        if (
            disk != 0
            or directory_disk != 0
            or disk_entries != entries
            or entries == 0
            or entries > MAX_KRA_ENTRIES
            or len(tail) - offset != 22 + comment
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        ):
            raise ValueError("Unsupported ZIP directory")
        # Cap directory bytes as well: the EOCD entry count can be dishonest.
        if directory_size > MAX_KRA_ENTRIES * 1024:
            raise ValueError("ZIP directory exceeds its bound")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != entries:
                raise ValueError("ZIP entry count mismatch")
            expanded = 0
            seen = set()
            for info in infos:
                name = info.filename
                member = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (
                    len(name) > 1024
                    or "\x00" in name
                    or "\\" in name
                    or member.is_absolute()
                    or ".." in member.parts
                    or name in seen
                    or info.flag_bits & 1
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or stat.S_ISLNK(mode)
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                ):
                    raise ValueError("Unsupported ZIP member")
                seen.add(name)
                expanded += info.file_size
                if expanded > MAX_KRA_EXPANDED_BYTES:
                    raise BridgeError("INPUT_TOO_LARGE", "KRA expanded data exceeds 256 MiB.")
    except BridgeError:
        raise
    except (OSError, ValueError, RuntimeError, struct.error, zipfile.BadZipFile):
        raise BridgeError(
            "INVALID_INPUT", "KRA must be a bounded, supported ZIP document."
        ) from None


def resolve_input_path(roots, root_name, relative, extensions, max_bytes=MAX_IMAGE_BYTES):
    """Return an existing regular file within a root, without changing any file."""
    if root_name not in roots:
        raise BridgeError("INPUT_ROOT_NOT_FOUND", "Choose a configured input root.")
    if (
        not isinstance(relative, str)
        or not 1 <= len(relative) <= 4096
        or "\x00" in relative
        or "\\" in relative
    ):
        raise BridgeError("INVALID_PATH", "Use a bounded relative input path.")
    path = Path(relative)
    allowed = {extensions} if isinstance(extensions, str) else set(extensions)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in allowed:
        raise BridgeError("INVALID_PATH", "Use a relative path with a supported file extension.")
    try:
        root = Path(roots[root_name]).resolve(strict=True)
        source = (root / path).resolve(strict=True)
        source.relative_to(root)
        if not root.is_dir() or not stat.S_ISREG(source.stat().st_mode):
            raise ValueError("Not a regular file")
    except (OSError, ValueError, RuntimeError):
        raise BridgeError("INVALID_PATH", "Input must be a regular file inside its root.") from None
    if source.suffix.lower() not in allowed:
        raise BridgeError("INVALID_PATH", "The resolved source has an unsupported extension.")
    try:
        if source.stat().st_size > max_bytes:
            raise BridgeError("INPUT_TOO_LARGE", "Input file exceeds the command byte limit.")
    except OSError:
        raise BridgeError("INVALID_PATH", "The input file is no longer available.") from None
    if source.suffix.lower() == ".kra":
        _check_kra_archive(source)
    return source
