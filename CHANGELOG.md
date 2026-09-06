# Changelog

Changes are recorded here before release. Version 0.1.0 is currently unreleased.

## Unreleased — 0.1.0

### Added

- External stdio MCP server and separate PyQt6 plugin for Krita 6.
- Thirteen core tools for discovery, document/layer inspection and creation, native paths and pressure lines, inline PNG previews, layered save, PNG export, and operation reconciliation/cancellation.
- Authenticated loopback transport, private discovery, bounded queues/results, and mutation identities retained for the plugin session.
- Three optional read-only tools for an already loaded Krita AI Diffusion plugin.
- Isolated native and MCP probes with retained Linux/Krita 6.0.3 evidence.
- MIT licensing, contribution/security guidance, reproducible plugin packaging, and a Python CI matrix.

### Fixed during initial development

- Preset handles remain usable after bridge-owned brush setting changes.
- Native completion remains tracked after painting or layer-creation errors.
- Stopped executors release Qt ownership and cached state.
- Expired result bodies preserve recorded mutation outcomes and never trigger replay.
- Interrupted or malformed HTTP error responses retain structured uncertainty and reconciliation information.
- Plugin ZIPs include the explicit directory entry required by Krita's importer, plus the license and manual.

### Changed

- Shared preview-image retrieval between immediate and polled MCP responses, preserving metadata and retrieval errors.
- Removed unused automatic instance selection and redundant command validation in the HTTP route; the ledger still validates before admission.
- Moved output-root configuration and path checks into a standard-library module with direct tests, and removed a redundant host dispatch branch.

### Known limits

- Alpha status; live-host evidence covers Linux/Krita 6.0.3 and one pixel-brush preset on small fixtures.
- Painting is restricted to the documented color space, origin, selection, layer, and brush-engine conditions.
- Windows is unsupported; macOS is untested. Large-document and long-session stress coverage remains limited.
- AI Diffusion generation, cancellation, and result application are not implemented. Its tested Qt6 source is an unreleased development revision.
