from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import psutil
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
AUM_API_VERSION = 1
try:
    AUM_VERSION = version("agent-usage-manager")
except PackageNotFoundError:
    AUM_VERSION = "0+unknown"


def _resolve_config() -> Path:
    env = os.environ.get("AGENTS_CONFIG")
    if env:
        return Path(env)
    cwd_cfg = Path.cwd() / "agents.yaml"
    if cwd_cfg.exists():
        return cwd_cfg
    return BASE / "agents.yaml"


CONFIG_PATH = _resolve_config()


class Matcher:
    def __init__(self, label: str, pattern: str, is_regex: bool) -> None:
        self.label = label
        self.is_regex = is_regex
        self.raw = pattern
        self.rx = re.compile(pattern, re.IGNORECASE) if is_regex else None
        self.lower = pattern.lower()

    def matches(self, text: str) -> bool:
        if self.rx is not None:
            return self.rx.search(text) is not None
        return self.lower in text.lower()


def load_config(path: Optional[Path] = None) -> tuple[list[Matcher], list[str]]:
    path = path or CONFIG_PATH
    try:
        raw = path.read_text()
    except OSError as e:
        raise RuntimeError(f"Cannot read agents config {path}: {e}")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"{path} is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a YAML mapping with an 'agents:' list")
    matchers: list[Matcher] = []
    for i, a in enumerate(data.get("agents") or []):
        if not isinstance(a, dict) or "label" not in a or "match" not in a:
            raise RuntimeError(
                f"{path}: agents[{i}] needs both 'label' and 'match' (got {a!r})"
            )
        try:
            matchers.append(Matcher(a["label"], a["match"], bool(a.get("regex", False))))
        except re.error as e:
            raise RuntimeError(
                f"{path}: agents[{i}] ({a['label']}) has an invalid regex: {e}"
            )
    protect = [str(p).lower() for p in (data.get("protect") or [])]
    return matchers, protect


def load_ignore(path: Optional[Path] = None) -> list[str]:
    """Patterns that disqualify a process from being an agent entirely.

    Unlike `protect:` (matched, listed, but kill-refused), an `ignore:` hit means
    the process is never classified as an agent at all — it won't appear in the
    dashboard and isn't killable. Used to drop incidental processes that share a
    bundle/path with a real agent: crash handlers, auto-updaters, the editor's
    own integrated-terminal shells, etc. Best-effort: the config has already been
    validated by load_config() at import, so parse errors here just mean "none".
    """
    path = path or CONFIG_PATH
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    return [str(p).lower() for p in (data.get("ignore") or [])]


def load_alerts(path: Optional[Path] = None) -> Optional[dict]:
    """Optional `alerts:` block — a shell command to run when a flag appears.

        alerts:
          command: notify.py --plain "$AUM_MSG"
          cooldown: 600                # seconds per (agent, flag) pair
          flags: [hot, churn, leak]    # which badges alert (the default)

    `idle` is deliberately NOT in the default: for a fleet of agents that wait
    for work, idle is the normal state — alerting on it turns the channel into
    wallpaper (and every server restart would re-announce the whole idle
    fleet as the windows refill). Opt in with an explicit `flags:` list.

    Best-effort like load_ignore(): a malformed block means "no alerts", never
    a startup failure.
    """
    path = path or CONFIG_PATH
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    a = data.get("alerts")
    if not isinstance(a, dict) or not a.get("command"):
        return None
    try:
        cooldown = float(a.get("cooldown", 600))
    except (TypeError, ValueError):
        cooldown = 600.0
    raw_flags = a.get("flags", ["hot", "churn", "leak"])
    if not isinstance(raw_flags, list):
        raw_flags = ["hot", "churn", "leak"]
    flags = {str(f).lower() for f in raw_flags}
    return {"command": str(a["command"]), "cooldown": cooldown, "flags": flags}


def load_tmux_labels(path: Optional[Path] = None) -> Optional[re.Pattern]:
    """Optional `tmux_labels:` regex — per-instance labels from tmux sessions.

        tmux_labels: "^bot-(.+)$"    # session bot-coder_1 → row label coder_1

    A fleet of identical agents (several claude-code bots, one per tmux
    session) all hit the same `agents:` entry and land as N indistinguishable
    rows. Their cmdlines can't tell them apart — the match target is
    deliberately the executable + first args only (see _target_from_argv), so
    a distinguishing flag deeper in argv is invisible on purpose — but the
    tmux session each one runs in IS its durable identity. When a matched
    root (or one of its ancestors) is a tmux pane process whose session name
    matches this regex, the row is labeled with the first capture group (the
    whole session name if the regex has no group). Sessions that don't match
    keep their `agents:` label, so incidental tmux use never renames rows.

    Best-effort like load_ignore(): absent or malformed means "off".
    """
    path = path or CONFIG_PATH
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    pattern = data.get("tmux_labels")
    if not isinstance(pattern, str) or not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


MATCHERS, PROTECT = load_config()
IGNORE = load_ignore()
ALERTS = load_alerts()
TMUX_LABELS = load_tmux_labels()
SELF_PID = os.getpid()

CONFIG_ERROR: Optional[str] = None
_config_lock = threading.Lock()
try:
    _config_mtime: Optional[float] = CONFIG_PATH.stat().st_mtime
