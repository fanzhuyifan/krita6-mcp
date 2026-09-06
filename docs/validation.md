# Validation record

Initial implementation 0.1.0 · 2026-09-06

The native painting loop and production MCP workflow pass on the reference Linux build below. This is evidence for that build and tested preset, not a guarantee for every Krita 6 package or brush engine. Runtime capability metadata identifies the reference build and explicitly says the current session has not self-tested.

| Component | Tested version/environment |
| --- | --- |
| Krita | 6.0.3 (installed Linux package 6.0.3-2) |
| Qt / PyQt | 6.11.2 / 6.11.0 |
| Embedded Python | 3.14.7 |
| Display | xcb on Xvfb, software GL, private D-Bus session |
| External Python / MCP SDK | 3.11.16 / 2.1.1, dependencies in `uv.lock` |
| Brush | Bundled `b) Basic-5 Size`, pixel brush engine |
| Document | 160 × 120, RGBA/U8, `sRGB-elle-V2-srgbtrc.icc`, zero origin |

## Live native proof

`tools/probe_krita.py` installs a fixed test-only plugin in a disposable profile. Its [retained report](validation/native-linux-krita-6.0.3.json) contains 16 passing checks:

- Native path and endpoint-pressure line change actual document pixels.
- A foreground change immediately after dispatch does not alter the captured red stroke.
- One ordinary Krita undo restores the pixels before each primitive; redo restores the exact stroke pixels.
- Thumbnail dimensions are correct, and preview alone preserves the filename and modified flag.
- Layered `.kra` save, separate PNG export, reopen, layer structure, and exact pixel round-trip succeed.

The exported fixture shows the red path and blue line with increasing endpoint pressure. Pressure shape was visually checked; the test does not mathematically characterize the engine's pressure curve.

![Native brush fixture](validation/native-strokes.png)

The probe also found that `Document.projection()` with omitted bounds returns a null image on this build. Its pixel assertions now use explicit bounds. Production previews use the bounded thumbnail API instead.

## Production MCP proof

`tools/smoke_krita.py` loads the production plugin in another disposable profile and drives the external adapter through real MCP stdio. The [retained report](validation/mcp-linux-krita-6.0.3.json) records:

- All 32 tools are advertised, including the eight optional diffusion tools; the catalog was rechecked after the reference-editing additions.
- With AI Diffusion absent, its status returns `not_loaded` and the core workflow still succeeds.
- Retrying document creation with the same operation ID creates only one document.
- A paint layer, red native path, and blue pressure line are created through the bridge.
- An inline 160 × 120 PNG reaches the MCP client.
- `.kra` contains the named paint layer and merged image; PNG export preserves the `.kra` filename association and clean modified state.
- Export to an existing destination without overwrite permission is rejected.

![MCP inline preview](validation/mcp-preview.png)

The image was visually checked against the exported PNG. Its SHA-256 is retained in the report. After the reference-editing additions and again after the creation-ownership fixes, the 32-tool core smoke workflow passed with the same preview SHA-256, native path/line behavior, duplicate creation handling, and save/export checks. The first live run exposed preset XML changing after our own setting updates; the host now refreshes the used handle synchronously after restoration, with a regression test for a second stroke and later external edits.

## Reference editing

`tools/probe_editing.py` passes all eleven new editing tools through real MCP stdio and the production plugin in a separate Xvfb/D-Bus/profile/runtime/discovery environment. The [sanitized report](validation/editing-linux-krita-6.0.3.json) records the same Linux/Krita 6.0.3, Qt 6.11.2, PyQt 6.11.0, and embedded Python 3.14.7 runtime. The test uses 128 × 96 RGBA/U8/standard-sRGB canvases, 24 × 16 PNG/JPEG fixtures, and the bundled `b) Basic-5 Size` preset. Its independent GUI-thread fixture observes document/layer pixels and selection masks, separately from MCP result metadata.

