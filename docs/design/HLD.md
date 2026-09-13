# agent-usage-manager — High-Level Design

**Refreshed:** 2026-09-13 (0.3.0 release metadata).

## Purpose

`agent-usage-manager` (AUM) is a single-node monitor and guarded kill switch for
headless AI agent processes (OpenClaw, Hermes, Claude Code, Kiro, Aider, Codex,
Cline, Ollama, vLLM, llama.cpp — anything the operator names in `agents.yaml`).
It groups matched processes by process tree, shows per-agent liveness, CPU %,
RSS, GPU memory (NVIDIA only), uptime, a recent CPU sparkline, and four
sustained-state badges (`hot` / `idle` / `churn` / `leak`), and offers a
token-gated, best-effort process-tree stop. It is deliberately **not** a
fleet scheduler or multi-host orchestrator (README.md "Per-node only"):
external control planes may consume its read-only telemetry (`/api/agents`,
`/metrics`, `list --json`) but should own their own actuation. No database, no
auth framework — one FastAPI app, one static HTML page, psutil, and a `0600`
token file.

Process identity is PID plus OS creation time. Instance labels identify their
tmux, launchd, or matcher source; a working-directory basename does not establish
a task or conversation. The API provides descendant PIDs, timestamped CPU/RSS
samples, and the measurements behind each active condition. Server memory retains
at most 100 condition-onset observations for 48 hours, including their measured
window, after clearance or removal from the live sample. This covers the next daily
digest and a later inspection without raising the storage cap. Restart clears them.
Existing alert commands can link to these observations using an operator-configured
dashboard base URL; this adds no notification transport.

The browser opens on a compact, warning-first resource table. Its optional
inspector separates live process scope from saved warning evidence: observation
links remain read-only, with an explicit transition to the exact current process
before any stop review. Expired observations and missing process identities do
not fall back to another incarnation of the PID.

## System context

```
                          ┌──────────────────────────────────────────────┐
   operator's browser ──► │  agent-usage-manager (uvicorn on 127.0.0.1   │
   (GET /, poll 3s,       │  :8765 by default)                           │
    POST /api/kill        │                                              │
    + X-Kill-Token)       │  reads:                                      │
                          │   • psutil process table (all processes)     │
   curl / scripts ──────► │   • resolved agents.yaml (hot-reloaded)      │
   (list --json is the    │   • nvidia-smi  (GPU mem, Linux/NVIDIA)      │──► SIGTERM/SIGKILL
    no-server variant)    │   • launchctl list/print (macOS supervision  │    to matched
                          │     + launchd_labels: instance identity)     │    process trees
                          │   • tmux list-panes (tmux_labels: instance   │
   Prometheus/Grafana ──► │     identity)                                │
   (GET /metrics)         │  writes:                                     │──► alert command
                          │   • kill_token (0600, app-support dir)       │    (user-defined shell,
                          │   • actions.log (append-only kill audit)     │     $AUM_* env vars)
                          └──────────────────────────────────────────────┘
```

External dependencies (pyproject.toml:13-18): `fastapi`, `uvicorn[standard]`,
`psutil`, `pyyaml`; dev-only `pytest` + `httpx`. Optional external binaries,
each probed with `shutil.which` and degrading to empty results when absent:
`nvidia-smi` (agent_usage_manager/app.py:650), `launchctl`
(app.py:540), `tmux` (app.py:584).

## Component map

| Path | Responsibility |
|---|---|
| `agent_usage_manager/app.py` (~1500 lines) | Everything server-side: config loading + hot reload, process collection/matching, tree rollup, CPU/mem/GPU sampling, history + flag heuristics, churn tracking, per-instance labels (tmux/launchd), alert dispatch, kill token + action log, browser guard middleware, all HTTP endpoints (`/`, `/api/agents`, `/api/tree/{pid}`, `/api/kill/{pid}`, `/metrics`, `/static`). |
| `agent_usage_manager/cli.py` | The `agent-usage-manager` console entrypoint (pyproject.toml:28): argparse flags, fail-closed bind check (`_bind_allowed`, cli.py:10), browser auto-open, `uvicorn.run(...)` (cli.py:133), and the serverless `list` subcommand (`_run_list`, cli.py:40). |
| `agent_usage_manager/static/index.html` | The entire frontend: compact resource table, optional process inspector, retained-warning and PID/create-time navigation, desktop/phone layouts, light/dark themes, three-second polling, CPU/RSS evidence, guarded stop review, supervision guidance, and stale-data protection. No build step or JS dependencies. |
| `agent_usage_manager/agents.default.yaml` | Sanitized bundled fallback: `agents:` matchers, `protect:`, `ignore:`, plus commented examples for optional labels and alerts. Operator configs remain outside Git. |
| `tests/test_smoke.py` | Unit + API tests: redaction, match-target rules, config validation, guard middleware (DNS-rebind/CSRF), token gating, flag windows, alert transitions/cooldown, hot reload. |
| `tests/test_synthetic.py` | Synthetic trace fixtures (hot/idle/churn/leak series), fake process tables for churn/leak surfacing, kill-path pid/create_time pinning, real-process kill test, adversarial matcher cases. |
| `tests/test_reliability.py` | Regression coverage for displayed process identity, read-only telemetry consumers, stale snapshots, scoped exits, project attribution, and bounded alert delivery. |
| `run.sh` | Dev launcher from a clone: creates `.venv`, editable install, runs the CLI. |
| `.github/workflows/ci.yml` | CI: `pytest -q` on ubuntu + macos × Python 3.9 / 3.12 on push/PR. |
| `pyproject.toml` | Hatchling build; version 0.3.0; publishes wheel/sdist (built artifacts in `dist/`). |
| `demo.tape` | VHS tape for the README demo GIF. |