except OSError:
    _config_mtime = None


def _maybe_reload_config() -> None:
    """Pick up agents.yaml edits without a server restart.

    A parse error keeps the last good config (the dashboard must not lose its
    allowlist mid-session) and is surfaced via CONFIG_ERROR so the UI can show
    it. The bad mtime is recorded so the file isn't re-parsed on every poll —
    only the next edit triggers another attempt.
    """
    global MATCHERS, PROTECT, IGNORE, ALERTS, TMUX_LABELS, CONFIG_ERROR, _config_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return
    with _config_lock:
        if mtime == _config_mtime:
            return
        _config_mtime = mtime
        try:
            matchers, protect = load_config()
        except RuntimeError as e:
            CONFIG_ERROR = str(e)
            return
        MATCHERS, PROTECT = matchers, protect
        IGNORE = load_ignore()
        ALERTS = load_alerts()
        TMUX_LABELS = load_tmux_labels()
        CONFIG_ERROR = None


def _state_dir() -> Path:
    """Per-user state dir for the kill token and the action log.

    Deliberately OUTSIDE the package and any project tree: agent sandboxes are
    typically scoped to a working directory, so a file under the OS app-support
    dir is readable by the operator but not by a sandboxed agent on the same
    box — that asymmetry is what the kill token's security rests on.
    macOS: ~/Library/Application Support; elsewhere: XDG state dir.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "agent-usage-manager"


STATE_DIR = _state_dir()
TOKEN_PATH = STATE_DIR / "kill_token"
ACTION_LOG_PATH = STATE_DIR / "actions.log"


def _load_kill_token() -> str:
    """The static token that authorizes kill CALLERS. (The TARGET is authorized
    separately, by the allowlist re-match in kill_agent — two different
    questions: the re-match says what may be killed, the token says who may
    kill.)

    The monitored agents are themselves untrusted callers: any prompt-injected
    agent with an HTTP tool and localhost reach can POST /api/kill, and Origin
    headers are trivially forged outside a browser — so the browser guard
    can't tell the operator's curl from an agent's. The boundary is file
    permissions instead: a 0600 file under the user's app-support dir, which
    the operator can read and a sandboxed agent can't. The token is NEVER
    served over HTTP (any process that can curl this server could read it) —
    the dashboard asks the operator to paste it once and keeps it in the
    browser's localStorage, which same-host curl can't reach.

    Auto-generated on first run; delete the file to rotate.
    """
    try:
        tok = TOKEN_PATH.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


KILL_TOKEN = _load_kill_token()

_action_log_lock = threading.Lock()


def _log_action(request: Request, pid: int, outcome: str, **extra) -> None:
    """Append one JSON line per kill attempt — successes AND refusals.

    A monitor that can kill must be able to answer "what was killed at 3am"
    after the fact, and the refusal lines (403/409) are the interesting ones
    when something on the box is probing the endpoint. Append-only, no
    rotation: one line per kill attempt stays tiny. Best-effort like the
    alert spawn — logging must never break the kill path itself.
    """
    if "target" in extra:
        extra["target"] = _redact(str(extra["target"]))
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "client": f"{request.client.host}:{request.client.port}" if request.client else "?",
        "pid": pid,
        "outcome": outcome,
        **extra,
    }
    try:
        with _action_log_lock:
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(ACTION_LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


_SECRET_KV = re.compile(
    r"(?i)([\w.-]*(?:token|key|secret|password|passwd|api[_-]?key|auth)[\w.-]*\s*[=:]\s*)\S+"
)
_SECRET_FLAG = re.compile(
    r"(?i)(--?(?:token|key|secret|password|api[_-]?key|auth)\S*\s+)\S+"
)
_SECRET_VALUE = re.compile(
    r"\b(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[bap]-[A-Za-z0-9-]{8,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})"
)


def _redact(text: str) -> str:
    text = _SECRET_KV.sub(r"\1***", text)
    text = _SECRET_FLAG.sub(r"\1***", text)
    text = _SECRET_VALUE.sub("***", text)
    return text


def _cmdline(proc: psutil.Process) -> str:
    try:
        raw = " ".join(proc.cmdline()) or proc.name()
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess):
        try:
            raw = proc.name()
        except psutil.Error:
            return ""
    return _redact(raw)


_APP_BUNDLE = re.compile(r"/([^/]+)\.app/")


def _target_from_argv(argv: Optional[list], name: str) -> str:
    """Matching text: executable basename + first few args.

    Deliberately NOT the full command line — a long embedded argument (e.g. a
    system prompt that happens to contain the word "claude") must not cause a
    parent/wrapper process to be misclassified as an agent.

    On macOS the executable basename is often generic (Kiro.app and many other
    Electron apps launch a binary literally named "Electron"), so the outermost
    `.app` bundle name from argv[0] is prepended — that's the app's real
    identity. Only the bundle name is added, never arbitrary path/arg text, so
    the deep-arg protection above is preserved.
    """
    if argv:
        head = [a for a in argv[:4] if a]
        if head:
            exe = head[0]
            head[0] = os.path.basename(exe)
            target = " ".join(head)
            bundle = _APP_BUNDLE.search(exe)
            if bundle:
                target = f"{bundle.group(1)} {target}"
            return target
    return name


def _match_target(proc: psutil.Process) -> str:
    """One-off match target for a single Process (used off the hot path)."""
    try:
        argv = proc.cmdline()
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess):
        argv = None
    try:
        name = proc.name()
    except psutil.Error:
        name = ""
    return _target_from_argv(argv, name)


def _ignored(text: str) -> bool:
    low = text.lower()
    return any(ig in low for ig in IGNORE)


def _label_for(text: str) -> Optional[str]:
    if _ignored(text):
        return None
    for m in MATCHERS:
        if m.matches(text):
            return m.label
    return None


def _is_protected(text: str, pid: int) -> bool:
    if pid in (SELF_PID, 1):
        return True
    low = text.lower()
    return any(p in low for p in PROTECT)


_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fn):
    """Memoize fn() for `ttl` seconds. Collapses the per-request subprocess
    calls (launchctl list, nvidia-smi) that back slowly-changing data, so a 3s
    poll plus an immediate kill don't each re-shell-out."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    val = fn()  # computed outside the lock so slow subprocesses don't serialize
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def _keepalive(label: str) -> bool:
    """True if the launchd job has a KeepAlive policy — i.e. launchd respawns it
    on exit, so a signal genuinely won't stick. Read from `launchctl print`'s
    `properties = …` line. Best-effort: any error returns False so the caller
    uses milder 'restarts at login' wording and never over-claims.
    """
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    for line in out.splitlines():
        s = line.strip().lower()
        if s.startswith("properties =") and "keepalive" in s:
            return True
    return False


