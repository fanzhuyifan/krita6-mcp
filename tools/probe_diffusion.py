"""Inspect a real AI Diffusion development plugin through MCP in isolated Krita (Linux)."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import traceback

from mcp import Client, StdioServerParameters

from probe_krita import isolated_environment, launch_krita

REFERENCE_COMMIT = "dda58d1c63e361207ccec085efbc34dbd32f1654"
REFERENCE_FILES = {
    "ai_diffusion/__init__.py": "ca760fdad688fbf8d84150d82f6e7cec0b5ee1ec03fe2fd5c636c968b4ef772e",
    "ai_diffusion/extension.py": "b39c47d683891a221da2cdfc79efb24d86c99975f76b53919e506b794c63bf93",
    "ai_diffusion/document.py": "d3fab9a0f67456a033b543d82e577b17e52bb4c3a59001ea9f66877bbd520364",
    "ai_diffusion/model/root.py": "7449ddc8cb465767f57d52092d2917c45ead8ab5d34633272f5d2fc969d1471d",
    "ai_diffusion/model/model.py": "e662dc70fe54c7f561467b7081874095cea41d146d19b4e67bac807c20966f9d",
    "ai_diffusion/model/jobs.py": "f7d282cafa44bfe4524cf4559f49dc87291a0338d7b31e7e9f8dd78450a79cae",
    "ai_diffusion/model/connection.py": "629665d8587e46b88bd58a8dc34f993f774b7ef8e4bfc3f6c031d436c8fde162",
}


async def scenario(base, instance_id):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "krita6_mcp.cli", "serve"],
        env=dict(os.environ, KRITA6_MCP_STATE_DIR=str(base / "state")),
    )
    async with Client(parameters) as client:
        host_response = await client.call_tool("krita_status", {"instance_id": instance_id})
        assert not host_response.is_error
        host = host_response.structured_content
        host_versions = {
            key: host[key]
            for key in (
                "krita_version",
                "qt_version",
                "pyqt_version",
                "python_version",
                "platform",
                "display_backend",
                "plugin_version",
                "bridge_protocol",
            )
        }
        listing = await client.list_tools()
        tools = listing.tools if hasattr(listing, "tools") else listing
        assert len(tools) == 35

        async def read(name, **arguments):
            response = await client.call_tool(name, {"instance_id": instance_id, **arguments})
            if response.is_error:
                raise AssertionError(f"{name}: {response.structured_content}")
            data = response.structured_content
            if data.get("state") != "succeeded":
                raise AssertionError(f"Unexpected pending read: {name}")
            return data["result"]

        status = await read("krita_diffusion_status")
        assert status["availability"] == "available", status
        assert status["connection_state"] == "auth_missing", status
        assert status["generation_control"] is False
        assert status["available_model_count"] is None
        inventory = await read("krita_list_documents")
        documents = [
            doc for doc in inventory["documents"] if doc["name"] == "Diffusion inspection fixture"
        ]
        assert len(documents) == 1
        target = {"document_id": documents[0]["document_id"]}
        document = await read("krita_inspect_diffusion_document", **target)
        assert document["document_status"] == "tracked", document
        assert document["model"]["positive_prompt"] == "A red test square", document
        assert document["model"]["negative_prompt"] == "blur", document
        assert document["model"]["strength"] == 0.65, document
        assert document["model"]["batch_count"] == 2, document
        jobs = await read("krita_list_diffusion_jobs", **target, offset=0, limit=2)
        tail = await read("krita_list_diffusion_jobs", **target, offset=2, limit=2)
        empty = await read("krita_list_diffusion_jobs", **target, offset=10, limit=2)
        assert len(jobs["jobs"]) == len(tail["jobs"]) == 2
        assert empty["jobs"] == []
        states = [job["state"] for job in jobs["jobs"] + tail["jobs"]]
        assert states == ["queued", "executing", "finished", "cancelled"], states
        assert jobs["jobs"][0]["job_id"] is None
        assert tail["jobs"][0]["job_id"] == "fixture-job-2"
        # The fixture independently verifies reads preserved canvas and plugin state.
        (base / "verify-fixture").touch()
        deadline = time.monotonic() + 10
        while not (base / "fixture-report.json").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Fixture verification did not complete")
            await asyncio.sleep(0.05)
        fixture = json.loads((base / "fixture-report.json").read_text())
        assert fixture["passed"], fixture
        config_checks = {}
        native = await read("krita_inspect_document", **target)
        node_id = native["active_node_id"]
        assert status["configuration_control"] is True

        async def mutate(name, operation_id, **params):
            args = {**target, "instance_id": instance_id, "operation_id": operation_id, **params}
            first = await client.call_tool(name, args)
            assert not first.is_error, first.structured_content
            second = await client.call_tool(name, args)
            assert first.structured_content == second.structured_content
            assert first.structured_content["state"] == "succeeded"
            return first.structured_content["result"]

        await mutate(
            "krita_configure_diffusion",
            "configure-settings",
            positive_prompt="Configured root",
            negative_prompt="noise",
            strength=0.72,
            seed=42,
            fixed_seed=True,
            batch_count=3,
            region_only=True,
            resolution_multiplier=0.75,
            inpaint_mode="custom",
            use_inpaint=False,
            use_prompt_focus=True,
        )
        observed = (await read("krita_inspect_diffusion_document", **target))["model"]
        assert observed["positive_prompt"] == "Configured root"
        assert observed["negative_prompt"] == "noise"
        assert observed["strength"] == 0.72 and observed["seed"] == 42
        assert observed["fixed_seed"] and observed["batch_count"] == 3 and observed["region_only"]
        assert observed["resolution_multiplier"] == 0.75
        assert not observed["use_inpaint"] and observed["use_prompt_focus"]
        assert observed["canvas_context"]["inpaint_mode"] == "custom"
        config_checks["persistent_settings_and_duplicate_ids"] = True
        controls = [
            {"node_id": node_id, "mode": "scribble", "strength": 0.8, "start": 0.2, "end": 0.9}
        ]
        await mutate("krita_set_diffusion_controls", "root-controls", controls=controls)
        await mutate(
            "krita_set_diffusion_region",
            "create-region",
            node_id=node_id,
            positive_prompt="Regional subject",
        )
        await mutate(
            "krita_set_diffusion_controls",
            "region-controls",
            controls=controls,
            region_node_id=node_id,
        )
        context = (await read("krita_inspect_diffusion_document", **target))["model"][
            "canvas_context"
        ]
        assert len(context["control_layers"]) == len(context["regions"]) == 1
        assert context["regions"][0]["positive_prompt"] == "Regional subject"
        assert context["regions"][0]["linked_node_ids"] == [node_id]
        for entry in [context["control_layers"][0], context["regions"][0]["control_layers"][0]]:
            assert all(entry[key] == value for key, value in controls[0].items()), entry
        config_checks["root_and_region_controls_and_duplicate_ids"] = True
        await mutate(
            "krita_set_diffusion_region",
            "update-region",
            node_id=node_id,
            positive_prompt="Updated subject",
        )
        context = (await read("krita_inspect_diffusion_document", **target))["model"][
            "canvas_context"
        ]
        assert (
            len(context["regions"]) == 1
            and context["regions"][0]["positive_prompt"] == "Updated subject"
        )
        await mutate("krita_set_diffusion_controls", "clear-root-controls", controls=[])
        await mutate("krita_set_diffusion_region", "remove-region", node_id=node_id, remove=True)
        context = (await read("krita_inspect_diffusion_document", **target))["model"][
            "canvas_context"
        ]
        assert context["control_layers"] == context["regions"] == []
        config_checks["update_clear_remove"] = True
        # Ask the native fixture to independently verify pixels, layers and job state.
        (base / "verify-configuration").touch()
        deadline = time.monotonic() + 10
        while not (base / "configuration-report.json").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Configuration fixture did not finish")
            await asyncio.sleep(0.05)
        checked = json.loads((base / "configuration-report.json").read_text())
        assert checked["passed"], checked
        config_checks.update(checked["checks"])
        document.pop("document_id", None)
        return {
            "passed": True,
            "reference_commit": REFERENCE_COMMIT,
            "source_kind": "unreleased Krita 6 development snapshot",
            "generated_images": False,
            "host": host_versions,
            "fixture_jobs": "Synthetic records in the real upstream JobQueue; no backend execution",
            "status": status,
            "document": document,
            "observed_job_states": states,
            "checks": {
                "tool_count": len(tools),
                "real_plugin_loaded": True,
                "document_inspection": True,
                "paginated_jobs": True,
                "nullable_plugin_job_id": True,
                **fixture["checks"],
                **config_checks,
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True, help="Pinned upstream source with bundled websockets"
    )
    parser.add_argument("--krita", default="krita")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    for name, expected in REFERENCE_FILES.items():
        path = source / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            parser.error(f"Source does not match reference commit {REFERENCE_COMMIT}: {name}")
    if not (source / "ai_diffusion/websockets/src/websockets").is_dir():
        parser.error("Source must include its bundled websockets dependency.")
    if "from PyQt6" not in (source / "ai_diffusion/model/root.py").read_text():
        parser.error("This probe requires the Krita 6 development plugin, not the stable Qt5 ZIP.")
    root = Path(__file__).resolve().parents[1]
    base = (args.output or Path(tempfile.mkdtemp(prefix="krita6-diffusion-"))).resolve()
    if (base / "report.json").exists():
        parser.error("Output already contains a report; choose a new directory.")
    env = isolated_environment(base)
    plugins = base / "data/krita/pykrita"
    plugins.mkdir(parents=True, exist_ok=True)
    for name, origin in (("ai_diffusion", source), ("krita6_bridge", root / "plugin")):
        shutil.copytree(
            origin / name,
            plugins / name,
            ignore=shutil.ignore_patterns("__pycache__", ".git", "debugpy"),
            dirs_exist_ok=True,
        )
        shutil.copyfile(origin / f"{name}.desktop", plugins / f"{name}.desktop")
    (plugins / "diffusion_fixture").mkdir(exist_ok=True)
    shutil.copyfile(
        root / "tests/host/diffusion_fixture.py", plugins / "diffusion_fixture/__init__.py"
    )
    (plugins / "diffusion_fixture.desktop").write_text(
        "[Desktop Entry]\nType=Service\nServiceTypes=Krita/PythonPlugin\n"
        "X-KDE-Library=diffusion_fixture\nX-Python-2-Compatible=false\nName=Diffusion Read Probe\n"
    )
    # Upstream cloud mode with an empty token returns auth_missing without creating
    # a client. Auto-update is disabled; no backend is started or contacted.
    for directory in (base / "data/krita/ai_diffusion", base / "data/krita-ai-diffusion"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(
            json.dumps({"server_mode": "cloud", "access_token": "", "auto_update": False})
        )
    (base / "config/kritarc").write_text(
        "[python]\nenable_ai_diffusion=true\nenable_krita6_bridge=true\nenable_diffusion_fixture=true\n"
    )
    env.update(
        KRITA6_MCP_STATE_DIR=str(base / "state"),
        KRITA6_MCP_OUTPUT_ROOTS="{}",
        KRITA6_MCP_AUTOSTART="1",
        KRITA6_DIFFUSION_FIXTURE_OUTPUT=str(base),
    )
    report = {"passed": False}
    with launch_krita(base, args.krita, env) as process:
        try:
            deadline = time.monotonic() + 90
            while True:
                discovery = list((base / "state").glob("instance-*.json"))
                if discovery and (base / "fixture-ready.json").exists():
                    instance_id = json.loads(discovery[0].read_text())["instance_id"]
                    break
                if (base / "fixture-report.json").exists():
                    raise AssertionError((base / "fixture-report.json").read_text())
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Fixture did not start; inspect the isolated krita.log")
                time.sleep(0.1)
            report = asyncio.run(scenario(base, instance_id))
            report["upstream_files_sha256"] = REFERENCE_FILES
        except Exception:
            report["traceback"] = traceback.format_exc()
        finally:
            (base / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Artifacts: {base}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
