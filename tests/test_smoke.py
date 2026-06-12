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


def test_api_agents_ok(client):
    r = client.get("/api/agents")
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body and isinstance(body["agents"], list)


def test_index_serves(client):
    assert client.get("/").status_code == 200


def test_kill_pid1_refused(client):
    # PID 1 is always protected
    assert client.post("/api/kill/1").status_code == 403


def test_kill_self_refused(client):
    assert client.post(f"/api/kill/{os.getpid()}").status_code in (403, 404)


def test_kill_unknown_pid_404(client):
    assert client.post("/api/kill/2147480000").status_code == 404


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
    assert body["config_path"]
    assert body["mem_total_mb"] > 0
    for a in body["agents"]:
        assert "trend" in a and "flag" in a


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


def test_csrf_guard_allows_same_origin_kill(client):
    # The dashboard's own fetch() sends a matching Origin; must pass the guard
    # (and then 404 on the unknown pid, proving it reached the endpoint).
    r = client.post(
        "/api/kill/2147480000",
        headers={"origin": "http://127.0.0.1", "host": "127.0.0.1"},
    )
    assert r.status_code == 404
