# krita6-mcp

An MCP server that lets an assistant inspect and edit a running Krita 6 desktop session, paint with Krita's native brushes, and see the result.

**Status: initial implementation, 0.1.0.** Linux is the first validation target. See [validation evidence and limits](docs/validation.md) before relying on other builds or platforms.

The system has two parts: an external Python MCP server and a PyQt6 plugin inside Krita. The plugin executes Krita calls on the GUI thread; the external server handles MCP and tool schemas. MCP dependencies are never installed into Krita's embedded Python.

## What works

- Discover instances, inspect documents/layers, and search brush presets.
- Create documents and paint layers; paint native paths or lines with endpoint pressure.
- Return a bounded inline PNG so the assistant can inspect its work.
- Save layered `.kra` files and export separate PNG files under configured output directories.
- Reconcile, deduplicate, and cancel requests with explicit operation IDs.

Native painting currently requires an active document view, an unlocked nonanimated paint layer, no selection, zero canvas offset, and RGBA/U8 with `sRGB-elle-V2-srgbtrc.icc`. The initial supported brush engine is the pixel brush engine. Paths do not support arbitrary per-point pressure. There is no arbitrary Python or action-execution tool, and no MCP undo tool; ordinary Krita undo is available for native strokes.

The bridge can inspect and edit layers produced by Krita AI Diffusion through Krita's normal API. It does not yet control diffusion generation, prompts, settings, or jobs. An [optional integration](docs/design.md#optional-krita-ai-diffusion-integration) is documented for future work.

## Install from this checkout

Requirements: Krita 6 with Python plugin support and PyQt6, external Python 3.10+, and [uv](https://docs.astral.sh/uv/). The current discovery implementation requires POSIX file permissions; Windows support is not implemented. macOS is untested.

```bash
uv sync --locked
uv run python tools/build_plugin.py
```

In Krita, use **Tools → Scripts → Import Python Plugin from File** and select `dist/krita6-bridge-0.1.0.zip`. Enable **Krita 6 MCP Bridge** in **Settings → Configure Krita → Python Plugin Manager**, then restart Krita. Alternatively, copy the folder and `.desktop` file from `plugin/` into the `pykrita/` directory under Krita's resource folder and enable the plugin.

The enabled plugin starts automatically. **Tools → Scripts** contains Start, Stop, and Status controls. Set `KRITA6_MCP_AUTOSTART=0` in Krita's environment to start it manually. Stop drains any running native operation before allowing a new bridge session.

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

On Linux, the live checks require `krita`, `Xvfb`/`xvfb-run`, `xauth`, and `dbus-run-session`:

```bash
uv run python tools/probe_krita.py
uv run python tools/smoke_krita.py
```

Both create disposable profiles, a virtual X display, and a private temporary socket directory. They do not install into the normal Krita profile. The first checks native pixels, resource capture, undo/redo, preview state, and `.kra` round-trip. The second drives the production plugin through real MCP stdio. Each prints the location of its report and test artwork. `--output` selects a new artifact directory; keep reports/logs outside Git unless deliberately sanitized for validation evidence.

## Project notes

- [Architecture and tool contract](docs/design.md)
- [Existing implementations and API evidence](docs/research.md)
- [Implementation milestones and validation gates](docs/implementation-plan.md)
- [Contributor guidance](AGENTS.md)

Keep protocol changes and host behavior documented together. Unimplemented capabilities are not silently emulated with a different rendering backend.
