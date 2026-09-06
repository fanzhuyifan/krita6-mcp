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

Before editing, list documents and inspect the target. The catalog has 13 core tools and eight optional AI Diffusion tools; the [bridge contract](bridge-contract.md) documents the commands.

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

Enable the [supported Qt6 add-on revision](diffusion-integration.md#source-boundary), connect its local server in Krita, and open the AI Diffusion docker for the target document. Check `krita_diffusion_status`: generation requires `generation_control: true`.

Use `krita_inspect_diffusion_document` to inspect prompts, selection, regional prompts, and control/reference layers. Configure selections, regions, and controls in the add-on; generation inherits them. Use `krita_list_diffusion_styles` to choose a style, or omit `style_id` to use the current one.

Example MCP tool arguments (replace the illustrative handles):

```text
krita_generate_diffusion(
  instance_id="instance-…", document_id="doc-…", operation_id="tree-001",
  positive_prompt="A watercolor oak tree in a sunny meadow",
  negative_prompt="text, watermark", strength=1.0, seed=42
)
```

Poll `krita_get_diffusion_generation` with that document and `generation_id="tree-001"` until it reports finished results. Inspect one with `krita_get_diffusion_result`, then call `krita_apply_diffusion_result` with its `result_id` and a new operation ID such as `tree-apply-001`. Application creates a new top paint layer. Repeating either mutation with the original ID and arguments does not submit/apply twice.

Strength below 1 refines the current canvas; an existing selection uses the add-on’s inpainting/refinement preparation. Generation requests one image and suppresses automatic canvas application, but the add-on still updates its job history and document annotations. Image handles expire if that history is removed.

`krita_list_diffusion_jobs` shows all current add-on jobs; only bridge-owned generations have retrievable/applicable MCP result handles. There is no backend cancellation tool. Cancelling a bridge submission after it has run does not stop rendering. No tool installs plugins, creates diffusion models, or connects backends. Core tools work when AI Diffusion is absent.

See [integration behavior and limits](diffusion-integration.md) and the [testing guide](testing.md).
