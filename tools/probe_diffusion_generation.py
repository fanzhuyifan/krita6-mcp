"""Generate through the installed AI Diffusion add-on using disposable Linux test profiles."""

import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

from mcp import Client, StdioServerParameters

from probe_diffusion import REFERENCE_COMMIT, REFERENCE_FILES
from probe_krita import isolated_environment, launch_krita


def read_backend(url, endpoint):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{url}/{endpoint}", timeout=5) as response:
        return json.load(response)


@contextmanager
def launch_backend(base, installed):
    """Use installed Python/models but copy code that may write local node caches."""
    backend = base / "backend"
    source = installed / "ComfyUI"
    code = backend / "ComfyUI"
    code.mkdir(parents=True)
    excluded = {
        ".git",
        ".cache",
        "__pycache__",
        "models",
        "input",
        "output",
        "temp",
        "user",
        "extra_model_paths.yaml",
    }
    for item in source.iterdir():
        if item.name in excluded:
            continue
        target = code / item.name
        if item.is_dir():
            shutil.copytree(
                item, target, ignore=shutil.ignore_patterns("__pycache__", ".git", ".cache")
            )
        else:
            shutil.copyfile(item, target)
    for name in ("user", "input", "output", "temp", "cache", "home"):
        (backend / name).mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH"):
        env.pop(name, None)
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        XDG_CACHE_HOME=str(backend / "cache"),
        XDG_CONFIG_HOME=str(backend / "home"),
        XDG_DATA_HOME=str(backend / "home"),
        TMPDIR=str(backend / "temp"),
        HF_HOME=str(backend / "cache/huggingface"),
        TORCH_HOME=str(backend / "cache/torch"),
        CUDA_CACHE_PATH=str(backend / "cache/cuda"),
        TRITON_CACHE_DIR=str(backend / "cache/triton"),
        AUX_ANNOTATOR_CKPTS_PATH=str(backend / "cache/annotators"),
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    command = [
        str(installed / "venv/bin/python"),
        "-su",
        str(code / "main.py"),
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--models-directory",
        str(installed / "models"),
        "--user-directory",
        str(backend / "user"),
        "--input-directory",
        str(backend / "input"),
        "--output-directory",
        str(backend / "output"),
        "--temp-directory",
        str(backend / "temp"),
        "--database-url",
        f"sqlite:///{backend / 'user/test.db'}",
        "--disable-auto-launch",
        "--disable-api-nodes",
    ]
    with (base / "backend.log").open("w") as log:
        process = subprocess.Popen(
            command, cwd=code, env=env, stdout=log, stderr=log, start_new_session=True
        )
        try:
            deadline = time.monotonic() + 180
            while True:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Scratch backend did not start; inspect backend.log")
                try:
                    stats = read_backend(url, "system_stats")
                    break
                except (OSError, ValueError):
                    time.sleep(0.25)
            print("Scratch ComfyUI ready on a private loopback port", flush=True)
            yield url, stats
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


async def fixture_phase(base, phase):
    (base / f"fixture-request-{phase}").touch()
    deadline = time.monotonic() + 30
    path = base / f"fixture-{phase}.json"
    while not path.exists():
        if (base / "fixture-failure.json").exists():
            raise AssertionError((base / "fixture-failure.json").read_text())
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Fixture phase timed out: {phase}")
        await asyncio.sleep(0.05)
    return json.loads(path.read_text())


