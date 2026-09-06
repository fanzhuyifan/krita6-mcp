# Krita AI Diffusion integration

Decision record · 2026-09-06

The optional adapter integrates with the existing [Krita AI Diffusion add-on](https://github.com/Acly/krita-ai-diffusion). It uses the add-on’s document model, canvas preparation, connected backend client, job queue, and result application. MCP tools do not install the add-on, start a backend, or construct a separate ComfyUI workflow.

## Source boundary

Generation targets Qt6 development commit [`dda58d1c63e361207ccec085efbc34dbd32f1654`](https://github.com/Acly/krita-ai-diffusion/commit/dda58d1c63e361207ccec085efbc34dbd32f1654), also packaged locally as `1.53.0.r6.gdda58d1`. Stable v1.53.0 targets Krita 5; both sources report version 1.53.0. The mutation adapter checks source fingerprints and loaded interfaces, rather than trusting that version string. Other revisions need review and live validation before enabling generation.

Detection inspects already loaded modules. All add-on, Qt, and Krita access runs on the GUI thread; network workers receive plain JSON or encoded PNG bytes. Read tools do not create models, refresh layer managers, change job selection, or connect a backend. Open the AI Diffusion docker for the target document first so its document model exists.

Only an already connected local loopback ComfyUI client is accepted for generation. Cloud and remote backends are outside this implementation. Status reports observed connection state, not a fresh network health check. Diagnostics omit backend URLs, authentication data, raw errors, and workflow graphs.

## Workflow

1. Discover `krita_diffusion_status`, inspect `krita_inspect_diffusion_document`, and optionally list `krita_list_diffusion_styles`.
2. Call `krita_generate_diffusion` with explicit instance/document handles, a reusable `operation_id`, positive/negative prompts, strength, seed, and optionally a listed style handle. The document must be active and use RGBA/U8/standard sRGB with a zero-offset canvas. One image is requested, independent of the add-on’s batch setting.
3. Poll `krita_get_diffusion_generation` using the returned `generation_id`. A successful bridge submission means scheduling succeeded; rendering has its own lifecycle. A plugin job ID initially means local queue admission, not confirmed backend acceptance.
4. Inspect `krita_get_diffusion_result` with the returned `result_id`. It returns an inline PNG up to 1024 pixels per edge without selecting a canvas preview.
5. Call `krita_apply_diffusion_result` with a new reusable `operation_id`. Application targets the original document and generation bounds, creates a new top paint layer, and waits for Krita’s native completion barrier.

The [usage guide](usage.md) contains an example; [validation](validation.md) records which live cases passed.

## Canvas behavior

Generation calls the add-on’s existing preparation logic:

- Strength below 1 refines the current canvas.
- A selection supplies the add-on’s inpainting/refinement mask and context.
- Existing linked regions contribute their regional prompts and masks.
- Existing control/reference layers provide conditioning, including region-specific controls.

Document inspection exposes bounded settings, selection bounds, regional prompts, and control/reference layer links without activating them. MCP can configure root settings, linked regions, and control lists as described below; the core selection tools configure canvas selections. Ordinary generation is supported; live, animation, custom workflow, edit-model, and layered-output modes are excluded.

The request supplies the root/global prompts, which the add-on combines with configured regional and style prompts; it does not replace each region’s prompt. The request temporarily overrides prompt/style/strength/seed settings while preparing input, then restores them before yielding the GUI thread. Canvas layer visibility is restored if preparation fails. Result placement uses the captured generation bounds; changing document geometry or color settings invalidates application. Intervening ordinary painting is allowed, so inspect the current canvas before applying an older result.

Bridge-owned jobs suppress the add-on’s automatic preview/apply action through a narrowly scoped per-model completion hook. Other jobs retain the original completion behavior, and the hook is removed when owned pending jobs settle. This prevents layer/pixel application before explicit review. The add-on still records generated images in its history and document annotations; generation is a state-changing operation and can mark the document modified.

Explicit application uses the add-on’s new-layer behavior with region restructuring disabled. It does not follow ambient replace-layer, canvas-resize, or regional regrouping settings. Masked results retain their generated alpha. Application is not promised to be one undo action; use the recorded native evidence for tested behavior.

## Ownership, retries, and limits

The bridge reserves `generation_id = operation_id` before scheduling. Reuse the same operation ID and payload after a timeout; the existing ledger prevents duplicate dispatch within that bridge instance. Poll a generation separately from its submission operation. Submission failures after possible backend dispatch retain an uncertain outcome and must not trigger automatic resubmission.

Generation records belong to an exact document model and job. Result handles identify exact image objects rather than mutable queue positions. Closing the document, removing a job/result, or restarting the bridge invalidates the corresponding handles. Weak references avoid retaining images after the add-on prunes history. The session accepts at most 64 generation records and rejects further submissions instead of evicting ownership records. Generated canvas/workflow dimensions are bounded to 16 megapixels.

`krita_list_diffusion_jobs` remains an observational view of all jobs, including user-created ones; it does not grant mutation ownership. Upstream job IDs may be null, and `cancelled` can include failures or queue repair. Model-level progress is not job-specific.

There is no backend cancellation tool. `krita_cancel_operation` cancels only unstarted bridge work; after submission it cannot interrupt rendering. Upstream’s global interrupt and broad queue cancellation cannot safely stand in for cancellation of one bridge-owned job. Pending generation may continue after the MCP client or bridge stops; inspect the add-on before starting a new session and intentionally resubmitting.

## Persistent configuration

Three mutation tools configure the existing active Generate model without submitting jobs:

- `krita_configure_diffusion` patches supplied root prompts, strength, seed/fixed seed, listed style, batch count, region-only behavior, resolution multiplier, inpaint mode, and custom inpaint/prompt-focus flags. Omitted fields remain unchanged; empty prompts clear them. Style selection requires the connected local backend and rejects edit architectures.
- `krita_set_diffusion_region` creates or updates a prompt region linked directly to an existing paint/group layer. `remove=true` deletes the region and its controls, preserving artwork. Multiple matching regions or a region linked to multiple layers are rejected, so a single layer handle never silently edits other links. Creating a region selects it in the add-on, matching its normal insertion behavior.
- `krita_set_diffusion_controls` replaces the complete root conditioning list, or one region's list selected by `region_node_id`. An empty list clears it. Each entry specifies an existing image layer, a typed mode, normalized strength 0..2 in increments of 0.02, and start/end fractions satisfying 0 <= start <= end <= 1. Custom strength is enabled. There are at most 32 regions and 64 controls across the generation root. Active preprocessor jobs prevent replacing their controls or editing/removing their region.

Configuration uses the same reviewed source fingerprints as generation and runs entirely on the GUI thread. It works without a connected backend except for style selection. Disconnected `is_supported` is only the add-on's observed flag, not proof that models are installed; re-inspect after connecting. Control preprocessors, backend lifecycle/cancellation, live/animation/custom/edit workspaces, style-file editing, and arbitrary workflow parameters remain outside these tools.

These are persistent add-on model changes and can affect document annotations/modified state; there is no guaranteed undo transaction. Reuse the operation ID after an uncertain outcome. Validation resolves all requested layer/style targets before configuration; setter/signal failures after mutation begins report `effect: partial`, with inspection required before a new operation. Snapshot indices are never mutation identities.

`krita_generate_diffusion` retains its explicit request contract: it supplies root prompts, strength and seed temporarily and requests one image, regardless of persistent batch settings. Persistent region, control, resolution, and inpaint configuration feeds the add-on's normal preparation. Configuration alone does not prove that every control mode/model combination can generate; see the live evidence and its limits.
