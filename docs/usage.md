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

Before editing, list documents and inspect the target. The catalog has 45 core tools and eleven optional AI Diffusion tools; the [bridge contract](bridge-contract.md) documents the commands.

Native painting requires an active document view, an unlocked nonanimated paint layer, no selection, zero canvas offset, and RGBA/U8 with `sRGB-elle-V2-srgbtrc.icc`. The supported engine is the pixel brush engine. Paths and cubic Bézier paths do not support arbitrary per-point pressure; lines accept endpoint pressure. There is no arbitrary Python/action-execution tool. Ordinary Krita undo and `krita_edit_history` are available for native strokes. The MCP history tool takes one undo/redo step on the explicitly targeted active document, including user edits; reuse its operation ID on retries. See [validation evidence](validation.md) for the exact tested build and preset.

Every mutation requires an `operation_id`, such as `sketch-outline-001`. Reuse the same ID and identical arguments when retrying uncertain work; use a new ID for an intentional second edit. A timed-out or running operation can still complete. Query `krita_get_operation` to reconcile it. Cancellation prevents unstarted work; it cannot forcibly interrupt a native stroke already running. Operation identities are retained within a live bridge session, not across a Krita crash.

## Reference overlays

Use `krita_activate_document` to select an existing view of the target document. `krita_clear_selection` explicitly removes a selection; native painting continues to reject nonempty selections. `krita_set_selection` uses replace/add/subtract/intersect mode with an in-canvas rectangle or a polygon of 3–256 integer points; replace is the default and other modes require an existing selection. Polygon selection uses a hard, unfeathered mask and odd-even filling.

An overlay workflow is: copy the sketch with `krita_copy_layer`, place it above a reference using `krita_move_layer`, align it with `krita_transform_layer`, adjust visibility/opacity with `krita_set_layer_properties`, and inspect the face with `krita_get_region_preview`. Copying accepts an explicit destination document and name; copying and moving accept an optional destination group and sibling to insert above. Moving stays within its document. Copy/transform accept unlocked, nonanimated paint layers without masks or children. Property edits also support groups, transparency masks, and file layers, with paint/group/file-layer compositing and paint-only alpha lock. Moves also support bounded groups and reject cycles or locked/animated descendants. Pixel copying/transforms require the same standard RGBA/U8/sRGB authoring space as painting.

Transform arguments include an explicit image-space `pivot`, `scale_x`/`scale_y`, `rotation_degrees`, and `translate_x`/`translate_y`. The order is scale, clockwise rotation around the pivot, then translation. The entire output must fit inside the canvas. Transforms resample pixels using Qt smooth interpolation and replace the layer's pixels; they are not nondestructive transform masks. Copy the layer first to retain the original. These direct editing operations report `undo: "not_guaranteed"`; only tested native strokes have the stated one-stroke undo behavior.

Region previews accept `x`, `y`, `width`, `height`, and `max_edge` (32–1024), with the entire requested region inside the zero-offset canvas. The returned PNG includes crop origin and scale so its coordinates can be mapped back to the document. Profile/alpha conversion follows Krita's projection API and is not a general archival color guarantee.

For smooth contours, `krita_paint_bezier_path` accepts a starting point and 1–256 cubic segments. Each segment is `[control1, control2, end]`, with each point `[x, y]`. It paints one native path with the same brush settings and active-view restrictions as `krita_paint_path`.

## Linked file layers

Use `krita_create_file_layer(instance_id, operation_id, document_id, root, path, name)` for a linked PNG/JPEG reference. An optional `parent_node_id` places it in a group. `scaling_method="None"` keeps the source size; `"ToImageSize"` fits it to the image with Bicubic filtering. Inputs use `KRITA6_MCP_INPUT_ROOTS` and the same 32 MiB / 16 MP image limits as import.

Use `krita_set_file_layer` with explicit document/node IDs, root, path, and scaling to relink an existing file layer. Inspection returns its native path and scaling settings; `krita_set_layer_properties` supports name, visibility, opacity, blending, and alpha inheritance. Reuse each operation ID on retries. These operations do not guarantee undo grouping.

Keep the linked source available alongside your work. Krita owns future file reloads; bridge bounds are checked when creating or relinking, and do not constrain later external file changes. File-layer copying, moving, deleting, raster transforms, and direct painting are not added by these tools.

## Save and export

File writes require named output roots configured **in Krita's environment**, not merely in the MCP server's environment. Create the directory first, fully exit Krita, then relaunch it:

