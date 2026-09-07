# Vector editing

Six tools expose native vector authoring and editing. Shapes stay editable in Krita
and in saved `.kra` files; these operations do not paint raster approximations.

| Tool | Behavior |
| --- | --- |
| `krita_create_vector_layer` | Create a vector layer at the top of the document root or an explicit group. |
| `krita_add_vector_shape` | Add a rectangle, ellipse, polygon or cubic Bézier path, with solid sRGB fill/stroke or `none`. |
| `krita_inspect_vector_layer` | Read top-level shape names, types, visibility, protection, stacking and image-pixel bounds, plus a layer-state snapshot. |
| `krita_edit_vector_shape` | Rename, show/hide, change stacking, or transform a top-level path shape. |
| `krita_delete_vector_shape` | Remove one top-level path shape from an unchanged snapshot. |
| `krita_merge_vector_layer_down` | Copy editable shapes into the immediately lower sibling vector layer, retaining stacking, then remove the source layer. |

`krita_set_layer_properties` also accepts vector-layer names, visibility, opacity,
blend modes and alpha inheritance. General raster copy/transform/merge tools still
have their existing type restrictions; use the dedicated vector merge tool.

## Authoring and addressing

Geometry uses image pixels. Rectangles and ellipses take `kind`, `x`, `y`, `width`,
`height`; polygons take 3–256 `points`; Bézier paths take `start`, 1–256 cubic
`segments` (each `[control1, control2, endpoint]`), and optional `closed`.
The bridge generates SVG from validated typed data. It does not accept arbitrary
SVG, external resources, CSS, or text markup from callers.

Inspect the layer before editing an existing shape. Pass the returned `snapshot_id`
and the selected `shape_index` to edit/delete. The bridge checks the current ordered
layer state immediately before dispatch. If it changed, `STALE_VECTOR_SNAPSHOT`
requires reinspection. Krita's Shape API has no persistent UUID: these are explicit
state-and-index addresses, **not durable shape identities**. An identical restored
or recreated state is intentionally indistinguishable. Reopening a document gives
it a new document handle and invalidates its old snapshots. No Shape wrappers are
retained between commands.

Reuse the same `operation_id` and payload on retries, even if the original edit
changed the snapshot. Admission returns the recorded result without checking or
executing the edit again. An uncertain timeout never justifies a new operation ID.

Transforms scale, then rotate clockwise about the image origin, then translate,
composed after the existing shape transform. Translation uses pixels; the bridge
converts to Krita's native points (1/72 inch) using document resolution. Reinspect
after editing for updated bounds and addresses. Geometry/control-point replacement,
fill/stroke restyling of existing shapes, text, grouping/ungrouping, and SVG file
import/export are outside this increment.

## Merge semantics

The target is the upper vector layer. The result's `node_id` identifies the surviving
lower layer. Source shapes are copied in native stacking order above all existing
destination shapes. The source is removed only after the complete copy is confirmed.
Both layers must be visible, unlocked, full-opacity, normal blend, without masks,
alpha inheritance, alpha lock, or layer styles. Vector antialiasing must match;
pass-through ancestor groups are rejected. Shapes must be visible, unprotected,
top-level paths; nested groups, text, images and unsupported shape types are rejected.

Merging is not an atomic transaction and has no promised single-step undo. A partial
failure can leave copies in the destination with the source retained. Inspect the
reported layer handles and reconcile the original operation before proceeding.
The destination's name and other layer properties survive; the source layer's
identity is removed. Shape names and vector geometry are preserved.

## Limits and validation

Mutations require the target document's active view, a ready zero-origin canvas,
unlocked ancestry and RGBA/U8/standard-sRGB document color. Shape mutations require
a vector layer without masks. Supported canvases are at most 8192 pixels per side
and 16 megapixels; mutation DPI must be equal horizontally/vertically and 1–1200.
Creation geometry and control points must lie inside the canvas; stroke extents can
extend outside it. Transform bounds are limited to 16384 pixels in absolute position
and size. Each layer is limited to 256 top-level shapes and 256 KiB of serialized SVG;
adds reserve 64 KiB and merges reserve 8 KiB of additional serialization headroom.

Direct property/transform edits and merges have `undo: not_guaranteed`. Native
creation/deletion use Krita's shape helpers, but the bridge does not promise that a
whole request is one undo transaction. Native helpers can synchronously block;
after dispatch the executor retains its gate through projection completion, including
on failures. The test runner always uses isolated profiles and scratch documents.

Run on the documented clean Linux host environment:

```bash
env -u PYTHONPATH -u LD_LIBRARY_PATH -u QT_PLUGIN_PATH \
  .venv/bin/python tools/probe_vector_editing.py --krita /usr/bin/krita
```

The retained validation record is linked from [validation](validation.md#vector-editing).
Compatibility claims apply only to the recorded Linux/Krita build; Windows remains
unsupported and macOS untested.

API references: [VectorLayer](https://api.kde.org/legacy/krita/html/classVectorLayer.html),
[Shape](https://api.kde.org/legacy/krita/html/classShape.html). Local Krita 6.0.3 bindings
and live results take precedence over the older generated API documentation.
