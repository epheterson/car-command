# Security

Car Command holds where people's cars have been and the credentials to reach them.

## Reporting a vulnerability

Report it privately: **[Security › Report a vulnerability](https://github.com/epheterson/car-command/security/advisories/new)**. Please don't open a public issue, discussion or pull request for it.

Say what you found, how to reproduce it, and what an attacker could reach. You'll get credit in the advisory and the changelog unless you'd rather not.

## Supported versions

The latest release. Fixes are not backported; upgrading is a `docker pull` (under compose, a `git pull` and a rebuild), and the database upgrades in place.

## In scope

- The site (`stack/web`): sign-in, API tokens, cross-site request checks, and anything that shows one car's data to a token scoped to another.
- How credentials are kept: the sealed sources, `stack/.env`, `stack/secrets/`.
- Anything that sends a position, or other data about where a car has been, somewhere the owner did not turn on.
- The Tesla stream's internet-facing containers (`stack/tesla`): anything reachable through them beyond the public key and the receiver.

Vulnerabilities in CarConnectivity, its connectors, Postgres or Tesla's fleet-telemetry belong with those projects; tell us too if Car Command's use of them makes it worse.
