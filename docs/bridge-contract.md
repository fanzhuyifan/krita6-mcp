# Implementation contract v1

This records the current implementation. All private-bridge JSON uses snake_case. Package `krita6_bridge` can be imported without Qt outside Krita; its pure protocol, ledger, discovery and transport modules are also packaged with the external server. The host and executor require Krita 6 and PyQt6. Capability labels distinguish implemented operations from completed host validation.

## Requests and results

Request: `{bridge_protocol: 1, instance_id: str, operation_id: str, command: str, target: object, params: object, queue_timeout_ms: int}`. The first four fields are required. Target and params default to `{}`; queue timeout defaults to 10000 ms, with range 1..120000. IDs match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. Unknown fields, booleans as numbers, non-finite numbers, duplicate JSON keys and malformed commands are rejected. Missing command-specific fields remain errors even when their enclosing object has a default.

Snapshot: `{instance_id, operation_id, command, state, effect, result, error}`. State is `queued`, `running`, `cancel_requested`, `succeeded`, `failed`, `cancelled` or `expired`. Effect is `none`, `applied`, `partial` or `unknown`. Result is a JSON object or null. Error is `{code, message}` or null. Domain exceptions use `BridgeError(code, message, effect="none")` from `krita6_bridge.protocol`.

Queued mutations initially have effect `none`; dispatch changes their effect to `unknown` until completion records a known effect. All reads have effect `none`. A failed mutation can have a partial or unknown effect. A completed operation whose result body expired retains its original state/effect but returns `result: null` and error `RESULT_EXPIRED`. The MCP adapter flags explicit errors as tool errors without replacing the known outcome or replaying the operation.

## Command catalog

All fields shown without a default are required. Text fields reject NUL. Target identifiers and preset/parent handles use the ID rule above.

| command | target | params |
| --- | --- | --- |
| list_documents | empty | empty |
| inspect_document | document_id | empty |
| diffusion_status | empty | empty |
| inspect_diffusion_document | document_id | empty |
| list_diffusion_jobs | document_id | offset: int=0 (0..2147483647), limit: int=50 (1..100) |
| get_preview | document_id | max_edge: int=1024 (32..1024) |
| list_brush_presets | empty | query: str="" (0..256 characters), offset: int=0 (0..2147483647), limit: int=50 (1..100) |
| create_document | empty | width,height: int (1..8192, product <=16777216), name: str (1..128 characters) |
| create_paint_layer | document_id | name: str (1..128 characters), parent_node_id: optional identifier |
| paint_path | document_id,node_id | preset_id, size_px: finite number (0.1..1000), opacity: finite number (0..1), color: #RRGGBB, points: list of 2..2048 [finite x,y] pairs |
| paint_line | document_id,node_id | same brush settings, start/end: [int x,y] pairs (signed 32-bit components), pressure_start/pressure_end: finite number=1 (0..1) |
| save_document | document_id | root: identifier, path: str (1..4096 characters), overwrite: bool=false |
| export_png | document_id | root: identifier, path: str (1..4096 characters), overwrite: bool=false |

There are exactly six mutations: `create_document`, `create_paint_layer`, `paint_path`, `paint_line`, `save_document` and `export_png`. The other seven commands are reads. Colors normalize to uppercase and numeric brush/path parameters normalize to floats before hashing. The host validates coordinates against live dimensions and file paths against configured roots immediately before use.

## Operation ledger

`OperationLedger(instance_id, queue_limit=32, mutation_limit=10000, clock=time.monotonic, *, read_limit=256, result_ttl=300, read_ttl=60, result_bytes_limit=16777216)` is thread-safe and independent of Qt.

The HTTP transport authenticates and parses the bounded request body; `admit()` owns command validation and normalization before reserving any state. The transport does not repeat that validation. The external MCP adapter also validates before sending, but the ledger independently checks every incoming request.

| Method | Behavior |
| --- | --- |
| `admit(request) -> snapshot` | Validate and reserve an ID and queue slot atomically. A matching admitted ID returns existing work; different normalized command/target/params return `OPERATION_ID_CONFLICT`. Queue timeout is excluded from the semantic hash. Rejected admission does not reserve an ID. |
| `take_next() -> request or None` | Skip expired/cancelled entries and mark one request running. No next request dispatches while any request is running or cancel_requested. |
| `finish(operation_id, result=None, error=None, effect="applied") -> snapshot` | Finish a dispatched operation as succeeded or failed. Error is a plain `{code, message}` object. Results are JSON-round-tripped so no host wrappers cross threads. |
| `get(operation_id) -> snapshot` | Return current state after cache/deadline maintenance; no GUI dispatch. |
| `cancel(operation_id) -> snapshot` | Cancel queued work under the dispatch lock, or change running to cancel_requested. Repeated cancellation returns the current state. |
| `drain()` | Reject new admissions and cancel queued work; existing identities remain queryable. |
| `is_idle() -> bool` | True when no nonterminal entries remain. |
| `status() -> dict` | Return draining, queued, running (including cancel_requested), queue_limit, mutation_limit, mutations, read_entries, result_bytes, result_bytes_limit and bridge_sequence. |

