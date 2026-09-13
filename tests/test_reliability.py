import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent_usage_manager import app as m
from test_synthetic import FakeProc, _fake_table


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    for name in (
        "_history", "_leak_since", "_churn_deaths", "_label_of_key", "_handles",
        "_prev_flag", "_last_alert", "_alert_deliveries", "_snapshot_trees",
    ):
        monkeypatch.setattr(m, name, {})
    monkeypatch.setattr(m, "_snapshot", None)
    monkeypatch.setattr(m, "_snapshot_error", None)
    monkeypatch.setattr(m, "_sampler_started", threading.Event())
    monkeypatch.setattr(m, "_maybe_reload_config", lambda: None)
    monkeypatch.setattr(m, "_cached", lambda key, ttl, fn: {})
    monkeypatch.setattr(m, "ALERTS", None)
    monkeypatch.setattr(m, "ACTION_LOG_PATH", tmp_path / "actions.log")


@pytest.fixture
def client():
    return TestClient(m.app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("expected,code", [
    (None, 428), (1710000000.123, 409), ("nan", 409), ("inf", 409),
])
def test_stop_refuses_missing_or_replaced_displayed_identity(client, monkeypatch, expected, code):
    replacement = FakeProc(70101, ["ollama", "serve"], ct=1710000000.123456)
    signal = Mock()
    monkeypatch.setattr(m.psutil, "Process", lambda pid: replacement)
    monkeypatch.setattr(m, "_signal_tree", signal)
    params = {} if expected is None else {"create_time": expected}
    response = client.post(
        f"/api/kill/{replacement.pid}", params=params,
        headers={"X-Kill-Token": m.KILL_TOKEN},
    )
    assert response.status_code == code
    signal.assert_not_called()


@pytest.mark.parametrize("force,signal", [(False, "terminate"), (True, "kill")])
def test_stop_accepts_exact_displayed_identity(client, monkeypatch, force, signal):
    proc = FakeProc(70101, ["ollama", "serve"], ct=1710000000.123456)
    monkeypatch.setattr(m.psutil, "Process", lambda pid: proc)
    monkeypatch.setattr(m, "_collect", lambda: _fake_table([proc]))
    monkeypatch.setattr(m.psutil, "wait_procs", lambda procs, timeout: (procs, []))
    response = client.post(
        f"/api/kill/{proc.pid}", params={"create_time": proc.create_time(), "force": force},
        headers={"X-Kill-Token": m.KILL_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["still_running"] == 0
    assert proc.signaled == [signal]


def test_http_readers_share_one_sample_and_cpu_baseline(client, monkeypatch):
    proc = FakeProc(70101, ["ollama", "serve"], ct=1710000000.123456)
    cpu = Mock(return_value=(7.5, 32.0))
    monkeypatch.setattr(m, "_collect", lambda: _fake_table([proc]))
    monkeypatch.setattr(m, "_cpu_mem", cpu)
    monkeypatch.setattr(m.psutil, "Process", lambda pid: proc)
    m._publish_snapshot()
    m._sampler_started.set()
    timestamps = set()
    for _ in range(12):
        agents = client.get("/api/agents")
        tree = client.get(f"/api/tree/{proc.pid}", params={"create_time": proc.create_time()})
        metrics = client.get("/metrics")
        assert agents.status_code == tree.status_code == metrics.status_code == 200
        timestamps.update((agents.json()["ts"], tree.json()["ts"]))
        assert agents.json()["agents"][0]["cpu_percent"] == 7.5
        assert tree.json()["tree"][0]["cpu_percent"] == 7.5
        assert 'aum_agent_cpu_percent{agent="ollama"} 7.5' in metrics.text
    assert len(timestamps) == 1
    assert len(m._history[(proc.pid, proc.create_time())]) == 1
    assert cpu.call_count == 1
    assert client.get(f"/api/tree/{proc.pid}", params={"create_time": 1}).status_code == 409
    returned = m.list_agents()
    returned["agents"].clear()
    assert len(m.list_agents()["agents"]) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_unhealthy_sampling_refuses_reads_until_recovered(client, monkeypatch, failed):
    proc = FakeProc(70101, ["ollama", "serve"], ct=1710000000.123456)
    monkeypatch.setattr(m, "_collect", lambda: _fake_table([proc]))
    monkeypatch.setattr(m, "_cpu_mem", lambda pid, procmap: (7.5, 32.0))
    monkeypatch.setattr(m.psutil, "Process", lambda pid: proc)
    now = m.time.time()
    monkeypatch.setattr(m.time, "time", lambda: now)
    m._publish_snapshot()
    m._sampler_started.set()
    if failed:
        monkeypatch.setattr(m, "_snapshot_error", "collection failed")
    else:
        now += 11
    for path in ("/api/agents", "/metrics", f"/api/tree/{proc.pid}"):
        assert client.get(path).status_code == 503
    m._publish_snapshot()
    assert client.get("/api/agents").status_code == 200


def test_runtime_exits_do_not_mark_long_lived_sessions(monkeypatch):
    now = m.time.time()
    long_lived = FakeProc(70100, ["codex"], ct=now - 5 * 86400)
    monkeypatch.setattr(m, "_cpu_mem", lambda pid, procmap: (3.0, 32.0))
    for offset in range(5):
        short_lived = FakeProc(70101 + offset, ["codex"], ct=now - 2)
        monkeypatch.setattr(m, "_collect", lambda: _fake_table([long_lived, short_lived]))
        data = m.list_agents()
        assert all(a["restarts"] == 0 and a["last_restart"] is None for a in data["agents"])
        assert all(a["flag"] is None for a in data["agents"])
    assert data["runtime_exits"][0]["short_lived_exits"] == 4
    assert data["runtime_exits"][0]["runtime"] == "codex"


def test_supervised_exits_stay_with_the_service(monkeypatch):
    now = m.time.time()
    steady = FakeProc(70100, ["ollama", "serve"], ct=now - 5 * 86400)
    monkeypatch.setattr(m, "_cpu_mem", lambda pid, procmap: (3.0, 32.0))
    for offset in range(5):
        young = FakeProc(70101 + offset, ["ollama", "serve"], ct=now - 2)
        jobs = {young.pid: "example.restarting", steady.pid: "example.steady"}
        monkeypatch.setattr(m, "_cached", lambda key, ttl, fn: (
            jobs if key == "launchd" else True if key.startswith("keepalive:") else {}
        ))
        monkeypatch.setattr(m, "_keepalive", lambda label: True)
        monkeypatch.setattr(m, "_collect", lambda: _fake_table([steady, young]))
        data = m.list_agents()
    rows = {a["pid"]: a for a in data["agents"]}
    assert rows[young.pid]["flag"] == "churn"
    assert rows[young.pid]["instance_id"] == "launchd:example.restarting"
    assert rows[steady.pid]["flag"] is None
    assert rows[steady.pid]["restarts"] == 0
    assert data["runtime_exits"] == []


def test_live_root_regrouping_does_not_count_as_an_exit(monkeypatch):
    proc = FakeProc(70101, ["ollama", "serve"], ct=m.time.time() - 2)
    table = _fake_table([proc])
    monkeypatch.setattr(m, "_collect", lambda: table)
    monkeypatch.setattr(m, "_cpu_mem", lambda pid, procmap: (3.0, 32.0))
    m.list_agents()
    table[2].clear()
    data = m.list_agents()
    assert data["agents"] == []
    assert data["runtime_exits"] == []


def test_project_is_a_redacted_directory_basename(monkeypatch):
    proc = FakeProc(70101, ["ollama", "serve"])
    assert m._project_name(proc) == "aum-test-project"
    monkeypatch.setattr(proc, "cwd", lambda: str(Path.home()))
    assert m._project_name(proc) == "Home directory"
    monkeypatch.setattr(proc, "cwd", Mock(side_effect=m.psutil.AccessDenied(proc.pid)))
    assert m._project_name(proc) == ""


def _flagged(identity="process:70101:1.0"):
    return m.Agent(
        pid=70101, create_time=1.0, label="ollama", runtime="ollama", instance_id=identity,
        name="ollama", cmdline="ollama serve", status="running", alive=True,
        cpu_percent=100.0, mem_mb=32.0, gpu_mem_mb=None, uptime_s=1000,
        child_count=0, protected=False, flag="hot",
    )


@pytest.fixture
def delivery_clock(monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(m.time, "time", lambda: clock[0])
    monkeypatch.setattr(m, "ALERTS", {"command": "configured-notifier", "cooldown": 600, "flags": {"hot"}})
    return clock


@pytest.mark.parametrize("failure", ["spawn", "exit"])
def test_failed_command_retries_without_a_new_flag(monkeypatch, delivery_clock, failure):
    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(m.threading, "Thread", ImmediateThread)
    proc = Mock(returncode=3)
    proc.communicate.return_value = (b"", b"notification failed")
    command = Mock(side_effect=OSError("unavailable")) if failure == "spawn" else Mock(return_value=proc)
    monkeypatch.setattr(m.subprocess, "Popen", command)
    agent = _flagged()
    for offset, attempts, status in ((0, 1, "retry"), (4, 1, "retry"), (5, 2, "retry"),
                                     (14, 2, "retry"), (15, 3, "failed"), (700, 3, "failed")):
        delivery_clock[0] = 10000.0 + offset
        m._check_alerts([agent], delivery_clock[0], "test-host")
        snapshot = m._delivery_snapshot()[0]
        assert command.call_count == snapshot["attempts"] == attempts
        assert snapshot["status"] == status
    assert m._last_alert == {}


def test_recovered_condition_cancels_pending_retry(monkeypatch, delivery_clock):
    monkeypatch.setattr(m.subprocess, "Popen", Mock(side_effect=OSError("unavailable")))
    agent = _flagged()
    m._check_alerts([agent], delivery_clock[0], "test-host")
    agent.flag = None
    delivery_clock[0] += 10
    m._check_alerts([agent], delivery_clock[0], "test-host")
    assert m._delivery_snapshot() == []
    assert m.subprocess.Popen.call_count == 1


def test_successful_retry_finishes_delivery(monkeypatch, delivery_clock):
    dispatched = []

    def complete(command, agent, host):
        key = m._alert_key(agent)
        delivery = m._alert_deliveries[key]
        dispatched.append(delivery.attempts)
        m._finish_alert(key, delivery, "failed" if delivery.attempts == 1 else None)

    monkeypatch.setattr(m, "_spawn_alert", complete)
    agent = _flagged()
    for offset in (0, 5, 700):
        delivery_clock[0] = 10000.0 + offset
        m._check_alerts([agent], delivery_clock[0], "test-host")
    assert dispatched == [1, 2]
    assert m._delivery_snapshot()[0]["status"] == "delivered"
    assert m._delivery_snapshot()[0]["error"] is None


def test_old_callback_cannot_change_a_new_delivery(monkeypatch, delivery_clock):
    monkeypatch.setattr(m, "_spawn_alert", lambda *args: None)
    agent = _flagged()
    key = m._alert_key(agent)
    m._check_alerts([agent], delivery_clock[0], "test-host")
    old = m._alert_deliveries[key]
    agent.flag = None
    m._check_alerts([agent], delivery_clock[0], "test-host")
    agent.flag = "hot"
    delivery_clock[0] += 601
    m._check_alerts([agent], delivery_clock[0], "test-host")
    current = m._alert_deliveries[key]
    assert current is not old
    m._finish_alert(key, old, "late failure")
    assert current.status == "sending"
    assert m._last_alert[key] == delivery_clock[0]


def test_alert_cooldown_is_per_instance(monkeypatch, delivery_clock):
    dispatch = Mock()
    monkeypatch.setattr(m, "_spawn_alert", dispatch)
    m._check_alerts([_flagged("process:70101:1"), _flagged("process:70102:2")],
                    delivery_clock[0], "test-host")
    assert dispatch.call_count == 2
    assert len(m._delivery_snapshot()) == 2


def test_unconfirmed_delivery_does_not_retry(monkeypatch, delivery_clock):
    monkeypatch.setattr(m, "_spawn_alert", lambda *args: None)
    agent = _flagged()
    m._check_alerts([agent], delivery_clock[0], "test-host")
    key = m._alert_key(agent)
    delivery = m._alert_deliveries[key]
    m._finish_alert(key, delivery, "timeout", retry=False)
    delivery_clock[0] += 700
    m._check_alerts([agent], delivery_clock[0], "test-host")
    assert delivery.status == "unconfirmed"
    assert delivery.attempts == 1