async def scenario(base, instance_id):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "krita6_mcp.cli", "serve"],
        env=dict(os.environ, KRITA6_MCP_STATE_DIR=str(base / "state")),
    )
    checks = {}
    async with Client(parameters) as client:

        async def call(name, **arguments):
            (base / "progress.json").write_text(json.dumps({"tool": name}))
            response = await client.call_tool(name, {"instance_id": instance_id, **arguments})
            data = response.structured_content
            deadline = time.monotonic() + 30
            while not response.is_error and data.get("state") in {
                "queued",
                "running",
                "cancel_requested",
            }:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Bridge operation did not settle: {name}")
                await asyncio.sleep(0.05)
                response = await client.call_tool(
                    "krita_get_operation",
                    {"instance_id": instance_id, "operation_id": data["operation_id"]},
                )
                data = response.structured_content
            if response.is_error:
                raise AssertionError(f"{name}: {data}")
            return response, data.get("result", data)

        _, session = await call("krita_status")
        _, inventory = await call("krita_list_documents")
        docs = [d for d in inventory["documents"] if d["name"] == "Diffusion generation fixture"]
        assert len(docs) == 1
        document_id = docs[0]["document_id"]
        target = {"document_id": document_id}
        _, status = await call("krita_diffusion_status")
        assert status["connection_state"] == "connected", status
        assert status["generation_control"] is True
        assert status["read_only"] is False
        checks["local_generation_capability"] = True
        _, styles = await call("krita_list_diffusion_styles")
        style = next(s for s in styles["styles"] if s["name"] == "Digital Artwork (SD1.5)")
        before = json.loads((base / "fixture-ready.json").read_text())["snapshot"]
        generation_reports = []

        for index, strength in enumerate((1.0, 0.65)):
            if index:
                before = await fixture_phase(base, "selection")
                assert before["selection"] == [160, 160, 192, 192]
            _, inspected = await call("krita_inspect_diffusion_document", **target)
            context = inspected["model"]["canvas_context"]
            assert context["snapshot_only"] is True
            assert context["selection_bounds"] == (
                {"x": 160, "y": 160, "width": 192, "height": 192} if index else None
            )
            checks[f"generation_{index}_canvas_context"] = True
            operation_id = f"generation-smoke-{index}"
            arguments = dict(
                **target,
                operation_id=operation_id,
                positive_prompt="A red ceramic teapot on a pale blue table, simple illustration",
                negative_prompt="blurry, text",
                style_id=style["style_id"],
                strength=strength,
                seed=4050 + index,
            )
            _, submitted = await call("krita_generate_diffusion", **arguments)
            _, repeated = await call("krita_generate_diffusion", **arguments)
            assert submitted == repeated
            generation_id = submitted["generation_id"]
            assert generation_id == operation_id
            deadline = time.monotonic() + 300
            while True:
                _, generation = await call(
                    "krita_get_diffusion_generation", **target, generation_id=generation_id
                )
                (base / "generation-progress.json").write_text(json.dumps(generation))
                if generation["state"] in {"finished", "succeeded"}:
                    break
                if generation["state"] in {
                    "failed",
                    "cancelled",
                    "error",
                    "submission_failed",
                    "submission_unknown",
                    "unavailable",
                }:
                    raise AssertionError(generation)
                if time.monotonic() >= deadline:
                    raise TimeoutError("AI Diffusion generation did not finish")
                await asyncio.sleep(0.5)
            results = generation["results"]
            assert len(results) == 1, generation
            result_id = results[0]["result_id"]
            response, preview = await call(
                "krita_get_diffusion_result",
                **target,
                generation_id=generation_id,
                result_id=result_id,
                max_edge=512,
            )
            images = [item for item in response.content if item.type == "image"]
            assert len(images) == 1
            png = base64.b64decode(images[0].data)
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            dimensions = struct.unpack(">II", png[16:24])
            assert max(dimensions) <= 512
            (base / f"result-{index}.png").write_bytes(png)
            if not index:
                small_response, _ = await call(
                    "krita_get_diffusion_result",
                    **target,
                    generation_id=generation_id,
                    result_id=result_id,
                    max_edge=32,
                )
                small_image = next(item for item in small_response.content if item.type == "image")
                small_png = base64.b64decode(small_image.data)
                assert struct.unpack(">II", small_png[16:24]) == (32, 32)
                checks["minimum_preview_size"] = True
            ready = await fixture_phase(base, f"before-apply-{index}")
            assert ready["pixels"] == before["pixels"]
            assert ready["layers"] == before["layers"]
            assert ready["selection"] == before["selection"]
            assert ready["settings"] == before["settings"]
            assert len(ready["jobs"]) == index + 1
            assert ready["jobs"][-1]["result_count"] == 1
            if index:
                assert ready["jobs"][-1]["workflow_kind"] == "refine_region"
                assert ready["jobs"][-1]["has_mask"] is True
            else:
                assert ready["jobs"][-1]["workflow_kind"] == "generate"
            apply_arguments = dict(
                **target,
                generation_id=generation_id,
                result_id=result_id,
                operation_id=f"generation-apply-{index}",
            )
            _, applied = await call("krita_apply_diffusion_result", **apply_arguments)
            _, reapplied = await call("krita_apply_diffusion_result", **apply_arguments)
            assert applied == reapplied
            _, canvas = await call("krita_inspect_document", **target)
            assert len(applied["new_node_ids"]) == 1
            assert applied["new_node_ids"][0] in {node["node_id"] for node in canvas["layers"]}
            checks[f"generation_{index}_applied_layer_handle"] = True
            after = await fixture_phase(base, f"after-apply-{index}")
            assert after["pixels"] != before["pixels"]
            assert len(after["layers"]) == len(before["layers"]) + 1
            assert after["selection"] == before["selection"]
            assert after["settings"] == before["settings"]
            if index:
                assert after["corner_pixels"] == before["corner_pixels"]
                checks["selection_refine_preserves_far_corner"] = True
            undone = await fixture_phase(base, f"undo-{index}")
            assert undone["pixels"] == before["pixels"]
            assert undone["layers"] == before["layers"]
            redone = await fixture_phase(base, f"redo-{index}")
            assert redone["pixels"] == after["pixels"]
            assert redone["layers"] == after["layers"]
            checks[f"generation_{index}_native_undo_redo"] = True
            checks[f"generation_{index}_duplicate_submitted_once"] = True
            checks[f"generation_{index}_canvas_unchanged_until_apply"] = True
            checks[f"generation_{index}_settings_preserved"] = True
            checks[f"generation_{index}_inline_png"] = True
            checks[f"generation_{index}_duplicate_apply_once"] = True
            generation_reports.append(
                {
                    "workflow_kind": ready["jobs"][-1]["workflow_kind"],
                    "has_mask": ready["jobs"][-1]["has_mask"],
                    "dimensions": list(dimensions),
                    "png_sha256": hashlib.sha256(png).hexdigest(),
                    "preview": {
                        key: preview[key]
                        for key in (
                            "mime_type",
                            "size_bytes",
                            "source_bounds",
                            "preview_width",
                            "preview_height",
                            "scale",
                            "color",
                        )
                    },
                }
            )
        return {
            "passed": True,
            "reference_commit": REFERENCE_COMMIT,
            "host": {
                key: session[key]
                for key in (
                    "krita_version",
                    "qt_version",
                    "pyqt_version",
                    "python_version",
                    "platform",
                )
            },
            "checks": checks,
            "generations": generation_reports,
        }


