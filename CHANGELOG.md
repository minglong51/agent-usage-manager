# Changelog

Notable changes to `agent-usage-manager` are recorded here. GitHub Releases
remain the source for release artifacts.

## Unreleased

## 0.3.0 - 2026-09-13

### Added

- Compact resource triage with CPU/memory sorting, runtime and warning filters,
  process search, and an optional inspector on desktop and phone layouts.
- Measured warning evidence and CPU/RSS history. Saved warning links preserve
  the first observation after clearance, with at most 100 warnings retained
  in memory for up to 48 hours; restart clears them.
- Structured alert fields for a plain-language title, exact process identity,
  and an optional inspection URL configured with `alerts.dashboard_url`.
- Reviewed process-tree revisions and explicit captured, signaled, skipped,
  stopped, and surviving process identities in stop results and the action log.

### Changed

- API v2 uses one shared three-second sampler, reports stale collection as
  unavailable, and exposes runtime, instance, warning, and delivery metadata.
- Actionable stop requests require the exact `create_time` from the displayed
  row. The dashboard also checks the reviewed tree revision before signaling.
- Stop review stays bound to the child details actually displayed, including
  while text selection defers a refresh; changed or unavailable scope disables it.
- Failed alert commands retry while the condition persists, with visible
  delivery state and a bounded three-attempt limit.
- Runtime-wide short-lived exits are separated from proven launchd service
  restart evidence. Process labels and working directories remain navigation
  hints rather than claims about task identity or progress.
- Dashboard HTML requires cache revalidation after an upgrade. The standalone
  home link stays within the local AUM installation.
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

### Upgrade notes

- API clients that stop processes must send the exact `create_time` returned by
  `/api/agents`; actionable requests without it return HTTP 428. Use the
  `tree_revision` returned by `/api/tree/{pid}` to reject a changed reviewed
  scope with HTTP 409 before any signal. Refresh an open dashboard after upgrade.
- Warning history is temporary. A retention deadline is a maximum, and the
  100-warning cap or a restart can remove an observation earlier.

## 0.2.6 - 2026-08-22

### Fixed

- The wheel and sdist now exclude the maintainer's operational configuration
  and ship `agents.default.yaml` as the clean fallback.
- Removed a maintainer-specific trusted hostname from the DNS-rebinding guard.
  Proxy hostnames are opt-in through `AUM_TRUSTED_HOSTS`.

For earlier history, see
[GitHub Releases](https://github.com/minglong51/agent-usage-manager/releases).

[Unreleased]: https://github.com/minglong51/agent-usage-manager/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/minglong51/agent-usage-manager/releases/tag/v0.3.0
[0.2.6]: https://github.com/minglong51/agent-usage-manager/releases/tag/v0.2.6
