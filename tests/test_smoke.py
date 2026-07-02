import os

import pytest
from fastapi.testclient import TestClient

from agent_usage_manager import app as m


def test_redact_strips_secrets():
    out = m._redact("server --token=abcdef123456 FOO_API_KEY=sk-aaaaaaaaaaaa")
    assert "abcdef123456" not in out
    assert "sk-aaaaaaaaaaaa" not in out
    assert "***" in out


def test_redact_leaves_plain_text():
    assert m._redact("ollama runner --model llama3") == "ollama runner --model llama3"


def test_target_uses_basename_and_first_args():
    t = m._target_from_argv(["/usr/bin/python3", "-m", "vllm.entrypoints"], "python3")
    assert t == "python3 -m vllm.entrypoints"


def test_target_ignores_deep_args():
    # "claude" appears only in a deep arg (index >= 4) → must NOT match-target
    argv = ["tmux", "new-session", "-d", "-s", "sess", "--prompt", "You are Claude Code"]
    t = m._target_from_argv(argv, "tmux")
    assert "Claude Code" not in t
    assert t.startswith("tmux")


def test_target_falls_back_to_name():
    assert m._target_from_argv(None, "ollama") == "ollama"


def test_load_config_valid(tmp_path):
    cfg = tmp_path / "agents.yaml"
    cfg.write_text("agents:\n  - label: ollama\n    match: ollama\nprotect:\n  - uvicorn\n")
    matchers, protect = m.load_config(cfg)
    assert matchers[0].label == "ollama"
    assert "uvicorn" in protect


def test_load_config_missing_keys(tmp_path):
    cfg = tmp_path / "agents.yaml"
    cfg.write_text("agents:\n  - label: oops\n")  # no 'match'
    with pytest.raises(RuntimeError):
        m.load_config(cfg)


def test_load_config_bad_yaml(tmp_path):
    cfg = tmp_path / "agents.yaml"
    cfg.write_text("agents: [unterminated")
    with pytest.raises(RuntimeError):
        m.load_config(cfg)


def test_load_config_bad_regex(tmp_path):
    cfg = tmp_path / "agents.yaml"
    cfg.write_text("agents:\n  - label: x\n    match: '([a'\n    regex: true\n")
    with pytest.raises(RuntimeError):
        m.load_config(cfg)


@pytest.fixture
def client():
    # base_url matters: the browser guard rejects non-local Host headers, and
    # TestClient's default base_url ("http://testserver") is a DNS name.
    return TestClient(m.app, base_url="http://127.0.0.1")


# The real kill token, as the operator would read it from the 0600 file.
TOKEN = {"x-kill-token": m.KILL_TOKEN}


@pytest.fixture(autouse=True)
def action_log(tmp_path, monkeypatch):
    # Test kills must not pollute the user's real audit log.
    log = tmp_path / "actions.log"
    monkeypatch.setattr(m, "ACTION_LOG_PATH", log)
    return log


def test_api_agents_ok(client):
    r = client.get("/api/agents")
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body and isinstance(body["agents"], list)


def test_index_serves(client):
    assert client.get("/").status_code == 200


def test_kill_pid1_refused(client):
    # PID 1 is always protected (token supplied, so it's the target check firing)
    assert client.post("/api/kill/1", headers=TOKEN).status_code == 403


def test_kill_self_refused(client):
    assert client.post(f"/api/kill/{os.getpid()}", headers=TOKEN).status_code in (403, 404)


def test_kill_unknown_pid_404(client):
    assert client.post("/api/kill/2147480000", headers=TOKEN).status_code == 404


def test_kill_without_token_refused(client):
    # The caller gate: monitored agents can curl this endpoint; without the
    # token from the 0600 file they must get nothing but a 403.
    r = client.post("/api/kill/2147480000")
    assert r.status_code == 403
    assert "X-Kill-Token" in r.json()["detail"]


def test_kill_wrong_token_refused(client):
    r = client.post("/api/kill/2147480000", headers={"x-kill-token": "not-the-token"})
    assert r.status_code == 403


