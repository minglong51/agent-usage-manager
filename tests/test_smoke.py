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


def test_csrf_guard_allows_same_origin_kill(client):
    # The dashboard's own fetch() sends a matching Origin; must pass the guard
    # (and then 404 on the unknown pid, proving it reached the endpoint).
    r = client.post(
        "/api/kill/2147480000",
        headers={"origin": "http://127.0.0.1", "host": "127.0.0.1"},
    )
    assert r.status_code == 404