- Activation switches to the requested existing view; Bézier painting rejects the inactive target and a nonempty selection before dispatch.
- Rectangle and triangle selections have the expected inside/outside mask pixels; clearing removes the selection. The review regression also checks a self-intersecting bowtie: both odd-even lobes are selected while outside pixels remain clear, despite zero signed area. An empty identical-point polygon is rejected without replacing the existing selection.
- One cubic Bézier request paints a native stroke. Retrying its ID does not paint again. One ordinary undo restores the prior projection; redo restores exact layer pixel bytes and the same visible projection. Krita may change RGB values in fully transparent projection pixels after redo, so that projection comparison ignores RGB only where alpha is zero.
- PNG import preserves red/green/blue channels, transparency, and the requested offset. JPEG import matches its known color within compression tolerance. Repeated import adds no duplicate layer.
- Name, visibility, and opacity updates appear in independently observed node state and settled projection pixels.
- The 24 × 16 region PNG has exact crop coordinates, RGB/alpha, and unit scale. A 128 × 96 region downsamples to 64 × 48 with scale 0.5. Both reads preserve the saved source's filename, modified flag, layers, selection, and checked pixels.
- Cross-document copy preserves source state and creates one new layer UUID, including on retry. Reordering preserves the layer UUID and verifies both sibling order and which overlapping color appears in the projection.
- A transform combining a twofold scale, 90° clockwise rotation, explicit pivot, and translation places each colored quadrant at its independently calculated coordinates. Original pixels are cleared; transparent pixels remain transparent. Retrying does not transform twice.
- PNG/JPEG and layered KRA files open through the bounded input root; a repeated PNG open creates only one document. Invalid paths, off-canvas regions/import/selection, and conflicting operation IDs fail without changing the checked documents.

The same full probe also exercises native creation recovery with fixed test-only failure injection. A control creates a viewless native document, drops its unretained originating wrapper, and forces garbage collection; the native document disappears. Production create/open commands now register a handle and retain their originating wrapper before fallible view setup. For both direct document creation and PNG opening, an injected exception before the real `addView` call produces a settled `partial` error carrying the document handle. The viewless native document survives garbage collection and three rounds of fresh MCP list/inspect reconciliation. Retrying the original operation creates no second document. The fixture then attaches a real view using the retained original wrapper, activates the same handle, closes the scratch document, and verifies owner/handle pruning. Retrying the recorded failure after closure still does not create a document.

A separate injected exception at layer activation occurs after a real paint layer has attached. The terminal partial result retains its document and node handles; inspection finds that exact node, and a repeated operation ID adds no layer. These tests exercise actual native object lifetime and attachments with deliberately injected initialization failures; they do not claim exhaustive live failure or modal-dialog coverage.

The probe exposed a native API detail: `addChildNode` can return success without moving an already-attached node. Production reordering now detaches then reinserts the same node and verifies its parent and sibling position. Immediate inspection after opening may return `DOCUMENT_BUSY`; the test retries only that read with a bound and never resubmits a mutation under a new identity.

Direct layer/selection/pixel edits still report `undo: "not_guaranteed"`. This fixture establishes standard-sRGB, little-endian pixel handling on the stated build, not arbitrary profile conversion, all rotations/resampling cases, large documents, group/mask combinations, or live multi-window races. No normal profile, existing discovery file, personal artwork, or already-open Krita instance is used.

## AI Diffusion readers

`tools/probe_diffusion.py` passes with the actual PyQt6 development plugin at upstream commit `dda58d1c63e361207ccec085efbc34dbd32f1654`, on the same Linux/Krita 6.0.3 runtime. The [retained report](validation/diffusion-linux-krita-6.0.3.json) records host versions and fingerprints for the seven upstream files defining the integration boundary. The latest rerun used the installed Arch Qt6 package `1.53.0.r6.gdda58d1-1`, including its bundled dependencies. Upstream source/dependencies and the disposable profile are excluded from this repository.

All three reader tools were exercised over real MCP stdio. They detected the loaded plugin in `auth_missing` state without a backend client, inspected the scratch model's expected prompts/strength/batch count, and paginated the real upstream `JobQueue`. A test-only plugin inserted four **synthetic** records to cover `queued`, `executing`, `finished`, and `cancelled`, including a null job ID. No images were generated and synthetic finished jobs have no results.