def prepare_krita(base, source, url):
    root = Path(__file__).resolve().parents[1]
    env = isolated_environment(base)
    plugins = base / "data/krita/pykrita"
    plugins.mkdir(parents=True, exist_ok=True)
    for name, origin in (("ai_diffusion", source), ("krita6_bridge", root / "plugin")):
        shutil.copytree(
            origin / name,
            plugins / name,
            ignore=shutil.ignore_patterns("__pycache__", ".git", "debugpy"),
        )
        shutil.copyfile(origin / f"{name}.desktop", plugins / f"{name}.desktop")
    (plugins / "diffusion_generation_fixture").mkdir()
    shutil.copyfile(
        root / "tests/host/diffusion_generation_fixture.py",
        plugins / "diffusion_generation_fixture/__init__.py",
    )
    (plugins / "diffusion_generation_fixture.desktop").write_text(
        "[Desktop Entry]\nType=Service\nServiceTypes=Krita/PythonPlugin\n"
        "X-KDE-Library=diffusion_generation_fixture\nX-Python-2-Compatible=false\n"
        "Name=Diffusion Generation Probe\n"
    )
    for directory in (base / "data/krita/ai_diffusion", base / "data/krita-ai-diffusion"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(
            json.dumps(
                {
                    "server_mode": "external",
                    "server_url": url,
                    "auto_update": False,
                    "access_token": "",
                    "generation_finished_action": "apply",
                    "batch_size": 2,
                }
            )
        )
    (base / "config/kritarc").write_text(
        "[python]\nenable_ai_diffusion=true\nenable_krita6_bridge=true\n"
        "enable_diffusion_generation_fixture=true\n"
    )
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "LD_LIBRARY_PATH",
        "QT_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
        "QT_QUICK_CONTROLS_STYLE",
        "KRITA_RESOURCE_DIRS",
    ):
        env.pop(key, None)
    env.update(
        KRITA6_MCP_STATE_DIR=str(base / "state"),
        KRITA6_MCP_OUTPUT_ROOTS="{}",
        KRITA6_MCP_AUTOSTART="1",
        KRITA6_DIFFUSION_FIXTURE_OUTPUT=str(base),
    )
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Trusted pinned plugin parent")
    parser.add_argument("--server", type=Path, required=True, help="Installed local managed server")
    parser.add_argument("--krita", default="krita")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-backend", action="store_true", help="Check scratch startup only")
    args = parser.parse_args()
    source, server = args.source.resolve(), args.server.resolve()
    for name, expected in REFERENCE_FILES.items():
        path = source / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            parser.error(f"Source differs from pinned {REFERENCE_COMMIT}: {name}")
    for relative in (
        "ComfyUI/main.py",
        "venv/bin/python",
        "models/checkpoints/dreamshaper_8.safetensors",
    ):
        if not (server / relative).is_file():
            parser.error(f"Local test resource is missing: {relative}")
    base = (args.output or Path(tempfile.mkdtemp(prefix="krita6-generation-"))).resolve()
    if any((base / name).exists() for name in ("report.json", "backend", "data")):
        parser.error("Choose a new, empty output directory")
    base.mkdir(parents=True, exist_ok=True)
    report = {"passed": False}
    try:
        with launch_backend(base, server) as (url, stats):
            backend = {
                key: stats.get("system", {}).get(key)
                for key in ("comfyui_version", "pytorch_version", "python_version")
            }
            nodes = read_backend(url, "object_info")
            checkpoints = nodes["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            assert "dreamshaper_8.safetensors" in checkpoints
            if args.check_backend:
                report = {"passed": True, "backend": backend, "generation_tested": False}
            else:
                env = prepare_krita(base, source, url)
                with launch_krita(base, args.krita, env) as process:
                    deadline = time.monotonic() + 90
                    while True:
                        paths = list((base / "state").glob("instance-*.json"))
                        if paths and (base / "fixture-ready.json").exists():
                            instance_id = json.loads(paths[0].read_text())["instance_id"]
                            break
                        if (base / "fixture-failure.json").exists():
                            raise AssertionError((base / "fixture-failure.json").read_text())
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Fixture did not start; inspect krita.log")
                        time.sleep(0.1)
                    report = asyncio.run(scenario(base, instance_id))
                    report["backend"] = backend
    except Exception:
        report["traceback"] = traceback.format_exc()
    finally:
        (base / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Artifacts: {base}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
