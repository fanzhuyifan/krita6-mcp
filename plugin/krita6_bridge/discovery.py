"""Private per-instance discovery; these files contain bearer credentials."""

import json
import os
from pathlib import Path
import secrets
import stat
import sys

from .protocol import BridgeError, validate_id


def default_state_dir():
    override = os.environ.get("KRITA6_MCP_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "krita6-mcp"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "krita6-mcp"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "krita6-mcp"


def write_discovery(state_dir, data):
    if os.name != "posix":
        raise BridgeError(
            "UNSUPPORTED_PLATFORM", "Private discovery currently requires POSIX permissions"
        )
    validate_id(data["instance_id"], "instance_id")
    directory = Path(state_dir).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError(
            "INSECURE_STATE_DIR", "Discovery directory must be owned by this user with mode 0700"
        )
    path = directory / ("instance-" + data["instance_id"] + ".json")
    temporary = directory / (".discovery-" + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def remove_discovery(path, expected):
    """Do not delete a replacement discovery record belonging to another session."""
    if path is None:
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return
            raw = stream.read(16385)
            if len(raw) > 16384:
                return
            data = json.loads(raw)
        if data.get("instance_id") == expected.get("instance_id") and data.get(
            "token"
        ) == expected.get("token"):
            Path(path).unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        pass