def _launchd_jobs() -> dict[int, str]:
    """Map pid -> launchd label for currently-running launchd jobs (macOS).

    Parsed from `launchctl list`, whose rows are `PID<tab>Status<tab>Label`. A
    process that appears here with a real PID is supervised by launchd: stopping
    it with a signal won't persist if the job sets KeepAlive — launchd just
    respawns it under a new PID, which reads as "the kill didn't work". The
    correct way to stop such a job is `launchctl bootout`, not a signal.

    Scope limitation: this runs in the caller's (user) launchd domain, so it
    only sees `gui/$UID` LaunchAgents — not root `LaunchDaemons`, which require
    a privileged `launchctl print system/…`. A root daemon therefore won't be
    flagged here; that's a known gap, documented rather than papered over.

    Empty on non-macOS or when launchctl is unavailable.
    """
    if not shutil.which("launchctl"):
        return {}
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    jobs: dict[int, str] = {}
    for line in out.splitlines()[1:]:  # skip the PID/Status/Label header
        parts = line.split("\t")
        if len(parts) >= 3:
            pid_s = parts[0].strip()
            # Not-running jobs show "-" in the PID column; skip those.
            if pid_s.isdigit() and int(pid_s) > 0:
                jobs[int(pid_s)] = parts[2].strip()
    return jobs


def _stop_hint(label: str) -> str:
    """The appropriate `launchctl` command to actually stop a supervised job."""
    return f"launchctl bootout gui/{os.getuid()}/{label}"


def _tmux_panes() -> dict[int, str]:
    """Map pane pid → tmux session name for every live pane.

    Empty when tmux is absent or no server is running. The pid comes first in
    the format string because session names may themselves contain spaces.
    """
    if not shutil.which("tmux"):
        return {}
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
            capture_output=True,
            text=True,
            timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    panes: dict[int, str] = {}
    for line in out.splitlines():
        pid_s, _, session = line.partition(" ")
        if pid_s.isdigit() and session:
            panes[int(pid_s)] = session
    return panes


def _instance_label(root: int, base: str, meta: dict, panes: dict[int, str]) -> str:
    """Display label for one agent root: tmux-derived when configured and
    applicable, else the matcher label.

    Nearest enclosing pane wins: the root often IS the pane process (a shell
    that exec'd the agent), otherwise the ancestor walk finds the pane shell
    that spawned it. A root inside a session that doesn't match the
    `tmux_labels:` regex keeps the base label — the walk stops at its own
    pane rather than borrowing an outer session's name.
    """
    rx = TMUX_LABELS
    if rx and panes:
        for pid in (root, *_ancestors(root, meta)):
            session = panes.get(pid)
            if session is None:
                continue
            mo = rx.search(session)
            if mo:
                return (mo.group(1) if mo.groups() else mo.group(0)) or base
            break
    return base


def _gpu_by_pid() -> dict[int, float]:
    """Best-effort per-process GPU memory (MiB) via nvidia-smi. Empty on macOS."""
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    result: dict[int, float] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit():
            result[int(parts[0])] = float(parts[1])
    return result


_sampler_started = threading.Event()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # The Event guards against double-start under test clients that enter the
    # lifespan repeatedly; the thread itself (defined below) is a daemon.
    if not _sampler_started.is_set():
        _sampler_started.set()
        threading.Thread(target=_sampler_loop, daemon=True, name="aum-sampler").start()
    yield


app = FastAPI(title="agent-usage-manager", lifespan=_lifespan)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _hostname_of(hostport: str) -> str:
    try:
        return (urlsplit(f"//{hostport}").hostname or "").lower()
    except ValueError:
        return ""


