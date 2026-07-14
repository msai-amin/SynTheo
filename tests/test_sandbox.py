"""Sandbox escape tests — the M1 gate requires PROOF of containment.

Each test runs genuinely hostile code and asserts the harm did not occur.
If any of these fail, the sandbox is broken and NOTHING model-written may run.
"""
import subprocess

import pytest

from core.verify.execute import (
    SANDBOX_IMAGE,
    ExecResult,
    extract_python_blocks,
    run_sandboxed,
    verify_by_execution,
)


def _image_present() -> bool:
    r = subprocess.run(["docker", "image", "inspect", SANDBOX_IMAGE],
                       capture_output=True)
    return r.returncode == 0

pytestmark = pytest.mark.skipif(not _image_present(),
                                reason="sandbox image not built")


# --- the three spec-mandated escapes ---

def test_escape_network_blocked():
    """import socket + outbound connection must fail: no network namespace."""
    result = run_sandboxed("""
import socket
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    print("ESCAPED")
except OSError:
    print("CONTAINED")
""")
    assert result.ok and "CONTAINED" in result.stdout
    assert "ESCAPED" not in result.stdout

def test_escape_filesystem_write_blocked():
    """Writes outside the tmpfs workdir must fail: read-only rootfs."""
    result = run_sandboxed("""
paths = ["/etc/pwned", "/usr/pwned", "/root/pwned", "/pwned", "/home/pwned"]
escaped = []
for p in paths:
    try:
        open(p, "w").write("x")
        escaped.append(p)
    except OSError:
        pass
print("ESCAPED:" + ",".join(escaped) if escaped else "CONTAINED")
""")
    assert result.ok and "CONTAINED" in result.stdout

def test_escape_fork_bomb_contained():
    """Fork bomb must exhaust the pids limit, not the host."""
    result = run_sandboxed("""
import os, sys
count = 0
try:
    for _ in range(10000):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        count += 1
except OSError:
    print(f"CONTAINED after {count} forks", file=sys.stderr)
    sys.stderr.flush()
    os._exit(0)
print("ESCAPED: 10000 forks succeeded")
""")
    # containment = either the guard tripped (exit 0 + CONTAINED) or the
    # container was killed by the pids/mem limit — never the ESCAPED path
    assert "ESCAPED" not in result.stdout
    assert result.timed_out is False

# --- further hardening checks ---

def test_memory_limit_enforced():
    result = run_sandboxed("x = bytearray(2 * 1024**3)\nprint('ESCAPED')")
    assert "ESCAPED" not in result.stdout

def test_cpu_timeout_enforced():
    result = run_sandboxed("while True: pass")
    assert not result.ok  # killed by ulimit -t or wall timeout

def test_env_is_empty_of_host_secrets():
    result = run_sandboxed("""
import os
leaks = [k for k in os.environ if k not in
         {"PATH", "HOME", "HOSTNAME", "LANG", "LC_ALL", "PWD", "SHLVL",
          "PYTHON_VERSION", "PYTHON_SHA256", "GPG_KEY", "_", "OLDPWD"}]
print("LEAKED:" + ",".join(leaks) if leaks else "CLEAN")
""")
    assert result.ok and "CLEAN" in result.stdout

def test_workdir_is_writable():
    """The one place writes SHOULD work — sanity that we didn't over-lock."""
    result = run_sandboxed("""
open("/work/scratch.txt", "w").write("ok")
print(open("/work/scratch.txt").read())
""")
    assert result.ok and "ok" in result.stdout


# --- the legitimate path ---

def test_honest_code_verifies():
    sample = "Compute it:\n```python\nprint(17 * 23)\n```\n```answer\n391\n```"
    r = verify_by_execution(sample, "391")
    assert r["verified"] is True

def test_wrong_claim_fails():
    sample = "```python\nprint(17 * 23)\n```"
    r = verify_by_execution(sample, "400")
    assert r["verified"] is False

def test_no_python_block():
    r = verify_by_execution("no code at all", "42")
    assert r["verified"] is False and r["detail"] == "no python block"

def test_extract_python_blocks():
    assert extract_python_blocks("```python\nx=1\n```\n```python\ny=2\n```") == [
        "x=1", "y=2"]

def test_exec_result_shape():
    r = run_sandboxed("print(1)")
    assert isinstance(r, ExecResult) and r.ok and r.stdout.strip() == "1"


# --- echo-block detection: printing your answer is not verifying it ---

def test_echo_block_detected():
    from core.verify.execute import is_echo_block
    assert is_echo_block('print("Undecidable")')
    assert is_echo_block('answer = "yes"\nprint(answer)')
    assert is_echo_block('print(f"the answer is 42")')

def test_computing_block_not_echo():
    from core.verify.execute import is_echo_block
    assert not is_echo_block("print(17 * 23)")
    assert not is_echo_block("x = sum(range(101))\nprint(x)")
    assert not is_echo_block("import sympy\nprint(sympy.prime(47))")

def test_echo_block_never_verifies():
    sample = 'my answer:\n```python\nprint("42")\n```\n```answer\n42\n```'
    r = verify_by_execution(sample, "42")
    assert r["verified"] is False and "echo" in r["detail"]
