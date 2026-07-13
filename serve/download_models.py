#!/usr/bin/env python3
"""Fetch model weights into the project-local HF cache (config/syntheo.yaml: paths.hf_home).

The shared ~/.cache/huggingface is root-owned on this box, so every model download
and every vLLM server MUST point HF_HOME at the project-local cache instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from huggingface_hub import snapshot_download

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(REPO_ROOT / "config" / "syntheo.yaml") as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = load_config()
    hf_home = (REPO_ROOT / cfg["paths"]["hf_home"]).resolve()
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)

    only = set(sys.argv[1:]) or None  # optionally: download_models.py heavy mid

    for alias, model in cfg["models"].items():
        if alias == "super_swap" and (only is None or "super_swap" not in only):
            continue
        if only and alias not in only:
            continue
        repo = model["repo"]
        print(f"=== {alias}: {repo} -> {hf_home} ===", flush=True)
        snapshot_download(
            repo_id=repo,
            cache_dir=str(hf_home / "hub"),
            max_workers=8,
        )
        print(f"=== {alias}: done ===", flush=True)


if __name__ == "__main__":
    main()
