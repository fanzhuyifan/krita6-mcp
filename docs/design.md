# Krita 6 MCP design

Decision record · Initial implementation 0.1.0 · 2026-09-06

## Objective and scope

Build a local MCP server for deliberate, observable editing in a running Krita 6 session. An assistant should identify the right document and layer, perform a bounded operation, inspect the canvas, and preserve editable work. Native brush behavior and trustworthy completion matter more than exposing every menu action.

The first release covers document inspection/creation/activation, bounded file opening/import, selections, paint-layer properties/copying/reordering/affine transforms, native paths/cubic Bézier paths/lines, bounded canvas and region previews, `.kra` saving, and PNG export. Target Krita 6 with Python plugin support; start with the installed Linux package. Krita 5 compatibility, remote network service, headless rendering, animation, arbitrary Python execution, general action triggering, and standalone model/image-generation backends are outside the core bridge. Optional generation uses the existing Krita AI Diffusion add-on and its already connected local backend.

The initial workflow is implemented and tested on Linux with Krita 6.0.3. The [validation record](validation.md) distinguishes verified behavior from remaining gates. Background sources are listed in [references and acknowledgments](research.md); future work remains in the [implementation plan](implementation-plan.md).

## Architecture

```mermaid
flowchart LR
    A[MCP client] <-->|stdio| B[External Python MCP server]
    B <-->|Authenticated loopback HTTP| C[Krita plugin HTTP workers]
    C <--> D[Bounded queue and operation ledger]
    D <-->|GUI-thread dispatch| E[PyQt6 executor]
    E <--> F[Krita LibKis API]
    F --> G[Documents, layers, native brushes]
```

| Component | Responsibility | Dependencies |
| --- | --- | --- |
| External server | MCP transport, input/output schemas, connection discovery, operation polling, image results, diagnostics CLI | Normal Python 3.10+, official `mcp` SDK, typed validation |
| Plugin transport | Authenticate and validate requests, enforce limits, enqueue plain data, serve operation status | Python standard library |
| Plugin executor | Resolve live targets, validate host state, call Krita, track completion, capture previews | Krita's own Python runtime, `krita`, PyQt6 |
| Shared contract | Versioned command schemas, fixtures, errors, limits | JSON plus dependency-free plugin validators |

Keep MCP and its dependency graph outside Krita. The plugin must not install packages into Krita or bundle another Qt binding. Use the current official SDK v2, initially `mcp>=2.1.1,<3` with an exact lockfile at implementation time. Use typed `MCPServer` tools and the SDK's stdio implementation; validate older-client interoperability through the SDK. Current [installation documentation](https://py.sdk.modelcontextprotocol.io/get-started/installation/) and [PyPI](https://pypi.org/project/mcp/) establish this baseline.

Choose a small private HTTP/JSON bridge for easy diagnostics and clear request framing. It is an internal application protocol, not an MCP HTTP endpoint. Raw TCP JSON-lines is a reasonable alternative used by existing projects, but does not remove authentication, framing, limits, or retry work. Qt networking and an embedded MCP runtime add event-loop coupling that this design does not need.

## Plugin lifecycle and GUI execution

Ship `krita6_bridge.desktop` and a `krita6_bridge/` Python package. An `Extension` creates one process-wide executor and one bridge; `createActions(window)` adds controls without starting a second listener for every window. Start/stop/status actions show connection state, running work, and actionable startup failures. Manual activation through Krita's plugin manager is the initial installation path.

Import `krita` and PyQt6 explicitly; use scoped Qt6 enums and current method names. Detect missing bindings and unsupported Krita versions at startup. Do not silently fall back to PyQt5. The inspected installed Krita bootstrap rejects the wrong Qt major; its exact location is recorded in the [local evidence](research.md).

