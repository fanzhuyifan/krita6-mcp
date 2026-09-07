# Changelog

Changes are recorded here before release. Version 0.1.0 is currently unreleased.

## Unreleased — 0.1.0

### Added

- Linked PNG/JPEG file-layer creation, source/scaling updates, inspection metadata, and common layer properties, with bounded input-root validation.

- External stdio MCP server and separate PyQt6 plugin for Krita 6.
- Thirty-nine core tools for discovery, document/layer inspection and creation, native paths and pressure lines, inline PNG previews, layered save, PNG export, operation reconciliation/cancellation, and reference editing.
- Reference editing: document activation, explicit selection clearing/replacement, cropped previews, paint-layer properties/copy/reordering, bounded affine raster transforms, input-root file opening and image import, and native cubic Bézier strokes.
- General editing: groups/transparency masks, compositing, deletion/merge, one-step undo/redo, selection combination/refinement, canvas transforms, native shapes, selection-aware raster fills/erasing, and layer/color/brush inspection.
- Authenticated loopback transport, private discovery, bounded queues/results, and mutation identities retained for the plugin session.
- Eleven optional tools for Krita AI Diffusion: inspect settings/canvas conditioning/jobs, list styles, submit one generation, poll it, inspect images, explicitly apply a result as a new layer, and configure persistent settings, regional prompts, and control/reference layers.
- Isolated native and MCP probes with retained Linux/Krita 6.0.3 evidence.
- MIT licensing, contribution/security guidance, reproducible plugin packaging, and a Python CI matrix.

### Fixed during initial development

- Reference-editing review fixes distinguish known partial mutations, preserve document and layer recovery handles on failed creation/completion, and accept nonempty odd-even self-intersecting selections.
- Preset handles remain usable after bridge-owned brush setting changes.
- Native completion remains tracked after painting or layer-creation errors.
- Stopped executors release Qt ownership and cached state.
- Expired result bodies preserve recorded mutation outcomes and never trigger replay.
- Interrupted or malformed HTTP error responses retain structured uncertainty and reconciliation information.
- Plugin ZIPs include the explicit directory entry required by Krita's importer, plus the license and manual.

### Changed

- Shared preview-image retrieval between immediate and polled MCP responses, preserving metadata and retrieval errors.
- Removed unused automatic instance selection and redundant command validation in the HTTP route; the ledger still validates before admission.
- Moved output-root configuration and path checks into a standard-library module with direct tests, and removed a redundant host dispatch branch.

### Known limits

- Alpha status; live-host evidence covers Linux/Krita 6.0.3 and one pixel-brush preset on small fixtures.
- Painting is restricted to the documented color space, origin, selection, layer, and brush-engine conditions.
- Reference pixel editing is limited to nonanimated RGBA/U8/sRGB paint layers without children, on little-endian hosts. Layer edits and selection edits do not promise an undo transaction.
- Windows is unsupported; macOS is untested. Large-document and long-session stress coverage remains limited.
- AI Diffusion generation requires the pinned Qt6 source and an already connected local backend. Backend cancellation, configuring diffusion regions/controls, cloud generation, and live/custom/edit-model workflows are not exposed.
