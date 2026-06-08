# agent-usage-manager

A tiny, single-file web dashboard for **headless AI agents** running on a machine —
OpenClaw, Hermes, Claude Code, Ollama, vLLM, llama.cpp, or anything you name. It
shows which agents are alive and what they're costing you (CPU, memory, GPU), and
gives you a **kill button** per agent.

No database, no auth layer, no dependencies beyond FastAPI + psutil. Runs on
macOS and Linux. Meant to be cloned, configured, and run on any node in a fleet.

```
AGENT        PID    STATUS     CPU %   MEM MB   GPU MB   UPTIME   COMMAND            ┆
openclaw     48213  ● running    62.4    1840     7320     2h 11m  openclaw serve …   [kill] [force]
hermes       49001  ● running    18.0     512        —     44m     hermes worker …    [kill] [force]
ollama       50122  ● running     3.1    9210    14080     6h 02m  ollama runner …    [kill] [force]
vllm         50890  ● running     0.0     220        —     12m     python -m vllm …   [kill] [force]
```

> A live web UI (auto-refreshing every 3s). The rendered GIF lands here once recorded —
> see `demo.tape`.

## What it does

- **Liveness** — green dot = running, red = zombie/dead. Status column shows the OS state.
- **Usage** — CPU % (since last poll), resident memory (MB), GPU memory (MB, NVIDIA only),
  and uptime, refreshed every 3s.
- **Kill** — `kill` sends SIGTERM (graceful), `force` sends SIGKILL. SIGTERM auto-escalates
  to SIGKILL if the process doesn't exit in 3s.

## Safety

This is the important part — a web page that can kill processes needs guardrails:

- **Allowlist only.** Only processes matching a pattern in `agents.yaml` are ever
  listed *or* killable. The kill endpoint re-checks the match server-side before
  sending any signal, so the dashboard can never be used to kill an arbitrary PID.
- **Protected patterns.** Anything matching `protect:` in `agents.yaml` — plus the
  monitor's own process and PID 1 — shows a disabled, greyed-out kill button and is
  refused server-side.
- **Secret redaction.** Command lines often carry tokens/keys in env vars or flags
  (`FOO_TOKEN=...`, `--api-key ...`, `sk-...`, `ghp_...`, JWTs). The command column
  redacts these to `***` before they ever reach the browser — safe to screenshot.
- **Bind local by default.** It listens on `127.0.0.1`. Don't expose it to a network
  without putting auth in front of it (reverse proxy + basic auth, SSH tunnel, etc.) —
  it has no built-in authentication.

## Quick start

Run it without installing anything (needs [`uv`](https://github.com/astral-sh/uv)):

```bash
uvx agent-usage-manager
# open http://127.0.0.1:8765
```

Or install it:

```bash
pipx install agent-usage-manager   # or: pip install agent-usage-manager
agent-usage-manager --port 8765
```

From a clone (for hacking on it):

```bash
git clone <this-repo> && cd agent-usage-manager
./run.sh                           # venv + editable install, serves on :8765
```

Flags: `--host`, `--port`, `--config /path/to/agents.yaml`.

## Configure which processes are "agents"

Edit `agents.yaml`:

```yaml
agents:
  - label: openclaw           # shown as the badge in the UI
    match: openclaw           # case-insensitive substring of the command line
  - label: hermes
    match: hermes
  - label: claude-code
    match: "claude(\\s|$|-code)"
    regex: true               # treat `match` as a regex instead of substring

protect:                      # never killable, even if matched above
  - uvicorn
```

A process matches if the pattern hits its **full command line** or its process name.
Point at a different file with `AGENTS_CONFIG=/path/to/agents.yaml`.

## GPU notes

Per-process GPU memory comes from `nvidia-smi` when it's on `PATH` (Linux / NVIDIA).
**Apple Silicon has no per-process GPU accounting API**, so the GPU column stays blank
on Macs — CPU and memory are the meaningful resource signals there.

## API

- `GET  /api/agents` → `{ agents: [...], host, cpu_count, ts }`
- `POST /api/kill/{pid}?force=false` → SIGTERM (or SIGKILL with `force=true`)

## Run as a service

Linux (systemd), `~/.config/systemd/user/agent-usage-manager.service`:

```ini
[Unit]
Description=agent usage manager
[Service]
ExecStart=%h/agent-usage-manager/.venv/bin/uvicorn app:app --port 8765
WorkingDirectory=%h/agent-usage-manager
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now agent-usage-manager
```

## License

MIT