def test_kill_with_token_reaches_target_auth(client):
    # Right token → past the caller gate, into the target re-match (404 here
    # proves the request reached the existing pid lookup, not the gate).
    r = client.post("/api/kill/2147480000", headers=TOKEN)
    assert r.status_code == 404


def test_token_file_is_0600():
    assert m.TOKEN_PATH.stat().st_mode & 0o777 == 0o600
    assert m.TOKEN_PATH.read_text().strip() == m.KILL_TOKEN


def test_kill_attempts_are_action_logged(client, action_log):
    import json

    client.post("/api/kill/2147480000")
    client.post("/api/kill/1", headers=TOKEN)  # launchd: unmatched, refused
    lines = [json.loads(s) for s in action_log.read_text().splitlines()]
    assert [e["outcome"] for e in lines] == ["403 no-token", "403 not-an-agent"]
    assert lines[0]["pid"] == 2147480000
    assert all(e["ts"] and e["client"] for e in lines)


def test_bind_fails_closed_without_unsafe_expose():
    from agent_usage_manager.cli import _bind_allowed

    assert _bind_allowed("127.0.0.1", False)
    assert _bind_allowed("localhost", False)
    assert _bind_allowed("::1", False)
    assert not _bind_allowed("0.0.0.0", False)
    assert not _bind_allowed("::", False)
    assert not _bind_allowed("192.168.1.50", False)
    assert _bind_allowed("0.0.0.0", True)  # the explicit scary flag


def test_host_guard_blocks_dns_rebinding(client):
    # A rebinding attack arrives with the attacker's domain in Host.
    r = client.get("/api/agents", headers={"host": "evil.example.com:8765"})
    assert r.status_code == 403


def test_host_guard_allows_ip_literal(client):
    # LAN access under --host 0.0.0.0 presents a bare IP — must keep working.
    r = client.get("/api/agents", headers={"host": "192.168.1.50:8765"})
    assert r.status_code == 200


