# agent-usage-manager

[![CI](https://github.com/minglong51/agent-usage-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/minglong51/agent-usage-manager/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-usage-manager.svg)](https://pypi.org/project/agent-usage-manager/)
[![Python](https://img.shields.io/pypi/pyversions/agent-usage-manager.svg)](https://pypi.org/project/agent-usage-manager/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/minglong51/agent-usage-manager/blob/main/LICENSE)

A local `htop`-style dashboard for headless AI-agent processes. It groups each
agent's process tree, tracks CPU/RAM/GPU and sustained hot/idle/churn/leak
states, and provides an allowlisted, token-gated stop control.

**Per-node only:** AUM is not a scheduler or multi-host control plane. It runs
on macOS and Linux; per-process GPU memory is currently NVIDIA/Linux only.

![agent-usage-manager dashboard](https://raw.githubusercontent.com/minglong51/agent-usage-manager/main/docs/dashboard.png)

*Sanitized capture from a real macOS run. Host, path, and command details are
replaced; Apple Silicon does not expose per-process GPU memory.*

## Quick start

```bash
uvx agent-usage-manager
```

The dashboard opens at `http://127.0.0.1:8765`. No database or hosted service is
required. The bundled config recognizes common tools including Claude Code,
Codex, OpenClaw, Hermes, Ollama, vLLM, llama.cpp, Kiro, Aider, and Cline.

Other install options:

```bash
pipx install agent-usage-manager
# or, inside a virtual environment:
pip install agent-usage-manager
```

## What it does

- **One row per agent.** Child inference processes, MCP servers, and helpers are
  rolled into the owning agent's row. CPU, memory, and GPU values are tree totals.
- **Shows change over time.** CPU sparklines and `hot`, `idle`, `churn`, and
  `leak?` states surface sustained problems rather than one noisy sample.
- **Supports alerts.** A state transition can call a local command with `$AUM_*`
  environment variables. `agent-usage-manager test-alert` proves the path first.
- **Stops the process tree.** `kill` sends SIGTERM and escalates after three
  seconds; `force` sends SIGKILL. launchd-supervised jobs get the command that
  actually stops the service.
- **Exposes read-only telemetry.** Use `agent-usage-manager list --json`,
  `GET /api/agents`, or Prometheus-compatible `GET /metrics`.
- **Keeps the control local.** The default bind is `127.0.0.1`; there is no
  account, remote collector, or central coordinator.

AUM complements process and telemetry tools:

- `htop` sees processes, but not ownership of a spawned agent tree or agent states.
- Grafana and Prometheus are useful fleet telemetry; AUM can feed them without
  requiring that stack for a single machine.
- GenAI traces describe sessions, tokens, and cost. AUM covers the host-process
  side: liveness, resource pressure, restart churn, and local stop control.

## Configure agents

Create `agents.yaml` in the directory where you launch AUM, or pass
`--config /path/to/agents.yaml`:

```yaml
agents:
  - label: claude-code
    match: "claude(\\s|$|-code)"
    regex: true
  - label: ollama
    match: ollama

protect:
  - uvicorn

ignore:
  - crashpad
```

Only matched processes are listed or stoppable. `protect:` keeps a matched
process visible but disables stopping it; `ignore:` removes a false match
entirely. Edits hot-reload while the server runs.

Advanced matching, per-instance tmux/launchd labels, alerts, API details,
service setup, and troubleshooting are in the
[operator reference](https://github.com/minglong51/agent-usage-manager/blob/main/docs/reference.md).

## Safety boundary

A web page that can stop processes needs a narrow boundary:

- The server re-checks the allowlist before every signal. PID 1, AUM itself, and
  configured `protect:` matches are always refused.
- Every stop request requires an `X-Kill-Token`. AUM creates the token in a
  mode-`0600` state file and never serves it over HTTP.
- Browser requests are checked for DNS-rebinding and cross-origin state changes.
- Non-loopback binds fail closed unless `--unsafe-expose` is explicit.
- Every stop attempt and refusal is appended to a local action log.
- Common token and key shapes are redacted before command lines reach the UI.
  Redaction is not a substitute for reviewing screenshots and issue attachments.
- Read-only endpoints still expose process metadata. Keep AUM on a trusted
  machine or put an authenticated boundary in front of any proxy.

Report vulnerabilities privately through the
[security policy](https://github.com/minglong51/agent-usage-manager/security/policy). Do not open a public issue for a kill-path,
authorization, redaction, or package-privacy defect.

## Limits

| Area | Current boundary |
|---|---|
| Process matching | Heuristic; use `ignore:` for false positives |
| GPU | Per-process values require `nvidia-smi`; unavailable on Apple Silicon |
| Supervision | launchd user jobs detected; systemd supervision is not |
| Permissions | AUM can only signal processes accessible to its OS user |
| History | In memory and reset when the server restarts |
| Windows | Untested; CI covers macOS and Linux |

## Command-line and API entry points

```bash
agent-usage-manager                     # dashboard
agent-usage-manager list                # one-shot table
agent-usage-manager list --json         # scriptable snapshot
agent-usage-manager test-alert          # verify configured alert delivery
```

The HTTP surface is documented in the
[operator reference](https://github.com/minglong51/agent-usage-manager/blob/main/docs/reference.md). External controllers should consume
the read-only endpoints and own their own scheduling or irreversible decisions.

## Project documentation

- [Operator reference](https://github.com/minglong51/agent-usage-manager/blob/main/docs/reference.md): configuration, alerts, API, services,
  proxying, and troubleshooting
- [Changelog](https://github.com/minglong51/agent-usage-manager/blob/main/CHANGELOG.md): current release and unreleased changes
- [Security policy](https://github.com/minglong51/agent-usage-manager/security/policy): supported versions and private reporting
- [Contributing](https://github.com/minglong51/agent-usage-manager/blob/main/CONTRIBUTING.md): setup, scope, and privacy requirements
- [Architecture](https://github.com/minglong51/agent-usage-manager/blob/main/docs/design/HLD.md) and [implementation design](https://github.com/minglong51/agent-usage-manager/blob/main/docs/design/LLD.md)

If AUM misclassifies a process or behaves differently on macOS or Linux,
[open a bug report](https://github.com/minglong51/agent-usage-manager/issues/new?template=bug_report.yml)
with sanitized output. Never attach a live config, token file, or unreviewed
process command line.

AUM is a personal open-source project. Maintenance and support are best-effort.

## License

MIT
