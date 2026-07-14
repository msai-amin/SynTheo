"""Sandboxed execution of model-written Python. Model code is HOSTILE by assumption.

User namespaces are kernel-restricted on this box (bwrap/unshare denied), so the
boundary is a Docker container instead — strictly stronger than the spec's rlimit
minimum: no network, read-only rootfs, tmpfs /work only, 512MB memory, pids-limit
(fork bombs), all capabilities dropped, non-root user, wall+CPU timeouts.
tests/test_sandbox.py PROVES containment of socket/file-write/fork-bomb escapes.
"""
from __future__ import annotations

import ast
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


def is_echo_block(code: str) -> bool:
    """True if the block cannot verify anything because it computes nothing —
    only constant assignments and prints of constants/those names. A model that
    writes `print("Undecidable")` is echoing its answer, not checking it."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False  # let the sandbox report the real error

    const_names: set[str] = set()

    def _is_const(node: ast.expr) -> bool:
        if isinstance(node, (ast.Constant, ast.JoinedStr)):
            return True
        if isinstance(node, ast.Name):
            return node.id in const_names
        return False

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            const_names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
              and isinstance(stmt.value.func, ast.Name)
              and stmt.value.func.id == "print"
              and all(_is_const(a) for a in stmt.value.args)):
            continue
        else:
            return False  # real computation present
    return True


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


def _answer_seeded_in_source(code: str, claimed_answer: str) -> bool:
    """True when a NON-NUMERIC claimed answer appears as a string literal in the
    block: the 'computation' is then seeded with its own conclusion (dict
    lookups, conditional prints of the answer word). Numeric literals are fine —
    `assert result == 391` is genuine checking."""
    claim = claimed_answer.strip().lower()
    if not claim:
        return False
    try:
        float(claim.replace("/", "").replace(".", "", 1).replace("-", "", 1))
        return False  # numeric-ish claims may legitimately appear in asserts
    except ValueError:
        pass
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if claim in node.value.lower():
                return True
    return False


def verify_by_execution(sample_text: str, claimed_answer: str) -> dict:
    """Run the sample's python blocks; verified iff a block runs cleanly and its
    final stdout line matches the claimed answer (via extract.answers_equivalent)."""
    from core.extract import answers_equivalent

    blocks = extract_python_blocks(sample_text)
    if not blocks:
        return {"verified": False, "method": "execute", "detail": "no python block"}
    blocks = [b for b in blocks if not is_echo_block(b)
              and not _answer_seeded_in_source(b, claimed_answer)]
    if not blocks:
        return {"verified": False, "method": "execute",
                "detail": "echo block: answer printed or seeded, not computed"}
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