Each serialized `{result, error}` body is limited to 1 MiB. Read and mutation result bodies share a 16 MiB byte budget; oldest completed bodies are compacted when that budget is exceeded. Mutation bodies also expire after 300 seconds. Their ID/hash/state/effect tombstones remain for the entire instance, including cancelled and expired mutations. The 10000-entry mutation limit rejects further mutations without evicting these identities. Reads use a separate 256-entry cache, with completed entries expiring after 60 seconds or being evicted to admit another read. Duplicate read IDs are compared only while their entries remain cached.

`bridge_sequence` counts calls that finish dispatched mutation operations, including failures and mutations ultimately reporting effect `none`. Admission, cancellation/expiry before dispatch, reads and duplicate retrievals do not increment it. It is neither a count of successful edits nor a document revision; it does not track user edits.

Queue deadlines govern unstarted work only. There is no native execution timeout and no forced interruption of a running Krita call. The adapter's polling deadline stops waiting and returns the operation handle; it never submits a fresh mutation automatically. At-most-once dispatch applies within the live instance, with no persistence across a Krita crash.

## Transport and discovery

`BridgeServer(ledger, session_info, artifacts, state_dir=None, *, max_workers=4, read_timeout=2.0, max_request_bytes=1048576)` exposes `start() -> discovery dict`, `session() -> dict`, `stop()` and `discovery_path`. Start binds an OS-assigned port on `127.0.0.1`. Session output combines cached host metadata with `bridge_protocol=1`, `plugin_version="0.1.0"`, instance_id and live ledger status. Stop drains the ledger, shuts down sockets/workers and removes its own discovery record. Restart requires a new server, ledger and instance identity.

| Endpoint | Response |
| --- | --- |
| `GET /v1/session` | Authenticated session metadata, HTTP 200 |
| `POST /v1/operations` | Admission or duplicate snapshot, HTTP 202 |
| `GET /v1/operations/{operation_id}` | Current snapshot, HTTP 200 |
| `POST /v1/operations/{operation_id}/cancel` | Body must be exactly `{instance_id}`; current snapshot, HTTP 200 |
| `GET /v1/artifacts/{artifact_id}` | Encoded PNG bytes with image/png content type |

Protocol failures return `{error:{code,message,effect}}` with an appropriate HTTP error status. Requests require `Authorization: Bearer TOKEN` and exact Host `127.0.0.1:PORT`; Origin headers, transfer encoding and ambiguous duplicate headers are rejected. POST requires application/json and bounded decimal Content-Length. GET cannot carry a body. Connections close after one response. Four request workers, a 2-second absolute connection deadline, an 8 KiB validated header limit and a 1 MiB body limit bound the transport. Saturated workers immediately close new connections. Status/cancellation avoid the GUI queue but share this worker bound.

`default_state_dir() -> Path` honors `KRITA6_MCP_STATE_DIR`; otherwise it selects Linux `$XDG_STATE_HOME/krita6-mcp` or `~/.local/state/krita6-mcp`, macOS `~/Library/Application Support/krita6-mcp`, or Windows LOCALAPPDATA/krita6-mcp. **Discovery writing currently supports POSIX only.** Windows path selection does not imply Windows bridge support: startup returns `UNSUPPORTED_PLATFORM` until private Windows ACL handling is implemented.

`write_discovery(state_dir, data)` requires a directory owned by the current user with no group/other permissions, creates it with mode 0700 if absent, writes a mode-0600 temporary file and atomically replaces `instance-<instance_id>.json`. Discovery contains bridge_protocol, plugin_version, krita_version, pid, port, instance_id and a fresh 256-bit token. `remove_discovery(path, expected)` removes only a record matching the original instance/token. Tokens never appear in session output. The adapter disables proxy use and redirects and verifies authenticated instance identity before issuing commands.

`ArtifactStore(*, max_count=32, max_bytes=16777216, max_artifact_bytes=2097152, ttl=60, clock=time.monotonic)` accepts nonempty PNG bytes through `put(data, mime_type="image/png")`, returning `{artifact_id,mime_type,size_bytes}`. `get(artifact_id)` returns `(bytes,mime_type)`. Its count/byte/60-second expiry bounds are separate from the ledger. A full artifact store rejects new previews; it does not evict mutation identities.

## Host and executor

`KritaHost(artifacts, output_roots)` provides `session_info() -> dict` and `execute(request) -> dict or Pending`. Every method touching Krita verifies the GUI thread. Document IDs are instance-local handles reconciled against fresh document enumeration using equality. Node UUIDs omit braces and are resolved in the requested document. Presets are instance-local handles backed by a resource signature; changed resources require re-listing. The catalog lists every matching preset with `engine` and `supported_for_painting`, but painting currently accepts only the `paintbrush` engine.

