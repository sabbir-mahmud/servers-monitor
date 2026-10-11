# Security policy

Server Monitor can run commands on the servers it monitors, so security reports are taken
seriously and handled first.

## Reporting a vulnerability

**Please don't open a public issue, pull request or discussion for a vulnerability.**

Report it privately through GitHub:
**[Security → Report a vulnerability](https://github.com/sabbir-mahmud/servers-monitor/security/advisories/new)**.

Please include:

- what an attacker can do, and what access they need to start (none, a Viewer account, a
  monitored server, Redis access…),
- steps to reproduce, or a proof of concept,
- the affected version (dashboard commit and/or agent version from `smctl status`).

## What to expect

- An acknowledgement within **3 working days**.
- An assessment and, if it's confirmed, a fix plan within **10 working days**.
- Credit in the release notes and the advisory, unless you'd rather stay anonymous.

Please give a reasonable window (normally up to 90 days) for a fix to be released before
disclosing publicly.

## Supported versions

Security fixes are made on the `main` branch and the most recent release tag. Upgrade the
dashboard, then upgrade agents with the verified command on each server's page.

## Scope

In scope: the dashboard (`main/`), the agent and `smctl` (`agent/`), the installer, and the
relay protocol, including anything that breaks the guarantees in
[docs/security.md](docs/security.md).

Out of scope: vulnerabilities in third-party services (your Redis provider, Telegram,
GitHub), attacks that need an already-compromised dashboard machine or root on a monitored
server (beyond what the security model claims to prevent), and missing hardening on
deployments that ignore the [hardening checklist](docs/security.md#hardening-checklist).
