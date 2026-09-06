"""Input containment and byte/archive limits without Krita or Qt."""

import json
import os
import stat
import struct
import zipfile

import pytest

from krita6_bridge import input_paths
from krita6_bridge.input_paths import (
    MAX_DOCUMENT_BYTES,
    MAX_IMAGE_BYTES,
    parse_input_roots,
    resolve_input_path,
)
from krita6_bridge.protocol import BridgeError


IMAGES = {".png", ".jpg", ".jpeg"}
DOCUMENTS = IMAGES | {".kra"}


@pytest.mark.parametrize("value", [None, "", "{}"])
def test_input_roots_default_to_empty(value):
    assert parse_input_roots(value) == {}


def test_input_roots_canonicalize_existing_directory_links(tmp_path):
    directory = tmp_path / "参考 artwork"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    assert parse_input_roots(json.dumps({"reference": str(link)})) == {"reference": directory}


@pytest.mark.parametrize(
    "value",
    [
        "{",
        "[]",
        "null",
        "false",
        42,
        json.dumps({"reference": "relative/path"}),
        json.dumps({"reference": 1}),
        json.dumps({"reference": "/tmp\x00"}),
        json.dumps({"bad name": "/tmp"}),
        json.dumps({"x" * 65: "/tmp"}),
        json.dumps({f"root-{index}": "/tmp" for index in range(33)}),
        '{"reference": "/tmp", "reference": "/tmp"}',
    ],
)
def test_input_roots_reject_invalid_configuration(value):
    with pytest.raises(BridgeError) as failure:
        parse_input_roots(value)
    assert failure.value.code == "INVALID_CONFIGURATION"


def test_input_roots_reject_files_missing_and_looped_directories(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"existing")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    for path in (file, tmp_path / "missing", loop):
        with pytest.raises(BridgeError) as failure:
            parse_input_roots(json.dumps({"reference": str(path)}))
        assert failure.value.code == "INVALID_CONFIGURATION"


@pytest.mark.parametrize("filename", ["参考 image.PNG", "photo.jpg", "photo.JPEG"])
def test_input_path_resolves_supported_files_and_preserves_contents(tmp_path, filename):
    source = tmp_path / filename
    source.write_bytes(b"fixture")
    result = resolve_input_path({"reference": tmp_path}, "reference", filename, IMAGES)
    assert result == source
    assert source.read_bytes() == b"fixture"


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "../outside.png",
        "/tmp/outside.png",
        "folder/../../outside.png",
        "wrong.gif",
        "bad\x00.png",
        "x" * 4097,
        "folder\\outside.png",
        None,
        42,
    ],
)
def test_input_paths_reject_invalid_text_formats_and_traversal(tmp_path, relative):
    with pytest.raises(BridgeError) as failure:
        resolve_input_path({"reference": tmp_path}, "reference", relative, IMAGES)
    assert failure.value.code == "INVALID_PATH"


def test_input_path_requires_known_available_root_and_existing_file(tmp_path):
    with pytest.raises(BridgeError) as failure:
        resolve_input_path({}, "reference", "image.png", IMAGES)
    assert failure.value.code == "INPUT_ROOT_NOT_FOUND"
    for root in (tmp_path, tmp_path / "missing"):
        with pytest.raises(BridgeError) as failure:
            resolve_input_path({"reference": root}, "reference", "image.png", IMAGES)
        assert failure.value.code == "INVALID_PATH"


