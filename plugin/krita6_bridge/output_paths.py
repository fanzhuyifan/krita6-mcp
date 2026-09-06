"""Standard-library configuration and containment checks for output files."""

import json
import re
from pathlib import Path

from .protocol import BridgeError


def parse_output_roots(value):
    try:
        roots = json.loads(value or "{}")
    except (ValueError, TypeError):
        raise BridgeError(
            "INVALID_CONFIGURATION", "KRITA6_MCP_OUTPUT_ROOTS must be a JSON object."
        ) from None
    if not isinstance(roots, dict) or len(roots) > 32:
        raise BridgeError("INVALID_CONFIGURATION", "Configure at most 32 named output roots.")
    result = {}
    for name, path in roots.items():
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None
            or not isinstance(path, str)
            or not Path(path).is_absolute()
        ):
            raise BridgeError(
                "INVALID_CONFIGURATION",
                "Output roots need simple names and absolute directory paths.",
            )
        try:
            directory = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise BridgeError(
                "INVALID_CONFIGURATION", "Each output root must be an existing directory."
            ) from None
        if not directory.is_dir():
            raise BridgeError(
                "INVALID_CONFIGURATION", "Each output root must be an existing directory."
            )
        result[name] = directory
    return result


def resolve_output_path(output_roots, root_name, relative, extension, overwrite):
    """Resolve a configured output destination without creating or writing files."""
    if root_name not in output_roots:
        raise BridgeError("OUTPUT_ROOT_NOT_FOUND", "Choose a configured output root.")
    try:
        root = Path(output_roots[root_name]).resolve(strict=True)
    except (OSError, RuntimeError):
        raise BridgeError(
            "INVALID_PATH", "The configured output root is no longer available."
        ) from None
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts or path.suffix.lower() != extension:
        raise BridgeError("INVALID_PATH", "Use a relative path with the required file extension.")
    try:
        destination = (root / path).resolve(strict=False)
        destination.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        raise BridgeError("INVALID_PATH", "The output path leaves its configured root.") from None
    if destination.suffix.lower() != extension:
        raise BridgeError(
            "INVALID_PATH", "The resolved destination has an unsupported file extension."
        )
    if not root.is_dir() or not destination.parent.is_dir():
        raise BridgeError("INVALID_PATH", "The output root and parent directory must exist.")
    if destination.exists():
        if not destination.is_file():
            raise BridgeError("INVALID_PATH", "The destination is not a regular file.")
        if not overwrite:
            raise BridgeError("FILE_EXISTS", "The destination exists; overwrite must be explicit.")
    return destination
