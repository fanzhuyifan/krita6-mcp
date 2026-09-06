# Existing implementations and Krita 6 evidence

Inspected 2026-09-06. Sources are project repositories, official Krita/Qt documentation, installed package files, and the official MCP SDK. No third-party implementation was installed or executed. Repository observations below are branch-tip observations; commit SHAs were not captured. Recheck and pin a revision before adopting code.

## What the investigation establishes

Several inspected Krita MCP plugins still import PyQt5 directly, and published host validation often targets Krita 5.x. This supports building and testing specifically for PyQt6. It does **not** establish that no working Krita 6 MCP implementation exists: the search is not exhaustive, and some sources could not be verified.

Krita 6 shares the 5.3 codebase while moving to Qt6. Its native painting API changes what a new bridge can offer: an assistant can use real Krita brushes without relying entirely on custom pixel renderers or simulated mouse input. [Official release notes](https://krita.org/en/release-notes/krita-5-3-release-notes/)

## Projects worth learning from

| Project | Observed structure / Qt evidence | Ideas to adopt | Limits relevant to this design |
| --- | --- | --- | --- |
| [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp) | External FastMCP server → HTTP plugin → GUI timer queue; plugin directly imports PyQt5 | Simple two-process split; visible canvas feedback | Custom BGRA circle renderer rather than native brush strokes; queue timeout does not remove pending work |
| [edithatogo/krita-cli](https://github.com/edithatogo/krita-cli) | MCP server, CLI, typed Python client, expanded plugin; direct PyQt5 imports | Shared typed client, diagnostics, API coverage inventory, resource bounds | More features do not resolve Qt6 compatibility; timeout ambiguity remains |
| [SanSaSane/krita-mcp](https://github.com/SanSaSane/krita-mcp) | Stdlib MCP adapter, authenticated HTTP bridge, queued Qt signal; direct PyQt5 import and Qt5 enum | Discovery, lifecycle controls, calibrated previews, live selftest and explicit limitations | Published target is Krita 5.3.3; timed-out queued calls can still run; arbitrary Python tool broadens authority |
| [buttonscodes/painter](https://github.com/buttonscodes/painter) | Native painting bridge with direct PyQt5 imports; source reports testing 5.3.2.1 | Native brush API, crop previews with coordinate metadata, instance discovery | A path is implemented as many independent `paintLine` calls; timeout does not cancel queued work |
| [dcc-mcp/dcc-mcp-krita](https://github.com/dcc-mcp/dcc-mcp-krita) | Document/layer adapter with authenticated JSON-lines bridge; Qt imports not independently verified | Bounded queue, cancelled unstarted commands, scoped IDs, narrow commands, save/export separation, staged installer | Documented catalog excludes native freehand strokes; live Krita 6 coverage not established by this review |

### nanayax3: useful baseline, misleading brush expectations to avoid

The [plugin source](https://raw.githubusercontent.com/nanayax3/krita-mcp/master/krita-plugin/kritamcp/__init__.py) imports PyQt5 and schedules commands through a GUI `QTimer`. Stroke rendering writes custom circles into BGRA buffers with `setPixelData()`. Selecting a brush preset therefore does not make these strokes use that preset. The HTTP handler lacks authentication and a request-body bound in the inspected source; a queue timeout leaves work pending. The project advertises a longer export timeout, which addresses waiting duration but not whether timed-out commands execute later. The root did not list a test suite. License: MIT, as published by the project.

Adopt the small bridge architecture, but define brush behavior and operation outcomes precisely.

### edithatogo: reusable client and operational coverage

The [plugin](https://raw.githubusercontent.com/edithatogo/krita-cli/master/krita-plugin/kritamcp/__init__.py) adds validation, limits, logging, more operations, and optional NumPy rendering while retaining direct PyQt5 imports. Its POST path remains unauthenticated in the inspected source, and `get_result()` can stop waiting without cancelling the command. The [typed client](https://raw.githubusercontent.com/edithatogo/krita-cli/master/src/krita_client/client.py) centralizes transport and typed methods; its generic call path has weaker validation. The README describes mock end-to-end tests and coverage documentation; these were not run. License: MIT.

Adopt a common client/diagnostic layer and contract fixtures. Classify uncertain completion separately from an ordinary retryable connection failure.

### SanSaSane: lifecycle, testing, and explicit caveats

The [README](https://github.com/SanSaSane/krita-mcp) reports Windows/Krita 5.3.3 validation, 77 end-to-end checks, a selftest, and a close-path stress test. It describes token/port discovery, inline PNG feedback, and direct-drawing undo limitations. These are project-reported results, not tests reproduced here. The bridge also exposes arbitrary Python execution, which is unnecessary for the proposed initial tool surface. License: CC0 according to its project documentation.

The [dispatcher](https://raw.githubusercontent.com/SanSaSane/krita-mcp/main/pykrita/krita_mcp/mainthread.py) uses a queued signal and guards against reentrancy, but imports `PyQt5.QtCore` and uses `Qt.QueuedConnection`. Timeout ends waiting without a deadline/cancellation check in the executor. Converting exceptions to plain error data is useful: retaining traceback frames can also retain Krita wrappers. Adopt these lifecycle/testing ideas while implementing a stronger operation state machine.

### Painter: native brushes and interpretable previews

The [bridge source](https://raw.githubusercontent.com/buttonscodes/painter/main/bridge/painterbridge/__init__.py) uses native painting methods, presets, flow, opacity, and endpoint pressure. It also returns preview geometry so an assistant can map observed pixels back to document coordinates. It directly imports PyQt5, and its segmented stroke implementation calls `paintLine` once per segment. Native source inspection below explains why this should not be described as one continuous stroke.

The [project](https://github.com/buttonscodes/painter) documents discovery and journals. No project license was found in the inspected root; treat it as an ideas reference and do not copy its implementation without establishing permission. Automatic instance spawning and a model-specific critique CLI are beyond the initial scope.

### DCC-MCP: closest reliability reference

The [architecture](https://github.com/dcc-mcp/dcc-mcp-krita/blob/main/docs/architecture.md) documents authenticated loopback JSON-lines, a bounded GUI queue, cancellation before execution, opaque document IDs, re-resolved node UUIDs, restricted file operations, verified exports, and backup-preserving installation. This is a useful reference for the operational contract. Its [README](https://github.com/dcc-mcp/dcc-mcp-krita) describes 16 document/layer/fill/persistence tools and excludes native freehand painting. License: MIT.

Plugin source retrieval was incomplete, so neither actual Qt bindings nor host-test coverage is established here. The proposed design adds native brush primitives and explicit handling of already-running work, duplicate mutation IDs, and result retention.

### Additional architecture to avoid copying

[halby24/KritaMCP](https://github.com/halby24/KritaMCP) has a [threaded SSE client](https://raw.githubusercontent.com/halby24/KritaMCP/main/krita-plugin/kritamcp/mcp_client.py) that invokes the [Krita action executor](https://raw.githubusercontent.com/halby24/KritaMCP/main/krita-plugin/kritamcp/krita_actions.py) directly, including caller-supplied Python evaluation/execution. The inspected path has no GUI-thread handoff. Its [docker](https://raw.githubusercontent.com/halby24/KritaMCP/main/krita-plugin/kritamcp/docker_widget.py) imports PyQt5 and defaults a host input to `0.0.0.0`. These are source observations, not a runtime reproduction. The proposed bridge instead keeps Krita calls on the GUI thread and exposes fixed typed operations over loopback.

## Authoritative API evidence

### PyQt6 and installed build

Krita's [migration announcement](https://krita.org/en/posts/2025/monthly-update-25/) says built-in plugins were updated for PyQt6 and user plugins need author changes. The [6.0.3 build configuration](https://raw.githubusercontent.com/KDE/krita/v6.0.3/plugins/extensions/pykrita/CMakeLists.txt) makes Python-plugin support conditional on matching dependencies. Check actual plugin loading, not just the application's version.

Read-only inspection of this machine found:

```text
pacman -Q krita python-pyqt6
krita 6.0.3-2
python-pyqt6 6.11.0-3
```

- `/usr/lib/krita-python-libs/krita/__init__.py` selects the matching PyQt major and rejects the wrong one.
- `/usr/lib/krita-python-libs/PyKrita/krita.pyi` exposes `paintPath`, `paintLine`, shape painting, node UUIDs, document lookup, previews, and save/export.
- `/usr/share/krita/pykrita/assignprofiledialog/assignprofiledialog.py` demonstrates migrated scoped enums and Qt6 methods.

An attempted `krita --version` command did not return and was interrupted. No plugin was enabled, installed, or live-tested during this design work. Package metadata and stubs establish a promising validation target only.

### Painting requires the right live view

[Node declarations](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/Node.h) expose paths, lines with endpoint pressures, and shapes. Paths lack per-point pressure/time/tilt parameters. C++ declarations use floating point line coordinates, while the installed Python stub declares `QPoint`; test the binding before promising fractional line coordinates.

The [6.0.3 implementation](https://raw.githubusercontent.com/KDE/krita/v6.0.3/libs/libkis/Node.cpp) and [painting resources helper](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/PaintingResources.cpp) draw on active-view resources. `paintAbility()` dereferences the current view without a complete null guard, and unpaintable targets can cause void painting methods to do nothing. Guard active window/view, document ownership, layer type/locks, selection, and brush state before calling. The [View API](https://api.kde.org/legacy/krita/html/classView.html) supplies preset, size, opacity, and managed foreground color.

### Undo and completion are specific to each operation

The [figure-painting helper](https://raw.githubusercontent.com/KDE/krita/master/libs/ui/tool/kis_figure_painting_tool_helper.cpp) starts a native stroke and ends it after submitting jobs. The [stroke strategy](https://raw.githubusercontent.com/KDE/krita/master/libs/ui/tool/strokes/kis_painter_based_stroke_strategy.cpp) commits its undo command on completion. Each `paintLine` invocation creates a separate helper/stroke; segmented pressure lines are not one stroke or one undo unit.

By contrast, [raw pixel writing](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/Node.cpp) directly writes bytes and does not establish an undo transaction. The inspected [Document interface](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/Document.h) and local stubs contain no general undo-macro API. Test undo behavior per operation instead of promising universal rollback.

[Document implementation](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/Document.cpp) shows that projection refresh and waiting can block, while native painting submits asynchronous work. The design deliberately leaves the precise bounded completion strategy to a live-host spike.

### Identities and image representation

Use `Node.uniqueId()` with `Document.nodeByUniqueID()` and plugin-issued document handles. Do not key document identity by Python wrapper identity: wrappers can be recreated. [Notifier source](https://raw.githubusercontent.com/KDE/krita/master/libs/libkis/Notifier.cpp) also creates temporary wrapper arguments for some signals and deletes them after emission, so retaining callback arguments is unsafe.

The [Document API](https://api.kde.org/legacy/krita/html/classDocument.html) supplies thumbnails/projections and export. The [Node API](https://api.kde.org/legacy/krita/html/classNode.html) documents pixel layouts that depend on model/depth; integer RGBA buffers use BGRA order, while floating point data differs. Use encoded previews with explicit geometry, and verify managed color behavior rather than treating every byte buffer as RGBA8.

## Design synthesis

Adopt the common two-process split, DCC-MCP's narrow command/identity discipline, SanSaSane's host testing and lifecycle diagnostics, and Painter's native painting and preview coordinates. Implement our own PyQt6 bridge, operation ledger, and typed contract. No third-party source was copied into this repository.

The main unresolved engineering questions are native stroke completion, brush-state restoration, binding-specific coordinate behavior, and operation-specific undo. The first milestone tests these directly before the interface expands.
