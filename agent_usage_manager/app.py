from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
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


def load_config() -> tuple[list[Matcher], list[str]]:
    data = yaml.safe_load(CONFIG_PATH.read_text())
    matchers = [
        Matcher(a["label"], a["match"], bool(a.get("regex", False)))
        for a in data.get("agents", [])
    ]
    protect = [p.lower() for p in data.get("protect", [])]
    return matchers, protect


MATCHERS, PROTECT = load_config()
SELF_PID = os.getpid()


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline()) or proc.name()
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess):
        try:
            return proc.name()
        except psutil.Error:
            return ""


def _label_for(text: str) -> Optional[str]:
    for m in MATCHERS:
        if m.matches(text):
            return m.label
    return None


def _is_protected(text: str, pid: int) -> bool:
    if pid in (SELF_PID, 1):
        return True
    low = text.lower()
    return any(p in low for p in PROTECT)


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
_handles: dict[int, psutil.Process] = {}


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
    protected: bool


@app.get("/api/agents")
def list_agents() -> dict:
    gpu = _gpu_by_pid()
    seen: set[int] = set()
    agents: list[Agent] = []
    now = time.time()

    for proc in psutil.process_iter(["pid", "name", "status", "create_time"]):
        pid = proc.info["pid"]
        cmd = _cmdline(proc)
        label = _label_for(cmd) or _label_for(proc.info.get("name") or "")
        if not label:
            continue
        seen.add(pid)
        handle = _handles.get(pid)
        if handle is None or handle.pid != pid:
            handle = proc
            _handles[pid] = handle
            try:
                handle.cpu_percent(None)
            except psutil.Error:
                pass
        try:
            cpu = handle.cpu_percent(None)
            mem = handle.memory_info().rss / (1024 * 1024)
            status = handle.status()
            alive = handle.is_running() and status != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            cpu, mem, status, alive = 0.0, 0.0, "dead", False
        except psutil.AccessDenied:
            cpu, mem = 0.0, 0.0
            status, alive = proc.info.get("status", "?"), True

        ct = proc.info.get("create_time") or now
        agents.append(
            Agent(
                pid=pid,
                label=label,
                name=proc.info.get("name") or "",
                cmdline=cmd[:300],
                status=status,
                alive=alive,
                cpu_percent=round(cpu, 1),
                mem_mb=round(mem, 1),
                gpu_mem_mb=gpu.get(pid),
                uptime_s=round(now - ct, 0),
                protected=_is_protected(cmd, pid),
            )
        )

    for pid in list(_handles):
        if pid not in seen:
            _handles.pop(pid, None)

    agents.sort(key=lambda a: a.cpu_percent, reverse=True)
    return {
        "agents": [a.model_dump() for a in agents],
        "host": psutil.os.uname().nodename if hasattr(psutil.os, "uname") else "",
        "cpu_count": psutil.cpu_count(),
        "ts": now,
    }


@app.post("/api/kill/{pid}")
def kill_agent(pid: int, force: bool = False) -> dict:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise HTTPException(404, f"PID {pid} not found")

    cmd = _cmdline(proc)
    if _label_for(cmd) is None and _label_for(proc.name()) is None:
        raise HTTPException(403, f"PID {pid} is not a recognized agent — refusing")
    if _is_protected(cmd, pid):
        raise HTTPException(403, f"PID {pid} is protected — refusing")

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        proc.send_signal(sig)
    except psutil.NoSuchProcess:
        return {"pid": pid, "result": "already gone"}
    except psutil.AccessDenied:
        raise HTTPException(403, f"Access denied sending signal to PID {pid}")

    try:
        proc.wait(timeout=3)
        return {"pid": pid, "result": "terminated", "signal": sig.name}
    except psutil.TimeoutExpired:
        if not force:
            try:
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=3)
                return {"pid": pid, "result": "killed", "signal": "SIGKILL"}
            except psutil.Error:
                pass
        return {"pid": pid, "result": "signal sent, still running", "signal": sig.name}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