def _host_allowed(hostport: str) -> bool:
    """True for loopback names and bare IP-literal hosts.

    Rejecting non-local DNS names defeats DNS rebinding: a rebinding attack has
    to arrive via the attacker's domain, so its Host header carries that domain
    — never a bare IP. IP literals stay allowed so LAN access under
    `--host 0.0.0.0` (the user's explicit opt-in) keeps working.
    """
    host = _hostname_of(hostport)
    if host in _LOCAL_HOSTS:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@app.middleware("http")
async def _browser_guard(request: Request, call_next):
    # Binding to 127.0.0.1 does NOT keep browsers out: any web page the user
    # visits can fire fetch() at this port. Two checks close that off without
    # breaking curl or the dashboard itself:
    #  1. Host must be local/IP-literal (DNS-rebinding guard, all requests).
    #  2. A state-changing request carrying a foreign Origin is a cross-site
    #     call — refuse it (CSRF guard for /api/kill). Same-origin posts from
    #     the dashboard carry a matching Origin; CLI tools send none at all.
    #     `Origin: null` (sandboxed iframe, file://) is foreign by this rule.
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse(
            {"detail": "Host header is not a local address — refusing (DNS-rebinding guard)"},
            status_code=403,
        )
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
        try:
            o_host = (urlsplit(origin).hostname or "").lower()
        except ValueError:
            o_host = ""
        if o_host != _hostname_of(request.headers.get("host", "")) and o_host not in _LOCAL_HOSTS:
            return JSONResponse(
                {"detail": "Cross-origin request refused (CSRF guard)"},
                status_code=403,
            )
    return await call_next(request)

# Persistent Process handles so cpu_percent() reports usage since the last poll.
# Guarded by a lock because FastAPI runs sync endpoints in a threadpool, so
# concurrent requests can touch this dict at once.
_handles: dict[int, psutil.Process] = {}
_handles_lock = threading.Lock()

# Per-agent CPU/mem samples, keyed by (pid, create_time) so a recycled pid
# starts a fresh series instead of inheriting the dead agent's history.
_HISTORY_MAX = 400  # ~20 min at the 3s cadence
_history: dict[tuple[int, float], deque] = {}
_history_lock = threading.Lock()
_last_collect = 0.0  # monotonic time of the last full list_agents() pass

# Restart churn. A crash-looping agent is invisible to hot/idle: every poll
# shows a fresh pid whose history just started, so no per-incarnation window
# ever fills (seen live: a KeepAlive launchd job respawning every second).
# The durable identity across restarts is the matcher label, so deaths are
# tracked per label: several young deaths in a short window means a supervisor
# keeps respawning a process that keeps dying. All three dicts below are
# guarded by _history_lock.
_CHURN_LIFETIME_S = 120.0  # a death this young counts toward churn
_CHURN_WINDOW_S = 600.0  # deaths are remembered this long
_CHURN_MIN_DEATHS = 3  # young deaths in the window needed to flag
_label_of_key: dict[tuple[int, float], str] = {}
_churn_deaths: dict[str, deque] = {}


def _note_death_locked(key: tuple[int, float], now: float) -> None:
    """Record a vanished agent root (caller holds _history_lock)."""
    label = _label_of_key.pop(key, None)
    if label is None or now - key[1] > _CHURN_LIFETIME_S:
        return
    _churn_deaths.setdefault(label, deque(maxlen=64)).append(now)


def _restarts_in_window(label: str, now: float) -> int:
    with _history_lock:
        dq = _churn_deaths.get(label)
        if not dq:
            return 0
        while dq and now - dq[0] > _CHURN_WINDOW_S:
            dq.popleft()
        if not dq:
            _churn_deaths.pop(label, None)
            return 0
        return len(dq)


# Alerting: a dashboard only helps while someone is looking at it (a real
# crash loop once ran for 6 days unseen). When configured, a flag APPEARING
# on a label runs the user's alert command. Fires on transitions only — an
# ongoing condition alerts once, the dashboard owns ongoing state — with a
# per-(label, flag) cooldown so a flapping flag can't spam.
_alert_lock = threading.Lock()
_prev_flag: dict[str, Optional[str]] = {}
_last_alert: dict[tuple[str, str], float] = {}


