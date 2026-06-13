from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from typing import Callable, Optional

import pytest
from fastapi.testclient import TestClient

from agent_usage_manager import app as m


@pytest.fixture
def client():
    return TestClient(m.app, base_url="http://127.0.0.1")


TOKEN = {"x-kill-token": m.KILL_TOKEN}


@pytest.fixture(autouse=True)
def action_log(tmp_path, monkeypatch):
    log = tmp_path / "actions.log"
    monkeypatch.setattr(m, "ACTION_LOG_PATH", log)
    return log


@pytest.fixture
def sleeper():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.1)
    yield p
    if p.poll() is None:
        p.kill()
    p.wait()


def _series(now: float, duration_s: float, fn: Callable[[float], tuple]) -> deque:
    n = int(duration_s / 3)
    return deque(
        [(now - duration_s + i * 3, *fn(i * 3.0)) for i in range(n)],
        maxlen=m._HISTORY_MAX,
    )


TRACES: dict = {
    "memory_leaker_monotonic": (1200, lambda t: (5.0, 200.0 + t / 3.0), 1200, "leak"),
    "runaway_cpu_spinner": (600, lambda t: (100.0, 300.0), 600, "hot"),
    "gc_sawtooth": (1200, lambda t: (5.0, 200.0 + (t % 150.0) * 4.0), 1200, None),
    "short_cpu_burst": (600, lambda t: (100.0 if t >= 540 else 1.0, 300.0), 700, None),
    "crash_loop_single_incarnation": (90, lambda t: (100.0, 200.0), 90, None),
    "steady_worker": (1200, lambda t: (40.0, 500.0), 1200, None),
}


@pytest.mark.parametrize("name", sorted(TRACES))
def test_trace_fixture_flags_as_specified(name):
    duration, fn, uptime, expected = TRACES[name]
    now = time.time()
    key = (88000 + sorted(TRACES).index(name), 1.0)
    try:
        with m._history_lock:
            m._history[key] = _series(now, duration, fn)
        assert m._trend_and_flag(key, uptime)[1] == expected
    finally:
        with m._history_lock:
            m._history.pop(key, None)


class FakeProc:
    def __init__(self, pid: int, argv: list, name: str = "", ct: float = 0.0) -> None:
        self.pid = pid
        self._argv = argv
        self._name = name or os.path.basename(argv[0]) if argv else name
        self._ct = ct
        self.signaled: list = []

    def cmdline(self) -> list:
        return list(self._argv)

    def name(self) -> str:
        return self._name

    def create_time(self) -> float:
        return self._ct

    def terminate(self) -> None:
        self.signaled.append("terminate")

    def kill(self) -> None:
        self.signaled.append("kill")


def _fake_table(procs: list) -> tuple:
    meta: dict = {}
    children: dict = {}
    label_of: dict = {}
    procmap: dict = {}
    for p in procs:
        meta[p.pid] = {
            "ppid": 0,
            "name": p.name(),
            "status": "running",
            "ct": p.create_time(),
        }
        children.setdefault(0, []).append(p.pid)
        procmap[p.pid] = p
        label = m._label_for(m._target_from_argv(p.cmdline(), p.name()))
        if label:
            label_of[p.pid] = label
    return meta, children, label_of, procmap


def _cleanup_label(label: str, pids: tuple) -> None:
    with m._history_lock:
        m._churn_deaths.pop(label, None)
        for k in list(m._label_of_key):
            if k[0] in pids:
                m._label_of_key.pop(k)
        for k in list(m._history):
            if k[0] in pids:
                m._history.pop(k)
    with m._alert_lock:
        m._prev_flag.pop(label, None)


