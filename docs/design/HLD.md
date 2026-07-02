# agent-usage-manager — High-Level Design

## Purpose

`agent-usage-manager` (AUM) is a single-node monitor and guarded kill switch for
headless AI agent processes (OpenClaw, Hermes, Claude Code, Kiro, Aider, Codex,
Cline, Ollama, vLLM, llama.cpp — anything the operator names in `agents.yaml`).
It groups matched processes by process tree, shows per-agent liveness, CPU %,
RSS, GPU memory (NVIDIA only), uptime, a ~20-minute CPU sparkline, and four
sustained-state badges (`hot` / `idle` / `churn` / `leak`), and offers a
token-gated kill button that stops the whole tree. It is deliberately **not** a
fleet scheduler or multi-host orchestrator (README.md "Product boundary"):
external control planes may consume its read-only telemetry (`/api/agents`,
`/metrics`, `list --json`) but should own their own actuation. No database, no
auth framework — one FastAPI app, one static HTML page, psutil, and a `0600`
token file.

## System context

```
                          ┌────────────────────────────────────────────┐
   operator's browser ──► │  agent-usage-manager (uvicorn on 127.0.0.1 │
   (GET /, poll 3s,       │  :8765 by default)                          │
    POST /api/kill        │                                             │
    + X-Kill-Token)       │  reads:                                     │
                          │   • psutil process table (all processes)    │
   curl / scripts ──────► │   • agents.yaml (hot-reloaded on mtime)     │
   (list --json is the    │   • nvidia-smi  (GPU mem, Linux/NVIDIA)     │──► SIGTERM/SIGKILL
    no-server variant)    │   • launchctl list/print (macOS supervision)│    to matched
                          │   • tmux list-panes (per-instance labels)   │    process trees
   Prometheus/Grafana ──► │                                             │
   (GET /metrics)         │  writes:                                    │──► alert command
                          │   • kill_token (0600, app-support dir)      │    (user-defined shell,
                          │   • actions.log (append-only kill audit)    │     $AUM_* env vars)
                          └────────────────────────────────────────────┘
```

External dependencies (pyproject.toml:13-18): `fastapi`, `uvicorn[standard]`,
`psutil`, `pyyaml`; dev-only `pytest` + `httpx`. Optional external binaries,
each probed with `shutil.which` and degrading to empty results when absent:
`nvidia-smi` (agent_usage_manager/app.py:550), `launchctl`
(app.py:459), `tmux` (app.py:502).

## Component map

| Path | Responsibility |
|---|---|
| `agent_usage_manager/app.py` (1335 lines) | Everything server-side: config loading + hot reload, process collection/matching, tree rollup, CPU/mem/GPU sampling, history + flag heuristics, churn tracking, alert dispatch, kill token + action log, browser guard middleware, all HTTP endpoints (`/`, `/api/agents`, `/api/tree/{pid}`, `/api/kill/{pid}`, `/metrics`, `/static`). |
| `agent_usage_manager/cli.py` | The `agent-usage-manager` console entrypoint (pyproject.toml:28): argparse flags, fail-closed bind check (`_bind_allowed`, cli.py:10), browser auto-open, `uvicorn.run(...)` (cli.py:133), and the serverless `list` subcommand (`_run_list`, cli.py:40). |
| `agent_usage_manager/static/index.html` | The entire frontend: dark-theme table UI, 3s poll loop, sparklines, badge rendering, kill/force buttons with confirm + token paste prompt (localStorage), tree expansion, stale-data banner, pointer-freeze row ordering. No build step, no JS dependencies. |
| `agent_usage_manager/agents.yaml` | Default (bundled) config: `agents:` matchers, `protect:`, `ignore:`, `tmux_labels:`, `alerts:`. Doubles as documented example. |
| `tests/test_smoke.py` | Unit + API tests: redaction, match-target rules, config validation, guard middleware (DNS-rebind/CSRF), token gating, flag windows, alert transitions/cooldown, hot reload. |
| `tests/test_synthetic.py` | Synthetic trace fixtures (hot/idle/churn/leak series), fake process tables for churn/leak surfacing, kill-path pid/create_time pinning, real-process kill test, adversarial matcher cases. |
| `run.sh` | Dev launcher from a clone: creates `.venv`, editable install, runs the CLI. |
| `.github/workflows/ci.yml` | CI: `pytest -q` on ubuntu + macos × Python 3.9 / 3.12 on push/PR. |
| `pyproject.toml` | Hatchling build; version 0.2.2; publishes wheel/sdist (built artifacts in `dist/`). |
| `demo.tape` | VHS tape for the README demo GIF. |

