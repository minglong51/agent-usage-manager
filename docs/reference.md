# Operator reference

This is the detailed operating guide for `agent-usage-manager`. Start with the
[README](../README.md) if you have not run AUM yet.

## Install and run

Recommended:

```bash
uvx agent-usage-manager
```

Alternatives:

```bash
pipx install agent-usage-manager

python -m venv .venv
source .venv/bin/activate
pip install agent-usage-manager
agent-usage-manager
```

From a clone:

```bash
git clone https://github.com/minglong51/agent-usage-manager
cd agent-usage-manager
./run.sh
```

The dashboard opens automatically at `http://127.0.0.1:8765`.

Server flags:

| Flag | Purpose |
|---|---|
| `--host` | Bind host; defaults to `127.0.0.1` |
| `--port` | Bind port; defaults to `8765` |
| `--config PATH` | Use an explicit `agents.yaml` |
| `--no-browser` | Do not open the dashboard automatically |
| `--unsafe-expose` | Required for a non-loopback bind |

## Configuration resolution

AUM resolves its config once at startup. The first match wins:

1. `AGENTS_CONFIG=/path/to/agents.yaml`; `--config` sets this variable.
2. `./agents.yaml` in the launch directory.
3. An untracked `agent_usage_manager/agents.yaml` in a source checkout.
4. The sanitized `agents.default.yaml` bundled with the package.

The dashboard header and `list --json` show the resolved path. A missing
explicit config is a startup error. Once running, AUM hot-reloads edits by
mtime. A bad or deleted file keeps the last good configuration and surfaces a
config error in the API and header.

## Process matching

```yaml
agents:
  - label: claude-code
    match: "claude(\\s|$|-code)"
    regex: true
  - label: ollama
    match: ollama

protect:
  - agent-usage-manager
  - uvicorn

ignore:
  - crashpad
  - updater
```

A process matches against its executable basename, first few arguments, and,
on macOS, outermost `.app` bundle name. AUM deliberately does not search the
entire command line because system prompts and other long arguments can merely
mention an agent name.

- `agents:` is the allowlist. Only a matched process can be listed or stopped.
- `protect:` keeps a match visible but refuses stop requests.
- `ignore:` disqualifies an incidental match completely.

AUM groups a matched root with its descendants. CPU, RAM, and GPU values are
tree totals, and a successful stop targets the tree.

## Per-instance labels

Several identical agents otherwise share one base label. AUM can derive a
stable instance label from tmux or launchd.

For tmux sessions:

```yaml
tmux_labels: "^worker-(.+)$"
```

A session named `worker-review` produces the row label `review`. The first
capture group is used; without a group, the whole matching session name is
used.

For launchd jobs:

```yaml
launchd_labels: "^com\\.example\\.agent\\.(.+)$"
```

A job named `com.example.agent.indexer` produces the row label `indexer`.
`tmux_labels` wins if both mechanisms apply. Unmatched processes keep their
base `agents:` label.

## Idle suppression

Waiting for work is normal for many long-running agents. Suppress the visual
idle state for those labels:

```yaml
idle_ok:
  - gateway
  - worker
```

Entries are case-insensitive substrings matched against both the derived
instance label and the base matcher label.

## Alerts

A state transition can execute a local command:

```yaml
alerts:
  command: 'terminal-notifier -title agents -message "$AUM_MSG"'
  cooldown: 600
  flags: [hot, churn, leak]
  leak_floor_mb: 1536
```

The command runs through the shell with:

- `AUM_MSG`
- `AUM_LABEL`
- `AUM_FLAG`
- `AUM_PID`
- `AUM_CPU`
- `AUM_MEM_MB`
- `AUM_RESTARTS`
- `AUM_HOST`

Alerts fire when a state appears, not on every poll. Cooldown is tracked per
agent and flag and is charged only after the command exits successfully.
`idle` is opt-in because idle is often the normal fleet state. `leak_floor_mb`
gates leak alerts by absolute RSS without hiding the dashboard badge.

Prove the command before relying on it:

```bash
agent-usage-manager test-alert --config /path/to/agents.yaml
```

The one-shot `list` command never sends alerts.

## State signals

| State | Meaning |
|---|---|
| `hot` | At least 90% of one CPU core for five minutes |
| `idle` | A long-running process stays below the idle threshold for ten minutes |
| `churn` | The same label dies young at least three times in ten minutes |
| `leak?` | RSS rises at least 30% and 128 MB over fifteen minutes without falling back |

