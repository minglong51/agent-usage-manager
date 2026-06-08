from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import psutil
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent


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


MATCHERS, PROTECT = load_config()
IGNORE = load_ignore()
SELF_PID = os.getpid()


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


def _launchd_jobs() -> dict[int, str]:
    """Map pid -> launchd label for currently-running launchd jobs (macOS).

    Parsed from `launchctl list`, whose rows are `PID<tab>Status<tab>Label`. A
    process that appears here with a real PID is supervised by launchd: stopping
    it with a signal won't persist if the job sets KeepAlive — launchd just
    respawns it under a new PID, which reads as "the kill didn't work". The
    correct way to stop such a job is `launchctl bootout`, not a signal.

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


app = FastAPI(title="agent-usage-manager")

# Persistent Process handles so cpu_percent() reports usage since the last poll.
# Guarded by a lock because FastAPI runs sync endpoints in a threadpool, so
# concurrent requests can touch this dict at once.
_handles: dict[int, psutil.Process] = {}
_handles_lock = threading.Lock()


class Agent(BaseModel):
    pid: int
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
    gpu = _gpu_by_pid()
    jobs = _launchd_jobs()
    now = time.time()
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
        ct = info["ct"] or now
        agents.append(
            Agent(
                pid=root,
                label=label_of[root],
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
            )
        )

    live = set(meta)
    with _handles_lock:
        for pid in list(_handles):
            if pid not in live:
                _handles.pop(pid, None)

    agents.sort(key=lambda a: a.cpu_percent, reverse=True)
    return {
        "agents": [a.model_dump() for a in agents],
        "host": psutil.os.uname().nodename if hasattr(psutil.os, "uname") else "",
        "cpu_count": psutil.cpu_count(),
        "ts": now,
    }


def _signal_tree(root_pid: int, force: bool) -> list[psutil.Process]:
    """Stop the root and every descendant, skipping self/PID 1/protected.

    Uses psutil's terminate()/kill(), which map to SIGTERM/SIGKILL on POSIX and
    to TerminateProcess on Windows — so kill works cross-platform (raw
    signal.SIGKILL does not exist on Windows).
    """
    _, children, _, procmap = _collect()
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


@app.post("/api/kill/{pid}")
def kill_agent(pid: int, force: bool = False) -> dict:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise HTTPException(404, f"PID {pid} not found")

    target = _match_target(proc)
    if _label_for(target) is None and _label_for(proc.name()) is None:
        raise HTTPException(403, f"PID {pid} is not a recognized agent — refusing")
    if _is_protected(target, pid):
        raise HTTPException(403, f"PID {pid} is protected — refusing")

    # A launchd-supervised job won't die from a signal — if it sets KeepAlive,
    # launchd respawns it instantly under a new PID and the kill looks like it
    # silently failed. Don't pretend; hand back the launchctl command that
    # actually stops it. force=True can't beat KeepAlive either, so it's no
    # exception here.
    label = _launchd_jobs().get(pid)
    if label:
        hint = _stop_hint(label)
        raise HTTPException(
            409,
            f"PID {pid} is supervised by launchd (job '{label}') — a signal "
            f"won't stick because launchd respawns it. Stop it with:  {hint}  "
            f"(then `launchctl disable gui/{os.getuid()}/{label}` to keep it "
            f"from auto-starting at login).",
        )

    signaled = _signal_tree(pid, force)
    gone, alive = psutil.wait_procs(signaled, timeout=3)
    if alive and not force:  # graceful terminate didn't take — escalate to kill
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        more_gone, alive = psutil.wait_procs(alive, timeout=3)
        gone += more_gone

    return {
        "pid": pid,
        "result": "terminated" if not alive else "signal sent, some still running",
        "method": "kill" if force else "terminate",
        "killed": len(gone),
        "still_running": len(alive),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
