# Implementation contract v1

Working integration contract. All JSON uses snake_case. Package `krita6_bridge` is importable without Qt outside Krita; its pure modules are also packaged with the external server.

Request: `{bridge_protocol: 1, instance_id: str, operation_id: str, command: str, target: object, params: object, queue_timeout_ms: int}`. Target and params default to `{}`, timeout to 10000 (range 1..120000). IDs use `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. Reject unknown fields, booleans as numbers, non-finite numbers, and malformed commands.

Snapshot: `{instance_id, operation_id, command, state, effect, result, error}`. State is queued/running/cancel_requested/succeeded/failed/cancelled/expired. Effect is none/applied/partial/unknown. Result is a JSON object or null. Error is `{code, message}` or null. Domain exceptions use `BridgeError(code, message, effect="none")` in `krita6_bridge.protocol`.

## Command catalog

| command | target | params |
| --- | --- | --- |
| list_documents | empty | empty |
| inspect_document | document_id | empty |
| get_preview | document_id | max_edge: int=1024 (32..1024) |
| list_brush_presets | empty | query: str="", offset: int=0, limit: int=50 (1..100) |
| create_document | empty | width,height: int (1..8192, product <=16777216), name: str (1..128) |
| create_paint_layer | document_id | name: str (1..128), parent_node_id: optional str |
| paint_path | document_id,node_id | preset_id: str, size_px: finite float (0.1..1000), opacity: finite float (0..1), color: #RRGGBB, points: list of 2..2048 [finite x,y] pairs |
| paint_line | document_id,node_id | same brush settings, start/end: [int x,y], pressure_start/pressure_end: finite float=1 (0..1) |
| save_document | document_id | root: str, path: str, overwrite: bool=false |
| export_png | document_id | root: str, path: str, overwrite: bool=false |

Host must validate coordinates against live dimensions and file paths against configured roots. Mutations are the five commands starting at create_document, except the catalog includes six mutation commands in total (create document/layer, path/line, save/export).

## Python interfaces

`protocol.py`: `BridgeError`; `validate_request(body, instance_id)` returns normalized request; `MUTATIONS` set; `COMMANDS` collection; `PROTOCOL_VERSION=1`; `PLUGIN_VERSION="0.1.0"`.

`operations.py`: `OperationLedger(instance_id, queue_limit=32, mutation_limit=10000, clock=time.monotonic)`; `admit(request)->snapshot`; `take_next()->request|None` marks running and skips expired/cancelled; `finish(operation_id, result=None, error=None, effect="applied")->snapshot` (error is a plain `{code,message}` object); `get(operation_id)->snapshot`; `cancel(operation_id)->snapshot`; `drain()` rejects new submissions/cancels queued; `is_idle()->bool`; `status()->dict`. Thread-safe; no Qt; error snapshots have state failed. Read success uses effect none. Same ID never changes commands, even for reads; read entries may expire from a separate bounded cache. No result object retains host wrappers.

`transport.py`: `ArtifactStore` with `put(data: bytes, mime_type="image/png")->dict` returning artifact_id, mime_type, size_bytes; `get(artifact_id)->(bytes,mime_type)`; `BridgeServer(ledger, session_info: dict, artifacts, state_dir=None)` with `start()->discovery dict`, `stop()`, `discovery_path`. Start binds loopback ephemeral port, writes discovery; stop removes own discovery and shuts down workers. Session info supplies host versions/capabilities; server merges protocol/plugin/instance and ledger state. HTTP endpoints follow design.md. Protocol errors return `{error:{code,message,effect}}` with suitable HTTP status; operation admission returns HTTP 202, snapshots HTTP 200, artifacts return bytes with content type. Use `Authorization: Bearer TOKEN`; exact Host `127.0.0.1:PORT`.

`discovery.py`: `default_state_dir()->Path` honors `KRITA6_MCP_STATE_DIR`, otherwise Linux `$XDG_STATE_HOME/krita6-mcp` or `~/.local/state/krita6-mcp`, Windows LOCALAPPDATA, macOS Application Support. Discovery files `instance-<id>.json`.

`host.py`: `KritaHost(artifacts, output_roots: dict[str,Path])`; `session_info()->dict`; `execute(request)->dict|Pending`. `Pending.poll()->dict|None` runs only on GUI thread; returns result on native completion, raises BridgeError on failure. Host checks its GUI thread. Its result for get_preview includes artifact_id, source bounds, preview dimensions, color metadata. List documents result `{documents:[{document_id,...}]}`, create document `{document_id,...}`, create layer `{node_id,...}`, presets `{presets:[{preset_id,name}],...}`.

`executor.py`: GUI-owned timer takes one ledger command at a time, executes/polls host, finishes ledger, and catches errors into sanitized data. No waiting or socket I/O. `extension.py` registers one process-wide bridge/host/executor, startup/stop/status actions, and parses `KRITA6_MCP_OUTPUT_ROOTS` as a JSON object of root names to absolute paths. Autorun only after plugin is enabled. No network operation exposes arbitrary actions; host test harness may invoke fixed Undo/Redo internally in a disposable test profile.

This file is coordination guidance; update it to match verified implementation before release. Unverified host operations should be reported as such in capability metadata.
