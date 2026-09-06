# Setup and usage

Start with the [README installation steps](../README.md#install). The external MCP server runs in its own Python environment; the Krita plugin uses Krita's existing Python and PyQt6. Never install MCP dependencies or another Qt binding into Krita.

## Plugin installation and lifecycle

The ZIP contains `krita6_bridge.desktop` and the `krita6_bridge/` package. As an alternative to Krita's importer, copy those entries into `pykrita/` under Krita's resource folder, then enable the plugin and restart Krita. The resource folder is accessible through **Settings → Manage Resources → Open Resource Folder**.

The enabled plugin starts automatically. Set `KRITA6_MCP_AUTOSTART=0` in Krita's environment to start it manually using **Tools → Scripts**. Start, Stop, and Status controls are available there. Stop rejects new work and drains any running native operation before allowing a new bridge session.

For an upgrade, stop the bridge, close Krita, and back up the existing plugin directory before replacing it. Keep the external package and plugin from the same release. To uninstall, disable the plugin, restart Krita, and remove `krita6_bridge/` and `krita6_bridge.desktop` from the resource folder's `pykrita/` directory. Remove the MCP client's configuration separately. Artwork and output directories are not part of the plugin installation.

## Connection and discovery

Configure the MCP client using the [stdio example](../README.md#connect-an-mcp-client). Use the checkout's absolute Python path so launching the client does not depend on its working directory.

The external server discovers the local plugin through a private state file and authenticated loopback HTTP. `KRITA6_MCP_STATE_DIR` overrides the state directory and must match in Krita and the MCP process. `krita_status` lists registered instances; commands require an explicit instance ID even when only one is live.

Run `uv run krita6-mcp doctor --json` from the checkout to read connection state. It exits with code 1 when no bridge is reachable and does not install or enable the plugin. If no instance appears, confirm the plugin is enabled, restart Krita, and check its Status control and any state-directory override.

## Editing and retries

Before editing, list documents and inspect the target. The catalog has 13 core tools and three optional AI Diffusion readers; the [bridge contract](bridge-contract.md) documents the commands.

Native painting requires an active document view, an unlocked nonanimated paint layer, no selection, zero canvas offset, and RGBA/U8 with `sRGB-elle-V2-srgbtrc.icc`. The supported engine is the pixel brush engine. Paths do not support arbitrary per-point pressure; lines accept endpoint pressure. There is no arbitrary Python/action-execution tool or MCP undo tool. Ordinary Krita undo is available for native strokes. See [validation evidence](validation.md) for the exact tested build and preset.

Every mutation requires an `operation_id`, such as `sketch-outline-001`. Reuse the same ID and identical arguments when retrying uncertain work; use a new ID for an intentional second edit. A timed-out or running operation can still complete. Query `krita_get_operation` to reconcile it. Cancellation prevents unstarted work; it cannot forcibly interrupt a native stroke already running. Operation identities are retained within a live bridge session, not across a Krita crash.

## Save and export

File writes require named output roots configured **in Krita's environment**, not merely in the MCP server's environment. Create the directory first, fully exit Krita, then relaunch it:

```bash
mkdir -p /absolute/path/to/artwork
KRITA6_MCP_OUTPUT_ROOTS='{"art":"/absolute/path/to/artwork"}' krita
```

Use `root="art"` and a relative `path`, such as `sketch.kra` or `preview.png`, in file tools. Parent directories must already exist. Existing files are rejected unless `overwrite=true`. Document creation, painting, and inline previews work without output roots.

## Krita AI Diffusion

With a compatible plugin already enabled in Krita, use:

- `krita_diffusion_status` for loaded/compatible state, observed connection state, and counts.
- `krita_inspect_diffusion_document` for an existing document model's prompts, style, strength, batch count, and progress.
- `krita_list_diffusion_jobs` for paginated job IDs, kinds, raw upstream states, and result counts.

These tools do not load plugins, create diffusion models, connect backends, change settings, generate images, cancel jobs, or select/apply results. The core tools work when AI Diffusion is absent; its status reports `not_loaded`. Core painting tools can edit supported paint layers produced by AI Diffusion.

Job IDs can be null, and pagination indices are positions in the current snapshot. Upstream `cancelled` can include failures. Compatibility is tied to a tested Qt6 development revision, not just a version string. See the [pinned integration record](diffusion-integration.md) and [testing guide](testing.md) for details.
