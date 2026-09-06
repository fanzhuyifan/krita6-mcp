# Krita AI Diffusion integration

Source decision record · 2026-09-06

## Scope and upstream baseline

The implemented adapter provides optional, read-only observation of an already loaded [Krita AI Diffusion](https://github.com/Acly/krita-ai-diffusion) plugin. It reports connection state, settings of an existing document model, and bounded job snapshots. The core bridge continues to work when AI Diffusion is absent. It does not install or initialize AI Diffusion, connect a backend, submit generation, cancel jobs, select previews, or apply results.

Source inspection used a local archive of commit [`dda58d1c63e361207ccec085efbc34dbd32f1654`](https://github.com/Acly/krita-ai-diffusion/commit/dda58d1c63e361207ccec085efbc34dbd32f1654), the Qt6 migration merge from [PR #2491](https://github.com/Acly/krita-ai-diffusion/pull/2491). This is a development source baseline, not a released Krita 6 package. The latest stable release found during this investigation, [v1.53.0](https://github.com/Acly/krita-ai-diffusion/releases/tag/v1.53.0), explicitly targets Krita 5. Its release commit is `0217cd2197fcadbd70d7e63af25e29cc21cb7c8b`.

The development baseline still reports `__version__ = "1.53.0"`, but its bootstrap requires Krita 6 and its model, document, connection, and job modules use PyQt6. A version string alone therefore cannot distinguish the stable Qt5 release from this Qt6 source. Capability reporting must separate the reported version, observed interface compatibility, and the exact source/build for which live evidence exists. Do not present every installation reporting 1.53.0 as supported. See the pinned [bootstrap](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/__init__.py).

The source archive omits the `websockets` submodule required by that bootstrap. An isolated test installation must include its pinned dependency. Do not install another Qt binding into Krita or use a personal AI Diffusion profile as a test fixture. Upstream extension initialization loads settings, starts its event loop, creates root state, and can schedule backend autostart; these are upstream lifecycle effects, not steps performed by an observation tool. See [extension initialization](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/extension.py).

## Adapter boundary

| Bridge command | Read-only contract |
| --- | --- |
| `diffusion_status` | Detect loaded modules and compatible Qt6 interfaces; report plugin version, connection state, and bounded capability/count information |
| `inspect_diffusion_document` | Resolve an explicit bridge document handle and inspect its existing AI Diffusion model, or report that no model exists |
| `list_diffusion_jobs` | Inspect that document model's current queue/history using `offset` and `limit`, with at most 100 entries per response |

All access runs inside the existing GUI executor. HTTP workers and the external MCP process receive plain JSON only. Detection uses modules already present in `sys.modules`; it must not import AI Diffusion to activate it. A missing, partially initialized, incompatible, or stale plugin produces an explicit availability result or domain error. It must not silently initialize replacement state.

The adapter uses a small allowlist of fields with bounded strings and collections. It does not return raw object dictionaries, diagnostic logs, settings dumps, server URLs, authorization fields, cloud account information, workflow graphs, or raw error messages/data. Connection/model error categories can be reported without their potentially sensitive text. Prompts, where explicitly included in document inspection, are bounded user-content fields, not executable instructions.

## Existing interfaces and identity

The pinned [root model](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/model/root.py) exposes `root.models`, returning existing `DocumentModel` objects, and `root.connection`. Reading these properties does not create a model. In contrast, `root.active_model` calls `model_for_active_document()`, which prunes models, can create one, attaches persistence, and can import a prompt from disk. Neither accessor belongs in the read-only adapter.

Each `DocumentModel.document` is an upstream wrapper. Match its existing native `_doc` against a freshly resolved Krita document using Krita's document equality, on the GUI thread. Catch invalid/deleted wrappers and fail clearly. Keep the bridge's own document handle as the public target identity. Upstream `KritaDocument.id` comes from a document annotation and can be copied with a document; it is not proof of current native ownership. Do not instantiate `KritaDocument` or call `KritaDocument.active()` during inspection: those paths can write an annotation and start a timer. These private wrapper details are isolated in the adapter because they can change upstream. See the pinned [document wrapper](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/document.py).

The [connection object](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/model/connection.py) has states `disconnected`, `connecting`, `connected`, `error`, `discover_models`, and authentication-related states. `client_if_connected` returns the stored client without checking the current state; do not infer connectivity merely from a non-null client. A temporary backend disconnection may leave the reported state connected while setting an error, so report the observed state and error presence without claiming a fresh health check. No network probe or model refresh is part of status.

Useful document fields include workspace, style label, root positive/negative prompts, strength, seed and fixed-seed flag, batch count, queue mode, and model progress/error category. These are upstream UI/model state, not a validated future request or a guarantee that a model is available on the backend. Model-level progress and errors do not identify a particular job.

## Job snapshots

The pinned [job queue](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/model/jobs.py) is iterable and has a length. A `Job` contains `id`, kind, state, timestamp, parameters, results, and per-result use flags. Its ID can be null before local admission finishes. Preserve this absence; an array index, prompt, timestamp, or result position must not become a fabricated durable job identity.

The upstream states are `queued`, `executing`, `finished`, and `cancelled`. Preserve their names as upstream observations. There is no separate failed state: model error handling can mark a job cancelled, and queue repair can label earlier stale jobs cancelled. A cancelled snapshot therefore does not prove that a particular backend operation was interrupted. History is pruned; some job kinds are removed on completion, and deleting a result shifts later result indices. Pagination is a view of the current queue, not a durable history cursor. Counts of available results do not promise later retrieval.

Never select a job to inspect it. Updating `jobs.selection` invokes the model's preview handler, which can create or change a Krita layer. Reading job/result counts requires neither selection nor pixel extraction.

## Future generation, cancellation, and application

Generation control requires a separate increment and live backend evidence. The current [document model](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/model/model.py) exposes `generate()`, which returns no job handle and schedules an asynchronous enqueue operation. The model adds a job with a null ID, then assigns the ID returned by its client. A future bridge must retain its own operation identity before scheduling and distinguish local admission, backend dispatch, generation completion, and document application. Repeating `generate()` after a lost response is not reconciliation.

Upstream IDs are plugin-local job identities. The [ComfyUI client](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/backend/comfy_client.py) allocates an ID and queues locally before remote submission; it later sends that ID as `prompt_id` and verifies the returned ID. Its interrupt operation posts to the backend's global `/interrupt` endpoint without a job ID. Consequently, that operation cannot be presented as cancellation scoped to one bridge document or bridge-owned job.

The [cloud client](https://github.com/Acly/krita-ai-diffusion/blob/dda58d1c63e361207ccec085efbc34dbd32f1654/ai_diffusion/backend/cloud_client.py) maintains distinct local and remote IDs. Its interruption behavior depends on the current send/generate stage and allows receive-stage work to finish. A future cancellation API must verify ownership and report requested versus confirmed outcomes. It also needs an explicit authorized backend choice before submitting artwork or spending cloud credits.

The document model's queued cancellation clears every queued job for that document, and replacement queue mode also cancels existing queued work. Completion can automatically preview or apply a generated image according to global settings. `apply_generated_result()` also follows ambient application settings, can replace layers or resize the canvas, and has special layered/animation behavior. Generation and application therefore need explicit, tested policies before exposing them as independent tools; invoking upstream UI actions is insufficient.

## Validation gates

Read-only acceptance requires an isolated Krita 6 host with the exact pinned development plugin: absent plugin, initialized plugin without a backend, existing and missing document models, stale targets, bounded job pagination, and unchanged document/model/queue state after reads. Synthetic jobs in a test-only harness can validate serialization and state labels, but do not establish generation, backend cancellation, result application, or undo compatibility.

Record the actual test results in [validation.md](validation.md). This source record establishes interface choices and limits; it does not establish that a live test passed. No generation backend is configured for this increment. Later mutation work needs explicit scratch-document/backend tests for admission identity, disconnect reconciliation, job ownership, automatic preview/application behavior, exact target application, native completion, color, and undo.