def test_crash_looper_caught_by_label_churn(monkeypatch):
    monkeypatch.setattr(m, "_cached", lambda key, ttl, fn: {})
    monkeypatch.setattr(m, "_cpu_mem", lambda pid, procmap: (2.0, 64.0))
    pids = (91000, 91001, 91002, 91003, 91004)
    try:
        observed = []
        for pid in pids:
            proc = FakeProc(pid, ["vllm", "serve"], ct=time.time() - 2.0)
            monkeypatch.setattr(m, "_collect", lambda p=proc: _fake_table([p]))
            row = next(
                a for a in m.list_agents()["agents"] if a["pid"] == pid
            )
            assert row["label"] == "vllm"
            observed.append((row["flag"], row["restarts"]))
        assert observed == [
            (None, 0),
            (None, 0),
            (None, 1),
            (None, 2),
            ("churn", 3),
        ]
        assert m._restarts_in_window("vllm", time.time()) == 4
    finally:
        _cleanup_label("vllm", pids)


def test_leaker_flag_surfaces_in_api_row(monkeypatch):
    now = time.time()
    ct = now - 1200.0
    pid = 92000
    proc = FakeProc(pid, ["vllm", "serve"], ct=ct)
    monkeypatch.setattr(m, "_cached", lambda key, ttl, fn: {})
    monkeypatch.setattr(m, "_cpu_mem", lambda p, pm: (5.0, 600.0))
    monkeypatch.setattr(m, "_collect", lambda: _fake_table([proc]))
    try:
        with m._history_lock:
            m._history[(pid, ct)] = _series(now, 1200, lambda t: (5.0, 200.0 + t / 3.0))
        row = next(a for a in m.list_agents()["agents"] if a["pid"] == pid)
        assert row["flag"] == "leak"
    finally:
        _cleanup_label("vllm", (pid,))


def test_signal_tree_refuses_on_create_time_mismatch(monkeypatch):
    root = FakeProc(70001, ["ollama", "serve"], ct=111.0)
    reused = FakeProc(70001, ["ollama", "serve"], ct=999.0)
    monkeypatch.setattr(m, "_collect", lambda: ({}, {}, {}, {70001: reused}))
    assert m._signal_tree(root, force=False) == []
    assert reused.signaled == []


def test_signal_tree_refuses_when_pid_gone(monkeypatch):
    root = FakeProc(70001, ["ollama", "serve"], ct=111.0)
    monkeypatch.setattr(m, "_collect", lambda: ({}, {}, {}, {}))
    assert m._signal_tree(root, force=False) == []


def test_signal_tree_kills_tree_and_skips_protected(monkeypatch):
    root = FakeProc(70001, ["ollama", "serve"], ct=111.0)
    same = FakeProc(70001, ["ollama", "serve"], ct=111.0)
    child = FakeProc(70002, ["ollama-runner"], ct=112.0)
    prot = FakeProc(70003, ["uvicorn", "app:app"], ct=113.0)
    children = {70001: [70002, 70003]}
    procmap = {70001: same, 70002: child, 70003: prot}
    monkeypatch.setattr(m, "_collect", lambda: ({}, children, {}, procmap))
    signaled = m._signal_tree(root, force=False)
    assert {p.pid for p in signaled} == {70001, 70002}
    assert same.signaled == ["terminate"]
    assert child.signaled == ["terminate"]
    assert prot.signaled == []
    forced = m._signal_tree(root, force=True)
    assert all(p.signaled[-1] == "kill" for p in forced)


def test_kill_launchd_supervised_409(client, action_log, monkeypatch, sleeper):
    monkeypatch.setattr(m, "_match_target", lambda p: "ollama serve")
    monkeypatch.setattr(m, "_cached", lambda key, ttl, fn: {sleeper.pid: "com.test.aum"})
    monkeypatch.setattr(m, "_keepalive", lambda label: True)
    r = client.post(f"/api/kill/{sleeper.pid}", headers=TOKEN)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert f"launchctl bootout gui/{os.getuid()}/com.test.aum" in detail
    assert "KeepAlive" in detail
    monkeypatch.setattr(m, "_keepalive", lambda label: False)
    r2 = client.post(f"/api/kill/{sleeper.pid}", headers=TOKEN)
    assert r2.status_code == 409
    assert "RunAtLoad" in r2.json()["detail"]
    assert sleeper.poll() is None
    entries = [json.loads(s) for s in action_log.read_text().splitlines()]
    assert [e["outcome"] for e in entries] == ["409 launchd-supervised"] * 2
    assert entries[0]["job"] == "com.test.aum"


