# Implementation plan

Updated 2026-09-06 for initial implementation 0.1.0.

The core native workflow and 16-tool MCP surface are implemented and pass on Linux/Krita 6.0.3. This includes three read-only AI Diffusion tools tested with a pinned Qt6 development plugin. The bridge has automated protocol, transport, ledger, host-guard, diffusion-reader, and real stdio tests. See [validation evidence](validation.md) for exact coverage. The gates below remain the acceptance checklist for broader compatibility; the first successful workflow does not establish every race, brush engine, or platform scenario.

## 0. Prove the host API before building the catalog

Use an isolated Krita profile and disposable documents on the installed Krita 6.0.3 build. Record Krita, Qt, PyQt, embedded Python, OS/display backend, preset identity, and test artifacts.

1. Load a minimal PyQt6 `Extension`, create one GUI executor, and return host versions through an authenticated local bridge.
2. Create a small RGBA/U8/sRGB document with a view and a paint layer. Exercise native `paintPath` and `paintLine` with a bundled preset. Verify pixels changed, endpoint pressure behavior, zoom-independent coordinates, nonzero-origin rejection, explicit eraser/flow/blending/alpha-lock/pressure state, and settings restoration without overwriting intervening user changes.
3. Establish native completion detection. Test `tryBarrierLock`/unlock ordering, a busy image, no active view, the wrong active document, and closure between deferred checks. Request a preview immediately after painting and verify it waits or reports busy. Exercise stop/start draining during native work. Avoid unbounded `waitForDone` as a network timeout strategy.
4. Capture a PNG preview with red/blue/transparency fixtures. Verify channel order, profile conversion, scale metadata, and unchanged filename/modified state from preview capture alone.
5. Undo and redo each native painting primitive through Krita's UI. Check that pixels restore and that multiple line calls are separate operations. Record layer creation/property behavior separately.
6. Save `.kra`, export PNG, reopen the `.kra` in the isolated test session, and verify dimensions, layer structure, and retained artwork. Exercise existing-destination and invalid-path handling.

**Gate:** retained output images and a host capability report demonstrate the complete loop. A stub method, import success, or mocked test is not proof that painting works. If native painting or completion is unreliable, narrow the published capability instead of substituting an undocumented pixel renderer.

## 1. Reliable bridge and observation

Build the versioned protocol, bounded workers/queue, GUI dispatch, instance discovery, authentication, operation ledger, startup/shutdown controls, document/node registry, and read tools. Keep native operations behind a small host interface so queue/protocol tests can use a fake host.

**Gate:** duplicate IDs execute once; conflicting IDs fail; expired/cancelled queued work never executes; an HTTP timeout does not trigger an automatic replay; an adapter restart can reconcile in-flight work. Full queues remain observable; queue rejection leaves an ID unadmitted. Saturated HTTP workers recover within configured deadlines. Inject a failure after mutation and verify the result reports partial/unknown effects accurately. Stale instances/documents/nodes are rejected. Authentication failures never reach the executor. Every host call has a GUI-thread assertion in the integration harness.

## 2. Minimal MCP authoring workflow

Package the external server with the official SDK and add typed tools for create document/layer, list presets, paint path/line, preview, `.kra` save, PNG export, operation lookup and cancellation. Add a matching plugin ZIP and a diagnostic CLI. Return real image content and schema-validated results.

**Gate:** a real stdio client completes create → layer → paint → preview → save. Repeat while switching tabs, interrupting the client, filling the queue, and cancelling immediately before/after dispatch. Verify the error identifies whether the work is queued, running, complete, or unknown. Run the workflow with both current and representative older MCP client behavior supported by the SDK.

## 2a. Optional AI Diffusion observation — implemented

Read loaded plugin status, existing document settings, and bounded job snapshots without creating models, connecting a backend, selecting previews, or generating images. The adapter checks Qt6 objects and its narrow read interfaces. Stable AI Diffusion v1.53.0 is for Krita 5; the tested Qt6 development revision still reports that version. Pin evidence to the exact source, not just the version string.

