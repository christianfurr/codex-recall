#!/usr/bin/env python3
"""Measure the real MCP server's Linux RSS on an explicit benchmark snapshot.

Linux /proc VmHWM describes this executable's memory image. resource.ru_maxrss
can retain the launching process's pre-exec high-water mark and is unsuitable
as a standalone service footprint measurement in this execution environment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import time

from benchmark_memory import child_server_pid, mcp_call, mcp_session, query_mix


def memory(pid: int) -> dict:
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in ("VmRSS", "VmHWM"):
            fields[name] = int(value.split()[0])
    return {"rss_kib": fields["VmRSS"], "high_water_kib": fields["VmHWM"]}


async def profile(path: Path, output: Path, count: int) -> dict:
    samples = []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        record_count = connection.execute("SELECT count(*) FROM memories").fetchone()[0]
    startup = time.perf_counter()
    async with mcp_session(path, output) as session:
        startup_ms = (time.perf_counter() - startup) * 1000
        pid = child_server_pid(path)
        idle = memory(pid)
        for query in query_mix():
            await mcp_call(session, "recall", query)
        samples.append(memory(pid))
        for i in range(count):
            await mcp_call(session, "recall", query_mix()[i % len(query_mix())])
            samples.append(memory(pid))
        steady = memory(pid)
        health = await mcp_call(session, "memory_status")
        after_health = memory(pid)
        assert health["integrity"] == health["fts_integrity"] == "ok"
    return {
        "source": "Linux /proc/<own MCP child>/status VmRSS and VmHWM",
        "unit": "KiB (Linux /proc reports kB in 1024-byte units)",
        "database_records": record_count,
        "warmup_recalls": len(query_mix()), "measured_recalls": count,
        "startup_ms": startup_ms, "startup_samples": 1,
        "idle": idle, "after_recalls": steady, "after_integrity_check": after_health,
        "max_sampled_recall_rss_kib": max(sample["rss_kib"] for sample in samples),
        "server_process_closed": True,
        "limitations": ["A new process with ordinary OS cache; not a cold-disk-cache experiment.",
                        "RSS includes shared pages and does not equal private physical RAM.",
                        "One server process and one authored query mix; workload-dependent.",
                        "Integrity-check peak reported separately from normal recall.",
                        "These extra profile calls are not included in the main benchmark's timed-operation totals."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recalls", default=200, type=int)
    args = parser.parse_args()
    if not args.database.is_file() or args.recalls < 1:
        parser.error("Supply an existing isolated benchmark database and positive recalls.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(profile(args.database.resolve(), args.output.parent.resolve(), args.recalls))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