def test_kill_terminates_real_process(client, action_log, monkeypatch, sleeper):
    monkeypatch.setattr(m, "_match_target", lambda p: "ollama serve")
    monkeypatch.setattr(m, "_cached", lambda key, ttl, fn: {})
    r = client.post(f"/api/kill/{sleeper.pid}", headers=TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "terminated"
    assert body["killed"] == 1
    assert body["still_running"] == 0
    assert sleeper.wait(timeout=5) is not None
    entry = json.loads(action_log.read_text().splitlines()[-1])
    assert entry["outcome"] == "terminated"
    assert entry["method"] == "terminate"
    assert entry["pid"] == sleeper.pid


def test_kill_refusal_log_redacts_secrets(client, action_log, monkeypatch, sleeper):
    monkeypatch.setattr(
        m, "_match_target", lambda p: "mytool --token hunter2secret serve"
    )
    r = client.post(f"/api/kill/{sleeper.pid}", headers=TOKEN)
    assert r.status_code == 403
    raw = action_log.read_text()
    assert "hunter2secret" not in raw
    entry = json.loads(raw.splitlines()[-1])
    assert entry["outcome"] == "403 not-an-agent"
    assert "***" in entry["target"]


def _classify(argv: Optional[list], name: str) -> Optional[str]:
    return m._label_for(m._target_from_argv(argv, name))


def test_real_agents_classify():
    assert _classify(["/usr/local/bin/ollama", "serve"], "ollama") == "ollama"
    assert _classify(
        ["/usr/bin/python3", "-m", "vllm.entrypoints.openai.api_server"], "python3"
    ) == "vllm"
    assert _classify(
        ["/Applications/Kiro.app/Contents/MacOS/Electron", "--type=renderer"],
        "Electron",
    ) == "kiro"


def test_lookalike_argv0_impersonator_is_classified():
    assert _classify(["/tmp/evil/ollama", "--exfil"], "ollama") == "ollama"


def test_lookalike_word_in_early_args_is_classified():
    assert _classify(["man", "claude"], "man") == "claude-code"
    assert _classify(["python3", "-c", "import claude", "--x"], "python3") == "claude-code"


def test_lookalike_substring_neighbors_not_classified():
    assert _classify(["raider", "--level", "3"], "raider") is None
    assert _classify(["decline-bot"], "decline-bot") is None
    assert _classify(["spider", "crawl"], "spider") is None
    assert _classify(["claudette"], "claudette") is None
    assert _classify(["vim", "claude-notes.md"], "vim") is None
    assert _classify(["codexify", "build"], "codexify") is None


def test_deep_arg_mention_not_classified():
    assert _classify(
        ["python3", "serve.py", "--port", "80", "--prompt", "you are claude"],
        "python3",
    ) is None
    assert _classify(["tmux", "new-session", "-d", "-s", "claude-sess"], "tmux") is None


def test_agent_behind_shell_wrapper_is_classified():
    assert _classify(["bash", "-c", "exec ollama serve"], "bash") == "ollama"


def test_agent_behind_long_env_wrapper_is_invisible():
    assert _classify(["env", "A=1", "B=2", "C=3", "ollama", "serve"], "env") is None


def test_agent_can_cloak_itself_with_ignore_pattern():
    assert _classify(["ollama", "serve", "--name", "crashpad-x"], "ollama") is None


def test_protect_substring_shields_lookalikes():
    assert m._is_protected("uvicorn-evil server", 55555)
    assert not m._is_protected("ollama serve", 55555)
