# agent-usage-manager — Low-Level Design

Code layout: one FastAPI module (`agent_usage_manager/app.py`), one CLI module
(`agent_usage_manager/cli.py`), one static frontend
(`agent_usage_manager/static/index.html`). Python ≥3.9, type-hinted, no ORM,
no async I/O beyond FastAPI's own plumbing (all endpoints are sync `def`s run
in the threadpool).

## 1. Configuration

### 1.1 Resolution & hot reload (app.py)

- `_resolve_config() -> Path` (app.py:35-42) — resolved **once at import**:
  `$AGENTS_CONFIG` env var (set by the `--config` flag, cli.py:110-111) →
  `./agents.yaml` in the launch cwd → the package-bundled
  `agent_usage_manager/agents.yaml`. Stored in module-global `CONFIG_PATH`.
- `load_config(path) -> tuple[list[Matcher], list[str]]` (app.py:62-87) —
  strict loader for `agents:` + `protect:`; raises `RuntimeError` on unreadable
  file, invalid YAML, non-mapping root, missing `label`/`match`, or a bad
  regex. This is the only fail-loud loader — it runs at import, so a broken
  config is a startup failure.
- `load_ignore(path) -> list[str]` (app.py:90-107), `load_alerts(path) ->
  Optional[dict]` (app.py:110-161), `load_tmux_labels(path) ->
  Optional[re.Pattern]` (app.py:164-195), `load_idle_ok(path) -> list[str]`
  (app.py:200-221) — best-effort loaders: any error means
  "feature off", never a crash (config validity was already gated by
  `load_config`).
- `_maybe_reload_config()` (app.py:212-238) — called at the top of
  `list_agents()` and `kill_agent()`; compares `CONFIG_PATH` mtime under
  `_config_lock`, re-runs all five loaders on change. A parse error keeps the
  last good config and sets `CONFIG_ERROR` (surfaced in the API/header); the
  bad mtime is recorded so the file isn't re-parsed every poll.

### 1.2 agents.yaml schema (agent_usage_manager/agents.yaml)

```yaml
agents:                       # required: the allowlist (list AND killability)
  - label: claude-code        # row badge / metrics label
    match: "claude(\\s|$|-code)"  # substring (default) or regex, case-insensitive
    regex: true               # optional, default false
protect: [uvicorn, ...]       # matched + listed, kill refused (substring, lowercased)
ignore: [crashpad, ...]       # never an agent: not listed, not killable
tmux_labels: "^bot-(.+)$"     # optional: per-instance label from tmux session name
idle_ok: [admin, ...]         # optional: labels whose idle is NORMAL — no idle badge
alerts:                       # optional: shell command on badge appearance
  command: '...$AUM_MSG...'   # run via shell; data rides $AUM_* env vars only
  cooldown: 600               # seconds per (label, flag) pair; default 600
  flags: [hot, churn, leak]   # default; idle is opt-in
  leak_floor_mb: 1536         # gate leak ALERTS (not the badge) below this RSS; default 0
```

### 1.3 Matching semantics

- `class Matcher` (app.py:48-59) — `matches(text)` is case-insensitive
  regex-search or substring.
- `_target_from_argv(argv, name) -> str` (app.py:358-381) — the match target is
  **executable basename + argv[1:4]**, never the full command line (a deep arg
  like a system prompt mentioning "claude" must not misclassify a wrapper). On
  macOS the outermost `.app` bundle name from argv[0] is prepended
  (`_APP_BUNDLE` regex, app.py:355) so Electron agents match by app name.
- `_label_for(text) -> Optional[str]` (app.py:402-408) — `None` if any
  `ignore:` pattern hits (`_ignored`, app.py:397), else first matcher's label.
- `_is_protected(text, pid) -> bool` (app.py:411-415) — PID == self or 1, or
  any `protect:` substring hit.

## 2. Process collection & telemetry (app.py)

- `_collect() -> (meta, children, label_of, procmap)` (app.py:879-910) — one
  `psutil.process_iter` pass with batched attrs. `meta[pid] = {ppid, name,
  status, ct}`, `children[ppid] = [pid...]`, `label_of[pid]` only for matched
  processes, `procmap[pid]` = live psutil handles.
- `_ancestors(pid, meta)` (app.py:913-919, generator) / `_descendants(root,
  children) -> list[int]` (app.py:922-933) — cycle-safe tree walks. An agent
  **root** is a matched pid with no matched ancestor (app.py:971-975); matched
  descendants roll up into it.