def test_csrf_guard_blocks_cross_origin_kill(client):
    r = client.post("/api/kill/2147480000", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_csrf_guard_blocks_null_origin_kill(client):
    # Origin: null = sandboxed iframe / file:// — foreign, not trusted.
    r = client.post("/api/kill/2147480000", headers={"origin": "null"})
    assert r.status_code == 403


def test_api_agents_has_system_fields(client):
    body = client.get("/api/agents").json()
    assert body["api_version"] == 1
    assert body["aum_version"]
    assert body["config_path"]
    assert body["mem_total_mb"] > 0
    for a in body["agents"]:
        assert "trend" in a and "flag" in a
        assert "create_time" in a


def test_tree_refuses_non_agent(client):
    assert client.get("/api/tree/1").status_code == 403


def test_tree_unknown_pid_404(client):
    assert client.get("/api/tree/2147480000").status_code == 404


def test_config_hot_reload(client, tmp_path, monkeypatch):
    import agent_usage_manager.app as m

    cfg = tmp_path / "agents.yaml"
    cfg.write_text("agents:\n  - label: reloaded\n    match: zz-reload-probe\n")
    monkeypatch.setattr(m, "CONFIG_PATH", cfg)
    monkeypatch.setattr(m, "_config_mtime", None)
    orig = (m.MATCHERS, m.PROTECT, m.IGNORE)
    try:
        m._maybe_reload_config()
        assert [x.label for x in m.MATCHERS] == ["reloaded"]
        assert m.CONFIG_ERROR is None

        # a broken edit keeps the last good config and surfaces the error
        cfg.write_text("agents: [broken")
        monkeypatch.setattr(m, "_config_mtime", None)  # force mtime mismatch
        m._maybe_reload_config()
        assert [x.label for x in m.MATCHERS] == ["reloaded"]
        assert "not valid YAML" in m.CONFIG_ERROR
    finally:
        m.MATCHERS, m.PROTECT, m.IGNORE = orig
        m.CONFIG_ERROR = None


def test_flags_need_sustained_window():
    import time

    import agent_usage_manager.app as m

    now = time.time()
    key = (99999, 123.0)
    try:
        m._history[key] = m.deque(
            [(now - 360 + i * 3, 95.0, 100.0) for i in range(120)], maxlen=400
        )
        assert m._trend_and_flag(key, 700)[1] == "hot"
        m._history[key] = m.deque(
            [(now - 660 + i * 3, 0.5, 100.0) for i in range(220)], maxlen=400
        )
        assert m._trend_and_flag(key, 700)[1] == "idle"
        # a few isolated blips (GC, housekeeping) must not suppress idle (p95)
        blippy = [(now - 660 + i * 3, 0.5, 100.0) for i in range(220)]
        for j in (50, 120, 190):
            blippy[j] = (blippy[j][0], 5.0, 100.0)
        m._history[key] = m.deque(blippy, maxlen=400)
        assert m._trend_and_flag(key, 700)[1] == "idle"
        # sustained low-but-real activity is NOT idle
        m._history[key] = m.deque(
            [(now - 660 + i * 3, 3.0, 100.0) for i in range(220)], maxlen=400
        )
        assert m._trend_and_flag(key, 700)[1] is None
        # young series and short uptime must not flag
        m._history[key] = m.deque(
            [(now - 60 + i * 3, 95.0, 100.0) for i in range(20)], maxlen=400
        )
        assert m._trend_and_flag(key, 700)[1] is None
        m._history[key] = m.deque(
            [(now - 660 + i * 3, 0.5, 100.0) for i in range(220)], maxlen=400
        )
        assert m._trend_and_flag(key, 300)[1] is None
    finally:
        m._history.pop(key, None)


def test_leak_flag_needs_sustained_ratchet():
    import time

    import agent_usage_manager.app as m

    now = time.time()
    key = (99998, 123.0)

    def series(mem_fn):
        # 320 samples over ~16 min at 3s cadence; cpu 3% = neither hot nor idle
        return m.deque(
            [(now - 960 + i * 3, 3.0, mem_fn(i)) for i in range(320)], maxlen=400
        )

    try:
        # steady ratchet: 200 MB → ~840 MB, never comes back down → arms the
        # sustain clock but must not flag until it has held for _LEAK_SUSTAIN_S
        m._history[key] = series(lambda i: 200.0 + i * 2.0)
        assert m._trend_and_flag(key, 1000)[1] is None
        assert key in m._leak_since
        m._leak_since[key] = now - m._LEAK_SUSTAIN_S
        assert m._trend_and_flag(key, 1000)[1] == "leak"
        # growth too small in absolute terms (~32 MB) → not a leak, disarms
        m._history[key] = series(lambda i: 1000.0 + i * 0.1)
        assert m._trend_and_flag(key, 1000)[1] is None
        assert key not in m._leak_since
        # ramp with periodic GC dips back below the old median → not a leak
        m._history[key] = series(lambda i: 150.0 if i % 30 == 0 else 300.0 + i * 1.5)
        assert m._trend_and_flag(key, 1000)[1] is None
        # young process must not flag regardless of growth
        m._history[key] = series(lambda i: 200.0 + i * 2.0)
        assert m._trend_and_flag(key, 300)[1] is None
    finally:
        m._history.pop(key, None)
        m._leak_since.pop(key, None)


def _agent(label, flag=None, **kw):
    import agent_usage_manager.app as m

    defaults = dict(
        pid=1234, create_time=1.0, label=label, name="x", cmdline="x", status="running",
        alive=True, cpu_percent=0.0, mem_mb=0.0, gpu_mem_mb=None,
        uptime_s=5.0, child_count=0, protected=False, flag=flag,
    )
    defaults.update(kw)
    return m.Agent(**defaults)


def test_alerts_fire_on_transition_with_cooldown(monkeypatch):
    import time

    import agent_usage_manager.app as m

    fired = []
    monkeypatch.setattr(
        m, "ALERTS", {"command": "true", "cooldown": 600, "flags": {"hot", "churn", "leak"}}
    )
    monkeypatch.setattr(m, "_spawn_alert", lambda cmd, a, host: fired.append(a.flag))
    now = time.time()
    try:
        m._check_alerts([_agent("bot")], now, "h")  # no flag → nothing
        assert fired == []
        m._check_alerts([_agent("bot", "idle")], now, "h")  # idle not in flags → silent
        assert fired == []
        m._check_alerts([_agent("bot")], now, "h")
        m._check_alerts([_agent("bot", "churn")], now, "h")  # appears → fires
        assert fired == ["churn"]
        m._check_alerts([_agent("bot", "churn")], now + 1, "h")  # ongoing → once
        assert fired == ["churn"]
        m._check_alerts([_agent("bot")], now + 2, "h")  # flag clears
        m._check_alerts([_agent("bot", "churn")], now + 3, "h")  # flaps back → cooldown
        assert fired == ["churn"]
        m._check_alerts([_agent("bot")], now + 4, "h")
        m._check_alerts([_agent("bot", "churn")], now + 700, "h")  # past cooldown
        assert fired == ["churn", "churn"]
        # a different flag has its own cooldown bucket
        m._check_alerts([_agent("bot", "hot")], now + 701, "h")
        assert fired == ["churn", "churn", "hot"]
        # default flag set comes from load_alerts when `flags:` is omitted
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "a.yaml"
            p.write_text("agents:\n  - label: x\n    match: x\nalerts:\n  command: echo hi\n")
            cfg = m.load_alerts(p)
            assert cfg["flags"] == {"hot", "churn", "leak"}  # idle opt-in only
            p.write_text(
                "agents:\n  - label: x\n    match: x\n"
                "alerts:\n  command: echo hi\n  flags: [idle]\n"
            )
            assert m.load_alerts(p)["flags"] == {"idle"}
    finally:
        with m._alert_lock:
            m._prev_flag.pop("bot", None)
            for k in list(m._last_alert):
                if k[0] == "bot":
                    m._last_alert.pop(k)


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "aum_agents " in r.text
    assert "aum_host_cpu_count" in r.text


def test_churn_counts_young_deaths_per_label():
    import time

    import agent_usage_manager.app as m

    now = time.time()
    try:
        with m._history_lock:
            # three young deaths (lifetime < 120s) under one label → churn
            for i, pid in enumerate((9001, 9002, 9003)):
                key = (pid, now - 5.0)
                m._label_of_key[key] = "looper"
                m._note_death_locked(key, now - (3 - i))
            # an old process dying must NOT count (lifetime way over 120s)
            old = (9004, now - 7200)
            m._label_of_key[old] = "looper"
            m._note_death_locked(old, now)
            # deaths under a different label stay separate
            other = (9005, now - 5.0)
            m._label_of_key[other] = "calm"
            m._note_death_locked(other, now)
        assert m._restarts_in_window("looper", now) == 3
        assert m._restarts_in_window("calm", now) == 1
        # outside the 10-minute window the deaths age out and the state clears
        assert m._restarts_in_window("looper", now + 601) == 0
        with m._history_lock:
            assert "looper" not in m._churn_deaths
    finally:
        with m._history_lock:
            m._churn_deaths.pop("looper", None)
            m._churn_deaths.pop("calm", None)
            for k in list(m._label_of_key):
                if k[0] in (9001, 9002, 9003, 9004, 9005):
                    m._label_of_key.pop(k)


def test_csrf_guard_allows_same_origin_kill(client):
    # The dashboard's own fetch() sends a matching Origin; must pass the guard
    # (and then 404 on the unknown pid, proving it reached the endpoint).
    r = client.post(
        "/api/kill/2147480000",
        headers={"origin": "http://127.0.0.1", "host": "127.0.0.1", **TOKEN},
    )
    assert r.status_code == 404
