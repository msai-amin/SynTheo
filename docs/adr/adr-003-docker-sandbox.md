# ADR-003: Use Docker containers as the sandbox for model-written code

**Status:** Accepted
**Date:** 2026-07-14
**Deciders:** Claude (implementation; forced by kernel restrictions, verified by tests)

## Decision in one sentence

Python code written by the AI models is executed inside a locked-down Docker container (no network, read-only filesystem, memory and process caps) rather than the originally planned lightweight sandboxes (bubblewrap/unshare), because this machine's kernel forbids those tools for regular users.

## The problem we're solving

SynTheo verifies math answers by actually running the checking code the model wrote. That code must be treated as **hostile**: a model can be manipulated (or just wrong) into producing code that opens network connections, overwrites files, or fork-bombs the machine. The project spec is explicit: *"the sandbox is a security boundary — treat model-written code as hostile, always"*, and the M1 milestone required tests **proving** three specific escapes are contained: network access, writes outside a scratch directory, and fork bombs.

Constraint discovered during implementation: this Ubuntu kernel restricts **user namespaces** for unprivileged users (`bwrap` and `unshare -rn` both fail with "Operation not permitted"), which disables the standard lightweight sandboxing tools we planned to use.

## The platforms involved

### Docker (as a sandbox)

Docker (see ADR-001) is best known for packaging services, but its isolation flags make a serviceable security boundary: `--network=none` (no network exists inside), `--read-only` (the filesystem cannot be written), `--pids-limit` (caps how many processes can exist, defusing fork bombs), `--memory` (hard memory cap), `--cap-drop=ALL` (removes all special privileges). Analogy: an interview room with no phone line, bolted-down furniture, and a guard counting who goes in.

### bubblewrap (bwrap) and unshare

Small, fast tools that create the same kinds of isolation *without* a container daemon, used by e.g. the Flatpak app system. They rely on the kernel letting normal users create isolated namespaces — the exact feature this machine's kernel has switched off (a hardening choice on recent Ubuntu). Analogy: a folding privacy screen — light and instant, but our venue's rules forbid setting one up.

### Plain resource limits (rlimits)

The bare-minimum fallback the spec allowed: cap CPU seconds and memory on an ordinary process, run it with an empty environment. Crucially, rlimits **cannot block network access or filesystem writes** — two of the three escapes we must provably contain.

## Options considered

### Docker sandbox (chosen)

Each code snippet runs in a fresh container from a purpose-built ~330 MB image (Python + SymPy + NumPy, non-root user), with every isolation flag listed above, a 5-second CPU limit inside and a 20-second wall-clock kill outside. Weakness: ~0.5–1 s container start-up per snippet, and a hard dependency on the Docker daemon.

### bubblewrap / unshare

Faster (~10 ms start-up) and daemon-free — the right choice on a machine that allows it. Verified unavailable here: both fail with "Operation not permitted" under this kernel's user-namespace restriction.

### Re-enable user namespaces via sysctl

One `sudo sysctl` would unlock bwrap. Rejected: weakening a deliberate kernel hardening setting machine-wide, to benefit one subsystem, inverts the security goal — the sandbox exists to *add* a boundary, not to remove one the OS already had.

### rlimits-only subprocess

Meets the spec's stated minimum and needs nothing installed. But it demonstrably cannot contain two of the three mandated escapes (network, filesystem writes) — it would pass the letter of "minimum" while failing the tests that define the boundary.

## Comparison

| What matters to us | Docker | bwrap/unshare | sysctl + bwrap | rlimits only |
|---|---|---|---|---|
| Blocks network (`import socket`) | Yes (proven by test) | n/a here | Yes | **No** |
| Blocks writes outside scratch dir | Yes (proven by test) | n/a here | Yes | **No** |
| Contains fork bombs | Yes (pids-limit, proven) | n/a here | Yes | Partially |
| Works on this kernel today | Yes | **No** | Only after weakening the kernel | Yes |
| Per-run overhead | ~0.5–1 s | ~10 ms | ~10 ms | ~0 |

## Why we chose Docker

It is the only option on this machine that actually contains all three mandated escapes without weakening system security. The containment is not asserted but **proven**: the M1 test suite runs genuinely hostile code — a socket connection to 1.1.1.1, write attempts to `/etc`, `/usr`, `/root`, `/`, `/home`, and a 10,000-process fork bomb — and asserts the harm did not occur. Those tests pass and run on every test invocation, so any future regression in the sandbox flags fails the build loudly.

The 0.5–1 s start-up cost is acceptable because verification runs on the CPU **while the GPU is busy generating other samples** — sandbox time hides inside generation time (a property of the machine the spec told us to exploit). Up to four sandboxes run concurrently.

Docker was also already installed, already trusted with the far larger job of running the model servers themselves, and required zero new system configuration.

## Trade-offs we accepted

Every verification pays the container start-up tax, and the verifier stack now requires the Docker daemon to be running (if it isn't, samples are marked "unverifiable" rather than crashing the run — degraded, not broken). Container isolation shares the host kernel, so it is a weaker theoretical boundary than a virtual machine; we judged that proportionate for code written by our own locally-hosted models rather than anonymous internet users. We would revisit this if the kernel restriction is lifted (bwrap becomes attractive for its speed) or if snippet volume grows enough that start-up time dominates.

## Glossary

- **Container daemon**: the always-running background service (dockerd) that creates and supervises containers.
- **Fork bomb**: code that endlessly creates new processes until the machine chokes.
- **Kernel**: the core of the operating system; its settings bound what any program may do.
- **rlimits**: per-process caps (CPU time, memory) the OS enforces; useful but blind to network and file access.
- **Sandbox**: an environment that lets code run while confining what it can touch.
- **User namespaces**: a kernel feature letting unprivileged programs create their own isolated view of the system; disabled for regular users on this machine.
- **Wall-clock timeout**: a kill-switch based on elapsed real time, regardless of what the code is doing.
