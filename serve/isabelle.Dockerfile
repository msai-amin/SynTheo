# Isabelle/HOL verification backend for formal metaphysics (core/verify/isabelle_hol.py). [ADR-006]
#
# Like sandbox.Dockerfile, this image only PROVIDES the prover + prebuilt heaps; the
# runtime isolation (network none, read-only rootfs, mem/pids caps, non-root, wall
# kill) is applied by run_isabelle_sandboxed() at `docker run` time, not here.
#
# Heaps are prebuilt (`isabelle build -b`) so a proof at runtime is a heap-LOAD
# (seconds) not a rebuild-the-world (the whole reason the on-demand pattern is viable
# under ADR-002's memory budget). Expect a large image and a long first build; record
# the measured size in ADR-006.
#
# aarch64 only (the DGX Spark). The bundled prover set is thinner on arm64 than x86 —
# Z3/CVC5 are absent, E and Leo-III (JVM) work. serve/isabelle_gate.py enumerates the
# real roster on this box; do not assume a hammer that isn't there.

FROM eclipse-temurin:21-jdk-jammy

# Isabelle2025-2 is the current release (verified 2026-07-15). The canonical host
# isabelle.in.tum.de is unreachable from the Spark (TLS reset); the Clarkson mirror
# serves the same bundles and IS reachable. AFP tracks the current Isabelle release.
ARG ISABELLE_VERSION=Isabelle2025-2
ARG ISABELLE_MIRROR=https://mirror.clarkson.edu/isabelle/dist
ARG AFP_URL=https://www.isa-afp.org/release/afp-current.tar.gz

# Isabelle needs a few tools + a working E prover; curl/tar to fetch the bundles.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl less libgomp1 perl rlwrap fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 -s /bin/bash isabelle
WORKDIR /opt

# --- Isabelle (arm64 linux bundle, ~1.2 GB) ---
RUN curl -fsSL "${ISABELLE_MIRROR}/${ISABELLE_VERSION}_linux_arm.tar.gz" \
      -o isabelle.tar.gz \
    && tar -xzf isabelle.tar.gz \
    && rm isabelle.tar.gz \
    && ln -s "/opt/${ISABELLE_VERSION}/bin/isabelle" /usr/local/bin/isabelle

# --- AFP (theory sources for the metaphysics entries) ---
RUN curl -fsSL "${AFP_URL}" -o afp.tar.gz \
    && mkdir -p /opt/afp \
    && tar -xzf afp.tar.gz -C /opt/afp --strip-components=1 \
    && rm afp.tar.gz

ENV ISABELLE_HOME=/opt/${ISABELLE_VERSION}

# Register the AFP as a SYSTEM component (read at runtime from the read-only rootfs) and
# let isabelle own the SYSTEM heaps dir so the prebuilt heaps land there — NOT in the
# user dir, which the runtime tmpfs would hide. HOL already ships in the system heaps;
# we add GoedelGod. Start small (AOT/PLM are large — added after the gate, per the plan).
RUN echo "/opt/afp/thys" >> "${ISABELLE_HOME}/etc/components" \
    && mkdir -p "${ISABELLE_HOME}/heaps" \
    && chown -R isabelle:isabelle "${ISABELLE_HOME}/heaps"

USER isabelle
RUN isabelle build -o system_heaps=true -b -v GoedelGod

# Wrapper LAST so iterating on it doesn't invalidate the (slow) heap-build layer above.
COPY --chmod=755 check_theory.sh /usr/local/bin/check-theory

# Sanity: the wrapper and heaps are present. A real proof is exercised by the gate.
CMD ["isabelle", "version"]