def _spawn_alert(command: str, a: "Agent", host: str) -> None:
    # Runtime data rides environment variables, never the shell string — the
    # command text itself comes only from the user's own config file.
    env = {
        **os.environ,
        "AUM_MSG": (
            f"[agent-usage-manager] {a.label} is {a.flag} on {host or 'this host'}: "
            f"cpu {a.cpu_percent:.0f}%, mem {a.mem_mb:.0f}MB, "
            f"restarts(10m) {a.restarts}, pid {a.pid}, up {int(a.uptime_s)}s"
        ),
        "AUM_LABEL": a.label,
        "AUM_FLAG": a.flag or "",
        "AUM_PID": str(a.pid),
        "AUM_CPU": f"{a.cpu_percent:.1f}",
        "AUM_MEM_MB": f"{a.mem_mb:.0f}",
        "AUM_RESTARTS": str(a.restarts),
        "AUM_HOST": host,
    }
    try:
        subprocess.Popen(
            command,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _check_alerts(agents: list["Agent"], now: float, host: str) -> None:
    # Flags live per label (several rows can share one), so transitions are
    # tracked per label too: first flagged row represents the label.
    flagged: dict[str, Agent] = {}
    seen: set[str] = set()
    for a in agents:
        seen.add(a.label)
        if a.flag and a.label not in flagged:
            flagged[a.label] = a
    cfg = ALERTS
    with _alert_lock:
        for label, a in flagged.items():
            if a.flag != _prev_flag.get(label) and cfg and a.flag in cfg["flags"]:
                key = (label, a.flag)
                if now - _last_alert.get(key, 0.0) >= cfg["cooldown"]:
                    _last_alert[key] = now
                    _spawn_alert(cfg["command"], a, host)
        # Transitions are tracked even with no alert command configured, so
        # enabling alerts later doesn't instantly fire for long-standing flags.
        for label in seen:
            a = flagged.get(label)
            _prev_flag[label] = a.flag if a else None
        for label in list(_prev_flag):
            if label not in seen:
                _prev_flag.pop(label)


def _trend_and_flag(key: tuple[int, float], uptime_s: float) -> tuple[list[float], Optional[str]]:
    """Recent CPU series for the row sparkline, plus a sustained-state flag.

    hot  — mean CPU >= 90% of one core across a fully-covered 5-minute window:
           the agent has been pegged, not momentarily busy.
    leak — RSS up >= 30% AND >= 128 MB across a fully-covered 15-minute
           window, with the recent FLOOR above the old median: a sawtooth
           (allocate, GC, repeat) dips back down and must not flag — only a
           ratchet that never gives the memory back does.
    idle — alive >= 10 minutes with p95 CPU under 2% across a fully-covered
           10-minute window: plausibly wedged, worth a look (idle isn't proof
           of wedged, so the UI words it as a question, not a verdict). p95
           rather than max: a single GC/housekeeping blip in an otherwise dead
           process must not suppress the flag (seen live: an Electron agent at
           0.36% mean with one 2.9% sample).
    All require the window to actually span the threshold duration — a
    just-started series must not flag off two samples.
    """
    now = time.time()
    with _history_lock:
        hist = list(_history.get(key, ()))
    trend = [c for _, c, _ in hist[-40:]]
    if not hist:
        return trend, None
    span = now - hist[0][0]
    win5 = [c for t, c, _ in hist if now - t <= 300]
    win10 = [c for t, c, _ in hist if now - t <= 600]
    if span >= 300 and win5 and sum(win5) / len(win5) >= 90.0:
        return trend, "hot"
    if span >= 900 and uptime_s >= 900:
        win15 = [(t, m) for t, _, m in hist if now - t <= 900]
        if win15:
            t0 = win15[0][0]
            head = sorted(m for t, m in win15 if t - t0 <= 180)
            tail = sorted(m for t, m in win15 if now - t <= 180)
            if head and tail:
                med_head, med_tail = head[len(head) // 2], tail[len(tail) // 2]
                if (
                    med_tail >= med_head * 1.3
                    and med_tail - med_head >= 128.0
                    and tail[0] > med_head
                ):
                    return trend, "leak"
    if span >= 600 and uptime_s >= 600 and win10:
        p95 = sorted(win10)[int(0.95 * (len(win10) - 1))]
        if p95 < 2.0:
            return trend, "idle"
    return trend, None


def _sampler_loop() -> None:
    """Keep history accruing while no browser is polling, so the dashboard
    shows real trends the moment it's opened — without double-sampling when
    the 3s frontend poll is already driving collection."""
    while True:
        time.sleep(3.0)
        if time.monotonic() - _last_collect > 3.0:
            try:
                list_agents()
            except Exception:
                pass


class Agent(BaseModel):
    pid: int
    # Process start time from the OS. Consumers should pair this with pid when
    # caching rows so PID reuse does not make two different agents look identical.
    create_time: float
    label: str
    name: str
    cmdline: str
    status: str
    alive: bool
    cpu_percent: float
    mem_mb: float
    gpu_mem_mb: Optional[float]
    uptime_s: float
    child_count: int
    protected: bool
    # When set, this agent is supervised by launchd (value = its launchd label).
    # A signal won't stop it for good; `stop_hint` is the launchctl command that will.
    supervised: Optional[str] = None
    stop_hint: Optional[str] = None
    # Recent CPU samples (sparkline) and the sustained-state flag
    # ("hot"/"idle"/"churn"/"leak").
    trend: list[float] = []
    flag: Optional[str] = None
    # Short-lived deaths under this label in the last 10 minutes (the count
    # behind a "churn" flag; informative even below the flag threshold).
    restarts: int = 0


def _collect() -> tuple[dict, dict, dict, dict]:
    """One pass over all processes.

    Returns (meta, children, label_of, procmap):
      meta[pid]     = {ppid, name, status, ct}
      children[ppid]= [pid, ...]
      label_of[pid] = matcher label (only present for matched processes)
      procmap[pid]  = the psutil.Process from this poll
    """
    meta: dict[int, dict] = {}
    children: dict[int, list[int]] = {}
    label_of: dict[int, str] = {}
    procmap: dict[int, psutil.Process] = {}
    attrs = ["pid", "ppid", "name", "status", "create_time", "cmdline"]
    for proc in psutil.process_iter(attrs):
        pid = proc.info["pid"]
        ppid = proc.info.get("ppid") or 0
        name = proc.info.get("name") or ""
        meta[pid] = {
            "ppid": ppid,
            "name": name,
            "status": proc.info.get("status") or "?",
            "ct": proc.info.get("create_time"),
        }
        children.setdefault(ppid, []).append(pid)
        procmap[pid] = proc
        # Use the cmdline psutil already batched into proc.info (one read per
        # process) rather than a second cmdline() syscall per process.
        label = _label_for(_target_from_argv(proc.info.get("cmdline"), name))
        if label:
            label_of[pid] = label
    return meta, children, label_of, procmap


def _ancestors(pid: int, meta: dict):
    seen: set[int] = set()
    cur = meta.get(pid, {}).get("ppid", 0)
    while cur and cur not in seen:
        seen.add(cur)
        yield cur
        cur = meta.get(cur, {}).get("ppid", 0)


def _descendants(root: int, children: dict) -> list[int]:
    out: list[int] = []
    stack = list(children.get(root, []))
    seen: set[int] = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        stack.extend(children.get(p, []))
    return out


def _cpu_mem(pid: int, procmap: dict) -> tuple[float, float]:
    with _handles_lock:
        handle = _handles.get(pid)
        if handle is None or handle.pid != pid:
            handle = procmap.get(pid)
            if handle is None:
                try:
                    handle = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    return 0.0, 0.0
            _handles[pid] = handle
            try:
                handle.cpu_percent(None)
            except psutil.Error:
                pass
    try:
        return handle.cpu_percent(None), handle.memory_info().rss / (1024 * 1024)
    except psutil.Error:
        return 0.0, 0.0


@app.get("/api/agents")
def list_agents() -> dict:
    global _last_collect
    _maybe_reload_config()
    gpu = _cached("gpu", 2.0, _gpu_by_pid)
    jobs = _cached("launchd", 2.0, _launchd_jobs)
    panes = _cached("tmux", 2.0, _tmux_panes) if TMUX_LABELS else {}
    now = time.time()
    _last_collect = time.monotonic()
    meta, children, label_of, procmap = _collect()
    matched = set(label_of)

    # An agent's "root" is a matched process with no matched ancestor; any matched
    # descendant is rolled up into it rather than shown as its own row.
    roots = [
        pid
        for pid in matched
        if not any(a in matched for a in _ancestors(pid, meta))
    ]

    agents: list[Agent] = []
    current_keys: set[tuple[int, float]] = set()
    for root in roots:
        subtree = [root] + _descendants(root, children)
        cpu = mem = gpu_sum = 0.0
        has_gpu = False
        for p in subtree:
            c, m = _cpu_mem(p, procmap)
            cpu += c
            mem += m
            if p in gpu:
                gpu_sum += gpu[p]
                has_gpu = True
        info = meta[root]
        rproc = procmap.get(root)
        cmd = _cmdline(rproc) if rproc else info["name"]
        # The per-instance label (tmux-derived when configured) is used for
        # everything identity-shaped downstream — churn tracking, alert
        # transitions, /metrics series — so a fleet of same-matcher bots gets
        # per-bot state instead of one blurred series.
        label = _instance_label(root, label_of[root], meta, panes)
        ct = info["ct"] or now
        hkey = (root, ct)
        current_keys.add(hkey)
        with _history_lock:
            series = _history.get(hkey)
            if series is None:
                series = _history[hkey] = deque(maxlen=_HISTORY_MAX)
            series.append((now, round(cpu, 1), round(mem, 1)))
            _label_of_key[hkey] = label
        trend, flag = _trend_and_flag(hkey, now - ct)
        restarts = _restarts_in_window(label, now)
        if restarts >= _CHURN_MIN_DEATHS:
            # Churn outranks hot/idle: a respawning process can't accrue
            # either window, and the loop itself is the urgent signal.
            flag = "churn"
        agents.append(
            Agent(
                pid=root,
                create_time=round(ct, 3),
                label=label,
                name=info["name"],
                cmdline=cmd[:300],
                status=info["status"],
                alive=info["status"] != psutil.STATUS_ZOMBIE,
                cpu_percent=round(cpu, 1),
                mem_mb=round(mem, 1),
                gpu_mem_mb=round(gpu_sum, 1) if has_gpu else None,
                uptime_s=round(now - ct, 0),
                child_count=len(subtree) - 1,
                protected=_is_protected(
                    _match_target(rproc) if rproc else info["name"], root
                ),
                supervised=jobs.get(root),
                stop_hint=_stop_hint(jobs[root]) if root in jobs else None,
                trend=trend,
                flag=flag,
                restarts=restarts,
            )
        )

    live = set(meta)
    with _handles_lock:
        for pid in list(_handles):
            if pid not in live:
                _handles.pop(pid, None)
    with _history_lock:
        for key in list(_history):
            if key not in current_keys:
                _history.pop(key, None)
                _note_death_locked(key, now)

    agents.sort(key=lambda a: a.cpu_percent, reverse=True)
    vm = psutil.virtual_memory()
    host = psutil.os.uname().nodename if hasattr(psutil.os, "uname") else ""
    # Alerts only in server mode (the sampler marks it): a one-shot `list`
    # invocation inspecting current state must not fire notification commands.
    if _sampler_started.is_set():
        _check_alerts(agents, now, host)
    return {
        "api_version": AUM_API_VERSION,
        "aum_version": AUM_VERSION,
        "agents": [a.model_dump() for a in agents],
        "host": host,
        "cpu_count": psutil.cpu_count(),
        "mem_total_mb": round(vm.total / (1024 * 1024), 0),
        "mem_used_pct": vm.percent,
        "config_path": str(CONFIG_PATH),
        "config_error": CONFIG_ERROR,
        # The PATH of the kill-token file, so the dashboard's paste prompt can
        # point the operator at it. The path is not the secret — a curl-capable
        # agent learning it changes nothing, since the file itself is 0600 and
        # outside its sandbox.
        "token_path": str(TOKEN_PATH),
        "ts": now,
    }


def _signal_tree(root: psutil.Process, force: bool) -> list[psutil.Process]:
    """Stop the root and every descendant, skipping self/PID 1/protected.

    `root` is the handle that passed authorization in kill_agent — its cached
    create_time pins the process identity. The fresh _collect() below re-finds
    the pid; if the original process exited in between and the OS reused its
    pid, the create_times differ and nothing is signaled. Without this check a
    just-spawned unrelated process could be killed under the dead agent's pid.

    Uses psutil's terminate()/kill(), which map to SIGTERM/SIGKILL on POSIX and
    to TerminateProcess on Windows — so kill works cross-platform (raw
    signal.SIGKILL does not exist on Windows).
    """
    root_pid = root.pid
    _, children, _, procmap = _collect()
    current = procmap.get(root_pid)
    try:
        if current is None or current.create_time() != root.create_time():
            return []
    except psutil.Error:
        return []
    victims = [root_pid] + _descendants(root_pid, children)
    signaled: list[psutil.Process] = []
    for p in victims:
        if p in (SELF_PID, 1):
            continue
        proc = procmap.get(p)
        if proc is None:
            try:
                proc = psutil.Process(p)
            except psutil.NoSuchProcess:
                continue
        try:
            if _is_protected(_match_target(proc), p):
                continue
            proc.kill() if force else proc.terminate()
            signaled.append(proc)
        except psutil.Error:
            pass
    return signaled


@app.get("/api/tree/{pid}")
def agent_tree(pid: int) -> dict:
    """The processes inside an agent's subtree — what a kill would actually hit.

    Authorized exactly like kill: only a recognized agent root may be inspected,
    so the endpoint can't be used to walk arbitrary process trees.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise HTTPException(404, f"PID {pid} not found")
    if _label_for(_match_target(proc)) is None:
        raise HTTPException(403, f"PID {pid} is not a recognized agent — refusing")

    meta, children, _, procmap = _collect()
    rows: list[dict] = []
    stack: list[tuple[int, int]] = [(pid, 0)]
    seen: set[int] = set()
    while stack:
        p, depth = stack.pop()
        if p in seen or p not in meta:
            continue
        seen.add(p)
        cpu, mem = _cpu_mem(p, procmap)
        pr = procmap.get(p)
        rows.append(
            {
                "pid": p,
                "name": meta[p]["name"],
                "cpu_percent": round(cpu, 1),
                "mem_mb": round(mem, 1),
                "cmdline": (_cmdline(pr) if pr else meta[p]["name"])[:200],
                "depth": depth,
            }
        )
        # reversed → DFS pops keep sibling order, so rows read parent-first
        for child in reversed(children.get(p, [])):
            stack.append((child, depth + 1))
    return {"pid": pid, "tree": rows}


@app.post("/api/kill/{pid}")
def kill_agent(pid: int, request: Request, force: bool = False) -> dict:
    # Caller authorization comes FIRST and is a separate question from the
    # target authorization below: the token says who may kill, the allowlist
    # re-match says what may be killed. Without this gate, every monitored
    # agent with an HTTP tool and localhost reach holds a fleet-wide kill
    # switch (see _load_kill_token for the threat model). compare_digest on
    # bytes: constant-time, and header values aren't guaranteed ASCII.
    sent = request.headers.get("x-kill-token", "")
    if not secrets.compare_digest(sent.encode(), KILL_TOKEN.encode()):
        _log_action(request, pid, "403 wrong-token" if sent else "403 no-token")
        raise HTTPException(
            403,
            "Kill requires the X-Kill-Token header — the token is in "
            f"{TOKEN_PATH} (operator-readable file; never served over HTTP).",
        )
    _maybe_reload_config()
    try:
        proc = psutil.Process(pid)
        proc.create_time()  # cache identity now — _signal_tree compares against it
    except psutil.NoSuchProcess:
        _log_action(request, pid, "404 no-such-pid")
        raise HTTPException(404, f"PID {pid} not found")

    # Authorize exactly as listing does: a single _label_for(target) check.
    # _match_target already falls back to the process name when the cmdline is
    # unreadable, and _label_for returns None for ignored processes — so an
    # `ignore:`d process is never killable (no proc.name() fallback to slip
    # through, which previously bypassed the ignore list).
    target = _match_target(proc)
    if _label_for(target) is None:
        _log_action(request, pid, "403 not-an-agent", target=target[:120])
        raise HTTPException(403, f"PID {pid} is not a recognized agent — refusing")
    if _is_protected(target, pid):
        _log_action(request, pid, "403 protected", target=target[:120])
        raise HTTPException(403, f"PID {pid} is protected — refusing")

    # A launchd-supervised job is stopped via launchctl, not a signal — so route
    # the caller there instead of silently failing. The wording is tailored to
    # whether the job actually has KeepAlive (signal truly won't stick) vs. only
    # RunAtLoad (signal works now but it restarts at next login) so we never
    # over-claim. force=True can't beat KeepAlive either, so it's no exception.
    label = _cached("launchd", 2.0, _launchd_jobs).get(pid)
    if label:
        hint = _stop_hint(label)
        disable = f"launchctl disable gui/{os.getuid()}/{label}"
        why = (
            "a signal won't stick — launchd respawns it (KeepAlive)"
            if _keepalive(label)
            else "a signal stops it now but launchd restarts it at next login (RunAtLoad)"
        )
        _log_action(request, pid, "409 launchd-supervised", target=target[:120], job=label)
        raise HTTPException(
            409,
            f"PID {pid} is supervised by launchd (job '{label}') — {why}. "
            f"Stop it with:  {hint}  (then `{disable}` to keep it from "
            f"auto-starting).",
        )

    signaled = _signal_tree(proc, force)
    method = "kill" if force else "terminate"
    if not signaled and not proc.is_running():
        # Exited between authorization and signaling (is_running() is pid-reuse
        # aware) — report that rather than a fake "terminated".
        _log_action(request, pid, "already exited", target=target[:120], method=method)
        return {
            "pid": pid,
            "result": "already exited",
            "method": method,
            "killed": 0,
            "still_running": 0,
        }
    gone, alive = psutil.wait_procs(signaled, timeout=3)
    if alive and not force:  # graceful terminate didn't take — escalate to kill
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        more_gone, alive = psutil.wait_procs(alive, timeout=3)
        gone += more_gone

    result = "terminated" if not alive else "signal sent, some still running"
    _log_action(
        request, pid, result, target=target[:120], method=method,
        killed=len(gone), still_running=len(alive),
    )
    return {
        "pid": pid,
        "result": result,
        "method": method,
        "killed": len(gone),
        "still_running": len(alive),
    }


_FLAG_VALUES = ("hot", "idle", "churn", "leak")


def _prom_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """Prometheus text exposition, aggregated per agent label.

    Per-label (not per-pid) on purpose: pids churn, and every dead pid would
    linger as a stale Prometheus series. Multiple instances of one label are
    summed (instances has the count); restarts takes the max since the
    10-minute death count is already label-level.
    """
    data = list_agents()
    by_label: dict[str, dict] = {}
    for a in data["agents"]:
        d = by_label.setdefault(
            a["label"],
            {"n": 0, "cpu": 0.0, "mem": 0.0, "gpu": 0.0, "has_gpu": False,
             "restarts": 0, "flags": set()},
        )
        d["n"] += 1
        d["cpu"] += a["cpu_percent"]
        d["mem"] += a["mem_mb"]
        if a["gpu_mem_mb"] is not None:
            d["gpu"] += a["gpu_mem_mb"]
            d["has_gpu"] = True
        d["restarts"] = max(d["restarts"], a["restarts"])
        if a["flag"]:
            d["flags"].add(a["flag"])

    out = [
        "# HELP aum_agent_instances Matched process trees for this agent label",
        "# TYPE aum_agent_instances gauge",
        "# HELP aum_agent_cpu_percent Sum of tree CPU%% across instances (100 = one core)",
        "# TYPE aum_agent_cpu_percent gauge",
        "# HELP aum_agent_mem_mb Sum of tree RSS in MiB across instances",
        "# TYPE aum_agent_mem_mb gauge",
        "# HELP aum_agent_restarts_10m Short-lived deaths under this label in the last 10 minutes",
        "# TYPE aum_agent_restarts_10m gauge",
        "# HELP aum_agent_flag Sustained-state flag is currently raised (hot/idle/churn/leak)",
        "# TYPE aum_agent_flag gauge",
    ]
    for label in sorted(by_label):
        d = by_label[label]
        ql = _prom_escape(label)
        out.append(f'aum_agent_instances{{agent="{ql}"}} {d["n"]}')
        out.append(f'aum_agent_cpu_percent{{agent="{ql}"}} {d["cpu"]:.1f}')
        out.append(f'aum_agent_mem_mb{{agent="{ql}"}} {d["mem"]:.1f}')
        out.append(f'aum_agent_restarts_10m{{agent="{ql}"}} {d["restarts"]}')
        for f in _FLAG_VALUES:
            out.append(
                f'aum_agent_flag{{agent="{ql}",flag="{f}"}} {1 if f in d["flags"] else 0}'
            )
        if d["has_gpu"]:
            out.append(f'aum_agent_gpu_mem_mb{{agent="{ql}"}} {d["gpu"]:.1f}')
    out += [
        "# HELP aum_agents Total matched agent instances",
        "# TYPE aum_agents gauge",
        f"aum_agents {len(data['agents'])}",
        "# HELP aum_host_mem_used_percent Host memory in use",
        "# TYPE aum_host_mem_used_percent gauge",
        f"aum_host_mem_used_percent {data['mem_used_pct']}",
        "# HELP aum_host_cpu_count Host logical CPU count",
        "# TYPE aum_host_cpu_count gauge",
        f"aum_host_cpu_count {data['cpu_count']}",
    ]
    return PlainTextResponse(
        "\n".join(out) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
