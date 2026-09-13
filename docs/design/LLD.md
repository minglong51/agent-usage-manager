# agent-usage-manager — Low-Level Design

**Refreshed:** 2026-09-13 (0.3.0 release metadata and operator reference).

Code layout: one FastAPI module (`agent_usage_manager/app.py`), one CLI module
(`agent_usage_manager/cli.py`), one static frontend
(`agent_usage_manager/static/index.html`). Python ≥3.9, type-hinted, no ORM,
no async I/O beyond FastAPI's own plumbing (all endpoints are sync `def`s run
in the threadpool).

## 1. Configuration

### 1.1 Resolution & hot reload (app.py)

- `_resolve_config() -> Path` (app.py:37-47) — resolved **once at import**:
  `$AGENTS_CONFIG` env var (set by the `--config` flag, cli.py:110-111) →
  `./agents.yaml` in the launch cwd → an untracked package-local
  `agent_usage_manager/agents.yaml` when present in a source checkout → the
  sanitized package-bundled `agents.default.yaml`. Stored in module-global
  `CONFIG_PATH`.
- `load_config(path) -> tuple[list[Matcher], list[str]]` (app.py:64-89) —
  strict loader for `agents:` + `protect:`; raises `RuntimeError` on unreadable
  file, invalid YAML, non-mapping root, missing `label`/`match`, or a bad
  regex. This is the only fail-loud loader — it runs at import, so a broken
  config is a startup failure.
- `load_ignore(path) -> list[str]` (app.py:92-109), `load_alerts(path) ->
  Optional[dict]` (app.py:112-163), `load_tmux_labels(path) ->
  Optional[re.Pattern]` (app.py:166-197), `load_launchd_labels(path) ->
  Optional[re.Pattern]` (app.py:200-230), `load_idle_ok(path) -> list[str]`
  (app.py:233-255) — best-effort loaders: any error means
  "feature off", never a crash (config validity was already gated by
  `load_config`).
- `_maybe_reload_config()` (app.py:281-319) — called at the top of
  `list_agents()` and `kill_agent()`; compares `CONFIG_PATH` mtime under
  `_config_lock`, re-runs all six loaders on change. A parse error keeps the
  last good config and sets `CONFIG_ERROR` (surfaced in the API/header); the
  bad mtime is recorded so the file isn't re-parsed every poll. A config that
  VANISHED (stat fails) likewise keeps the last good config but sets a
  "missing — running on the last good config" `CONFIG_ERROR` — deletion must
  not look like hot-reload still works; the error clears when the file
  returns, including the rename-back case where the mtime is unchanged.

### 1.2 agents.yaml schema (`agents.default.yaml` is the public example)

```yaml
agents:                       # required: the allowlist (list AND killability)
  - label: claude-code        # row badge / metrics label
    match: "claude(\\s|$|-code)"  # substring (default) or regex, case-insensitive
    regex: true               # optional, default false
protect: [uvicorn, ...]       # matched + listed, kill refused (substring, lowercased)
ignore: [crashpad, ...]       # never an agent: not listed, not killable
tmux_labels: "^bot-(.+)$"     # optional: per-instance label from tmux session name
launchd_labels: "^com\\.example\\.agent\\.(.+)$"
                              # optional: per-instance label from the launchd job label
idle_ok: [worker, ...]        # optional: labels whose idle is NORMAL — no idle badge
                              # (matches the instance label AND the base matcher label)
alerts:                       # optional: shell command on badge appearance
  command: '...$AUM_MSG...'   # run via shell; data rides $AUM_* env vars only
  cooldown: 600               # seconds per (label, flag) pair; default 600
  flags: [hot, churn, leak]   # default; idle is opt-in
  leak_floor_mb: 1536         # gate leak ALERTS (not the badge) below this RSS; default 0
  dashboard_url: https://monitor.example/aum
                              # optional, operator-owned base URL for observation links
```

### 1.3 Matching semantics

- `class Matcher` (app.py:50-61) — `matches(text)` is case-insensitive
  regex-search or substring.
- `_target_from_argv(argv, name) -> str` (app.py:439-461) — the match target is
  **executable basename + argv[1:4]**, never the full command line (a deep arg
  like a system prompt mentioning "claude" must not misclassify a wrapper). On
  macOS the outermost `.app` bundle name from argv[0] is prepended
  (`_APP_BUNDLE` regex, app.py:436) so Electron agents match by app name.