**Evidence:** the real plugin loads in an isolated Krita 6 profile, the MCP readers return settings and synthetic upstream queue records, and the checked canvas/model/queue state remains unchanged. Native MCP painting/save also passes with AI Diffusion absent. See [integration decisions](diffusion-integration.md).

**Next:** establish a local ComfyUI test backend and disposable workflow before exposing generation. Retain bridge-to-plugin job identities across admission and disconnects. Define how automatic preview/application and broad upstream cancellation interact with bridge-owned work, then verify targeted result application, color, completion, and undo. No backend is currently configured; synthetic queue tests do not satisfy these mutation gates.

## 3. Useful document editing and distribution

Add bounded open-file support, layer visibility/name/opacity, native shapes, and crop previews after individual host validation. Include backup-preserving plugin installation, upgrade/uninstall instructions, and Windows/macOS discovery paths. Leave plugin enablement visible to the user. Keep installer behavior separate from MCP runtime behavior.

**Gate:** the same host smoke workflow passes on each advertised platform/build. Test paths with spaces/non-ASCII, multiple Krita instances, port reuse, token rotation, plugin/server version skew, and missing Python plugin packages. Publish a compatibility matrix with tested versions and retained evidence, not a blanket “Krita 6 compatible” badge.

## Later, only when justified

- Continuous strokes with per-point pressure/tilt/time: investigate a public API extension; separate line segments are not equivalent.
- Selection editing, pixel imports, filters, transforms, additional color spaces: specify mutation and undo semantics per operation.
- Safe programmatic undo or grouped edits: requires reliable ownership/history evidence or an upstream undo API.
- Animation, vector/text editing, large-document tiling, remote transport: separate capability work after the local loop is dependable.

## Current repository layout

```text
pyproject.toml
uv.lock
src/krita6_mcp/
  server.py                 # MCP registration and lifespan
  bridge_client.py          # Discovery, HTTP, polling, retry policy
  cli.py                    # doctor and stdio server
plugin/
  krita6_bridge.desktop
  krita6_bridge/
    __init__.py
    extension.py            # Lifetime and GUI controls
    transport.py            # Authenticated bounded HTTP
    executor.py             # GUI queue and completion checks
    operations.py           # Admission, cancellation, deduplication
    discovery.py            # POSIX state files and session discovery
    protocol.py             # Shared strict command validation
    host.py                 # Live identities, commands, painting, preview
    diffusion.py            # Optional observation of already loaded Qt6 plugin
tests/
  unit/                     # Pure state transitions and validation
  integration/              # Bridge plus fake host, real stdio MCP
  host/                     # Explicit live-Krita scratch workflows
tools/
  build_plugin.py
  probe_krita.py             # Isolated native API proof
  smoke_krita.py             # Production plugin through real MCP
  probe_diffusion.py         # Pinned upstream plugin, synthetic jobs, real MCP reads
docs/
  validation.md
  validation/               # Sanitized reports and small test PNGs
```

The external wheel also includes the dependency-free bridge modules; importing them does not load Qt outside Krita. The plugin ZIP contains no MCP dependency or test harness. MCP input models and the strict shared validator both validate commands. Per-tool output schemas, installation automation, and advanced editing remain future work.

## Decisions established by the first implementation

Keep the two-process architecture. A GUI timer polls nonblocking image barriers and retains the dispatch gate across native work. Native paths and lines produce real pixels and separate undo entries on the reference build; immediate foreground restoration does not alter the submitted stroke. Use Krita's bounded thumbnail API for feedback with explicit unspecified output-profile metadata. Krita's preset XML changes under bridge-owned setting updates, so synchronously refresh that handle fingerprint after restoring the originating view.

Next core validation priorities are busy-image and tab-closure races, repeated stop/start under live native work, multiple simultaneous Krita instances, alternate DPI/zoom configurations, and additional presets. Windows ACL support and macOS host validation require separate work. AI Diffusion observation is implemented; generation, cancellation, and application remain gated on the backend and ownership work above.