def test_input_path_rejects_symlink_escape_and_disguised_extension(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    (root / "escape.png").symlink_to(outside)
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    (root / "wrong.txt").write_bytes(b"not an image")
    (root / "disguised.png").symlink_to(root / "wrong.txt")
    (root / "loop.png").symlink_to(root / "loop.png")
    for relative in ("escape.png", "outside/outside.png", "disguised.png", "loop.png"):
        with pytest.raises(BridgeError) as failure:
            resolve_input_path({"reference": root}, "reference", relative, IMAGES)
        assert failure.value.code == "INVALID_PATH"
    assert outside.read_bytes() == b"outside"


def test_input_path_allows_internal_symlink_to_supported_regular_file(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"fixture")
    (tmp_path / "link.png").symlink_to(source)
    assert resolve_input_path({"reference": tmp_path}, "reference", "link.png", IMAGES) == source


def test_input_path_rejects_directory_and_fifo_without_opening(tmp_path):
    directory = tmp_path / "directory.png"
    directory.mkdir()
    fifo = tmp_path / "fifo.png"
    os.mkfifo(fifo)
    for source in (directory, fifo):
        with pytest.raises(BridgeError) as failure:
            resolve_input_path({"reference": tmp_path}, "reference", source.name, IMAGES)
        assert failure.value.code == "INVALID_PATH"


@pytest.mark.parametrize("limit", [MAX_IMAGE_BYTES, MAX_DOCUMENT_BYTES])
def test_input_file_accepts_exact_byte_limit_and_rejects_one_more(tmp_path, limit):
    source = tmp_path / "large.png"
    with source.open("wb") as stream:
        stream.truncate(limit)
    assert (
        resolve_input_path(
            {"reference": tmp_path}, "reference", source.name, DOCUMENTS, max_bytes=limit
        )
        == source
    )
    with source.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(BridgeError) as failure:
        resolve_input_path(
            {"reference": tmp_path}, "reference", source.name, DOCUMENTS, max_bytes=limit
        )
    assert failure.value.code == "INPUT_TOO_LARGE"


def test_image_import_default_limit_is_32_mib(tmp_path):
    source = tmp_path / "large.png"
    with source.open("wb") as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(BridgeError) as failure:
        resolve_input_path({"reference": tmp_path}, "reference", source.name, IMAGES)
    assert failure.value.code == "INPUT_TOO_LARGE"


def kra(tmp_path, members=None):
    path = tmp_path / "reference.kra"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members or [
            ("mimetype", b"application/x-krita"),
            ("maindoc.xml", b"<DOC/>"),
        ]:
            archive.writestr(name, data)
    return path


def resolve_kra(path):
    return resolve_input_path(
        {"reference": path.parent},
        "reference",
        path.name,
        DOCUMENTS,
        max_bytes=MAX_DOCUMENT_BYTES,
    )


def test_kra_checks_archive_without_extracting_or_mutating_files(tmp_path):
    path = kra(tmp_path)
    original = path.read_bytes()
    assert resolve_kra(path) == path
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(BridgeError) as failure:
        resolve_input_path({"reference": tmp_path}, "reference", path.name, IMAGES)
    assert failure.value.code == "INVALID_PATH"


def test_kra_expanded_limit_counts_all_members(tmp_path, monkeypatch):
    monkeypatch.setattr(input_paths, "MAX_KRA_EXPANDED_BYTES", 100)
    path = kra(tmp_path, [("layer1", b"a" * 50), ("layer2", b"b" * 50)])
    assert resolve_kra(path) == path
    path = kra(tmp_path, [("layer1", b"a" * 50), ("layer2", b"b" * 51)])
    with pytest.raises(BridgeError) as failure:
        resolve_kra(path)
    assert failure.value.code == "INPUT_TOO_LARGE"


def test_kra_entry_limit_is_checked_before_zipfile_allocation(tmp_path, monkeypatch):
    path = kra(tmp_path, [(f"entry-{index}", b"") for index in range(3)])
    monkeypatch.setattr(input_paths, "MAX_KRA_ENTRIES", 2)

    def forbid_zipfile(*args):
        raise AssertionError("Oversized archive must be rejected before ZipFile construction")

    monkeypatch.setattr(zipfile, "ZipFile", forbid_zipfile)
    with pytest.raises(BridgeError) as failure:
        resolve_kra(path)
    assert failure.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "folder\\escape"])
def test_kra_rejects_unsafe_member_names(tmp_path, name):
    with pytest.raises(BridgeError) as failure:
        resolve_kra(kra(tmp_path, [(name, b"fixture")]))
    assert failure.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_kra_rejects_special_members(tmp_path, mode):
    info = zipfile.ZipInfo("special")
    info.create_system = 3
    info.external_attr = mode << 16
    with pytest.raises(BridgeError) as failure:
        resolve_kra(kra(tmp_path, [(info, b"target")]))
    assert failure.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("content", [b"not a zip", b"PK\x05\x06", b"PK\x03\x04" + b"x" * 100])
def test_kra_rejects_malformed_archive(tmp_path, content):
    path = tmp_path / "broken.kra"
    path.write_bytes(content)
    with pytest.raises(BridgeError) as failure:
        resolve_kra(path)
    assert failure.value.code == "INVALID_INPUT"


def test_kra_rejects_misreported_archive_count(tmp_path):
    path = kra(tmp_path)
    data = bytearray(path.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 1, 1)
    path.write_bytes(data)
    with pytest.raises(BridgeError) as failure:
        resolve_kra(path)
    assert failure.value.code == "INVALID_INPUT"
