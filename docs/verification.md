# Release verification

Version **1.0.0**, verified locally on **2026-10-09** using Linux, Python **3.10.12**, SQLite **3.37.2**, official MCP Python SDK **1.30.0**, and Codex CLI **0.162.0-alpha.2**.

| Check | Actual result |
| --- | --- |
| Unit, security, concurrency, configuration and maintenance suite | **71 passed, 0 failed** |
| Official MCP client, real stdio subprocesses | **9 tests passed** as part of the suite |
| Codex discovery | Exactly six tools discovered by the installed Codex app-server |
| Actual Codex calls | All six tools called successfully through Codex |
| Persistence | Temporary memory saved in one Codex process, recalled by a second independent process |
| Cleanup | Temporary verification memory removed; active production database has no test records |
| Installer rerun | Configuration, guidance and memory counts preserved; no duplicate registration |
| Uninstall rehearsal | Ran the real script on isolated configuration/data; unrelated settings and a different database preserved |
| Backup / restore | Live-WAL snapshot, validated restore, rollback, malformed/corrupt backup refusal and exclusive locking tested |
| Files | Private data directory, database, SQLite sidecars, backups and exports checked |
| Package dependencies | `pip check` passed |
| GitHub CI | Workflow prepared for Python 3.10–3.14; remote jobs have **not** run |

Actual Codex verification used two ephemeral `codex app-server` processes reading the installed registration. Other MCP servers and plugins were disabled only in those verification processes. No model turn was requested, no API key was supplied, and the sentinel was removed. This verifies Codex's own discovery, transport and persistence; it does not test whether a model will always choose to remember or recall information without prompting. A desktop session restart may be needed to load the new tool list.

The local engine also handled **5,000 synthetic memories** with healthy SQLite and FTS indexes. Across 100 exact-token searches, median recall was **0.887 ms**, maximum **1.709 ms**. This is a warm local engine measurement, not an end-to-end MCP or model latency promise. Individual transactional inserts took 36.046 seconds total; bulk ingestion is outside this service's purpose.

To reproduce:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python scripts/verify_codex.py
```

The Codex check requires an installed Codex CLI and an existing `local_memory` registration. It writes only a unique temporary expiring sentinel to the configured database. Run it when you want an integration check, not on every session.

Machine-readable evidence: [Codex check](../verification/codex.json), [engine benchmark](../verification/performance.json). Personal database files, private configuration backups and local installation logs are excluded from release artifacts.