```bash
mkdir -p /absolute/path/to/artwork
KRITA6_MCP_OUTPUT_ROOTS='{"art":"/absolute/path/to/artwork"}' krita
```

Use `root="art"` and a relative `path`, such as `sketch.kra` or `preview.png`, in file tools. Parent directories must already exist. Existing files are rejected unless `overwrite=true`. Document creation, painting, and inline previews work without output roots.

Opening and importing files require separate named input roots in Krita's environment:

```bash
KRITA6_MCP_INPUT_ROOTS='{"references":"/absolute/path/to/references"}' krita
```

`krita_open_document` opens PNG, JPEG, or KRA files and attaches an active view. `krita_import_image_layer` imports PNG/JPEG as a new top paint layer at explicit integer `x`/`y` coordinates. Both use a relative `path` under an existing named input root, reject path traversal and unsupported files, and bound image dimensions to 8192 pixels per side and 16 megapixels. Imports convert embedded profiles to sRGB; untagged images are assumed sRGB. Import requires a standard RGBA/U8/sRGB destination and keeps the complete image inside the canvas. Input file limits are 32 MiB for layer import and 64 MiB for document opening. These environment settings take effect in a newly launched Krita process; building updated code does not change an existing session.

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

### Configure diffusion conditioning

Use the current instance/document handles and a native layer UUID from document inspection:

```text
krita_configure_diffusion(instance_id, operation_id="settings-1", document_id,
    positive_prompt="A mountain landscape", fixed_seed=true, seed=42,
    inpaint_mode="automatic")
krita_set_diffusion_region(instance_id, operation_id="region-1", document_id,
    node_id=subject_layer_id, positive_prompt="A snowy peak")
krita_set_diffusion_controls(instance_id, operation_id="controls-1", document_id,
    controls=[{node_id: reference_layer_id, mode: "depth", strength: 0.8, start: 0, end: 1}])
```

Pass `region_node_id=subject_layer_id` to configure regional controls. Passing `controls=[]` clears only the selected list. Inspect the diffusion document after configuration and after backend connection to check control support. These persistent settings do not start generation; generation calls still specify their root prompts/strength/seed explicitly. See the [configuration scope and limits](diffusion-integration.md#persistent-configuration).

### General editing

Use `krita_create_group_layer` and `krita_move_layer` to organize paint layers/groups. Add a transparency mask from the current selection with `krita_create_transparency_mask`; update its coverage with `krita_set_transparency_mask`. `krita_set_layer_properties` also supports typed blend modes, alpha inheritance, and paint-layer alpha lock. Deletion returns removed handles; merging returns the new layer handle.

`krita_set_selection` accepts replace/add/subtract/intersect; `krita_modify_selection` adds inversion, grow/shrink and feathering. `krita_transform_canvas` operates on the full image: crop, resize, scale, right-angle rotation, or image flip. Inspect geometry after a transform before using earlier coordinates.

`krita_paint_shape` draws native rectangles/ellipses. `krita_fill_layer` provides solid, linear-gradient, exact connected flood fills and selection-aware erasing within a one-megapixel canvas, respecting selection coverage. These raster fills do not promise native brush behavior or undo transactions.

Use `krita_get_layer_preview`, `krita_sample_color`, and `krita_inspect_brush` for inspection. `krita_edit_history` performs one undo/redo step on the active document, including user edits in that history. Reuse its operation ID on retries, then inspect the result. It cannot selectively undo an arbitrary bridge operation or make direct writes undoable.

## Vector editing and merging

Create a layer with `krita_create_vector_layer`, then call `krita_add_vector_shape`
with geometry such as `{"kind":"rectangle","x":8,"y":8,"width":32,"height":24}`
and `fill="#FF0000"`. Shapes remain editable vectors. `krita_inspect_vector_layer`
returns a `snapshot_id` and `shape_index` values. Supply both to
`krita_edit_vector_shape` or `krita_delete_vector_shape`; reinspect after changes.
For example, `translate_x=16` moves the selected shape 16 image pixels right.

Use `krita_merge_vector_layer_down` on the upper of two adjacent vector layers to
retain editable shapes in the lower layer and remove the upper layer. Layers must
use the supported plain compositing state; mixed raster/vector merges are rejected.
Reuse operation IDs on retries. See [vector scope, merge restrictions and addressing](vector-editing.md).
