# Repository guidance

Build a Krita 6 desktop MCP bridge. Read `docs/design.md` and the current implementation status before changing behavior.

- Keep the external MCP runtime separate from Krita's embedded Python. The plugin uses only the standard library, Krita, and PyQt6. Never install another Qt binding into Krita.
- All Krita API calls and wrapper access belong on the GUI thread. Network workers exchange plain data only.
- Native painting, completion, undo, and color handling require live-host evidence. Mock tests do not establish Krita compatibility. Run host tests only with isolated profiles and scratch documents.
- Keep commands typed and bounded. No caller-supplied Python, shell execution, or arbitrary Krita action tool.
- Preserve operation identities on retries. A timeout after dispatch does not establish that nothing changed. Cancellation before dispatch and duplicate-ID handling need regression tests.
- Never commit tokens, discovery files, personal artwork, local profiles, environments, or generated build artifacts.
- Use focused commits: document intent, implement one coherent change, run relevant checks, then commit. Do not amend unrelated history or include another contributor's unfinished work.
- Update documented behavior and validation evidence alongside implementation. State platform/build limits explicitly.
- Prefer the smallest useful implementation. Do not claim unfinished milestones or add placeholder tools that pretend to work.

Validation commands and host-test instructions belong in the README as they become available. Keep `agent.md` as a pointer to this canonical file.
