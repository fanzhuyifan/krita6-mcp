# Implementation plan

Draft · 2026-09-06 · All milestones are pending.

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

## 3. Useful document editing and distribution

Add bounded open-file support, layer visibility/name/opacity, native shapes, and crop previews after individual host validation. Include backup-preserving plugin installation, upgrade/uninstall instructions, and Windows/macOS discovery paths. Leave plugin enablement visible to the user. Keep installer behavior separate from MCP runtime behavior.

**Gate:** the same host smoke workflow passes on each advertised platform/build. Test paths with spaces/non-ASCII, multiple Krita instances, port reuse, token rotation, plugin/server version skew, and missing Python plugin packages. Publish a compatibility matrix with tested versions and retained evidence, not a blanket “Krita 6 compatible” badge.

## Later, only when justified

- Continuous strokes with per-point pressure/tilt/time: investigate a public API extension; separate line segments are not equivalent.
- Selection editing, pixel imports, filters, transforms, additional color spaces: specify mutation and undo semantics per operation.
- Safe programmatic undo or grouped edits: requires reliable ownership/history evidence or an upstream undo API.
- Animation, vector/text editing, large-document tiling, remote transport: separate capability work after the local loop is dependable.

## Proposed repository layout

```text
pyproject.toml
uv.lock
src/krita6_mcp/
  server.py                 # MCP registration and lifespan
  models.py                 # Typed tool inputs/results
  bridge_client.py          # Discovery, HTTP, polling, retry policy
  cli.py                    # doctor, server, explicit smoke workflow
plugin/
  krita6_bridge.desktop
  krita6_bridge/
    __init__.py
    extension.py            # Lifetime and GUI controls
    transport.py            # Authenticated bounded HTTP
    executor.py             # GUI queue and completion checks
    operations.py           # Admission, cancellation, deduplication
    registry.py             # Live document/node/preset identities
    commands/               # Fixed Krita operation implementations
    imaging.py              # Preview and managed color handling
protocol/
  v1/                       # Schemas and shared valid/invalid fixtures
tests/
  unit/                     # Pure state transitions and validation
  integration/              # Bridge plus fake host, real stdio MCP
  host/                     # Explicit live-Krita scratch workflows
tools/
  build_plugin.py
docs/
```

The layout is a proposal; these implementation files do not yet exist. Generate or validate both sides' command contracts from the same fixtures to prevent the dependency-free plugin validators from drifting from the external typed models.

## First engineering decision after the spike

Keep the proposed two-process architecture if native painting, preview capture, and completion work reliably on Krita 6. Select the exact executor completion strategy from those results. Freeze bridge protocol v1 and the smallest tool schemas only after that evidence exists.
