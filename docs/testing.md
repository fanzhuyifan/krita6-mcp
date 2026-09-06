# Testing

The [development setup](../CONTRIBUTING.md#development-setup) covers dependencies, automated checks, and builds. Tests use local loopback sockets, so the runner needs local-network permission. CI runs the non-GUI suite and distribution builds on Python 3.10, 3.12, and 3.14; it does not run Krita or establish native-host support.

Native painting, completion, undo, color handling, and optional plugin compatibility require live-host evidence. The [validation record](validation.md) separates these results from unit and fake-host coverage.

## Live Krita checks

On Linux, install `krita`, `Xvfb`/`xvfb-run`, `xauth`, and `dbus-run-session`. Run the checks from the repository root, after `uv sync --locked`, in a shell without an activated Python environment. Invoke the checkout's Python directly: `uv run` prepends its environment to `PATH`, which can cause Krita's embedded Python to use that environment and fail to find the system PyQt6 bindings.

```bash
.venv/bin/python tools/probe_krita.py
.venv/bin/python tools/smoke_krita.py
.venv/bin/python tools/probe_editing.py
.venv/bin/python tools/probe_general_editing.py
.venv/bin/python tools/probe_plugin_import.py
```

The native, MCP smoke, and editing harnesses create disposable XDG profiles, a virtual X display, a private D-Bus session, and an isolated `TMPDIR`. The private temporary socket directory prevents Krita's single-instance mechanism from routing test work to an already-open personal session. They use scratch documents and do not install into the normal Krita profile. Never run host tests against personal artwork or a normal profile.

- `probe_krita.py` checks native pixels, resource capture, undo/redo, preview state, and `.kra` round-trip through a fixed test-only plugin.
- `smoke_krita.py` drives the production plugin through real MCP stdio.
- `probe_editing.py` drives all eleven reference-editing additions through real MCP. An independent test-only GUI fixture creates small color images and observes actual document/layer pixels, selection masks, and PNG previews. It exercises activation, selection replacement/clearing, Bézier painting and one ordinary undo/redo, import/open, copying/reordering, opacity/visibility, affine transforms, duplicate IDs, and rejected paths/bounds. It also injects fixed view/layer initialization failures around real native creation to verify owning-wrapper lifetime through garbage collection, partial recovery handles, duplicate retries, later view attachment, and cleanup. It never uses the normal discovery directory or an already-open Krita instance.
- `probe_plugin_import.py` uses Krita's installed importer to extract the generated ZIP into a temporary resource directory and verifies the installed modules, license, and manual. It does not exercise native painting. Pass `--importer /path/to/plugin_importer.py` if the module is installed elsewhere; this executes the selected importer, so use a trusted Krita installation.

The native and MCP harnesses print the location of their reports and test artwork. Pass `--output /path/to/new-artifact-directory` to select a new location. Keep generated reports, profiles, and logs outside Git unless deliberately sanitized for validation evidence.

For a system Krita build, clear inherited KDE development overrides such as `PYTHONPATH`, `LD_LIBRARY_PATH`, and `QT_PLUGIN_PATH` if they point to another build. Pass `--krita /usr/bin/krita` to select the installed binary explicitly.

## AI Diffusion observation

Use a checkout of upstream development commit `dda58d1c63e361207ccec085efbc34dbd32f1654`, including its `ai_diffusion/websockets` submodule. Stable v1.53.0 targets Krita 5; the pinned Qt6 source also reports 1.53.0, so the version string alone is insufficient. See the [integration record](diffusion-integration.md) for the pinned interfaces and [live evidence](validation.md#ai-diffusion-readers) for the tested configuration.

Use the same Linux prerequisites and clean host-test environment described above:

```bash
.venv/bin/python tools/probe_diffusion.py --source /absolute/path/to/pinned/krita-ai-diffusion
```

The probe verifies source fingerprints, loads the real plugin in an isolated profile, and inserts synthetic job records into its actual queue for inspection. Automatic updates are disabled and cloud mode has an empty token, which the pinned plugin handles without creating a backend client. No images are generated. After verifying observation, this probe configures persistent settings, root/regional controls, and region create/update/remove through real MCP. It independently checks preserved pixels/layers/selection/jobs, duplicate IDs, and native settings. It proves configuration behavior on the pinned add-on, not generation or control-model backend compatibility.

## Recording results

Record the exact Krita, Qt/PyQt, Python, platform, and relevant plugin/preset versions with the checks performed. State what remains untested. Retain only small, deliberately sanitized evidence in `docs/validation/`; never commit tokens, discovery files, personal artwork, environments, generated build artifacts, or raw profile logs.

## AI Diffusion generation

This probe also verifies persistent selection of a supported style through MCP before recording its native generation baseline.

The generation probe requires the pinned Qt6 add-on plus an installed managed ComfyUI server with `dreamshaper_8.safetensors` and the add-on’s required models/custom nodes. It does not download or install models. Use trusted local source and server paths:

```bash
.venv/bin/python tools/probe_diffusion_generation.py \
  --source /absolute/path/to/krita/pykrita \
  --server /absolute/path/to/ai_diffusion/server \
  --krita /usr/bin/krita
```

On an Arch installation, the add-on parent may be `/usr/share/krita/pykrita`; the managed backend is normally under the user’s Krita data directory. Pass `--check-backend` to verify isolated backend startup/model discovery without generating images. Use a fresh `--output` directory for each run.

The harness copies backend code and custom nodes into a scratch directory, reuses installed model weights, and starts its own loopback server with fresh database/cache/input/output directories. Its Krita profile connects to that scratch server. API nodes and online model downloads are disabled. Test-owned process groups are stopped afterward; the normal Krita profile and server configuration are not used.

The full probe requests a 512×512 image, inspects it, and explicitly applies it; a second request refines a rectangular selection. It checks duplicate submission/application IDs, unchanged pixels/layers before application even with upstream automatic apply enabled, restored prompt/style/settings, selection preservation, native completion, and application undo/redo. Generated images and raw profiles stay in the scratch output directory. Passing this fixture does not establish every regional/control configuration or model family.

## General editing

`probe_general_editing.py` reuses the isolated profile/display/scratch-document infrastructure and independent native snapshot fixture. It drives the added MCP tools, verifies exact pixels, selection masks and layer PNGs, checks native shape undo/redo and brush restoration, and repeats every mutation ID. It exercises selection combinations/refinement, transparency masks, groups and cycle rejection, alpha locks/compositing/merge, and canvas flip/rotation/crop/resize/scale. Never point it at a normal Krita session. Retain sanitized evidence only after a complete passing run.
