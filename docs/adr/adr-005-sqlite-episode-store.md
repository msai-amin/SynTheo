# ADR-005: SQLite for the episode store

**Status:** Accepted
**Date:** 2026-07-14
**Deciders:** Fixed in the project specification; ratified by implementation

## Decision in one sentence

Every run, sample, verification, and judgment is recorded in SQLite — a serverless database stored as one local file — rather than in a client-server database like PostgreSQL or in plain JSON logs.

## The problem we're solving

SynTheo must remember everything it does: which problem was asked, every candidate answer each model produced, what every verifier said about each candidate, every judge score, and the final verdict with its exact configuration. This record (the "episode store") serves three masters:

1. **Debugging and trust** — the UI's trace explorer must reconstruct any past run in full detail.
2. **Evaluation** — the harness compares strategies over stored runs.
3. **The flywheel** — future training exports mine the store for high-quality traces, with a hard rule that evaluation problems must never leak into training data (the "contamination firewall").

Context: exactly one machine, one user, a handful of writes per second at most, and a possibly-offline box.

## The platforms involved

### SQLite

A free, public-domain database that runs *inside* your program and stores everything in a single ordinary file — no server, no accounts, no setup. It is likely the most widely deployed software component on Earth (every phone, browser, and car uses it). Analogy: a meticulous lab notebook that lives on your desk, rather than an archive department across town.

### PostgreSQL

A free, open-source client-server relational database (see any standard reference): a separate always-running service that programs connect to over the network. Built for many simultaneous users and writers. Analogy: the archive department — industrial-strength, but you must staff it.

### JSONL log files

The do-it-yourself option: append one JSON record per event to text files. No dependencies at all. Analogy: a shoebox of dated receipts — trivial to toss things into, painful to answer questions from.

## Options considered

### SQLite (chosen)

Five linked tables (problems → runs → samples → verifications/judgments) with foreign keys, indexes, and **WAL mode** (a journaling setting that lets readers read while a writer writes). The contamination firewall is one shared SQL fragment (`problems.is_eval = 0`) that every export query must include — enforced by a test that inspects the exporter's source.

### PostgreSQL

Strictly more capable: better concurrency, richer types, remote access. But every one of those strengths answers a problem we don't have — there is one user, one writer at a time, on one box. The costs are real, though: another always-on service competing for the same 9.5 GB of free memory (ADR-002), another thing to start, back up, and keep healthy on an offline machine.

### JSONL log files

We already write per-call trace logs this way (in `core/llm.py`), and for append-only telemetry it's perfect. But the store's consumers ask *relational* questions — "all runs where the verdict was verified but a solo fast-model run failed" is the exact shape of the planned training export. In JSONL that's a custom script per question; in SQL it's a query. And the contamination firewall would become a convention scattered across scripts instead of one testable fragment.

## Comparison

| What matters to us | SQLite | PostgreSQL | JSONL files |
|---|---|---|---|
| Setup and operations on an offline box | None — it's a file | A service to run and back up | None |
| Extra resident memory | ~0 | ~50–200 MB service | 0 |
| Cross-table questions (exports, eval reports) | SQL, with enforced links | SQL, with enforced links | Hand-written scripts |
| Contamination firewall enforceability | One tested SQL fragment | Same | Convention only |
| Concurrent writers | One at a time (fine: one user) | Many (unneeded) | Racy |
| Backup | Copy one file | Dump procedure | Copy directory |

## Why we chose SQLite

The workload is the textbook SQLite case, and the deciding factors are the two hard requirements the alternatives handle worse *here*:

First, the **contamination firewall** must be structurally enforceable. In SQL it is one shared `WHERE` fragment defined once in `core/store.py`; a unit test verifies both that it filters eval rows and that the export code actually uses it. File-based storage can't offer that guarantee shape.

Second, **operational weight is a real budget** on this machine. After the memory crisis documented in ADR-002, the stack stabilized with 9.5 GB to spare; a PostgreSQL service would spend some of that — plus startup ordering, plus backup procedures — to provide concurrency headroom no one will use.

WAL mode covers our actual concurrency need (the UI reading traces while a run writes), and reproducibility comes from storing each run's full configuration as JSON in the row.

## Trade-offs we accepted

If SynTheo ever becomes multi-user or multi-machine, SQLite's single-writer model becomes the bottleneck and we'd migrate to PostgreSQL — the schema is deliberately plain SQL to keep that door open. Analytical queries over millions of rows would also eventually favor a server database; at ~50 rows per run, that is years away. We also accept that the store is only as safe as the file: backups are the user's copy of `data/episodes.sqlite3`.

## Glossary

- **Client-server database**: a database run as a separate always-on service that programs connect to.
- **Contamination firewall**: the project rule that evaluation problems are never exported into training data.
- **Foreign key**: a database rule keeping links between tables valid (a sample must belong to a real run).
- **JSONL**: a text file with one JSON record per line.
- **Serverless (database)**: embedded in the program itself; no separate service.
- **WAL mode**: "write-ahead logging" — an SQLite setting letting reads proceed while a write is in progress.
- **Schema**: the defined structure of a database's tables and their relationships.
