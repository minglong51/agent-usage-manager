# Security policy

`agent-usage-manager` exposes local process metadata and a stop endpoint. Please
report vulnerabilities privately.

## Supported versions

| Version | Supported |
|---|---|
| Latest PyPI release | Yes |
| Older releases | No |

Security fixes are made against the latest release. Upgrade before reporting a
problem that may already be fixed.

## Report privately

Use
[GitHub private vulnerability reporting](https://github.com/minglong51/agent-usage-manager/security/advisories/new).

Do not open a public issue for:

- bypasses of the process allowlist, protected-process checks, or kill token
- cross-origin, DNS-rebinding, or non-loopback exposure problems
- command-line redaction bypasses
- package contents that expose maintainer or user configuration
- action-log or token-file permission problems

Include the AUM version, operating system, installation method, reproduction
steps, expected impact, and the smallest safe evidence needed to reproduce the
problem.

Do not attach a live `agents.yaml`, kill token, full process command line,
hostname, home-directory path, or unsanitized screenshot. Replace identifying
values before submitting.

## What to expect

This is a personal open-source project maintained on a best-effort basis, with
no response-time SLA. The maintainer will acknowledge a reproducible report,
work toward a fix, and coordinate disclosure when the issue is resolved.

A local user who already has permission to signal the same processes is outside
the threat boundary. Unsafe deployment choices, such as exposing read endpoints
without an authenticated proxy, are also outside the default boundary unless
AUM fails to enforce a documented guard.
