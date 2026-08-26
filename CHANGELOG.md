# Changelog

Notable changes to `agent-usage-manager` are recorded here. GitHub Releases
remain the source for release artifacts.

## Unreleased

### Changed

- Reworked the README into a concise front door and moved operator detail into
  `docs/reference.md`.
- Added a security policy, private-reporting path, contribution guide, and
  privacy-aware bug report form.
- Added current changelog, documentation, and security links to package metadata.
- Improved the dashboard at mid-width and phone sizes; clipboard denial now
  fails visibly and leaves the stop command selected for manual copy.

### Security and privacy

- Stopped tracking the maintainer's operational `agents.yaml`. The current
  source tree keeps that file local and Git-ignored while future releases
  continue to ship only the sanitized `agents.default.yaml`.
- Removed identifying dashboard captures from the current source tree.

## 0.2.6 - 2026-08-22

### Fixed

- The wheel and sdist now exclude the maintainer's operational configuration
  and ship `agents.default.yaml` as the clean fallback.
- Removed a maintainer-specific trusted hostname from the DNS-rebinding guard.
  Proxy hostnames are opt-in through `AUM_TRUSTED_HOSTS`.

For earlier history, see
[GitHub Releases](https://github.com/minglong51/agent-usage-manager/releases).

[Unreleased]: https://github.com/minglong51/agent-usage-manager/compare/v0.2.6...HEAD
[0.2.6]: https://github.com/minglong51/agent-usage-manager/releases/tag/v0.2.6
