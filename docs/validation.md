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

- All 16 tools are advertised, including the three optional diffusion readers.
- With AI Diffusion absent, its status returns `not_loaded` and the core workflow still succeeds.
- Retrying document creation with the same operation ID creates only one document.
- A paint layer, red native path, and blue pressure line are created through the bridge.
- An inline 160 × 120 PNG reaches the MCP client.
- `.kra` contains the named paint layer and merged image; PNG export preserves the `.kra` filename association and clean modified state.
- Export to an existing destination without overwrite permission is rejected.

![MCP inline preview](validation/mcp-preview.png)

The image was visually checked against the exported PNG. Its SHA-256 is retained in the report. The first live run exposed preset XML changing after our own setting updates; the host now refreshes the used handle synchronously after restoration, with a regression test for a second stroke and later external edits.

## AI Diffusion readers

`tools/probe_diffusion.py` passes with the actual PyQt6 development plugin at upstream commit `dda58d1c63e361207ccec085efbc34dbd32f1654`, on the same Linux/Krita 6.0.3 runtime. The [retained report](validation/diffusion-linux-krita-6.0.3.json) records host versions and fingerprints for the seven upstream files defining the integration boundary. Its websockets dependency came from the official v1.53.0 ZIP; that archive's SHA-256 is also recorded. Upstream source/dependencies and the disposable profile are excluded from this repository.

All three reader tools were exercised over real MCP stdio. They detected the loaded plugin in `auth_missing` state without a backend client, inspected the scratch model's expected prompts/strength/batch count, and paginated the real upstream `JobQueue`. A test-only plugin inserted four **synthetic** records to cover `queued`, `executing`, `finished`, and `cancelled`, including a null job ID. No images were generated and synthetic finished jobs have no results.

The fixture verifies that reads preserve pixels, filename, modified state, tracked-model count, job IDs/states/result counts, job selection, root prompts, strength, batch count, style name, top-level layer IDs/names, connection state, and absence of a backend client. These specific checks do not prove preservation of every possible plugin field or real-generation behavior. Fake-object tests separately cover missing models, incompatible modules/Qt, stale matching, bounds, secret omission, and prohibited side effects.

The latest stable AI Diffusion release inspected, v1.53.0, targets Krita 5. The tested **unreleased** Qt6 source still reports 1.53.0, so compatibility is tied to the pinned source and runtime interfaces, not that version string. The reader never activates the plugin, creates its document models, connects a backend, or selects previews. See the [source findings and remaining generation gates](diffusion-integration.md).

## Automated coverage

**164 pytest tests pass on CPython 3.11.16**, using dependencies installed from `uv.lock`. CI runs the suite on Python 3.10, 3.12, and 3.14; the earlier 143-case suite also passed locally on CPython 3.10.21, 3.12.14, and 3.14.7. The suite covers strict schemas, authentication and discovery permissions, bounded HTTP workers and deadlines, queue admission, at-most-once mutation dispatch, cancellation, result expiry and byte limits, retained identities, fake-host guards, and MCP error/image results. Real stdio integration runs in both SDK `auto` and `legacy` client modes. Twenty-three host guards cover native completion/lifecycle limits; 18 filesystem/configuration cases run without Qt or Krita; 17 diffusion-reader tests cover compatibility and prohibited side effects. Client tests include truncated/stalled/malformed HTTP error responses, and preview checks cover equivalent immediate/polled images and retrieval failures. These tests establish protocol behavior independently of Krita; native and upstream integration claims come from the separate live probes above. Ruff checks and package builds also pass.

The release-preparation review found that Krita's ZIP importer requires an explicit module-directory entry. The builder now includes that entry, the MIT license, and the manual, with a packaging regression test. `tools/probe_plugin_import.py` successfully imported the generated archive through Krita's installed importer into a temporary resource directory and verified the installed source/license/manual. The importer check executes its real filesystem logic with only its translation function supplied; it does not establish native painting behavior or touch a personal Krita profile.

The subsequent cleanup pass preserved both filesystem helper functions unchanged while moving them into `output_paths.py`, verified their inclusion in the wheel and imported ZIP, and reran the production MCP smoke workflow successfully. Its preview SHA-256 matched the retained reference. An initial attempt using `uv run` failed in Krita's own Python bootstrap before the bridge loaded; a separate embedded-Python probe reproduced how the external virtual environment's `python3` on `PATH` hides system PyQt6. The successful run invoked `.venv/bin/python` directly with `/usr/bin/krita` and inherited Python/Qt/KDE development overrides cleared. The README now documents this host-test environment requirement.

Run the verification commands in the [README](../README.md#development-and-verification). The live harnesses isolate XDG profile directories **and `TMPDIR`**, preventing Krita's single-instance socket from routing test work to an already-open personal session. They use only scratch artwork. Reports retained here omit transient identifiers, private paths, tokens, and profile logs.

## Remaining limits

- Linux/Krita 6.0.3 is the only tested platform/build. Windows discovery fails explicitly until private ACL handling exists; macOS has not been validated.
- Other engines, arbitrary profiles, nonzero offsets, selections, animation, and per-point path pressure are outside the supported painting path. Preview output-profile/alpha conversion follows Krita's thumbnail API and is explicitly unspecified; this is not archival color validation.
- Busy-image, tab-close, modal, repeated stop/start, and simultaneous-instance races need broader **live** stress coverage. Fake-host checks cannot establish all native lifetime behavior.
- No current-session self-test runs automatically. AI Diffusion observation is verified on the pinned development source; generation, cancellation, and result application are not implemented or tested. No generation backend is configured.
- The bridge cannot forcibly interrupt a running native stroke or guarantee a hard deadline for synchronous Krita calls. It retains the execution gate while awaiting native completion.
- File checks do not provide race-proof filesystem isolation from another process running as the same user. At-most-once dispatch applies within a live plugin instance, not across a Krita crash.
- Layer creation has no promised undo grouping. The implementation provides native stroke undo metadata but no MCP undo tool. Per-tool output schemas and automatic plugin installation remain future work.
