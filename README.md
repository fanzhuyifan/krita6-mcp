# krita6-mcp

[![CI](https://github.com/fanzhuyifan/krita6-mcp/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/fanzhuyifan/krita6-mcp/actions/workflows/ci.yml)

An MCP server that lets an assistant inspect and edit a running Krita 6 desktop session, paint with Krita's native brushes, and see the result.

**Early alpha: 0.1.0, unreleased.** The native workflow has been tested on Linux/Krita 6.0.3 with small scratch documents and one pixel-brush preset. Large-document and long-session testing remains limited. See [validation evidence and limits](docs/validation.md) for the tested configurations.

The system has two parts: an external Python MCP server and a PyQt6 plugin inside Krita. The plugin executes Krita calls on the GUI thread; the external server handles MCP and tool schemas. MCP dependencies are never installed into Krita's embedded Python.

## What works

- Discover instances, inspect documents/layers, and search brush presets.
- Create documents and paint layers; paint native paths or lines with endpoint pressure.
- Return a bounded inline PNG so the assistant can inspect its work.
- Save layered `.kra` files and export separate PNG files under configured output directories.
- Reconcile, deduplicate, and cancel requests with explicit operation IDs.
- Inspect an already loaded Krita AI Diffusion plugin, its document settings, and its job queue.

Native painting currently requires an active document view, an unlocked nonanimated paint layer, no selection, zero canvas offset, and RGBA/U8 with `sRGB-elle-V2-srgbtrc.icc`. The initial supported brush engine is the pixel brush engine. Paths do not support arbitrary per-point pressure. There is no arbitrary Python or action-execution tool, and no MCP undo tool; ordinary Krita undo is available for native strokes.

The MCP catalog has 16 tools: 13 core tools and three optional AI Diffusion readers. It can inspect and edit layers produced by AI Diffusion through Krita's normal API. Direct diffusion generation, settings changes, job cancellation, and result application remain future work.

## Krita AI Diffusion

With a compatible AI Diffusion plugin already enabled in Krita, use:

- `krita_diffusion_status`: loaded/compatible state, observed connection state, and counts.
- `krita_inspect_diffusion_document`: an existing document model's prompts, style, strength, batch count, and progress.
- `krita_list_diffusion_jobs`: paginated job IDs, kinds, raw upstream states, and result counts.

These calls do not load the plugin, create a diffusion model, connect to a backend, or change a job/preview selection. The core tools work when AI Diffusion is absent; the status tool reports `not_loaded`. Job IDs can be null, and pagination indices are only positions in the current snapshot. Upstream `cancelled` can include failures.

**Compatibility as of 2026-09-06:** AI Diffusion's stable v1.53.0 targets Krita 5. The tested Krita 6 source is development commit `dda58d1c63e361207ccec085efbc34dbd32f1654`, which also reports version 1.53.0. The adapter checks loaded Qt6 objects and read interfaces; the version string alone is insufficient. See the [pinned integration record](docs/diffusion-integration.md) and [live evidence](docs/validation.md#ai-diffusion-readers). The test installation uses a disposable profile and does not install AI Diffusion into your normal Krita profile.

## Install from this checkout

Requirements: Krita 6 with Python plugin support and PyQt6, external Python 3.10+, and [uv](https://docs.astral.sh/uv/). The current discovery implementation requires POSIX file permissions; Windows support is not implemented. macOS is untested.

```bash
git clone https://github.com/fanzhuyifan/krita6-mcp.git
cd krita6-mcp
uv sync --locked
uv run python tools/build_plugin.py
```

In Krita, use **Tools → Scripts → Import Python Plugin from File** and select `dist/krita6-bridge-0.1.0.zip`. Enable **Krita 6 MCP Bridge** in **Settings → Configure Krita → Python Plugin Manager**, then restart Krita. Alternatively, copy the folder and `.desktop` file from `plugin/` into the `pykrita/` directory under Krita's resource folder and enable the plugin.

The enabled plugin starts automatically. **Tools → Scripts** contains Start, Stop, and Status controls. Set `KRITA6_MCP_AUTOSTART=0` in Krita's environment to start it manually. Stop drains any running native operation before allowing a new bridge session.

For an upgrade, stop the bridge, close Krita, and back up the existing plugin directory before replacing it. To uninstall, disable the plugin, restart Krita, and remove its `krita6_bridge/` folder and `krita6_bridge.desktop` file from the resource folder's `pykrita/` directory. Remove the MCP client's configuration separately. Your artwork and output directories are not part of the plugin installation.

Check the connection from the checkout:

```bash
uv run krita6-mcp doctor --json
```

`doctor` only reads connection state. It exits with code 1 when no bridge is reachable. It does not install or enable the plugin.

## Connect an MCP client

Use an absolute path to the checkout's Python environment:

```json
{
  "mcpServers": {
    "krita6": {
      "command": "/absolute/path/to/krita6-mcp/.venv/bin/python",
      "args": ["-m", "krita6_mcp.cli", "serve"]
    }
  }
}
```

The external server uses stdio. It discovers the local plugin through a private state file and authenticated loopback HTTP. `KRITA6_MCP_STATE_DIR` can override the state directory, but must be the same for Krita and the MCP process. With multiple live instances, use the explicit instance ID returned by `krita_status`.

Before editing, list documents and inspect the target. Every mutation requires an `operation_id`, such as `sketch-outline-001`. Reuse that same ID and identical arguments when retrying uncertain work; use a new ID for an intentional second edit. A timed-out/running operation can still complete. Query `krita_get_operation` instead of blindly repeating it. Cancellation prevents unstarted work; it cannot forcibly interrupt a native stroke already running.

## Save and export

File writes require named output roots configured **in Krita's environment**, not merely in the MCP server's environment. Create the directory first, then start Krita with, for example:

```bash
export KRITA6_MCP_OUTPUT_ROOTS='{"art":"/absolute/path/to/artwork"}'
krita
```

Fully exit an already-running Krita instance before relaunching with changed environment variables. Use `root="art"` and a relative `path`, such as `sketch.kra` or `preview.png`, in file tools. Existing files are rejected unless `overwrite=true`. Document creation, painting, and inline previews work without output roots.

## Development and verification

```bash
uv run pytest -q
uv run ruff check plugin src tools tests
uv run ruff format --check plugin src tools tests
uv build
```

Tests use local loopback sockets, so the runner needs local-network permission. Unit/fake-host tests do not prove native painting compatibility.

CI runs the non-GUI suite and distribution builds on Python 3.10, 3.12, and 3.14. It does not run Krita or establish native-host support. See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, review expectations, and test selection.

On Linux, the live checks require `krita`, `Xvfb`/`xvfb-run`, `xauth`, and `dbus-run-session`:

```bash
uv run python tools/probe_krita.py
uv run python tools/smoke_krita.py
uv run python tools/probe_plugin_import.py
```

The first two create disposable profiles, a virtual X display, and a private temporary socket directory. They do not install into the normal Krita profile. The first checks native pixels, resource capture, undo/redo, preview state, and `.kra` round-trip. The second drives the production plugin through real MCP stdio. Each prints the location of its report and test artwork. `--output` selects a new artifact directory; keep reports/logs outside Git unless deliberately sanitized for validation evidence. The third uses Krita's installed importer to extract the ZIP into a temporary directory; pass `--importer` if that module is installed at a different path.

For the optional diffusion reader, use a checkout of the exact development commit above, including its `ai_diffusion/websockets` submodule:

```bash
uv run python tools/probe_diffusion.py --source /absolute/path/to/pinned/krita-ai-diffusion
```

The probe verifies source fingerprints, loads the real plugin in an isolated profile, and inserts synthetic job records into its actual queue for inspection. Automatic updates are disabled and cloud mode has an empty token, which the pinned plugin handles without creating a backend client. No images are generated. This test proves observation behavior, not generation or backend compatibility.

## Project notes

- [Architecture and tool contract](docs/design.md)
- [Existing implementations and API evidence](docs/research.md)
- [Implementation milestones and validation gates](docs/implementation-plan.md)
- [Contributor guidance](AGENTS.md)
- [Contributing and development](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Maintainer release procedure](docs/releasing.md)

Keep protocol changes and host behavior documented together. Unimplemented capabilities are not silently emulated with a different rendering backend.

## Contributing and support

Bug reports, small focused pull requests, and reproducible compatibility results are welcome. Start with an issue for substantial API changes. Use [GitHub issues](https://github.com/fanzhuyifan/krita6-mcp/issues) for bugs and feature requests; use the [security policy](SECURITY.md) for vulnerabilities. Do not upload discovery tokens, private logs, credentials, or artwork without permission. The default development branch is `master`.

## License and acknowledgments

[MIT](LICENSE), copyright 2026 Yifan Zhu and contributors. This is an independent project, not an official KDE/Krita or Krita AI Diffusion integration. Krita, the MCP SDK, and optional plugins retain their own licenses. They are not bundled in the plugin ZIP.

The design builds on Krita's public scripting APIs and ideas from existing MCP integrations. The [research record](docs/research.md) credits those projects and separates source observations from live validation. No third-party implementation source was copied into this repository.
