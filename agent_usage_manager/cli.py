from __future__ import annotations

import argparse
import os
import threading
import webbrowser


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
    sub = parser.add_subparsers(dest="cmd")
    listp = sub.add_parser(
        "list", help="One-shot agent listing to stdout (no server) — for scripts and cron."
    )
    listp.add_argument("--json", action="store_true", help="Emit the raw API JSON.")
    listp.add_argument("--config", dest="config", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.config:
        os.environ["AGENTS_CONFIG"] = args.config

    if args.cmd == "list":
        _run_list(args.json)
        return

    import uvicorn

    open_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{open_host}:{args.port}"
    print(f"agent-usage-manager → {url}")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run("agent_usage_manager.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
