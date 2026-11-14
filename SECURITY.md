# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use GitHub's
**Report a vulnerability** button on this repository's Security tab. Include what an attacker can
do, the steps to reproduce, and the version or commit you tested.

You should get a reply within a week. Callsight is maintained by one person, so fixes may take
longer; you'll be kept informed, and credited in the release notes if you'd like.

## Supported versions

Only the latest release gets security fixes.

## Scope

In scope: the API, the web UI, the worker, authentication and organization separation, the
webhook connector, and the deployment files in this repository.

Out of scope: problems that need an already-compromised server or admin account, and
vulnerabilities in third-party models or dependencies. Report those upstream, but do tell us
if Callsight uses them in an unsafe way.

## Running it safely

See [docs/deployment.md](docs/deployment.md): put it behind TLS, set `CALLSIGHT_SECRET_KEY`, and
keep Postgres and MinIO off the public network.
