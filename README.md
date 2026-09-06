# krita6-mcp

Design for an MCP server that lets an assistant inspect and edit a running Krita 6 desktop session, paint with Krita's native brushes, and see the result.

**Status: design draft, 2026-09-06. No server or plugin is implemented yet.**

The proposed system has two parts: a Python MCP server running outside Krita and a small PyQt6 plugin inside Krita. The plugin owns all Krita API access and executes commands on the GUI thread. The external server owns MCP, typed tool schemas, and client integration.

- [Architecture and tool contract](docs/design.md)
- [Existing implementations and API evidence](docs/research.md)
- [Implementation milestones and validation gates](docs/implementation-plan.md)

The first deliverable should prove this loop on an actual Krita 6 build: **create a document → add a paint layer → paint a native path → return a PNG preview → save a layered `.kra` file**. Test undo and interrupted requests before expanding the tool catalog.

Initial target: Linux desktop with Krita's Python plugin support. Local package metadata reports Krita `6.0.3-2` and PyQt6 `6.11.0-3`; this is an available validation target, not a passing compatibility test. Windows and macOS follow after the same smoke workflow passes on their actual builds.
