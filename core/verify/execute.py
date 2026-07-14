"""Sandboxed execution of model-written Python. Model code is HOSTILE by assumption.

User namespaces are kernel-restricted on this box (bwrap/unshare denied), so the
boundary is a Docker container instead — strictly stronger than the spec's rlimit
minimum: no network, read-only rootfs, tmpfs /work only, 512MB memory, pids-limit
(fork bombs), all capabilities dropped, non-root user, wall+CPU timeouts.
tests/test_sandbox.py PROVES containment of socket/file-write/fork-bomb escapes.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

SANDBOX_IMAGE = "syntheo-sandbox:latest"
CPU_SECONDS = 5
WALL_SECONDS = 20  # container start + interpreter overhead on top of CPU budget
MEM_MB = 512
PIDS_LIMIT = 64

_PY_BLOCK = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class ExecResult:
    ok: bool          # process ran and exited 0 (says nothing about the answer)
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


def extract_python_blocks(sample_text: str) -> list[str]:
    return [b.strip() for b in _PY_BLOCK.findall(sample_text)]


def run_sandboxed(code: str) -> ExecResult:
    """Run one Python snippet inside the sandbox container."""
    name = f"syntheo-sbx-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="syntheo-sbx-") as td:
        script = Path(td) / "snippet.py"
        script.write_text(code)
        cmd = [
            "docker", "run", "--rm",
            "--name", name,
            "--network=none",
            "--read-only",
            "--tmpfs", "/work:rw,size=64m,uid=10001,gid=10001",
            f"--memory={MEM_MB}m", f"--memory-swap={MEM_MB}m",
            f"--pids-limit={PIDS_LIMIT}",
            "--cpus=2",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user", "10001:10001",
            "--env-file", "/dev/null",
            "-v", f"{script}:/snippet.py:ro",
            SANDBOX_IMAGE,
            "sh", "-c", f"ulimit -t {CPU_SECONDS}; python3 /snippet.py",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=WALL_SECONDS,
            )
            return ExecResult(
                ok=proc.returncode == 0,
                stdout=proc.stdout[-20_000:],
                stderr=proc.stderr[-20_000:],
                exit_code=proc.returncode,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            # docker-run was killed client-side; make sure the container dies too
            subprocess.run(["docker", "kill", name], capture_output=True)
            return ExecResult(
                ok=False,
                stdout=(exc.stdout or b"").decode(errors="replace")[-20_000:]
                if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-20_000:],
                stderr="wall-clock timeout",
                exit_code=None,
                timed_out=True,
            )


def verify_by_execution(sample_text: str, claimed_answer: str) -> dict:
    """Run the sample's python blocks; verified iff a block runs cleanly and its
    final stdout line matches the claimed answer (via extract.answers_equivalent)."""
    from core.extract import answers_equivalent

    blocks = extract_python_blocks(sample_text)
    if not blocks:
        return {"verified": False, "method": "execute", "detail": "no python block"}
    for code in blocks:
        result = run_sandboxed(code)
        if not result.ok:
            continue
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if lines and answers_equivalent(lines[-1], claimed_answer):
            return {"verified": True, "method": "execute",
                    "detail": f"stdout={lines[-1]!r} matches claim"}
    return {"verified": False, "method": "execute",
            "detail": "no block produced matching output"}
