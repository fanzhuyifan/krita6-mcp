# General editing tools

Decision record · 2026-09-06

Extend the bridge with typed layer/group/mask creation and compositing, deletion and adjacent paint-layer merging, one native undo/redo step, selection refinement and combination, bounded canvas transforms, native rectangle/ellipse painting and explicit erasing, raster fills, and read-only layer/color/brush inspection.

All host access remains on the GUI thread. Mutations use the operation ledger and native completion barrier. History is the active document's actual history, including user edits; it is not an undo-by-operation API. Arbitrary action IDs remain prohibited: only fixed undo/redo and image flip actions are used internally.

Pixel inspection and raster fill require the established little-endian RGBA/U8/standard-sRGB path. Fills use source-over compositing and erasing uses destination-out, limited to one megapixel and honoring the current selection; flood uses exact BGRA equality and four-connected neighbors. Native brushes are used for shapes and strokes, with explicit settings capture/restoration. Direct raster fills and mask/selection writes do not promise undo transactions.

Layer deletion rejects the last top-level layer, locked or animated subtrees. Merge targets two adjacent visible nonanimated paint layers without children or alpha inheritance. Group moves reject cycles. Transparency masks can be initialized or replaced from the canvas selection or constant opacity. Only typed, enumerated blend modes are exposed.

Canvas operations are limited to 8192 pixels per side and 16 MP, reject locked/animated subtrees, and preserve explicit document targeting; flips additionally require the active document. Rotation is limited to right angles. Crop must stay inside the current canvas; resize supplies a new canvas rectangle in existing image coordinates. Selection refinement rejects existing selection extents outside the supported canvas and clips generated coverage to it.

Validation is recorded separately in the validation guide after isolated live-host checks. Native behavior is based on the installed Krita 6.0.3 bindings and [Krita Node implementation](https://raw.githubusercontent.com/KDE/krita/v6.0.3/libs/libkis/Node.cpp), not inferred from mocks.
