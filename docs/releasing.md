# Maintainer release procedure

The source repository is [fanzhuyifan/krita6-mcp](https://github.com/fanzhuyifan/krita6-mcp), with default branch `master`. Publishing source does not automatically publish a package or GitHub release. The first version is still an alpha; mark releases as prereleases while these compatibility limits remain.

## Prepare a version

1. Start from a clean `master` checkout and review the complete diff and recent history.
2. Update the version together in `pyproject.toml`, `src/krita6_mcp/__init__.py`, `src/krita6_mcp/server.py`, and `plugin/krita6_bridge/protocol.py`. The ZIP builder derives its filename from the plugin version. Change bridge protocol major only for incompatible wire changes.
3. Refresh `uv.lock` if project metadata/dependencies require it. Record changes, fixes, and limitations in `CHANGELOG.md`; do not describe untested capabilities as supported.
4. Update README examples and the compatibility evidence for the release. Check all issue/documentation/release links point to the intended repository and branch.

## Validate

```bash
uv sync --locked
uv run ruff check plugin src tools tests
uv run ruff format --check plugin src tools tests
uv run pytest -q
uv build --no-sources
uv run python tools/build_plugin.py
```

CI repeats non-GUI checks on Python 3.10, 3.12, and 3.14. Wait for those jobs on the actual commit; local success alone does not prove the GitHub workflow passed. Check the wheel's license metadata and contents, and ensure neither wheel nor source archive contains private profiles, secrets, downloaded upstream plugins, environments, or artwork outside the deliberate small validation fixtures.

On each advertised host/build, run `tools/probe_krita.py`, `tools/smoke_krita.py`, and `tools/probe_plugin_import.py` as described in the README. These use scratch resources. The ZIP must include an explicit `krita6_bridge/` directory entry, its `.desktop` file, runtime modules, manual, and MIT license. The importer probe uses the installed Krita importer; it does not modify a normal profile. Retain sanitized evidence and disclose any gates not run.

Changes to AI Diffusion observation also require its pinned development-plugin probe. Generation remains out of scope until an actual backend workflow and ownership semantics have been validated. Do not count synthetic jobs as generation evidence.

## Publish a release

1. Commit the release preparation with focused history and confirm `git status --short` is empty. Push the reviewed commit to `master`; do not force-push existing published history.
2. Tag that exact verified commit with `v` plus its version, then push the tag.
3. Create a GitHub prerelease from the tag. Use the changelog and exact tested platform/build as its release notes.
4. Attach the matching `dist/krita6-bridge-VERSION.zip`, Python wheel, and source archive. Publish SHA-256 checksums for those files. Build artifacts belong on the release, not in Git.
5. Download the published ZIP and verify its checksum/import behavior before announcing the release. Never substitute a later build under an existing version silently.

PyPI publishing is separate and is not automated here. Before enabling it, establish package ownership, configure narrowly scoped trusted publishing, and test package installation from a built wheel. Do not add a long-lived publishing token to CI.

## Repository settings

Keep `master` as the default branch and enable issues and private vulnerability reporting. Where the account supports it, protect `master` with the CI matrix checks, prevent force pushes, and use pull requests for routine contributions. Avoid requiring reviews from nonexistent additional maintainers; choose a policy that allows security fixes to ship. Repository settings should be verified in GitHub; files alone do not enable these controls.

No automated releases, deployments, package uploads, or paid services run from the current CI workflow.