All Krita API access, including reads and lifecycle discovery, belongs to an executor constructed on the GUI thread. HTTP workers carry JSON and immutable bytes, never document/node wrappers. A GUI-owned 20 ms timer dispatches queued commands and resumes pending completion checks. Keep strong references to these objects. Process one command at a time, and yield between commands. No blocking queued connections, recursive event pumping, or network calls from the GUI executor. This follows [Qt's threading model](https://doc.qt.io/qt-6/threads-qobject.html).

Do not equate a returned `paintPath()` call with a rendered, undoable result. The native API schedules stroke work. The implementation polls `tryBarrierLock()` and immediately unlocks when acquired; the reference host tests verify settled pixels after this barrier. Never hold an image lock while initiating painting or export. Native completion has no forced timeout: a client stopping its wait does not release the GUI dispatch gate. If a synchronous Krita method blocks, the bridge cannot promise a hard execution deadline or immediate cancellation.

While native work completes, keep later mutations and document reads queued, or return `DOCUMENT_BUSY` for reads that cannot wait. In particular, do not capture a preview before the preceding native work settles. Ledger status requires no Qt calls. A modal dialog, missing active view, closed document, locked layer, or busy image must produce an explicit state/error rather than unsafe dispatch. Re-resolve objects on every deferred GUI continuation because the user can close a tab between ticks.

Stopping enters `draining`: reject new work, cancel queued work, and keep the original executor/ledger alive until running work and its callbacks settle. It cannot forcibly interrupt an executing native call. Do not start a replacement executor during draining. Once stopped, a new session rotates the instance identity and token and invalidates previous handles. The external server can restart independently of Krita.

## Connection and command protocol

Bind only to `127.0.0.1`, using an OS-assigned port. Write one discovery file per bridge instance in a per-user application-state directory. Include `bridge_protocol`, plugin/Krita versions, PID, port, random `instance_id`, and a fresh 256-bit bearer token. Use atomic replacement and owner-only POSIX permissions. Windows is rejected until user-restricted ACL handling is implemented; macOS remains untested. Treat the PID as diagnostic data, not identity. Verify the instance with an authenticated handshake; the adapter ignores stale discovery without deleting another session's files.

The status tool discovers all registered instances. Commands and operation/artifact lookups require an explicit instance ID, even when only one instance is live. Every command includes its expected `instance_id`, so an old port or a restarted plugin cannot silently receive an edit. Never expose the bearer token through tools, stdout, or logs. Disable proxy use for local bridge requests and reject redirects.

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/session` | Authenticated bridge identity, cached host capabilities, queue/ledger health |
| `POST /v1/operations` | Validate, reserve an operation, enqueue, and promptly return its handle/state |
| `GET /v1/operations/{operation_id}` | Return current state and any bounded result; no GUI dispatch |
| `POST /v1/operations/{operation_id}/cancel` | Cancel unstarted work atomically or report that native work is already running |
| `GET /v1/artifacts/{artifact_id}` | Read a bounded, authenticated preview artifact tied to this instance |

Check bearer authorization, exact Host, JSON content type, message size, field types, and allowed command names before admission. Reject browser Origin headers and provide no CORS support. Bound HTTP worker concurrency and socket read times; a bounded command queue alone does not bound accepted threads. Workers acknowledge promptly rather than wait on GUI work. GUI reads such as document inspection also use the queue, while a full GUI queue does not prevent ledger status/cancellation. HTTP worker exhaustion can briefly delay those controls; enforce short connection/read deadlines and test bounded recovery instead of promising uninterrupted availability.

Example bridge request; identifiers below are illustrative:

```json
{
  "bridge_protocol": 1,
  "instance_id": "instance-a",
  "operation_id": "sketch-outline-001",
  "command": "paint_path",
  "target": {"document_id": "doc-a", "node_id": "node-uuid"},
  "queue_timeout_ms": 10000,
  "params": {
    "preset_id": "preset-a",
    "size_px": 12.0,
    "opacity": 1.0,
    "color": "#2855CC",
    "points": [[40.0, 50.0], [120.0, 90.0], [190.0, 60.0]]
  }
}
```

Protocol major mismatches fail before executing anything. Capabilities describe optional operations and limitations; merely possessing a Krita version string is insufficient evidence that a method is usable.

## Retries, timeouts, and cancellation

The authoritative ledger lives in the plugin, so an adapter restart does not lose knowledge of an in-flight edit. Mutation tools require a caller-supplied `operation_id` reused for retries. Generating a new UUID for each MCP call would protect transport retries but not a model repeating the tool call.

Admission atomically reserves queue capacity and stores `(instance_id, operation_id)` with a canonical hash of command, target, and semantic parameters. Queue-full rejection leaves the ID unadmitted and safe to resubmit. The same admitted ID and payload returns the original state/result; a changed payload returns `OPERATION_ID_CONFLICT`. Timeout/poll settings are not semantic payload. Identical strokes with different IDs are intentional separate operations.

```text
queued -> running -> succeeded | failed
queued -> cancelled | expired
running -> cancel_requested -> succeeded | failed
```

Here `cancel_requested` records intent, not a stopped stroke. Cancellation and transition to `running` use the same lock. Expired/cancelled queue entries are skipped before any Krita call. A deadline after execution starts means the caller stopped waiting; it does not mean the image is unchanged. Return the operation handle and let the client inspect it. Never automatically submit a fresh mutation after an uncertain result.

Results distinguish lifecycle state from mutation effect with `effect: none | applied | partial | unknown`. For example, rejected validation has `none`; a failure after a layer was created can have `partial`; disconnected native execution may have `unknown`. Include affected target/artifact handles whenever known. Never infer that `failed` means no change occurred.

Completed result bodies may expire, but retain compact ID/hash/outcome tombstones for the whole plugin instance. A duplicate whose body expired returns `RESULT_EXPIRED` and its known outcome, never re-executes. Bound the ledger (initially 10,000 admitted mutations); when full, reject new mutations and keep inspection/status available. Read-operation results have a separate bounded TTL cache and do not consume mutation tombstones. Do not evict tombstones and silently weaken deduplication. The UI can explain that a new bridge session is needed after active work settles.

This provides at-most-once dispatch within a live instance, not exactly-once execution across crashes. If Krita crashes after changing pixels and before recording completion, return `OUTCOME_UNKNOWN` on reconciliation. A restarted plugin rejects the old instance ID. Inspect recovered artwork before intentionally issuing a new edit.

## Identity and state

- `instance_id`: random on plugin start.
- `document_id`: opaque plugin-issued handle, stable only while that document is open in that instance. Match fresh enumerated wrappers using Krita's `Document.__eq__`, not Python `is`/`id`; discard vanished entries. Closing and reopening creates a new handle. Never use names, filenames, tab indices, or wrapper addresses as public identity.
- `node_id`: Krita `Node.uniqueId()`, resolved through the specified document's `nodeByUniqueID()`. A UUID alone does not establish document ownership. Reject a deleted/recreated node or a wrong-document target.
- `preset_id`: handle from the current instance's preset catalog. Names are labels; detect missing or changed resources before painting.

Direct `createDocument` and `openDocument` return originating wrappers that can own the native document until a view takes ownership. The GUI host registers a handle immediately and retains those original wrappers in a separate owner registry until the document closes, including after view initialization fails. Ordinary target lookup still uses fresh enumerated wrappers; this narrow retention does not apply to notifier callback objects.

Never retain document/view wrappers supplied by Notifier callbacks: some are deleted immediately after signal emission. Use signals to request reconciliation with fresh GUI-thread enumeration. The [Document](https://raw.githubusercontent.com/KDE/krita/v6.0.3/libs/libkis/Document.cpp) and [Notifier](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/Notifier.cpp) implementations establish these ownership details.

Document inspection reports bounds/origin, dimensions, profile/model/depth, modified state, active view, selection summary, current frame, and layer hierarchy. Validate target type, ancestor locks/visibility, dimensions, and color space immediately before editing. V1 painting supports nonanimated paint layers and rejects an active nonempty selection until selection semantics have been validated and added explicitly.

A `bridge_sequence` counts dispatched mutation commands reaching a terminal result, including failures. The ledger owns this counter. It is not a document revision and cannot detect every user edit. Do not claim optimistic concurrency or atomic multi-command transactions without a reliable host revision signal. The first implementation does not expose a general batch tool.

## Native painting and visual feedback

Use Krita's public `Node.paintPath`, `paintLine`, `paintRectangle`, and `paintEllipse` methods. After selecting a preset, set size, opacity, managed foreground color, and explicit baseline state: eraser off, flow 1, normal blending, rotation 0, global alpha lock off, and pressure enabled. Verify those controls for the supported engine; reject unsupported state rather than inherit an eraser or lock that changes the operation. The [6.0.3 View implementation](https://raw.githubusercontent.com/KDE/krita/v6.0.3/libs/libkis/View.cpp) and local bindings expose these controls. Require the requested document to be the active view for painting in v1. If it is not, return `TARGET_NOT_ACTIVE` instead of borrowing another document's brush context.

Save the originating view settings. Prefer immediate restoration after the native helper captures its resource snapshot, if the spike verifies this. If restoration must be deferred, only restore the same still-live originating view, and only settings still matching bridge-applied values; preserve newer user choices and report skipped restoration. Preset changes can affect multiple settings, so restore a coupled group only when its full observed state still matches. Do not restore into whichever view happens to be active later.

| Primitive | Proposed behavior | Limit |
| --- | --- | --- |
| `paint_path` | Convert bounded image-space points to a `QPainterPath`, use brush stroke style and no fill | Native path API has no per-point pressure/timing input; do not advertise pressure samples |
| `paint_line` | Native line with endpoint pressures in `[0,1]` | Local 6.0.3 stub requires `QPoint`; accept integer image coordinates initially |
| Native shapes | Typed rectangles/ellipses with brush outline and optional foreground fill | Validated with the reference pixel-brush preset; additional shapes/styles require further evidence |

Do not approximate a continuous pressure stroke by calling `paintLine` for every segment without exposing the difference: each call creates a separate native stroke and may reset brush dynamics and add a separate undo entry. Continuous variable-pressure strokes are a later capability that may require upstream API work. The [Node API](https://api.kde.org/legacy/krita/html/classNode.html) and [6.0.3 native implementation](https://raw.githubusercontent.com/KDE/krita/v6.0.3/libs/libkis/Node.cpp) are the source baseline; local stubs and live results take precedence for the tested build.

Coordinates refer to native image pixels, independent of zoom, pan, rotation, or display scaling. V1 writes require a zero-offset canvas whose top-left is `(0,0)`; inspection/preview reports actual bounds and offsets for all documents. Reject nonzero origins for painting until coordinate translation is tested. Require in-canvas input points and bound size/count explicitly. Color strings are sRGB; convert using managed colors for painting. Start the validated authoring path with RGBA/U8/sRGB documents. Inspect other document types, but return `UNSUPPORTED_COLORSPACE` for unsupported writes rather than reinterpret raw bytes.

The normal feedback loop uses a bounded in-memory document thumbnail/projection, encoded as PNG and returned as MCP image content alongside source dimensions, preview dimensions, scale/offset, alpha treatment, and profile information. Test color conversion; do not claim an arbitrary projection is sRGB without checking. Default maximum edge is 1024 pixels; bounded region previews also return their image-space origin and scale. A preview is a view of settled canvas state, not a disk save or exact archival export. It can include user edits made since the preceding operation.

Prefer encoded image APIs over raw pixel buffers. Raw Krita pixels vary by color model/depth and may use BGRA ordering. Pixel import and affine transforms isolate and test their RGBA/U8/sRGB, little-endian conversion backend; `setPixelData()` is not a substitute for native brushes or evidence of undo support.

## Undo, saving, and external files

Native painting's source path creates undo commands, but verify one-call/one-undo behavior on the target build. There is no generic public `Document.beginUndoMacro()` in the inspected API. Do not promise that layer-property changes, direct pixels, or a whole MCP request are one reversible transaction.

`krita_edit_history` performs exactly one enabled undo/redo action after verifying that the explicitly targeted document is active. It returns the action label and describes its scope as the document's actual history, including intervening user edits; it cannot undo a selected bridge operation by ID. Retries reuse the operation ID to avoid taking another history step. Return known undo behavior in operation metadata. Report partial mutation honestly if an error occurs after work begins; do not attempt a speculative automatic rollback.

`save_document` persists editable `.kra` work and reports filename association/modified state after `saveAs`/`save`. `export_png` writes a separate rendering with `exportImage` and explicit format settings; restore prior batch mode in `finally`. Verify success and a nonempty output artifact. Preview capture must not change document filenames or dirty state. Test native APIs' handling of existing destinations before asserting overwrite safety.

File tools use configured input/output roots, canonical containment checks, bounded allowed formats, and explicit overwrite intent. Paths are relative to a named root. Resolve symlinks; reject traversal, unsupported locations, and unexpected special files. No general filesystem browser or remote URL fetch is exposed. The local bridge is not a sandbox against malicious programs already running as the same OS user; do not claim race-proof filesystem isolation from string validation alone.

## General editing

The [general editing decision record](general-editing.md) defines typed group/mask, history, selection, canvas, shape, fill/erase and inspection operations and their bounded host behavior. Validation evidence lives in the [validation record](validation.md#general-editing).

## Initial MCP surface

Use individually typed tools rather than an unbounded `execute(command, args)` tool. The catalog contains 37 core tools and eleven optional AI Diffusion tools, for 48 total. The configuration tools persist bounded Generate settings and configure conditioning against explicit layer identities; [integration decisions](diffusion-integration.md#persistent-configuration) define their source and mutation boundary. Every state-changing tool includes `instance_id` and `operation_id`; document/layer writes also require explicit target handles.

| Tool | Contract |
| --- | --- |
| `krita_status` | Reachability, exact versions, capabilities, instance choices, queue state; useful when plugin is absent |
| `krita_list_documents` | Enumerate live document handles and active-view status |
| `krita_inspect_document` | Metadata, layers, editability, selection/frame context |
| `krita_get_preview` | Bounded inline PNG and coordinate/color metadata |
| `krita_get_region_preview` | In-canvas crop PNG with origin, scale, and color metadata |
| `krita_activate_document` | Activate an existing view of an explicit document |
| `krita_clear_selection` | Explicitly remove the active selection |
| `krita_set_selection` | Replace/add/subtract/intersect with a bounded rectangle or polygon mask |
| `krita_set_layer_properties` | Paint/group/mask name, visibility, opacity; paint/group compositing; paint-layer alpha lock |
| `krita_copy_layer` | Duplicate a supported paint layer into an explicit destination document |
| `krita_move_layer` | Reorder a paint layer or bounded group, rejecting locked/animated subtrees and cycles |
| `krita_transform_layer` | Bounded pixel affine transform about an explicit pivot |
| `krita_open_document` | Open bounded PNG/JPEG/KRA from a configured input root |
| `krita_import_image_layer` | Import bounded PNG/JPEG into a new top paint layer |
| `krita_create_document` | Bounded RGBA/U8/sRGB document plus an attached active view |
| `krita_create_paint_layer` | Explicit parent/document; return node UUID |
| `krita_list_brush_presets` | Search and paginate available preset handles |
| `krita_paint_path` | Explicit target, preset, size, opacity, color, bounded path |
| `krita_paint_line` | Explicit target and brush settings; two endpoint pressures |
| `krita_paint_bezier_path` | One native path from bounded cubic segments and explicit brush settings |
| `krita_save_document` | Layered `.kra` save to a configured output root |
| `krita_export_png` | Separate PNG export with explicit overwrite intent |
| `krita_get_operation` | Reconcile a running, timed-out, or duplicate operation |
| `krita_cancel_operation` | Best-effort cancellation with a precise result |
| `krita_diffusion_status` | Observe an already loaded AI Diffusion plugin and its connection state |
| `krita_inspect_diffusion_document` | Read an existing document model's bounded settings and progress |
| `krita_list_diffusion_jobs` | Paginated job state, nullable plugin IDs, and result counts |
| `krita_list_diffusion_styles` | Available style handles and labels |
| `krita_generate_diffusion` | Submit one generation through the existing add-on and local backend |
| `krita_get_diffusion_generation` | Poll owned generation state and stable result handles |
| `krita_get_diffusion_result` | Inspect a generated image without selecting its canvas preview |
| `krita_apply_diffusion_result` | Apply an owned result as a new top paint layer |
| `krita_create_group_layer` | Create a named group in an explicit parent |
| `krita_create_transparency_mask` | Attach a mask initialized from selection or constant canvas opacity |
| `krita_set_transparency_mask` | Replace mask coverage from selection or constant canvas opacity |
| `krita_delete_layer` | Remove a supported node and bounded subtree; retain a top-level layer |
| `krita_merge_layer_down` | Merge adjacent visible simple paint layers and return the reconciled handle |
| `krita_edit_history` | One enabled undo/redo step on the explicit active document, including user history |
| `krita_modify_selection` | Invert/grow/shrink/feather existing coverage and clip it to the canvas |
| `krita_transform_canvas` | Bounded crop/resize/scale/right-angle rotation/flip of the whole image |
| `krita_paint_shape` | Native rectangle/ellipse with explicit brush outline and optional foreground fill |
| `krita_fill_layer` | Selection-aware solid/linear-gradient/four-connected flood fill or raster erase, at most 1 MP |
| `krita_get_layer_preview` | Inline PNG of a node projection in canvas bounds |
| `krita_sample_color` | One settled canvas or node projection color with alpha |
| `krita_inspect_brush` | Read active-document brush settings without changing them |
| `krita_configure_diffusion` | Persist bounded Generate settings on an existing document model |
| `krita_set_diffusion_controls` | Replace root or regional control/reference conditioning lists |
| `krita_set_diffusion_region` | Create/update/remove a bounded single-linked prompt region |

Reference editing uses bounded typed commands, with direct layer/selection/pixel edits explicitly reporting no guaranteed undo grouping. Native Bézier paths reuse native painting and completion guards. Opening/importing files uses separate configured input roots. The supported behavior and exact live checks are recorded in [validation](validation.md). A static capability resource can complement these tools, but clients should not require resource subscriptions to perform the basic workflow.

Tool inputs have generated JSON schemas and shared strict bridge validation. Results carry structured JSON, concise text, and optional image blocks; published per-tool output schemas remain future work. Expected tool failures use MCP tool errors with stable domain codes. A returned pending operation is a known state, not falsely reported success. Tool annotations describe side effects but do not enforce permissions.

## Limits and observability

Enforced initial limits: 4 HTTP workers with absolute 2-second connection deadlines, 32 queued GUI commands, 1 MiB JSON requests, 2,048 path points, 16 megapixels for document creation, 32 open documents for creation, 4,096 layers/masks for layer creation, 1024-pixel preview edge, and 2 MiB encoded previews. Preset reads paginate; document/layer reads have bounds. Result bodies share a 16 MiB budget while mutation identities survive for the session. PNG artifacts have a separate 32-entry, 16 MiB, 60-second cache; exhausted preview storage does not evict mutation identities.

Use monotonic time for queue deadlines. Record operation ID, command, state transition, queue delay, execution duration, version, and a sanitized error code. Keep logs bounded and omit tokens, image payloads, and document text. Adapter logs go to stderr; stdout is exclusively MCP traffic.

Provide `krita6-mcp doctor` for discovery and version diagnostics and an explicit scratch-document smoke command for actual painting validation. Missing Qt bindings, missing Python plugin support, incompatible protocol, and Krita not running must be distinguishable.

## Optional Krita AI Diffusion integration

The optional [Krita AI Diffusion](https://github.com/Acly/krita-ai-diffusion) adapter observes already loaded Qt6 models and submits bounded jobs through the add-on’s own canvas preparation. Generation inherits selections, regional prompts, and control/reference layers. Submission, asynchronous rendering, image inspection, and native application have separate identities and completion states. Bridge-owned jobs suppress automatic preview/application regardless of the add-on’s global setting; explicit application creates a new top paint layer. The [integration record](diffusion-integration.md) defines the private source gate, local-backend restriction, result ownership, and limits. No backend is installed or connected by an MCP tool.

Generation, cancellation, and application remain a separate increment. Bind each job to an explicit document and bridge operation, preserve identities across retries, and distinguish local admission from backend dispatch and document application. Upstream cancellation can affect unrelated work, and completion follows ambient automatic-application settings; these require explicit policy and live backend evidence. An explicit backend choice is required before any cloud submission. The core bridge remains usable without AI Diffusion installed. Reference tests establish observation of the real plugin and synthetic job records; no image generation has been tested.
