#!/usr/bin/env python3
"""Poll all configured vLLM endpoints until healthy (or timeout). Exit 0 iff all green.

Used by M0's acceptance gate and by the UI's model-health dots (green/amber/red).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(REPO_ROOT / "config" / "syntheo.yaml") as f:
        return yaml.safe_load(f)


def check_one(alias: str, port: int, timeout: float = 5.0) -> str:
    """Returns 'green', 'amber' (reachable, not ready), or 'red' (unreachable)."""
    base = f"http://localhost:{port}"
    try:
        r = httpx.get(f"{base}/health", timeout=timeout)
        if r.status_code == 200:
            return "green"
        return "amber"
    except httpx.ConnectError:
        return "red"
    except httpx.HTTPError:
        return "amber"


def wait_all(cfg: dict, timeout_s: int = 900, poll_s: int = 5) -> dict[str, str]:
    aliases = [a for a in cfg["models"] if a != "super_swap"]
    deadline = time.monotonic() + timeout_s
    status = {a: "red" for a in aliases}
    while time.monotonic() < deadline:
        for alias in aliases:
            port = cfg["models"][alias]["port"]
            status[alias] = check_one(alias, port)
        print({a: status[a] for a in aliases}, flush=True)
        if all(v == "green" for v in status.values()):
            return status
        time.sleep(poll_s)
    return status


def main() -> None:
    cfg = load_config()
    timeout_s = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    status = wait_all(cfg, timeout_s=timeout_s)
    ok = all(v == "green" for v in status.values())
    print("ALL HEALTHY" if ok else "NOT ALL HEALTHY", status)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
