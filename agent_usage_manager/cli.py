from __future__ import annotations

import argparse
import os
import threading
import webbrowser

import uvicorn


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
    args = parser.parse_args()
    if args.config:
        os.environ["AGENTS_CONFIG"] = args.config

    open_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{open_host}:{args.port}"
    print(f"agent-usage-manager → {url}")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run("agent_usage_manager.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
