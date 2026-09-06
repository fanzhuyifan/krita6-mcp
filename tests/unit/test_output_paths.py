"""Output configuration and containment checks, independent of Krita and Qt."""

import json

import pytest

from krita6_bridge.output_paths import parse_output_roots, resolve_output_path
from krita6_bridge.protocol import BridgeError


@pytest.mark.parametrize("value", [None, "", "{}"])
def test_output_roots_default_to_empty(value):
    assert parse_output_roots(value) == {}


def test_output_roots_resolve_existing_directory_symlinks(tmp_path):
    directory = tmp_path / "artwork with spaces"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    assert parse_output_roots(json.dumps({"scratch": str(link)})) == {"scratch": directory}


@pytest.mark.parametrize(
    "value",
    [
        "{",
        "[]",
        "null",
        json.dumps({"scratch": "relative/path"}),
        json.dumps({"scratch": 1}),
        json.dumps({"bad name": "/tmp"}),
        json.dumps({f"root-{index}": "/tmp" for index in range(33)}),
    ],
)
def test_output_roots_reject_invalid_configuration(value):
    with pytest.raises(BridgeError) as error:
        parse_output_roots(value)
    assert error.value.code == "INVALID_CONFIGURATION"


def test_output_roots_reject_missing_directories_and_regular_files(tmp_path):
    regular_file = tmp_path / "file"
    regular_file.write_bytes(b"existing")
    for path in (tmp_path / "missing", regular_file):
        with pytest.raises(BridgeError) as error:
            parse_output_roots(json.dumps({"scratch": str(path)}))
        assert error.value.code == "INVALID_CONFIGURATION"


@pytest.mark.parametrize(
    "relative", ["../outside.kra", "/tmp/outside.kra", "wrong.png", "folder/../../outside.kra"]
)
def test_output_path_rejects_escaping_and_wrong_format(tmp_path, relative):
    with pytest.raises(BridgeError) as error:
        resolve_output_path({"scratch": tmp_path}, "scratch", relative, ".kra", False)
    assert error.value.code == "INVALID_PATH"


def test_output_path_requires_overwrite_and_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    output = root / "drawing.kra"
    output.write_bytes(b"existing")
    with pytest.raises(BridgeError) as error:
        resolve_output_path({"scratch": root}, "scratch", "drawing.kra", ".kra", False)
    assert error.value.code == "FILE_EXISTS"
    assert resolve_output_path({"scratch": root}, "scratch", "drawing.kra", ".kra", True) == output
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BridgeError) as error:
        resolve_output_path({"scratch": root}, "scratch", "escape/drawing.kra", ".kra", True)
    assert error.value.code == "INVALID_PATH"
    assert output.read_bytes() == b"existing"
    (root / "misleading.kra").symlink_to(root / "wrong.txt")
    with pytest.raises(BridgeError) as error:
        resolve_output_path({"scratch": root}, "scratch", "misleading.kra", ".kra", True)
    assert error.value.code == "INVALID_PATH"


def test_output_path_rejects_directory_and_missing_parent(tmp_path):
    (tmp_path / "directory.kra").mkdir()
    for relative in ("directory.kra", "missing/output.kra"):
        with pytest.raises(BridgeError) as error:
            resolve_output_path({"scratch": tmp_path}, "scratch", relative, ".kra", True)
        assert error.value.code == "INVALID_PATH"
