# krita6-mcp

[![CI](https://github.com/fanzhuyifan/krita6-mcp/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/fanzhuyifan/krita6-mcp/actions/workflows/ci.yml)

Inspect documents, paint with Krita's native brushes, preview the canvas, and save editable artwork through MCP.

**Early alpha, 0.1.0 unreleased.** Tested on Linux/Krita 6.0.3 with small documents and one pixel-brush preset. Windows is unsupported; macOS is untested. See [supported behavior and limits](docs/validation.md).

## What works

- Document/layer inspection and creation, preset search, native paths and lines with endpoint pressure.
- Inline PNG previews, layered `.kra` saves, and PNG export.
- Optional [AI Diffusion inspection](docs/diffusion-integration.md); generation is not implemented.

## Install

Requires Krita 6 with Python plugins and PyQt6, external Python 3.10+, and [uv](https://docs.astral.sh/uv/). The MCP environment stays separate from Krita's embedded Python.

```bash
git clone https://github.com/fanzhuyifan/krita6-mcp.git
cd krita6-mcp
uv sync --locked
uv run python tools/build_plugin.py
```

In Krita, import `dist/krita6-bridge-0.1.0.zip` through **Tools → Scripts → Import Python Plugin from File**, then restart. Enable **Krita 6 MCP Bridge** in **Settings → Configure Krita → Python Plugin Manager** and restart again.

The bridge starts automatically. Start/Stop/Status controls are under **Tools → Scripts**. See [setup, upgrades, and removal](docs/usage.md) for details.

## Connect an MCP client

Configure a stdio server using your checkout's absolute Python path:

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

With Krita running, check the connection:

```bash
uv run krita6-mcp doctor --json
```

Inspect targets before editing. Reuse `operation_id` when retrying an edit, and reconcile timeouts with `krita_get_operation`. See the [usage guide](docs/usage.md#editing-and-retries).

## Save and export

Fully exit Krita, create an output directory, and relaunch with that directory configured:

```bash
mkdir -p /absolute/path/to/artwork
KRITA6_MCP_OUTPUT_ROOTS='{"art":"/absolute/path/to/artwork"}' krita
```

File tools use `root="art"` and a relative path. Replacing files requires `overwrite=true`. Painting and previews work without output roots. [More configuration options](docs/usage.md#save-and-export).

## Development and verification

```bash
uv run pytest -q
uv run ruff check plugin src tools tests
uv run ruff format --check plugin src tools tests
uv build --no-sources
```

Linux host checks require Krita, Xvfb, xauth, and D-Bus. Run from a shell without an activated Python environment or conflicting KDE development paths:

```bash
.venv/bin/python tools/probe_krita.py
.venv/bin/python tools/smoke_krita.py
.venv/bin/python tools/probe_plugin_import.py
```

Host probes use isolated profiles and scratch files. CI tests the external runtime on Python 3.10, 3.12, and 3.14; native compatibility requires the live probes. See [testing instructions](docs/testing.md) and [contributing](CONTRIBUTING.md).

## Development approach

Developed primarily with AI coding agents. Review and additional compatibility testing are welcome.

[MIT](LICENSE) · Independent of KDE/Krita and Krita AI Diffusion.

[Issues](https://github.com/fanzhuyifan/krita6-mcp/issues) · [Security](SECURITY.md) · [Design](docs/design.md) · [Acknowledgments](docs/research.md) · [Changelog](CHANGELOG.md)