- `_cpu_mem(pid, procmap) -> (cpu%, mem_mb)` (app.py:936-954) — persistent
  `psutil.Process` handles in `_handles` (lock-guarded) so `cpu_percent(None)`
  measures since-last-poll; first sight primes the counter (reads 0.0 once).
- `_cached(key, ttl, fn)` (app.py:422-434) — TTL memo for the subprocess
  shell-outs, computed outside the lock. Users: `_gpu_by_pid()` (app.py:550,
  `nvidia-smi --query-compute-apps`, MiB per pid), `_launchd_jobs()`
  (app.py:459, `launchctl list` → pid→label, user domain only),
  `_tmux_panes()` (app.py:502, `tmux list-panes -a` → pane pid→session name).
  All 2s TTL, 4s subprocess timeout, empty on any failure — except the
  per-label `keepalive:<label>` entries behind the Agent payload field, which
  cache `_keepalive()` for 60s (a job's KeepAlive policy is plist-static).
- `_keepalive(label) -> bool` (app.py:437-456) — `launchctl print
  gui/$UID/<label>` grep for KeepAlive; False on any error (milder wording,
  never over-claims). Backs the supervised-row `keepalive` payload field and
  the 409 wording on the kill path.
- `_instance_label(root, base, meta, panes) -> str` (app.py:527-547) — walk
  root + ancestors for the nearest tmux pane; if its session matches
  `tmux_labels:`, the first capture group becomes the row label (whole match if
  no group), else the matcher label. The derived label feeds churn, alerts,
  and `/metrics` so same-binary fleet bots get per-instance state.

### 2.1 History, flags, churn (module globals, app.py:647-711)

- `_history: dict[(pid, create_time), deque[(ts, cpu, mem_mb)]]`, maxlen
  `_HISTORY_MAX = 400` (~20 min at 3s). Keyed on `(pid, create_time)` so a
  recycled pid starts fresh. Guarded by `_history_lock` along with
  `_label_of_key`, `_churn_deaths`, `_leak_since`.
- `_trend_and_flag(key, uptime_s) -> (trend, flag)` (app.py:777-834):
  - `trend` = last 40 CPU samples (sparkline).
  - `hot` — mean CPU ≥ 90% over a **fully-covered** 5-min window.
  - `leak` — over a 15-min window: tail median ≥ 1.3× head median, delta
    ≥ 128 MB, tail floor above head median, and the condition sustained for
    `_LEAK_SUSTAIN_S = 900` s (`_leak_since`) so a transient heavyweight child
    (headless-browser render) doesn't flag but a real ratchet does.
  - `idle` — uptime & span ≥ 10 min with p95 CPU < 2% (p95 not max, so one GC
    blip can't suppress it).
  - All windows require actual span coverage — a young series never flags.
- Churn (`app.py:661-701`): a crash loop is invisible to hot/idle (fresh pid
  every poll), so deaths are tracked **per label**. `_note_death_locked`
  records a vanished root that died younger than `_CHURN_LIFETIME_S = 120` s;
  `_restarts_in_window(label, now)` counts deaths within
  `_CHURN_WINDOW_S = 600` s; ≥ `_CHURN_MIN_DEATHS = 3` forces
  `flag = "churn"`, outranking hot/idle (app.py:1009-1012).

### 2.2 Alerts (app.py:704-774)

- `_check_alerts(agents, now, host)` (app.py:745-774) — flags tracked per
  label in `_prev_flag`; an alert fires only on a flag **appearing**
  (transition), only for flags in `cfg["flags"]`, and only if
  `now - _last_alert[(label, flag)] >= cooldown`. A `leak` below
  `leak_floor_mb` is tracked as *unflagged* so crossing the floor later counts
  as the appearance. Transitions are tracked even with no command configured,
  so enabling alerts later doesn't re-announce long-standing flags.
- `_spawn_alert(command, agent, host)` (app.py:714-742) — `subprocess.Popen(...,
  shell=True, start_new_session=True)`, output devnulled, errors swallowed.
  Runtime data goes **only** through env vars: `AUM_MSG`, `AUM_LABEL`,
  `AUM_FLAG`, `AUM_PID`, `AUM_CPU`, `AUM_MEM_MB`, `AUM_RESTARTS`, `AUM_HOST`.
  `AUM_MSG` (`_alert_message`) leads with the plain-English verdict — the
  opening words are what a notification shows — and trails the machine
  snapshot in brackets: "Codex is crash-looping — 4 restarts in 10 min
  [agent-usage-manager · churn · host · cpu 5% · …]"; one line, since alert
  commands pass it to `--title`/`-message` verbatim.
- Server-mode gate: alerts run only when `_sampler_started` is set
  (app.py:1055-1056) — the one-shot `list` CLI never alerts.

### 2.3 Background sampler

- `_lifespan` (app.py:578-585) starts one daemon thread running
  `_sampler_loop` (app.py:837-847): every 3s, call `list_agents()` unless the
  frontend poll already did within the last 3s (`_last_collect` monotonic
  stamp) — trends accrue browserless without double-sampling.

## 3. HTTP surface (app.py)

### 3.1 Middleware — `_browser_guard` (app.py:619-645)

Applies to every request:
1. **DNS-rebinding guard:** `Host` must be `localhost`/`127.0.0.1`/`::1` or an
   IP literal (`_host_allowed`, app.py:601-616) → else 403.
2. **CSRF guard:** a non-GET/HEAD/OPTIONS request with an `Origin` whose host
   differs from `Host` and isn't local → 403. `Origin: null` counts as
   foreign; CLI tools send no Origin and pass.

### 3.2 Endpoints

- `GET /api/agents` → `list_agents() -> dict` (app.py:957-1073). Main path:
  reload config → cached gpu/launchd/tmux maps → `_collect()` → compute roots
  → per root: sum tree cpu/mem/gpu, derive instance label, append history,
  `_trend_and_flag`, churn override, `idle_ok:` suppression (an `idle` flag on
  a waiting-class label is dropped — idle is that label's NORMAL state, so the
  badge would be wallpaper) → prune dead handles/history (recording
  deaths) → sort by CPU desc → maybe `_check_alerts`. Response shape:

  ```json
  { "api_version": 1, "aum_version": "0.2.2",
    "agents": [Agent...], "host": "...", "cpu_count": N,
    "mem_total_mb": N, "mem_used_pct": N,
    "config_path": "...", "config_error": null,
    "token_path": "...", "ts": epoch }
  ```

  `token_path` is deliberately non-secret (the file is 0600; knowing the path
  changes nothing — app.py:1067-1071).

- **`Agent` model** (pydantic `BaseModel`, app.py:850-877): `pid`,
  `create_time` (pair with pid to defeat PID-reuse aliasing), `label`, `name`,
  `cmdline` (redacted, truncated to 300), `status`, `alive` (not zombie),
  `cpu_percent`/`mem_mb`/`gpu_mem_mb` (tree totals; gpu `None` when no data),
  `uptime_s`, `child_count`, `protected`, `supervised` (launchd label or
  `None`), `stop_hint` (launchctl bootout command), `keepalive` (bool or
  `None` — whether the supervising job has KeepAlive, so consumers can word
  the supervision note precisely), `trend: list[float]`,
  `flag: Optional[str]` in {hot, idle, churn, leak}, `restarts: int`.

- `GET /api/tree/{pid}` → `agent_tree(pid) -> dict` (app.py:1118-1156). 404 if
  no such pid; 403 unless `_label_for(_match_target(proc))` hits (same target
  authorization as kill — can't walk arbitrary trees). DFS with parent-first
  sibling order; rows `{pid, name, cpu_percent, mem_mb, cmdline[:200], depth}`.

- `POST /api/kill/{pid}?force=false` → `kill_agent(...)` (app.py:1159-1252).
  Ordered gates, each refusal action-logged:
  1. **Caller auth:** `X-Kill-Token` vs `KILL_TOKEN` via
     `secrets.compare_digest` on bytes (constant-time) → 403
     `wrong-token`/`no-token` (app.py:1167-1174).
  2. Config reload; `psutil.Process(pid)` + `create_time()` cached to pin
     identity → 404 `no-such-pid`.
  3. **Target auth:** `_label_for(_match_target(proc))` → 403 `not-an-agent`;
     `_is_protected` → 403 `protected`.
  4. **Supervision:** pid in launchd jobs → 409 with `launchctl bootout`
     guidance, wording split on `_keepalive` (won't stick vs restarts at
     login) (app.py:1201-1216).
  5. `_signal_tree(proc, force)` (app.py:1076-1115): fresh `_collect()`,
     **create_time compared** against the pinned handle (pid reuse → signal
     nothing), then `terminate()`/`kill()` on root + descendants, skipping
     self/PID 1/protected. psutil methods map to SIGTERM/SIGKILL on POSIX,
     TerminateProcess on Windows.
  6. `psutil.wait_procs(timeout=3)`; a non-force kill **auto-escalates**
     survivors to `kill()` + another 3s wait (app.py:1231-1239). Returns
     `{pid, result, method, killed, still_running}`; a target that exited
     between auth and signal returns `result: "already exited"` (pid-reuse
     aware via `is_running()`).

- `GET /metrics` → `metrics() -> PlainTextResponse` (app.py:1262-1327).
  Calls `list_agents()`, aggregates **per label** (pids churn; per-pid series
  would go stale in Prometheus): `aum_agent_instances`, `aum_agent_cpu_percent`
  (sum), `aum_agent_mem_mb` (sum), `aum_agent_restarts_10m` (max),
  `aum_agent_flag{flag=hot|idle|churn|leak}` 0/1, `aum_agent_gpu_mem_mb` (only
  when reported), plus host-level `aum_agents`, `aum_host_mem_used_percent`,
  `aum_host_cpu_count`. Label values escaped via `_prom_escape` (app.py:1258).

- `GET /` → `FileResponse(static/index.html)` (app.py:1330-1332); `/static`
  mount (app.py:1335).

## 4. Security primitives (app.py)

- `_state_dir() -> Path` (app.py:241-254) — macOS `~/Library/Application
  Support/agent-usage-manager`, else `$XDG_STATE_HOME` (default
  `~/.local/state`) — deliberately outside any project tree so
  directory-sandboxed agents can't read it. Holds `TOKEN_PATH`
  (`kill_token`) and `ACTION_LOG_PATH` (`actions.log`).
- `_load_kill_token() -> str` (app.py:262-291) — read existing, else
  `secrets.token_urlsafe(32)` written via `os.open(..., 0o600)` in a `0700`
  dir. Rotation = delete the file, restart. Never served over HTTP.
- `_log_action(request, pid, outcome, **extra)` (app.py:299-323) — one JSON
  line per kill attempt (success **and** refusal): ts, `client` addr, pid,
  outcome, plus redacted `target`, `method`, counts. Lock-guarded append,
  best-effort (never breaks the kill path). No rotation.
- `_redact(text) -> str` (app.py:326-341) — three regex passes applied to
  every cmdline before it reaches the browser or log: key=value / flag-value
  pairs for token/key/secret/password/auth names, and bare value shapes
  (`sk-…`, `gh[pousr]_…`, `xox[bap]-…`, JWTs) → `***`.

## 5. CLI (cli.py)

- `main()` (cli.py:79-137) — argparse surface:
  - `--host` (default `$HOST` or `127.0.0.1`), `--port` (default `$PORT` or
    `8765`), `--config` (sets `AGENTS_CONFIG`), `--no-browser`,
    `--unsafe-expose`.
  - subcommand `list [--json]` — serverless one-shot.
  - Server path: `_bind_allowed` check → print URL → `threading.Timer(1.0,
    webbrowser.open)` unless `--no-browser` → `uvicorn.run("agent_usage_manager.app:app", ...)`.
- `_bind_allowed(host, unsafe_expose) -> bool` (cli.py:10-25) — True for
  `localhost`, loopback IPs, or when `--unsafe-expose` was passed; anything
  else is a `parser.error` at startup (fail closed — the kill endpoint must
  not reach a network on a casual flag).
- `_run_list(as_json)` (cli.py:40-76) — lazily imports `app` (no uvicorn
  import, no sampler thread, so no alerts), calls `list_agents()` twice with a
  0.5s gap (first `cpu_percent` read is always 0.0), prints raw JSON or an
  aligned table (`AGENT PID CPU% MEM MB [GPU MB] UPTIME FLAG COMMAND`; the
  FLAG column shows `launchd` for supervised rows).
- `_dur(s) -> str` (cli.py:28-37) — `2d 3h` / `1h 02m` / `44m` / `12s`.

## 6. Frontend (static/index.html)

Single inline `<script>` (index.html:104-353), no framework.

- **Poll loop:** `refresh()` every 3s (index.html:351-352). On fetch failure
  the table is kept but dimmed with a stale banner (`body.stale`,
  index.html:285-293) — never blank data someone might kill from. Skips
  re-render while text is selected (copying a launchctl hint).
- **Keyed rendering:** one `<tbody>` per agent, keyed by `pid:create_time`
  (`rowKey`; the `bodies`/`expanded` maps) so a recycled pid can't inherit
  another agent's row; rows are rewritten in place, tbodys sorted
  flagged-first (hot/churn/leak/idle, then the server's CPU-desc order within
  a group) but **only reordered when the pointer is off the table**
  (`overTable`, index.html:117-120, 342-346) so the kill button can't shift
  under the cursor. The header carries the flag counts (`1 hot · 4 idle`).
- **Kill flow:** `kill(pid, force, children)` (index.html:169-192) —
  `confirm()` with the child count → `killToken()` (index.html:158-167:
  localStorage, `prompt()` on first use pointing at `token_path` from the API)
  → `POST /api/kill/{pid}` with `X-Kill-Token`. A 403 mentioning the token
  clears localStorage so the next click re-prompts (rotation-aware). Both the
  ✓ and ✗ result lines auto-clear on a `setTimeout` (longer for the ✗
  partial-failure, which needs reading time) so no outcome sticks forever.
- **Tree expansion:** `toggleTree`/`loadTree`/`renderTree`
  (index.html:213-232) fetch `/api/tree/{pid}` and insert indented child rows;
  expanded subtrees are re-fetched on every refresh (index.html:348).
- **Rendering details:** `esc()` HTML-escapes all host data (injection into a
  page with a kill endpoint is a real risk, index.html:122-128); `spark()`
  draws the SVG sparkline; badges hot/idle/churn/leak with explanatory
  tooltips (index.html:242-250; the launchd badge's tooltip follows the
  payload's `keepalive` — "won't stick" only for KeepAlive jobs); supervised
  rows swap kill buttons for a
  click-to-copy `launchctl bootout` hint; GPU column hidden when nothing
  reports GPU (`body.hide-gpu`); protected rows get disabled buttons (the
  server refuses regardless). At ≤700px rows stack (agent+badges / cpu·mem /
  actions), the sparkline hides, and the column headers drop, so the
  kill verb is on-screen at phone width.

## 7. Error handling conventions

- **Fail loud at startup:** `load_config` errors and forbidden binds abort;
  a bad `AGENTS_CONFIG` path is a startup error, not a fallback.
- **Fail soft at runtime:** hot-reload keeps the last good config and surfaces
  `config_error`; every subprocess probe (`nvidia-smi`, `launchctl`, `tmux`)
  degrades to empty on error/timeout; psutil races
  (`NoSuchProcess`/`AccessDenied`/`ZombieProcess`) degrade to name-only
  cmdlines or 0.0 readings; alert spawn and action-log writes are best-effort.
- **Never lie on the kill path:** pid identity pinned by create_time at both
  auth and signal time; `already exited` distinguished from `terminated`;
  supervised kills 409 with the real stop command instead of a fake success;
  `killed: 0` is the honest answer for cross-user targets.

## 8. Config / env surface

| Surface | Where read | Effect |
|---|---|---|
| `AGENTS_CONFIG` | app.py:36 | Path to agents.yaml (must exist; `--config` sets it) |
| `HOST`, `PORT` | cli.py:84-85, run.sh:11 | Default bind host/port |
| `XDG_STATE_HOME` | app.py:253 | Non-macOS state dir base |
| `--config / --no-browser / --unsafe-expose / --host / --port` | cli.py:84-102 | See §5 |
| `agents.yaml` keys | §1.2 | agents / protect / ignore / tmux_labels / alerts |
| `$AUM_*` env vars | app.py:717-731 | Outbound contract to the alert command |
| `kill_token`, `actions.log` | app.py:257-259 | State files in `_state_dir()` |

Tunables are module constants, not config: `_HISTORY_MAX=400` (app.py:655),
`_CHURN_LIFETIME_S=120` / `_CHURN_WINDOW_S=600` / `_CHURN_MIN_DEATHS=3`
(app.py:667-669), `_LEAK_SUSTAIN_S=900` (app.py:679), flag thresholds inside
`_trend_and_flag` (hot 90%/5m, leak +30%/128MB/15m, idle p95<2%/10m).

## 9. Tests (tests/)

- `tests/test_smoke.py` — redaction, match-target rules (basename + first
  args, deep-arg immunity), config validation errors, `TestClient` API checks,
  token gating (no/wrong/right token), 0600 token file, action-log lines,
  fail-closed bind, DNS-rebind/CSRF guards, hot reload, sustained-window flag
  logic, leak-ratchet logic, alert transition/cooldown behavior.
- `tests/test_synthetic.py` — synthetic hot/idle/churn/leak trace fixtures
  asserted against `_trend_and_flag`; `FakeProc` process tables driving
  label-level churn and leak surfacing through `list_agents()`;
  `_signal_tree` pid/create_time pinning; a real spawned-sleeper kill through
  the API (409 launchd case monkeypatched); adversarial matcher cases
  (lookalike names, cloaking via ignore, protect shielding).
- CI (`.github/workflows/ci.yml`): `pip install -e ".[dev]" && pytest -q` on
  ubuntu/macos × Python 3.9/3.12.