- `_label_for(text) -> Optional[str]` (app.py:483-489) — `None` if any
  `ignore:` pattern hits (`_ignored`, app.py:478), else first matcher's label.
- `_is_protected(text, pid) -> bool` (app.py:492-500) — PID == self or 1, or
  any `protect:` substring hit.

## 2. Process collection & telemetry (app.py)

- `_collect() -> (meta, children, label_of, procmap)` (app.py:1057-1088) — one
  `psutil.process_iter` pass with batched attrs. `meta[pid] = {ppid, name,
  status, ct}`, `children[ppid] = [pid...]`, `label_of[pid]` only for matched
  processes, `procmap[pid]` = live psutil handles.
- `_ancestors(pid, meta)` (app.py:1091-1097, generator) / `_descendants(root,
  children) -> list[int]` (app.py:1100-1111) — cycle-safe tree walks. An agent
  **root** is a matched pid with no matched ancestor (app.py:971-975); matched
  descendants roll up into it.
- `_cpu_mem(pid, procmap) -> (cpu%, mem_mb)` (app.py:1114-1132) — persistent
  `psutil.Process` handles in `_handles` (lock-guarded) so `cpu_percent(None)`
  measures since-last-poll; first sight primes the counter (reads 0.0 once).
- `_project_name(proc) -> str` reads a root process's working directory and
  returns its redacted basename, `Home directory` for the user's home, or an
  empty string when access is unavailable. It does not discover repository or
  task identity, and the full directory path is not included in the response.
