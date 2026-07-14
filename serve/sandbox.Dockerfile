# Execution sandbox for model-written code (core/verify/execute.py).
# Model code is hostile by assumption; the runtime flags in execute.py
# (network none, read-only rootfs, mem/pids caps) are the boundary — this
# image just provides the interpreter and math libs.
FROM python:3.12-slim
RUN pip install --no-cache-dir sympy==1.14.0 numpy
RUN useradd -m -u 10001 runner
USER runner
WORKDIR /work