The fixture verifies that reads preserve pixels, filename, modified state, tracked-model count, job IDs/states/result counts, job selection, root prompts, strength, batch count, style name, top-level layer IDs/names, connection state, and absence of a backend client. These specific checks do not prove preservation of every possible plugin field or real-generation behavior. Fake-object tests separately cover missing models, incompatible modules/Qt, stale matching, bounds, secret omission, and prohibited side effects.

The latest stable AI Diffusion release inspected, v1.53.0, targets Krita 5. The tested **unreleased** Qt6 source still reports 1.53.0, so compatibility is tied to the pinned source and runtime interfaces, not that version string. The reader never activates the plugin, creates its document models, connects a backend, or selects previews. See the [integration source boundary and policies](diffusion-integration.md).

## AI Diffusion generation

`tools/probe_diffusion_generation.py` passes through real MCP stdio, the production bridge, the installed Qt6 add-on, and a scratch ComfyUI server using existing local model weights. The [sanitized report](validation/diffusion-generation-linux-krita-6.0.3.json) records these results. The normal Krita profile and server configuration were not used.

| Component | Tested configuration |
| --- | --- |
| Add-on | Arch `1.53.0.r6.gdda58d1-1`, pinned Qt6 source above |
| Backend | ComfyUI 0.33.3, PyTorch 2.13.0+cu130, Python 3.12.14 |
| GPU | NVIDIA RTX 4060 Laptop, 8 GiB |
| Style/model | Shipped Digital Artwork (SD1.5), `dreamshaper_8.safetensors` |
| Canvas | 512 × 512 RGBA/U8/standard sRGB scratch document |

The probe covers full image generation and selection refinement at strength 0.65. The latter uses a 192 × 192 selection; the add-on expands its context to a 288 × 288 masked result at the captured bounds. Both runs verify:

- Repeated submission creates one add-on job and one image despite a configured batch count of two.
- Generation and image inspection preserve the canvas pixels/layers, selection, and checked prompt/style/seed/strength/batch settings, even with the add-on’s automatic finish action set to apply.
- A generated PNG reaches the MCP client. Explicit application creates one new top layer; repeating the application ID adds no duplicate.
- After native completion, one ordinary Krita undo restores the exact pre-application pixels and layer IDs; redo restores the exact applied state on this fixture.
- Selection refinement preserves a checked 64 × 64 far-corner patch outside the generated area. The add-on’s mask expansion/feathering means the original selection rectangle is not a hard output boundary.

This establishes the workflow on the stated model/build. Existing regional prompts and control/reference layers are passed through the add-on’s preparation and exposed by inspection, but their combinations have not yet received equivalent live generation coverage. Full color-profile conversion, larger models/documents, backend disconnect races, and long-session history pruning need further live tests. Source-gate, ownership, pruning, and error behavior also have separate unit coverage. No backend cancellation or cloud generation is exposed.

## Automated coverage

**441 pytest tests pass on CPython 3.11.16**, using dependencies installed from `uv.lock`. CI runs the suite on Python 3.10, 3.12, and 3.14. Coverage includes strict schemas, authenticated transport, operation identity/cancellation/expiry, bounded results, native completion guards, and real MCP stdio in SDK `auto` and `legacy` modes. The 24 diffusion-reader cases cover bounded canvas metadata and prohibited read side effects; 43 generation cases cover source/backend gates, one-job submission, restored settings/visibility, scoped automatic-apply suppression, stable weak image handles, pruning/reordering, context changes, and uncertain native outcomes. Host guards cover lifecycle/barrier behavior, bounded editing, activation completion, layer reordering, and consistent capability reporting. These tests establish protocol and adapter behavior; native/upstream compatibility comes from the separate live probes. The reference-editing increment adds typed-schema/adapter, input-root/KRA-bound, retry/cancellation, and editing host-guard coverage. Review regressions cover partial effects after completed mutation phases, preserved recovery handles after pending failures, raster-mask emptiness without a signed-area restriction, and direct create/open wrapper retention plus creation-initialization error handles. Ruff and wheel/sdist builds pass on the updated tree; the ZIP and real importer also have the separately recorded checks below.