- `_cached(key, ttl, fn)` (app.py:503-515) — TTL memo for the subprocess
  shell-outs, computed outside the lock. Users: `_gpu_by_pid()` (app.py:650,
  `nvidia-smi --query-compute-apps`, MiB per pid), `_launchd_jobs()`
  (app.py:540, `launchctl list` → pid→label, user domain only),
  `_tmux_panes()` (app.py:584, `tmux list-panes -a` → pane pid→session name).
  All 2s TTL, 4s subprocess timeout, empty on any failure — except the
  per-label `keepalive:<label>` entries behind the Agent payload field, which
  cache `_keepalive()` for 60s (a job's KeepAlive policy is plist-static).
- `_keepalive(label) -> bool` (app.py:518-537) — `launchctl print
  gui/$UID/<label>` grep for KeepAlive; False on any error (milder wording,
  never over-claims). Backs the supervised-row `keepalive` payload field and
  the 409 wording on the kill path.
- `_instance_identity(root, base, meta, panes, jobs) -> tuple[str, str]` —
  per-instance identity, two configured sources in precedence order:
  (1) walk root + ancestors for the nearest tmux pane; if its session matches
  `tmux_labels:`, the first capture group becomes the row label (whole match if
  no group), else stop — a root in a non-matching session never borrows an
  outer session's name; (2) else the root's own launchd job label matched
  against `launchd_labels:` — first capture group, or the whole job label when
  the regex has no group (a prefix-only match would make a useless label).
  Fallback is the matcher label. The second value is `tmux`, `launchd`, or
  `matcher`; `_instance_label(...)` retains the existing string-returning
  interface. This is display and metrics grouping only.
  `instance_id` uses `launchd:<job>` for supervised roots and
  `process:<pid>:<create_time>` otherwise; alerts use this identity rather
  than a potentially shared display label.

### 2.1 History, flags, churn (module globals, app.py:761-789)

- `_history: dict[(pid, create_time), deque[(ts, cpu, mem_mb)]]`, maxlen
  `_HISTORY_MAX = 400` (~20 min at 3s). Keyed on `(pid, create_time)` so a
  recycled pid starts fresh. Guarded by `_history_lock` along with
  `_label_of_key`, `_churn_deaths`, `_leak_since`.
- `_history_assessment(key, uptime_s) -> (trend, flag, evidence)` owns the
  condition evaluation. `_trend_and_flag(...)` retains its two-value interface:
  - `trend` = last 40 CPU samples (sparkline).
  - `hot` — mean CPU ≥ 90% over a **fully-covered** 5-min window.
  - `leak` — over a 15-min window: tail median ≥ 1.3× head median, delta
    ≥ 128 MB, tail floor above head median, and the condition sustained for
    `_LEAK_SUSTAIN_S = 900` s (`_leak_since`) so a transient heavyweight child
    (headless-browser render) doesn't flag but a real ratchet does.
  - `idle` — uptime & span ≥ 10 min with p95 CPU < 2% (p95 not max, so one GC
    blip can't suppress it).
  - All windows require actual span coverage — a young series never flags.
  - Evidence is the statistic actually evaluated: five-minute CPU mean;
    fifteen-minute baseline/tail median, growth, floor, and sustained duration;
    or ten-minute p95 CPU. It includes thresholds, interval, sample timestamp,
    and count. Churn adds scoped exit times, count, lifetime limit, and threshold.
    `idle_ok` suppression clears both the flag and its evidence.
- Churn: a crash loop is invisible to hot/idle (fresh pid every poll),
  so deaths are tracked by supervisor identity. `_note_death_locked`
  records a vanished root that died younger than `_CHURN_LIFETIME_S = 120` s;
  `_restarts_in_window(label, now)` counts deaths within
  `_CHURN_WINDOW_S = 600` s; ≥ `_CHURN_MIN_DEATHS = 3` forces
  `flag = "churn"` for that launchd job, outranking hot/idle.
  Unsupervised exits use a separate `runtime:<matcher label>` bucket and
  appear in top-level `runtime_exits`; their rows retain `restarts=0` and
  `last_restart=null`. Neither a shared matcher nor tmux session proves that
  an exit was a crash. Removing a still-live root through config changes or
  tree regrouping does not record a death.
  `_last_death_in_window(identity, now)` reads the newest in-window death
  (read-only; `_restarts_in_window` owns pruning) for the `last_restart`
  payload field.

### 2.2 Alerts

- `_alert_key(agent)` returns `(instance_id, flag)`, falling back to the label
  for callers constructing an `Agent` without identity. `_prev_flag` records
  condition state; `_alert_deliveries` owns `_AlertDelivery` records containing
  the current agent, command, host, attempts, next attempt, status, error, and
  reservation timestamp. `_last_alert` remains the cooldown ledger.
- `_check_alerts(agents, now, host)` creates a pending delivery on a selected
  flag transition. Below-floor leaks remain unflagged. An appearance inside
  cooldown waits until cooldown expires; confirmed failures retry even while
  the flag stays unchanged. Clearing a condition or disabling its alert
  removes pending work. Enabling alerts does not announce pre-existing flags.
- `_spawn_alert(command, agent, host)` dispatches outside `_alert_lock` using
  the existing shell command and `AUM_*` environment fields. `_finish_alert`
  accepts a callback only for the same delivery record, so late callbacks
  cannot overwrite a newer condition. Nonzero exits and spawn errors refund
  the reservation and retry after five, then ten seconds, with three attempts
  maximum. A sixty-second timeout becomes `unconfirmed` without retry because
  the command may still deliver. Success becomes `delivered`, meaning exit 0
  from the configured command, not independently verified downstream receipt.
- `_delivery_snapshot()` returns identity, label, flag, status, attempts,
  next retry time, and a bounded error summary. Command text and environment
  values are not exposed. Subprocess stderr is redacted in server logs.
- Alerts run only after `_sampler_started`; one-shot `list` never sends them.
- `_record_events(agents, now, host)` captures each `(pid, create_time, flag)`
  onset into `_events`, with a random public observation ID, the root snapshot,
  evaluated evidence, and up to 400 timestamped CPU/RSS observations. Active
  observations update only `last_observed_at`; the onset evidence stays fixed.
  Clearance sets `status=cleared`; a root leaving the sample sets
  `status=not_observed`, which does not assert that the OS process exited.
  `_event_lock` guards `_events` and `_active_events`; retention is at most
  100 observations and 48 hours from onset. Each record carries `retained_until`
  as its maximum retention deadline; the count cap may evict it earlier. An
  expired ongoing condition does not mint another event until its condition
  changes. Restart clears history.
- `_inspection_path(agent)` selects `/?event=<id>` when the bounded observation
  exists, otherwise `/?pid=<pid>&create_time=<ct>`. `_inspection_url` prefixes
  optional `alerts.dashboard_url`; the loader accepts only HTTP(S) base URLs
  without embedded credentials, whitespace, a query, or a fragment. Missing or
  invalid bases leave absolute links disabled. Alerts add `AUM_CREATE_TIME`,
  `AUM_EVENT_ID`, `AUM_INSPECT_PATH`, and `AUM_INSPECT_URL` to the existing
  environment contract. `AUM_TITLE` contains the measured plain-English verdict
  from `_alert_title`, without diagnostic metadata or a URL; `AUM_MSG` retains its
  one-line compatibility format. The CLI supplies a synthetic test title. A
  configured absolute link is appended to the existing
  one-line `AUM_MSG`; hot/leak text uses the measured condition when available.
  The CLI's synthetic `test-alert` leaves these four observation fields empty.

### 2.3 Background sampler

- `_lifespan` publishes the first sample, then starts `_sampler_loop`.
  `_sample_agents() -> tuple[dict, dict]` owns collection, CPU reads, history,
  flags, and alert evaluation. `_tree_rows(...)` builds child detail from
  the same per-process measurements. `_publish_snapshot()` atomically stores
  data and trees under `_snapshot_lock`, clearing the collection error.
- Server reads return copies of the last sample. Browser, expanded-tree,
  and Prometheus traffic cannot change the sampling cadence or CPU baseline.
  The loop sleeps `_SAMPLE_INTERVAL_S=3` between collections; failures are
  logged and surfaced as 503. `_SNAPSHOT_MAX_AGE_S=10` also rejects a stalled
  collector. Without a running sampler, `list_agents()` publishes a one-shot
  collection for CLI use and direct invocation.

## 3. HTTP surface (app.py)

### 3.1 Middleware — `_browser_guard` (app.py:724-750)

Applies to every request:
1. **DNS-rebinding guard:** `Host` must be `localhost`/`127.0.0.1`/`::1` or an
   IP literal (`_host_allowed`, app.py:705-721) → else 403.
2. **CSRF guard:** a non-GET/HEAD/OPTIONS request with an `Origin` whose host
   differs from `Host` and isn't local → 403. `Origin: null` counts as
   foreign; CLI tools send no Origin and pass.

### 3.2 Endpoints

- `GET /api/agents` → `list_agents() -> dict` returns the cached server
  snapshot. The sampler's `_sample_agents()` collection path is:
  reload config → cached gpu/launchd/tmux maps → `_collect()` → compute roots
  → per root: sum tree cpu/mem/gpu, derive instance label, append history,
  `_trend_and_flag`, churn override, `idle_ok:` suppression (an `idle` flag on
  a waiting-class label is dropped — idle is that label's NORMAL state, so the
  badge would be wallpaper; matched against the instance label AND the base
  matcher label, so a `tmux_labels:`/`launchd_labels:` rename can't lose the
  suppression) → prune dead handles/history (recording
  deaths) → sort by CPU desc → maybe `_check_alerts`. Response shape:

  ```json
  { "api_version": 2, "aum_version": "0.3.0",
    "agents": [Agent...], "host": "...", "cpu_count": N,
    "mem_total_mb": N, "mem_used_pct": N,
    "config_path": "...", "config_error": null,
    "token_path": "...", "ts": epoch, "sample_interval_s": 3,
    "sample_age_s": seconds, "runtime_exits": [], "alert_deliveries": [],
    "events": [] }
  ```

  `token_path` is deliberately non-secret (the file is 0600; knowing the path
  changes nothing — app.py:1246-1250).

- **`Agent` model** (pydantic `BaseModel`, app.py:1020-1054): `pid`,
  `create_time` (unrounded OS timestamp), `label`, `runtime`, `instance_id`,
  `project` (working-directory basename, possibly empty), `name`,
  `cmdline` (redacted, truncated to 300), `status`, `alive` (not zombie),
  `cpu_percent`/`mem_mb`/`gpu_mem_mb` (tree totals; gpu `None` when no data),
  `uptime_s`, `child_count`, `protected`, `supervised` (launchd label or
  `None`), `stop_hint` (launchctl bootout command), `keepalive` (bool or
  `None` — whether the supervising job has KeepAlive, so consumers can word
  the supervision note precisely), `trend: list[float]`,
  `flag: Optional[str]` in {hot, idle, churn, leak}, `restarts: int`,
  `last_restart: Optional[float]` (epoch of the newest short-lived supervised
  exit). `runtime_exits` entries contain runtime, short-lived exit count,
  and last-exit epoch; they never imply a specific live process restarted.
  Added fields: `label_source`, `child_pids` (descendant lookup), nullable
  `tree_revision` (captured identity/protection fingerprint), nullable
  `evidence` (active condition), and nullable `event_id`.

- `GET /api/tree/{pid}?create_time=...` → `agent_tree(pid, create_time) -> dict`. 404 if
  no such pid; 403 unless `_label_for(_match_target(proc))` hits (same target
  authorization as kill — can't walk arbitrary trees). A supplied identity
  mismatch or an unsampled root returns 409. Returns cached DFS rows from
  the sampler, with response `pid`, `create_time`, `ts`, and `tree` fields;
  rows contain `{pid, create_time, protected, name, cpu_percent, mem_mb,
  cmdline[:200], depth}`. Response also includes `tree_revision`, `evidence`,
  and up to 400 `history` triples `[timestamp, cpu_percent, mem_mb]` through
  the captured sample timestamp. History reads do not evaluate conditions or
  advance CPU baselines. `_tree_revision(rows)` hashes sorted PID/create-time/
  protection tuples; missing or nonfinite creation times return null.

- `GET /api/events` returns bounded observation summaries and `retention_s`.
  `GET /api/events/{id}` returns the captured root, evidence/history, and
  current observation status plus `retained_until`. The browser uses that deadline
  for its retention label (one-hour fallback for earlier API v2 servers). Missing,
  expired, or restart-lost IDs return 404
  with an explicit retention explanation; these endpoints never select a
  replacement process or perform an action.

- `POST /api/kill/{pid}?force=false&create_time=...&tree_revision=...` → `kill_agent(...)`.
  The tree revision is optional for existing API v2 callers; creation time
  remains required. New clients can opt into the stronger scope precondition.
  Ordered gates, each refusal action-logged:
  1. **Caller auth:** `X-Kill-Token` vs `KILL_TOKEN` via
     `secrets.compare_digest` on bytes (constant-time) → 403
     `wrong-token`/`no-token` (app.py:1356-1363).
  2. Config reload; `psutil.Process(pid)` + `create_time()` cached to pin
     identity → 404 `no-such-pid`.
  3. **Target auth:** `_label_for(_match_target(proc))` → 403 `not-an-agent`;
     `_is_protected` → 403 `protected`.
  4. **Supervision:** pid in launchd jobs → 409 with `launchctl bootout`
     guidance, wording split on `_keepalive` (won't stick vs restarts at
     login) (app.py:1390-1405).
  5. **Displayed identity:** missing `create_time` → 428; non-finite or
     unequal to the current process's creation time → 409. Both are logged.
     The browser sends the exact timestamp retained when opening confirmation.
  6. `_signal_tree(proc, force, tree_revision=None, audit=None)`: fresh `_collect()`,
     **create_time compared** against the pinned handle (pid reuse → signal
     nothing), then capture all identities/protection states. A supplied revision
     mismatch raises 409 before any signal. Capture and signal errors are
     recorded as skipped identities with reasons. Eligible handles receive
     `terminate()`/`kill()`, skipping self/PID 1/protected. psutil methods map to SIGTERM/SIGKILL on POSIX,
     TerminateProcess on Windows.
  7. `psutil.wait_procs(timeout=3)`; a non-force kill **auto-escalates**
     survivors to `kill()` + another 3s wait (app.py:1420-1428). Returns
     `{pid, result, method, killed, still_running}`; a target that exited
     between auth and signal returns `result: "already exited"` (pid-reuse
     aware via `is_running()`).
     Additional fields are `captured`, `signaled`, `skipped`, `skipped_count`,
     `stopped`, `survivors`, `tree_revision`, and `scope_checked`. `killed` and
     `still_running` retain their counts within the signaled set. Any skipped
     process changes result wording; no claim covers subsequently born processes.
     `scope_checked` records an attempted tree comparison, including a mismatch;
     a root that disappears before capture still records false. A continuously
     changing tree can keep failing this precondition; the browser does not
     silently drop it or broaden the signal set.
     Early no-signal results omit stopped/survivor lists. Action logs include
     root creation time and the same captured identity accounting.

- `GET /metrics` → `metrics() -> PlainTextResponse` (app.py:1460-1525).
  Calls `list_agents()`, aggregates **per label** (pids churn; per-pid series
  would go stale in Prometheus): `aum_agent_instances`, `aum_agent_cpu_percent`
  (sum), `aum_agent_mem_mb` (sum), `aum_agent_restarts_10m` (max),
  `aum_agent_flag{flag=hot|idle|churn|leak}` 0/1, `aum_agent_gpu_mem_mb` (only
  when reported), plus host-level `aum_agents`, `aum_host_mem_used_percent`,
  `aum_host_cpu_count`. `aum_runtime_short_lived_exits_10m{runtime=...}`
  exposes uncorrelated exits separately. Label values are escaped through
  `_prom_escape`.

- `GET /` → `FileResponse(static/index.html)` with `Cache-Control: no-cache`
  so later visits revalidate the inline interface after deployment; `/static`
  mount (app.py:1532).

## 4. Security primitives (app.py)

- `_state_dir() -> Path` (app.py:322-335) — macOS `~/Library/Application
  Support/agent-usage-manager`, else `$XDG_STATE_HOME` (default
  `~/.local/state`) — deliberately outside any project tree so
  directory-sandboxed agents can't read it. Holds `TOKEN_PATH`
  (`kill_token`) and `ACTION_LOG_PATH` (`actions.log`).
- `_load_kill_token() -> str` (app.py:343-371) — read existing, else
  `secrets.token_urlsafe(32)` written via `os.open(..., 0o600)` in a `0700`
  dir. Rotation = delete the file, restart. Never served over HTTP.
- `_log_action(request, pid, outcome, **extra)` (app.py:380-412) — one JSON
  line per kill attempt (success **and** refusal): ts, `client` addr, pid,
  outcome, plus redacted `target`, `method`, counts. Lock-guarded append,
  best-effort (never breaks the kill path). No rotation.
- `_redact(text) -> str` (app.py:418-423) — three regex passes applied to
  every cmdline before it reaches the browser or log: key=value / flag-value
  pairs for token/key/secret/password/auth names, and bare value shapes
  (`sk-…`, `gh[pousr]_…`, `xox[bap]-…`, JWTs) → `***`.

## 5. CLI (cli.py)

- `main()` (cli.py:167-232) — argparse surface:
  - `--host` (default `$HOST` or `127.0.0.1`), `--port` (default `$PORT` or
    `8765`), `--config` (sets `AGENTS_CONFIG`), `--no-browser`,
    `--unsafe-expose`.
  - subcommand `list [--json]` — serverless one-shot; `test-alert` — fires the
    configured alert command once, synchronously, and reports delivery.
    Both also accept their own `--config` (with `default=argparse.SUPPRESS` so
    a subparser default can't clobber a top-level `--config` given before the
    subcommand).
  - Server path: `_bind_allowed` check → print URL →
    `_open_when_ready(url, host, port)` (cli.py:31-45: poll the port, open the
    browser only once it accepts — never a fixed timer; IPv6 literals get
    bracketed in the printed/opened URL) unless `--no-browser` →
    `uvicorn.run("agent_usage_manager.app:app", ...)`.
- `_bind_allowed(host, unsafe_expose) -> bool` (cli.py:13-28) — True for
  `localhost`, loopback IPs, or when `--unsafe-expose` was passed; anything
  else is a `parser.error` at startup (fail closed — the kill endpoint must
  not reach a network on a casual flag). A reverse proxy can still expose the
  loopback bind: the proxy is not itself a trust boundary, read endpoints need
  access control, and the static kill token remains the action boundary.
- `_run_list(as_json)` (cli.py:60-107) — lazily imports `app` (no uvicorn
  import, no sampler thread, so no alerts), calls `list_agents()` twice with a
  0.5s gap (first `cpu_percent` read is always 0.0), prints raw JSON or an
  aligned table (`AGENT PID CPU% MEM MB [GPU MB] UPTIME FLAG COMMAND`; the
  FLAG column shows `launchd` for supervised rows). One-shot mode has no
  history, so hot/idle/churn/leak can never populate: table mode prints a
  stderr note saying so, `--json` adds `"flags_available": false` — a cron
  check on `flag` from one-shot output must not silently never fire.
- `_run_test_alert() -> int` (cli.py:109-164) — runs the resolved config's
  `alerts.command` once via `shell=True` with the same `$AUM_*` env contract
  as a real firing and a synthetic `Test alert …` message; waits (60s cap) and
  reports exit status (0 = delivered, 1 = failed with stderr, 2 = no alerts
  configured). The alert channel otherwise only proves itself during a real
  incident. Touches no cooldown/transition state.
- `_dur(s) -> str` (cli.py:48-57) — `2d 3h` / `1h 02m` / `44m` / `12s`.

## 6. Frontend (static/index.html)

One static HTML document with embedded CSS and JavaScript; no framework or build step.

- **Layout:** a full-width resource table until a process or observation is selected;
  the optional desktop inspector then sits beside it. At widths up to 760px,
  selection hides the table/intro/totals and opens the inspector with Back/Close.
  The phone page scrolls normally rather than nesting a fixed-height table.
  Labels lead each row; directory, runtime, PID, age, supervision, and ambiguous
  shared names remain visible. Aligned CPU/RSS values and CPU sparklines support
  scanning. Warning-first then CPU is the default sort; a counted warning filter,
  runtime filter, and explicit CPU/memory sorting operate on the current snapshot.
  The identity header is hidden inside an iframe. Light/dark palettes follow the
  system preference or the browser's `aum-theme` choice; serif prose, 12px panels,
  9px inputs, and pill actions follow the approved mock.
- **Identity and selection:** `keyOf` pairs PID with exact creation time. Persistent
  row elements in `rowEls` preserve list focus; sorting pauses while the pointer
  or keyboard focus is inside the list. `selectedKey` never switches to a new
  incarnation of the same PID. A missing selection retains its last identity
  with an explicit unavailable state and no stop action.
  Exact numeric searches also match `child_pids`, locating the owning root.
- **Navigation:** `setRoute` writes either `?pid=<pid>&create_time=<ct>` or
  `?event=<id>`; `readRoute` supports direct entry and browser Back/Forward.
  A process link requires both identity fields and never follows PID reuse.
  `openObservation` validates the public observation ID and reads its bounded
  endpoint. An incrementing request identity prevents a late response from
  replacing a newer selection. Missing/expired/restart-lost observations render
  an explicit unavailable state. Saved views have no stop control: the quiet
  `Inspect current process` action exists only for the same PID/create-time pair
  in the latest snapshot and transitions to a fresh live review.
- **Polling:** `loop()` schedules `refreshNow()` every three seconds after the
  previous read completes. `refreshPending` deduplicates regular and post-stop
  refreshes; `fetchSnapshot()` aborts a hung request after eight seconds. Cached
  data stays visible on errors. `isStale()` combines the server's sample age
  with locally elapsed time; a 500ms watchdog updates the warning and disables
  both inspector and open-dialog stop actions independently of fetch completion.
  Text selection pauses rerendering without bypassing freshness checks.
- **Inspector:** source labels and OS status are separate from unavailable task
  progress. Resource totals, optional GPU memory, and supervised exit counts use
  supplied evidence. `evidenceHTML` renders the evaluated statistic, threshold,
  interval, timestamp, and count. `chartHTML` uses timestamped CPU/RSS samples;
  memory is primary for a growth warning, CPU otherwise, with the other metric
  in a disclosure. CPU sparklines are the fallback when history is unavailable;
  a missing memory series is explicit. Saved observations use their frozen onset
  samples and status rather than current resource numbers. Observation, command,
  and child-tree folds preserve reading state; no history is invented. Command
  text remains redacted and truncated by the API; the copy control identifies
  it as the displayed excerpt.
- **Tree detail:** `loadTree(key)` requests `/api/tree/{pid}?create_time=...` for
  the selected incarnation. `treePending` deduplicates per-key requests and
  `treeCache` stores their results. The selected live tree refreshes with the
  main poll so charts do not depend on opening the child fold. `readJSON` caps
  tree/event requests at eight seconds. A tree response renders only while its
  exact key remains selected. Nonselected departed-root cache entries are pruned.
  Cache entries retain history, evidence, revision, and timestamp along with rows.
  Each child has a disclosure for its timestamp, CPU, protection, and redacted
  command, so command inspection also works with touch and keyboard.
- **Stop review:** `openStop()` captures an immutable `dialogSnap` in a body-level
  native dialog. PID, exact start time, label, command excerpt, child count, and
  signal behavior are shown before confirmation. The primary choice sends
  SIGTERM and escalates survivors to SIGKILL after three seconds. A secondary
  choice switches to an explicit immediate-SIGKILL confirmation. `stopProblem()`
  rechecks freshness, current identity, protection, supervision, and the captured
  tree revision before a
  request; token entry is followed by another check. `pendingStop` prevents
  duplicate submission. The POST includes the captured `create_time` and
  `X-Kill-Token`, plus `tree_revision` when the server supplies one. A changed
  revision disables the open review; a null revision makes the root unavailable
  for stop. Older API v2 servers lacking the field retain root-only behavior and
  the dialog explicitly discloses that limitation. An open dialog never follows
  refreshed row data to a new process. Child counts are described as observed,
  and copy states that protection and later spawning can leave survivors.
- **Authorization and feedback:** `killToken()` retains the existing operator
  paste flow and browser-local token storage; the token is never fetched over
  HTTP. A token-related 403 clears the stored value. Protected processes have
  no active stop control. Supervised processes show a copyable service stop
  command with KeepAlive/RunAtLoad guidance; the browser never executes it.
  Clipboard failures are visible and allow manual selection. Success, partial
  completion, and error feedback live outside the refreshing inspector for
  12–20 seconds. Any skipped process is reported as partial completion rather
  than a green whole-tree success.
- **Evidence and escaping:** uncorrelated runtime exits appear once per runtime,
  never as a session restart. Monitor details contains configuration/delivery
  detail and the bounded Recent warnings list. Configuration errors and
  failed/retrying/unconfirmed alert delivery also get a visible summary above
  the table. Host-supplied strings are escaped before HTML
  insertion. Search, inspector, modal, warning, and result surfaces have
  accessible labels or status roles.

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
| `AGENTS_CONFIG` | app.py:38 | Path to agents.yaml (must exist; `--config` sets it) |
| `HOST`, `PORT` | cli.py:84-85, run.sh:11 | Default bind host/port |
| `XDG_STATE_HOME` | app.py:333 | Non-macOS state dir base |
| `--config / --no-browser / --unsafe-expose / --host / --port` | cli.py:84-102 | See §5 |
| `agents.yaml` keys | §1.2 | agents / protect / ignore / tmux_labels / launchd_labels / idle_ok / alerts |
| `$AUM_*` env vars | app.py:717-731 | Outbound contract to the alert command |
| `kill_token`, `actions.log` | app.py:337-340 | State files in `_state_dir()` |

Tunables are module constants, not config: `_HISTORY_MAX=400` (app.py:761),
`_CHURN_LIFETIME_S=120` / `_CHURN_WINDOW_S=600` / `_CHURN_MIN_DEATHS=3`
(app.py:773-775), `_LEAK_SUSTAIN_S=900` (app.py:785), flag thresholds inside
`_trend_and_flag` (hot 90%/5m, leak +30%/128MB/15m, idle p95<2%/10m).

## 9. Tests (tests/)

- `test_reliability.py` covers the v2 stop precondition, shared sampler reads and
  stale recovery, runtime versus supervised exits, cwd-basename projection, and
  alert delivery retries, cancellation, callback isolation, and cooldown scope.
- `test_frontend.js` runs six behavioral checks in Node's built-in test runner
  against the served inline script, using isolated DOM/network/clock fixtures.
  It checks open-dialog staleness, PID replacement, token-entry delay, poll
  deduplication, stop submission/completion, and escaping. `test_smoke.py` runs
  it when Node is available; Python-only environments skip that one wrapper.
  These are logic tests; responsive, theme, and iframe checks use a browser.

- `tests/test_smoke.py` — redaction, match-target rules (basename + first
  args, deep-arg immunity), config validation errors, `TestClient` API checks,
  token gating (no/wrong/right token), 0600 token file, action-log lines,
  fail-closed bind, DNS-rebind/CSRF guards, hot reload (including the
  deleted-config error surfacing AND clearing), sustained-window flag logic,
  leak-ratchet logic, alert transition/cooldown behavior, `launchd_labels`
  loader, `test-alert` exit codes + env contract, `list --json`
  `flags_available`, `--config`-before-subcommand ordering.
- `tests/test_synthetic.py` — synthetic hot/idle/churn/leak trace fixtures
  asserted against `_trend_and_flag`; `FakeProc` process tables driving
  label-level churn, leak surfacing, and tmux/launchd instance-label
  precedence through `list_agents()`; `_signal_tree` pid/create_time pinning;
  a real spawned-sleeper kill through the API (409 launchd case monkeypatched);
  adversarial matcher cases (lookalike names, cloaking via ignore, protect
  shielding).
- CI (`.github/workflows/ci.yml`): `pip install -e ".[dev]" && pytest -q` on
  ubuntu/macos × Python 3.9/3.12.