## Runtime / deploy model

- **Process model:** one uvicorn process serving the FastAPI app, plus one
  daemon sampler thread (`aum-sampler`, started from the FastAPI lifespan,
  app.py:578-585) that calls `list_agents()` every 3s so history/badges accrue
  with no browser open. FastAPI runs the sync endpoints in a threadpool, so all
  shared state (process handles, history, churn, alert transitions, subprocess
  cache) is lock-guarded.
- **State:** all telemetry history is **in-memory** (sparklines/flags reset on
  restart). Durable state is only two files in a per-user state dir
  (`~/Library/Application Support/agent-usage-manager` on macOS,
  `$XDG_STATE_HOME/agent-usage-manager` elsewhere — app.py:241-259): the
  auto-generated `0600` `kill_token` and the append-only `actions.log`.
- **Install/run paths:** `uvx agent-usage-manager` (recommended, README.md:169),
  `pipx`/`pip install agent-usage-manager`, or `./run.sh` from a clone. Binds
  `127.0.0.1:8765` by default and refuses non-loopback hosts without
  `--unsafe-expose` (cli.py:117-122).
- **As a service:** README.md:318-333 documents a systemd user unit; on this
  host it runs under launchd, and the bundled `agents.yaml:93-99` wires alerts
  into the fleet's Telegram notifier.
- **CI/release:** GitHub Actions test matrix; hatchling builds published to
  PyPI (the `uvx` path depends on that).

## Security model (load-bearing, summarized)

Two independent authorization questions on the kill path:
1. **What may be killed** — the `agents.yaml` allowlist, re-matched server-side
   per kill (app.py:1188-1191); `protect:` patterns, self, and PID 1 are always
   refused; `ignore:` hits are never agents at all.
2. **Who may kill** — the static `X-Kill-Token` header checked with
   `secrets.compare_digest` (app.py:1167-1174). The token lives in a `0600`
   file outside any project tree so sandboxed local agents can't read it, and
   it is never served over HTTP; the dashboard prompts the operator to paste it
   once into localStorage (index.html:158-167).

Plus: a browser-guard middleware rejecting non-local `Host` headers
(DNS-rebinding) and foreign-`Origin` state changes (CSRF) (app.py:619-645);
secret redaction of command lines before they reach the browser or the action
log (app.py:326-341); launchd-supervised kills refused with a 409 + the correct
`launchctl bootout` command (app.py:1201-1216); every kill attempt and refusal
appended as a JSON line to `actions.log` (app.py:299-323).

## How it's used

- **Interactive:** run `agent-usage-manager` (or `uvx agent-usage-manager`);
  it opens `http://127.0.0.1:8765` automatically (suppress with
  `--no-browser`). Watch rows, expand `+N` subtrees, click kill/force.
- **Scripts / cron:** `agent-usage-manager list` or `list --json` prints one
  snapshot to stdout with no server and no alert side effects (cli.py:40-76;
  alerts are gated on the sampler having started, app.py:1055-1056).
- **Machine consumers:** `GET /api/agents` (versioned JSON, `api_version` +
  `aum_version`) for external tooling; `GET /metrics` for Prometheus/Grafana,
  aggregated per label to avoid pid-churn series bloat (app.py:1262-1327).
- **Push alerts:** configure `alerts:` in `agents.yaml`; badge appearances run
  the command with `$AUM_*` env vars, once per transition with a cooldown.
- **Not** a bot, not a cron job itself, not a library API — a locally-run web
  service with a CLI wrapper.
