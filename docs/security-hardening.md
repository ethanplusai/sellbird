# Hosted-workspace security boundaries

This repository is primarily a self-hosted application. Multi-workspace hosting
requires independent tenant authentication and a correctly populated
`Home` for every request; `build_app` does **not** create tenant
authorization or determine the workspace for a user.

## Secrets

Do **not** put hosted tenants' credentials into the server process environment
or the server's global `.env`. In hosted mode `load_env()` resolves
provider credentials through the active workspace's `Home.secret_backend`,
using service `sellbird` and ref `env:<lowercase environment variable>`.
For example, the value of `GMAIL_CLIENT_SECRET` is stored under
`sellbird / env:gmail_client_secret`. A missing backend results in
empty/unconfigured credentials; no server-global fallback is permitted.

The provided `MemorySecretBackend` is for tests and does not persist keys.
A hosted deployment must inject a persistent, per-tenant authorized backend.
No endpoint in this repository provisions that backend or establishes tenant
identity. Hosted settings endpoints do not write `.env`; local browser OAuth
jobs, local processes, local discovery tasks and their global status are not
available in hosted mode.

Self-hosted installs retain their original environment and .env behavior.

## Credential files

The Gmail, M365 and LinkedIn integrations now write credentials through an
owner-only temporary file with atomic replacement. POSIX data directories are
restricted before publishing. Existing token files or backups with improper
permissions require independent inspection. Windows users must validate NTFS
ACLs: POSIX mode checks do not enforce Windows filesystem isolation.

For server deployments, avoid untrusted writable parent directories and
prefer a managed secret store over local files. This code does not provide
encryption at rest, key rotation or retention/deletion policies.

## Safety-critical reads

Dashboard display queries may tolerate absent data; decisions about whether
someone was already contacted must raise on unknown database state and avoid
presenting unsafe manual-send options.

## Release criteria

- Run the full pytest matrix on Python 3.11 and 3.12 across Linux, macOS,
  and Windows, including a two-tenant HTTP-level isolation harness.
- Check security-audit results, pin and verify third-party CLI installation,
  and review dependency provenance and lockfiles before a production release.
- Validate mail delivery failure recovery using mocked Instantly errors and
  OAuth refresh/revocation flows.
- Verify privacy, retention, opt-out, sending-domain policies, and applicable
  jurisdiction-specific marketing requirements with legal counsel.

These changes are security hardening, not a guarantee that the hosted or
self-hosted product is production secure.