## Runtime / deploy model

- **Process model:** one uvicorn process serving the FastAPI app, plus one
  daemon sampler thread (`aum-sampler`, started from the FastAPI lifespan).
  `_publish_snapshot()` collects once at startup and every three seconds;
  browser, tree, and metrics reads consume that snapshot without changing CPU
  counters or history. Collection failures or snapshots older than ten seconds
  return HTTP 503; the browser preserves its last view and disables stop actions.
  One-shot CLI collection remains independent. Shared state is lock-guarded.
- **State:** all telemetry history is **in-memory** (sparklines/flags reset on
  restart). Durable state is only two files in a per-user state dir
  (`~/Library/Application Support/agent-usage-manager` on macOS,
  `$XDG_STATE_HOME/agent-usage-manager` elsewhere — app.py:322-340): the
  auto-generated `0600` `kill_token` and the append-only `actions.log`.
  Restart evidence is scoped to launchd job identity. Uncorrelated CLI exits
  are runtime-level facts, never attributed to every live process of that runtime.
  Alert delivery state is separate from condition state, with three attempts
  per condition and no automatic retry of an unconfirmed timeout.
- **Install/run paths:** `uvx agent-usage-manager` (recommended),
  `pipx`/`pip install agent-usage-manager`, or `./run.sh` from a clone. Binds
  `127.0.0.1:8765` by default and refuses non-loopback hosts without
  `--unsafe-expose` (cli.py:219-225). A reverse proxy can still expose a
  loopback-bound service, so operators must authenticate the proxy and opt its
  hostname into `AUM_TRUSTED_HOSTS`; the loopback refusal covers direct binds.
- **As a service:** `docs/reference.md` documents a systemd user unit. Other
  supervisors invoke the same CLI with an operator-owned external config.
- **CI/release:** GitHub Actions test matrix; hatchling builds published to
  PyPI (the `uvx` path depends on that).

## Security model (load-bearing, summarized)

Two independent authorization questions on the kill path:
1. **What may be killed** — the `agents.yaml` allowlist, re-matched server-side
   per kill (app.py:1377-1382); `protect:` patterns, self, and PID 1 are always
   refused; `ignore:` hits are never agents at all.
2. **Who may kill** — the static `X-Kill-Token` header checked with
   `secrets.compare_digest` (app.py:1356-1363). The token lives in a `0600`
   file outside any project tree. Agents whose filesystem sandbox excludes that
   directory cannot read it; unrestricted same-user processes can. The token
   authenticates the caller, not the caller's interpretation of hosted work, and
   it is never served over HTTP; the dashboard prompts the operator to paste it
   once into localStorage (index.html:177-201).

Plus: a browser-guard middleware rejecting non-local `Host` headers
(DNS-rebinding) and foreign-`Origin` state changes (CSRF) (app.py:724-750);
secret redaction of command lines before they reach the browser or the action
log (app.py:418-423); launchd-supervised kills refused with a 409 + the correct
`launchctl bootout` command (app.py:1390-1405); every kill attempt and refusal
appended as a JSON line to `actions.log` (app.py:380-412).

API version 2 requires the displayed `create_time` on every actionable stop
request. Missing identity returns 428; a changed identity returns 409 before
signaling. The existing server-side creation-time pin still protects the later
authorization-to-signal race. Old clients must obtain a fresh snapshot and send
its identity; there is no PID-only compatibility bypass.

Clients may additionally send the sampled `tree_revision`. The server captures
root and descendant PID/create-time identities and protection state, compares
that revision before any signal, and returns 409 if it changed. Omitting the
revision retains API v2's root-identity-only contract. Neither mode atomically
freezes a tree: processes born after capture are outside the signal set, and
protected descendants are skipped. Results and action logs distinguish captured,
signaled, skipped, stopped, and surviving identities. TERM still escalates to KILL
after three seconds for the signaled processes that remain alive.

## How it's used

- **Interactive:** run `agent-usage-manager` (or `uvx agent-usage-manager`);
  it opens `http://127.0.0.1:8765` automatically (suppress with
  `--no-browser`). Watch rows, expand `+N` subtrees, click kill/force.
- **Scripts / cron:** `agent-usage-manager list` or `list --json` prints one
  snapshot to stdout with no server and no alert side effects (cli.py:60-107;
  alerts are gated on the sampler having started, app.py:1243-1244).
- **Machine consumers:** `GET /api/agents` (versioned JSON, `api_version` +
  `aum_version`) for external tooling; `GET /metrics` for Prometheus/Grafana,
  aggregated per label to avoid pid-churn series bloat (app.py:1460-1525).
- **Push alerts:** configure `alerts:` in `agents.yaml`; badge appearances run
  the command with `$AUM_*` env vars. Confirmed failures retry after five and
  ten seconds while the condition persists; successful delivery observes the
  configured cooldown. The snapshot exposes pending, failed, and unconfirmed
  delivery status without introducing another notification route.
- **Not** a bot, not a cron job itself, not a library API — a locally-run web
  service with a CLI wrapper.
