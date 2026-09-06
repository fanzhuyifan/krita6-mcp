# Security policy

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** option when available. Include the affected commit/version, platform, reproduction using scratch files, and the effect on authentication, files, documents, or operation identity.

Do not put exploit details, bearer tokens, discovery files, credentials, or private artwork in a public issue. If private reporting is unavailable, open an issue asking the maintainer for a private reporting channel without disclosing the vulnerability. Ordinary bugs belong in the bug-report form.

The project is an alpha. Security fixes target the current development branch and latest release; older revisions may require upgrading. There is no guaranteed response time or independently audited security certification.

## Trust boundary

- Enabling the Krita plugin starts a loopback listener. Possession of its private session token authorizes the fixed tool catalog, including edits to open documents and writes under configured output roots.
- HTTP authentication, exact Host checks, Origin rejection, typed commands, and bounded workers/queues limit unintended access. This is a local desktop integration, not an Internet service. Do not expose or tunnel its port to untrusted clients.
- Discovery uses owner-only POSIX permissions. Windows support is disabled until private ACL handling is implemented.
- Output roots and explicit overwrite flags constrain file tools. Path validation is not race-proof isolation against another process running as the same OS user. The bridge runs with Krita's OS permissions.
- A client timeout does not undo an operation. Mutation IDs prevent repeated dispatch during one plugin session; they are not durable across a Krita crash. Inspect uncertain outcomes before issuing a new mutation ID.
- The external MCP runtime is separate from Krita. The plugin exposes no caller-supplied Python, shell, or arbitrary action execution.
- Optional AI Diffusion generation sends prepared canvas content through the already loaded add-on to its connected loopback ComfyUI backend. Cloud/remote clients are rejected; MCP tools do not connect or install backends. Generation updates add-on history; canvas application requires a separate tool. Source fingerprints gate private mutation interfaces.

See [the protocol contract](docs/bridge-contract.md) and [validation limits](docs/validation.md) for details. These controls are implementation claims with documented tests, not a promise that software is free of vulnerabilities.
