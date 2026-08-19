from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import threading
import time
import webbrowser


def _bind_allowed(host: str, unsafe_expose: bool) -> bool:
    """Fail closed on non-loopback binds.

    This server carries a kill endpoint; binding it to a network interface on
    a casual flag would leave one static token between the network and SIGKILL
    for every agent on the box. "Put a proxy in front" is documentation, and
    documentation is not a trust boundary — so anything that isn't loopback
    refuses to start unless the user passes --unsafe-expose and owns the
    consequence.
    """
    if unsafe_expose or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _open_when_ready(url: str, host: str, port: int, timeout: float = 15.0) -> None:
    """Open the browser only once the port accepts, never on a fixed delay.

    A cold first run (uvx resolving the env, then importing fastapi/uvicorn)
    outruns any guess, and the browser lands on connection-refused. On timeout
    we stay silent: the URL is already on stdout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.1)


def _dur(s: float) -> str:
    s = max(0, int(s))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def _run_list(as_json: bool) -> None:
    import json
    import time

    # Imported lazily: `list` must not pay for (or require) uvicorn/webbrowser,
    # and importing app here doesn't start the server or the sampler thread.
    from agent_usage_manager import app as m

    m.list_agents()  # prime cpu_percent counters — first read is always 0.0
    time.sleep(0.5)
    data = m.list_agents()

    if as_json:
        # Flags need 5–15 minutes of in-process history; a one-shot process
        # can never populate them. Say so in the payload — a cron check on
        # `flag` from this output would silently never fire otherwise.
        # Query the running server's /api/agents if you need flags.
        data["flags_available"] = False
        print(json.dumps(data, indent=2))
        return

    agents = data["agents"]
    if not agents:
        print(f"No matching agents running. Config: {data['config_path']}")
        return
    show_gpu = any(a["gpu_mem_mb"] is not None for a in agents)
    head = ["AGENT", "PID", "CPU%", "MEM MB"] + (["GPU MB"] if show_gpu else []) + ["UPTIME", "FLAG", "COMMAND"]
    rows = []
    for a in agents:
        flag = a["flag"] or ("launchd" if a["supervised"] else "")
        row = [
            a["label"] + (f" +{a['child_count']}" if a["child_count"] else ""),
            str(a["pid"]),
            f"{a['cpu_percent']:.1f}",
            f"{a['mem_mb']:.0f}",
        ]
        if show_gpu:
            row.append("—" if a["gpu_mem_mb"] is None else f"{a['gpu_mem_mb']:.0f}")
        rows.append(row + [_dur(a["uptime_s"]), flag, a["cmdline"][:70]])
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(len(head))]
    for r in [head] + rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    print(
        "note: FLAG is always empty in one-shot mode — hot/idle/churn/leak need "
        "5–15 min of history from the running server (see its /api/agents).",
        file=sys.stderr,
    )


def _run_test_alert() -> int:
    """Fire the configured alert command once, synchronously, and report back.

    The alert path only proves itself when a real badge appears — the worst
    moment to learn the command is broken (a PATH that drifted, a notifier
    that moved). This runs the exact `alerts.command` from the resolved
    agents.yaml with the same $AUM_* env contract and a synthetic "test"
    message, waits for it, and says whether delivery succeeded. No cooldown
    or transition state is touched.
    """
    import subprocess

    from agent_usage_manager import app as m

    cfg = m.ALERTS
    if not cfg:
        print(
            f"no alerts.command configured in {m.CONFIG_PATH} — nothing to test",
            file=sys.stderr,
        )
        return 2
    host = socket.gethostname()
    msg = (
        f"Test alert — agent-usage-manager alert wiring works "
        f"[agent-usage-manager · test · {host} · sent by `test-alert`]"
    )
    env = {
        **os.environ,
        "AUM_MSG": msg,
        "AUM_LABEL": "test",
        "AUM_FLAG": "test",
        "AUM_PID": str(os.getpid()),
        "AUM_CPU": "0.0",
        "AUM_MEM_MB": "0",
        "AUM_RESTARTS": "0",
        "AUM_HOST": host,
    }
    print(f"config:  {m.CONFIG_PATH}")
    print(f"command: {cfg['command']}")
    print(f"message: {msg}")
    try:
        proc = subprocess.run(
            cfg["command"], shell=True, env=env,
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"FAILED to run: {e}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:500]
        print(f"FAILED: exit {proc.returncode}" + (f" — {err}" if err else ""),
              file=sys.stderr)
        return 1
    print("OK — command exited 0. If nothing showed up, the command's own "
          "delivery path is the thing to check.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-usage-manager",
        description="Web dashboard for headless AI agents: liveness, CPU/mem/GPU, kill.",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument(
        "--config",
        default=None,
        help="Path to agents.yaml (default: ./agents.yaml, else bundled).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open the dashboard in a browser on startup.",
    )
    parser.add_argument(
        "--unsafe-expose",
        action="store_true",
        help="Allow binding to a non-loopback interface, exposing the kill API "
        "to the network. You must put real auth (reverse proxy, SSH tunnel) "
        "in front.",
    )
    sub = parser.add_subparsers(dest="cmd")
    listp = sub.add_parser(
        "list", help="One-shot agent listing to stdout (no server) — for scripts and cron."
    )
    listp.add_argument("--json", action="store_true", help="Emit the raw API JSON.")
    # default=SUPPRESS: a subparser default would otherwise CLOBBER a top-level
    # `--config X` given before the subcommand (argparse applies subparser
    # defaults over the existing namespace) — the config would silently fall
    # back to cwd/bundled. SUPPRESS means "only set when actually given here".
    listp.add_argument("--config", dest="config", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)
    tap = sub.add_parser(
        "test-alert",
        help="Fire the configured alerts.command once and report whether it "
        "succeeded — proves the alert channel before a real badge depends on it.",
    )
    tap.add_argument("--config", dest="config", default=argparse.SUPPRESS,
                     help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.config:
        os.environ["AGENTS_CONFIG"] = args.config

    if args.cmd == "list":
        _run_list(args.json)
        return
    if args.cmd == "test-alert":
        sys.exit(_run_test_alert())

    if not _bind_allowed(args.host, args.unsafe_expose):
        parser.error(
            f"refusing to bind {args.host}: a non-loopback bind exposes the "
            "kill endpoint to the network. Pass --unsafe-expose if you really "
            "mean it (and put auth in front)."
        )

    import uvicorn

    open_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    # Bracket IPv6 literals — "http://::1:8765" is not a URL a browser can parse.
    url_host = f"[{open_host}]" if ":" in open_host else open_host
    url = f"http://{url_host}:{args.port}"
    print(f"agent-usage-manager → {url}")

    if not args.no_browser:
        threading.Thread(
            target=_open_when_ready, args=(url, open_host, args.port), daemon=True
        ).start()

    uvicorn.run("agent_usage_manager.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