The release-preparation review found that Krita's ZIP importer requires an explicit module-directory entry. The builder now includes that entry, the MIT license, and the manual, with a packaging regression test. `tools/probe_plugin_import.py` successfully imported the generated archive through Krita's installed importer into a temporary resource directory and verified the installed source/license/manual. The importer check executes its real filesystem logic with only its translation function supplied; it does not establish native painting behavior or touch a personal Krita profile.

The subsequent cleanup pass preserved both filesystem helper functions unchanged while moving them into `output_paths.py`, verified their inclusion in the wheel and imported ZIP, and reran the production MCP smoke workflow successfully. Its preview SHA-256 matched the retained reference. An initial attempt using `uv run` failed in Krita's own Python bootstrap before the bridge loaded; a separate embedded-Python probe reproduced how the external virtual environment's `python3` on `PATH` hides system PyQt6. The successful run invoked `.venv/bin/python` directly with `/usr/bin/krita` and inherited Python/Qt/KDE development overrides cleared. The [testing guide](testing.md#live-krita-checks) documents this host-test environment requirement.

Run the verification commands in the [testing guide](testing.md). The live harnesses isolate XDG profile directories **and `TMPDIR`**, preventing Krita's single-instance socket from routing test work to an already-open personal session. They use only scratch artwork. Reports retained here omit transient identifiers, private paths, tokens, and profile logs.

## Remaining limits

- Linux/Krita 6.0.3 is the only tested platform/build. Windows discovery fails explicitly until private ACL handling exists; macOS has not been validated.
- Other engines, arbitrary profiles, nonzero offsets, animation, and per-point path pressure are outside the supported painting path. Selections can be explicitly replaced/cleared; native painting still rejects a nonempty selection. Preview output-profile/alpha conversion follows Krita's thumbnail API and is explicitly unspecified; this is not archival color validation.
- Busy-image, tab-close, modal, repeated stop/start, and simultaneous-instance races need broader **live** stress coverage. Fake-host checks cannot establish all native lifetime behavior.
- No current-session self-test runs automatically. AI Diffusion generation/application is verified only on the pinned source and local backend/model above. Backend cancellation, cloud/remote backends, and live/custom/edit-model workflows are outside this implementation.
- The bridge cannot forcibly interrupt a running native stroke or guarantee a hard deadline for synchronous Krita calls. It retains the execution gate while awaiting native completion.
- File checks do not provide race-proof filesystem isolation from another process running as the same user. At-most-once dispatch applies within a live plugin instance, not across a Krita crash.
- Layer creation, property changes, selection edits, copying/reordering, imports, and raster transforms have no promised undo grouping. The implementation provides native stroke undo metadata but no MCP undo tool. Per-tool output schemas and automatic plugin installation remain future work.

## AI Diffusion configuration

The [configuration report](validation/diffusion-configuration-linux-krita-6.0.3.json) records a live isolated-profile run of the expanded `tools/probe_diffusion.py` on Linux/Krita 6.0.3, Qt 6.11.2/PyQt 6.11.0 and the pinned Qt6 add-on. All 35 MCP tools were listed. Persistent prompts, strength, seed/fixed seed, batch count, region-only, resolution multiplier and custom inpaint flags were observed through MCP; root and regional scribble controls retained their layer, mode, strength and interval. Region creation/update/removal and list clearing passed, including duplicate operation IDs. A native fixture independently checked settings and preserved pixels, artwork layers, selection and synthetic jobs.

No backend was connected for that configuration run. It does not establish generation compatibility for every control mode/model, multi-linked regions, persistence across save/reopen, undo grouping, or other platforms. The automated suite separately covers input bounds, cancellation before dispatch, deduplication/conflicts, ambiguous region identities, and partial setter failures.

The expanded local-backend generation probe also passed persistent selection of the supported Digital Artwork (SD1.5) style and duplicate-ID handling, independently observed by the native fixture. Full-image generation, selection refinement, explicit application, and native undo/redo passed with that persistent style. The same configuration report retains sanitized results and backend versions. The final automated suite passed 465 tests; Ruff checks/formatting and source/wheel/plugin-ZIP builds passed.
