# Contributing

Bug reports, small fixes, documentation, and evidence from additional Krita builds are welcome. Check existing issues and the [implementation plan](docs/implementation-plan.md) before starting a larger feature. Describe the intended workflow in an issue when an architectural change would benefit from discussion.

Read [AGENTS.md](AGENTS.md), the [design](docs/design.md), the [bridge contract](docs/bridge-contract.md), and the [validation record](docs/validation.md) before changing behavior. AGENTS.md is the canonical repository guidance for people and coding agents.

## Development setup

Use external Python 3.10 or newer and [uv](https://docs.astral.sh/uv/). Keep this environment separate from Krita's embedded Python:

```bash
uv sync --locked
uv run --no-sync pytest -q tests/unit tests/integration
uv run --no-sync ruff check plugin src tools tests
uv run --no-sync ruff format --check plugin src tools tests
uv build --no-sources
uv run --no-sync python tools/build_plugin.py
```

The tests need local loopback sockets. They do not require Krita or PyQt6. CI runs these checks on Linux with Python 3.10, 3.12, and 3.14; that matrix validates the external runtime and protocol, not native Krita compatibility. Run `uv run ruff format plugin src tools tests` when formatting Python changes. Intentional dependency changes should update `pyproject.toml` and `uv.lock` together; ordinary setup uses the checked-in lockfile.

## Where changes belong

| Location | Responsibility |
| --- | --- |
| `src/krita6_mcp/` | External MCP tools, discovery client, and CLI |
| `plugin/krita6_bridge/` | Dependency-free protocol/transport/ledger and GUI-thread host integration |
| `tests/unit/`, `tests/integration/` | Protocol, fake-host guards, and real MCP stdio tests |
| `tests/host/`, `tools/` | Explicit live-host fixtures, probes, and packaging |
| `docs/` | Design decisions, contracts, supported limits, and sanitized validation evidence |

The plugin may use only Python's standard library, Krita, and PyQt6. All Krita API calls and wrapper access belong on the GUI thread. Network workers exchange plain data. Do not install the MCP runtime or another Qt binding into Krita.

Keep commands typed, bounded, and explicit about their document/layer targets. Do not add caller-supplied Python, shell commands, or an arbitrary Krita action tool. Preserve operation IDs across retries: a timeout after dispatch does not prove that nothing changed. Changes to admission, cancellation, or result retention need regression coverage for duplicate IDs, conflicting payloads, and cancellation before dispatch.

## Native validation

Native painting, completion, undo, color handling, and optional plugin compatibility require live-host evidence. Use the isolated Linux harnesses described in the [README](README.md#development-and-verification):

```bash
uv run python tools/probe_krita.py
uv run python tools/smoke_krita.py
```

They isolate XDG profiles and `TMPDIR`, create a private display/session, and use scratch documents. Never run host tests against personal artwork or a normal Krita profile. For AI Diffusion observation changes, follow the pinned-source procedure in the README and run `tools/probe_diffusion.py`; its synthetic jobs do not establish backend generation support.

Record the exact Krita, Qt/PyQt, Python, platform, and relevant plugin/preset versions with the checks performed. State what remains untested. Keep generated reports and profiles out of Git unless a small, deliberately sanitized artifact is needed in `docs/validation/`. Never commit tokens, discovery files, personal artwork, environments, build output, or raw profile logs.

## Pull requests

Keep a pull request focused on one coherent change. Explain the concrete problem and resulting behavior, include relevant validation, and update the contract/capability documentation when behavior changes. Add meaningful regression tests for bugs and protocol changes; avoid tests that merely duplicate implementation details. Check the diff for unrelated formatting and generated files before submitting.

For native or optional plugin changes, separate source inspection, mocked checks, and actual host results in the evidence. Do not advertise unsupported milestones or add tools that pretend an unfinished feature works. If validation cannot run in your environment, say so in the pull request.

CI actions are pinned to immutable upstream commits. When updating an action, verify the commit against its upstream release, retain a readable version comment, and review changed permissions or inputs. The workflow has read-only repository permissions and performs no publishing.

Contributions are made under this project's [MIT license](LICENSE). No separate contributor license agreement is required. Only submit code and assets that you have the right to contribute; retain any required third-party attribution.