These are investigation hints, not diagnoses. Inference can be legitimately
hot, waiting agents can be legitimately idle, and conversational agents can
grow memory as session state grows.

History lives in memory and rebuilds after restart. `list` and `list --json`
are one-shot commands, so their output sets `flags_available` to false.

## launchd-supervised agents

A launchd job with `KeepAlive` restarts after a signal. AUM detects user-domain
jobs and replaces the ineffective stop buttons with a copyable command:

```bash
launchctl bootout gui/<uid>/<label>
launchctl disable gui/<uid>/<label>
```

The API refuses a direct stop request for a detected supervised job with HTTP
409 and returns the same guidance. Root `LaunchDaemons` are not detected.

On Linux, systemd supervision is not detected. Use `systemctl stop` for a
service that immediately respawns.

## GPU behavior

AUM calls `nvidia-smi` when available and reports per-process compute memory.
This is primarily NVIDIA/Linux behavior. AMD and Intel GPUs are not read,
graphics-only workloads may not appear, and Apple Silicon has no per-process
GPU accounting API, so the column is hidden on Macs.

## HTTP API

### `GET /api/agents`

Returns host totals and matched agents:

```text
{ api_version, aum_version, agents, host, cpu_count, mem_total_mb,
  mem_used_pct, config_path, config_error, ts }
```

Each agent includes its PID, creation time, label, resource totals, CPU trend,
states, protection status, and supervised-process guidance. Pair `pid` with
`create_time` when caching rows so PID reuse cannot alias two processes.

### `GET /api/tree/{pid}`

Returns the recognized agent's process subtree with per-child resource and
command information.

### `GET /metrics`

Returns Prometheus text exposition aggregated by label rather than PID.

### `POST /api/kill/{pid}?force=false`

Sends SIGTERM to a recognized process tree; `force=true` sends SIGKILL. The
request must include the token:

```bash
curl -X POST \
  -H "X-Kill-Token: $(cat "$HOME/Library/Application Support/agent-usage-manager/kill_token")" \
  http://127.0.0.1:8765/api/kill/48213
```

The token path differs on non-macOS platforms and is named in the server's 403
response. Never paste the token into an issue or screenshot.

## Run as a user service

Example systemd unit at
`~/.config/systemd/user/agent-usage-manager.service`:

```ini
[Unit]
Description=agent usage manager

[Service]
ExecStart=%h/agent-usage-manager/.venv/bin/uvicorn agent_usage_manager.app:app --port 8765
WorkingDirectory=%h/agent-usage-manager
Restart=on-failure

[Install]
WantedBy=default.target
```

Enable it with:

```bash
systemctl --user enable --now agent-usage-manager
```

## Proxying and remote access

A loopback bind is only a process-level default. A reverse proxy or tunnel can
still make the read endpoints reachable.

- Read endpoints expose process names, commands, resource usage, and config path.
- The static kill token is the action boundary, not general read authentication.
- Put authentication in front of any non-local access.
- A proxy hostname must be explicitly allowed:

```bash
AUM_TRUSTED_HOSTS=agents.example.internal agent-usage-manager
```

The value is comma-separated. Add only names controlled by the operator. A
name another party can mint reopens the DNS-rebinding path the host guard closes.

## Troubleshooting

### Installation compiles psutil and fails

Your platform may not have a compatible wheel. Install a compiler and Python
headers, or use `uvx agent-usage-manager`.

### The dashboard is empty

Check the config path in the dashboard header. Matching uses the executable
basename and first few arguments, not the full command line. Use `ignore:` to
remove false positives.

### A stopped process immediately returns

It is probably supervised. On macOS, use the launchd command shown in the row.
On Linux, use `systemctl stop` for a systemd service.

### Every request returns HTTP 403

The DNS-rebinding guard rejects non-local DNS names. Use
`http://127.0.0.1:8765`, a bare IP, or configure `AUM_TRUSTED_HOSTS` for a
controlled proxy name.

### Only stop requests return HTTP 403

Send the `X-Kill-Token` named in the response. If the browser stored a stale
token, the next stop attempt prompts again.

### Startup refuses the bind

Non-loopback `--host` values require `--unsafe-expose`. Use that flag only when
an authenticated boundary protects the service.