Document creation uses RGBA/U8 with `sRGB-elle-V2-srgbtrc.icc` at 72 DPI and attaches an active view. Creation is capped at 32 currently open documents and 4096 layer/mask nodes per document, counting user-created objects as well. Inspection returns document metadata, selection bounds, frame, active node and a flat layer list with parent_node_id; layer inspection also stops at 4096 nodes. Document listing is not paginated; preset listing is.

Painting requires the target's active view, matching RGBA/U8/sRGB document and node, zero origin/offset, an unanimated paint layer, no nonempty selection, unlocked/visible ancestry, and no layer alpha lock or alpha inheritance. The host applies explicit brush settings, invokes one native `paintPath` or `paintLine`, then synchronously restores the original view settings and active node before yielding. Paths do not accept pressure samples; lines use integer QPoint endpoints and endpoint pressure.

`Pending.poll() -> dict or None` runs on the GUI thread, re-resolves the document and checks `tryBarrierLock()` followed immediately by `unlock()`. It has no native deadline. A document closed before settlement produces `OUTCOME_UNKNOWN`. `GuiExecutor` uses a GUI-owned 20 ms QTimer to execute/poll one operation at a time, retaining the execution gate until settlement. No recursive event pumping or socket waits occur in the executor; synchronous host calls can still block Krita. Oversized/non-JSON host results become `HOST_RESULT_INVALID`, with mutation effect unknown.

Preview uses `Document.thumbnail`, with requested edges at most 1024 pixels, an explicit 1048576-pixel decoded-area limit and 2 MiB encoded PNG. Result fields include artifact_id, mime_type, size_bytes, document_id, source_bounds, source_offset, preview_width, preview_height, scale `{x,y}` and color metadata. Color/alpha conversion is reported as Krita's thumbnail behavior, not guaranteed archival sRGB output.

Save uses `saveAs` for a relative `.kra` destination; export uses `exportImage` for `.png` with alpha enabled, compression 6, forceSRGB/saveSRGBProfile true and indexed false. Both require an existing configured root/parent, canonical containment, and a matching resolved suffix. Existing destinations must be regular files and require explicit overwrite intent. Batch mode is restored in finally; success requires a nonempty output file. Results include document_id, root, relative path, size_bytes, filename association, modified state and format. The bridge does not create output directories or claim race-proof isolation from other programs running as the same user.

`KritaBridgeExtension` owns one process-wide host/ledger/server/executor and adds start/stop/status actions to each window. An enabled plugin autostarts unless `KRITA6_MCP_AUTOSTART` differs from `"1"`. `KRITA6_MCP_OUTPUT_ROOTS` is a JSON object of at most 32 simple root names (`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`) to existing absolute directories. Stop enters draining, keeps the original executor alive until native work settles, then shuts down the network on a worker and permits a fresh session. No bridge endpoint exposes arbitrary Python or Krita actions.

Capability metadata identifies Linux/Krita 6.0.3 as the verified reference workflow and reports `session_self_test: false`. Settings restoration is immediate without a GUI yield; native undo is described as one stroke on the reference build. Preview profile conversion remains unspecified. See [retained evidence and limits](validation.md). Tests invoke fixed Undo/Redo internally in an isolated profile; there is no public undo tool.

Failures after native dispatch, including restoration failures, retain a `Pending` barrier carrying the eventual error until the document settles or closes. Draining uses the same gate. After shutdown or partial startup, the extension explicitly disposes the GUI executor, disconnects its timer, clears host/ledger references, and schedules Qt deletion, avoiding retention across repeated bridge sessions.

## Optional diffusion reads

`DiffusionReader` observes only loaded `ai_diffusion` modules. Its methods execute on the GUI thread and return plain data. The three read commands never import the plugin, create a document model, connect a backend, submit/cancel a job, or select/apply a result. `availability` is `not_loaded`, `not_initialized`, `incompatible`, or `available`. Status includes the reported plugin version, observed connection enum, error-presence boolean, available checkpoint count (null when disconnected), tracked document count, and `backend_health_check: false`. No server URLs, credentials, account details, raw errors, or diagnostic dumps are returned.

Document reads match the explicit native target against existing upstream model wrappers. `document_status` is `tracked` or `model_not_created`; the latter does not initialize anything. A tracked model exposes generation-root prompts (each at most 4096 characters), style label (128), strength, batch count, model-level progress/kind, and error category. Truncated fields are named. Unknown shapes/enums fail closed as incompatible. At most 128 tracked models and 10000 queued/history jobs are inspected.

Job pages contain snapshot_index, nullable job_id, kind, raw upstream state, and result_count, with total/offset/next_offset. IDs belong to the upstream plugin; indices are not durable handles. `cancelled` can include failures, and history may change between pages. There are no diffusion mutation tools. Capability metadata points to the exact tested development revision and explicitly denies current-session self-testing. See [source contract and future control gates](diffusion-integration.md).
