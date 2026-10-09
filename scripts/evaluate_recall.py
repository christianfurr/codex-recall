#!/usr/bin/env python3
"""Measure target retrieval and filter invariants on a labelled synthetic corpus.

Requires explicit benchmark paths; never opens the personal memory database by
default. Query labels are authored and saved before running this evaluator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_memory.database import MemoryStore
from codex_memory.models import normalize_expiration, validate_filters
from codex_memory.search import fts_queries


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def metrics(rows: list[dict]) -> dict:
    count = len(rows)
    return {
        "query_count": count,
        "hit_at_1": sum(row["hit_at_1"] for row in rows) / count if count else None,
        "hit_at_5": sum(row["hit_at_5"] for row in rows) / count if count else None,
        "mrr_at_5": sum(row["reciprocal_rank_at_5"] for row in rows) / count if count else None,
    }


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def fixture_records(manifest: dict) -> dict[str, dict]:
    fixtures = manifest["fixtures"]
    if isinstance(fixtures, list):
        return {record["key"]: record for record in fixtures}
    return fixtures


def overlap_tokens(query: str, fixture: dict) -> list[str]:
    """Surface overlap diagnostic, not an implementation of SQLite tokenization."""
    def tokens(value: str) -> set[str]:
        folded = unicodedata.normalize("NFKD", value.casefold())
        folded = "".join(character for character in folded if not unicodedata.combining(character))
        return set(re.findall(r"[^\W_]+", folded))
    indexed_text = " ".join(str(fixture.get(field) or "") for field in ("content", "category", "project"))
    return sorted(tokens(query) & tokens(indexed_text))


def load_records(database: Path) -> dict[str, dict]:
    # Read labels against the actual corpus through a read-only connection.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM memories")}


def allowed(record: dict, filters: dict, now: str) -> bool:
    scope, project, category = validate_filters(filters.get("scope"), filters.get("project"), filters.get("category"))
    if record["expires_at"] and record["expires_at"] <= now:
        return False
    if record["status"] != "active" and not filters.get("include_superseded", False):
        return False
    if scope is not None and record["scope"] != scope:
        return False
    if scope == "project" and record["project"] != project:
        return False
    if scope is None:
        if record["scope"] == "project" and (project is None or record["project"] != project):
            return False
    return category is None or record["category"] == category


def metadata_issues(record: dict, filters: dict, now: str) -> list[str]:
    scope, project, category = validate_filters(filters.get("scope"), filters.get("project"), filters.get("category"))
    issues = []
    if record["expires_at"] and record["expires_at"] <= now:
        issues.append("expired")
    if record["status"] != "active" and not filters.get("include_superseded", False):
        issues.append("superseded")
    if scope is not None and record["scope"] != scope:
        issues.append("scope")
    if (scope == "project" and record["project"] != project) or (
        scope is None and record["scope"] == "project" and (project is None or record["project"] != project)
    ):
        issues.append("project")
    if category is not None and record["category"] != category:
        issues.append("category")
    return issues


def evaluate(database: Path, manifest_path: Path, queries_path: Path, output_dir: Path) -> dict:
    if not database.is_file():
        raise ValueError("Explicit benchmark database must already exist.")
    manifest = read_json(manifest_path)
    suite = read_json(queries_path)
    fixtures = fixture_records(manifest)
    records = load_records(database)
    fixture_ids = {key: record["id"] for key, record in fixtures.items()}
    for key, fixture in fixtures.items():
        actual = records.get(fixture["id"])
        fields = ("content", "category", "scope", "project", "status", "expires_at", "superseded_by")
        expected = {**fixture, "expires_at": normalize_expiration(fixture.get("expires_at"))}
        if actual is None or any(actual[field] != expected[field] for field in fields if field in expected):
            raise ValueError(f"Fixture does not match the provided database: {key}")
    labelled = suite["queries"]
    if len({case["id"] for case in labelled}) != len(labelled):
        raise ValueError("Query IDs must be unique.")
    now = iso_now()
    for case in labelled:
        targets = case["target_keys"]
        if not targets or any(key not in fixture_ids for key in targets):
            raise ValueError(f"Missing ground-truth fixture: {case['id']}")
        if any(not allowed(records[fixture_ids[key]], case.get("filters", {}), now) for key in targets):
            raise ValueError(f"Ground-truth fixture is excluded by query filters: {case['id']}")
        if case["group"] == "paraphrase_no_overlap" and any(overlap_tokens(case["query"], fixtures[key]) for key in targets):
            raise ValueError(f"A zero-overlap case has indexed-field surface overlap: {case['id']}")
    store = MemoryStore(database, max_results=100)
    leak_counts: Counter = Counter()
    leaked_rows = []
    returned_rows = 0

    def check_rows(rows: list[dict], filters: dict, case_id: str) -> None:
        nonlocal returned_rows
        returned_rows += len(rows)
        checked_at = iso_now()
        for row in rows:
            issues = metadata_issues(row, filters, checked_at)
            if row["id"] not in records:
                issues.append("unknown_id")
            elif any(row[field] != records[row["id"]][field] for field in ("content", "category", "scope", "project", "status", "expires_at")):
                issues.append("metadata_mismatch")
            leak_counts.update(issues)
            if issues:
                leaked_rows.append({"check_id": case_id, "memory_id": row["id"], "issues": issues,
                                    "expected_filters": filters,
                                    "actual_metadata": {field: row[field] for field in ("scope", "project", "category", "status", "expires_at")}})

    results = []
    failures = []
    for case in labelled:
        filters = case.get("filters", {})
        target_ids = [fixture_ids[key] for key in case["target_keys"]]
        started = time.perf_counter()
        error = None
        try:
            rows = store.recall(case["query"], **filters, limit=5)
        except (ValueError, sqlite3.Error, RuntimeError) as exc:
            rows = []
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000
        check_rows(rows, filters, case["id"])
        returned_ids = [row["id"] for row in rows]
        first_rank = next((rank for rank, identifier in enumerate(returned_ids, 1) if identifier in target_ids), None)
        row = {"query_id": case["id"], "query": case["query"], "group": case["group"],
               "filters": filters, "target_keys": case["target_keys"], "expected_ids": target_ids,
               "returned_ids": returned_ids, "returned_count": len(rows),
               "hit_at_1": int(first_rank == 1), "hit_at_5": int(first_rank is not None),
               "reciprocal_rank_at_5": 1 / first_rank if first_rank else 0.0,
               "first_relevant_rank": first_rank, "latency_ms": round(elapsed_ms, 4),
               "prepared_fts": fts_queries(case["query"]), "notes": case.get("notes", ""),
               "retrieval_error": error,
               "surface_overlap_tokens": {key: overlap_tokens(case["query"], fixtures[key]) for key in case["target_keys"]}}
        results.append(row)
        if first_rank != 1:
            failures.append({**row, "failure_type": "retrieval_error" if error else "target_missing_at_5" if first_rank is None else "target_below_rank_1",
                             "target_facts": {key: fixtures[key]["content"] for key in case["target_keys"]},
                             "returned_summaries": [{"id": memory["id"], "content_excerpt": memory["content"][:280],
                                                     "scope": memory["scope"], "project": memory["project"], "category": memory["category"]} for memory in rows]})

    diagnostics = []
    for case in suite.get("diagnostic_queries", []):
        filters = case.get("filters", {})
        try:
            rows = store.recall(case["query"], **filters, limit=5)
            check_rows(rows, filters, case["id"])
            result = {"query_id": case["id"], "query": case["query"], "kind": case["kind"],
                      "filters": filters, "returned_count": len(rows), "returned_ids": [row["id"] for row in rows],
                      "error": None, "notes": case.get("notes", "")}
        except ValueError as exc:
            result = {"query_id": case["id"], "query": case["query"], "kind": case["kind"],
                      "filters": filters, "returned_count": 0, "returned_ids": [], "error": str(exc),
                      "notes": case.get("notes", "")}
        if "expect_empty" in case:
            result["passed"] = result["error"] is None and (result["returned_count"] == 0) == case["expect_empty"]
        if "expected_error" in case:
            result["passed"] = result["error"] is not None and case["expected_error"] in result["error"]
        diagnostics.append(result)

    checks = []
    for case in suite.get("filter_checks", []):
        filters = case.get("filters", {})
        rows = store.recall(case["query"], **filters, limit=100) if "query" in case else store.list_memories(**filters, limit=100)
        check_rows(rows, filters, case["id"])
        ids = {row["id"] for row in rows}
        expected_present = [fixture_ids[key] for key in case.get("expected_present_keys", [])]
        expected_absent = [fixture_ids[key] for key in case.get("expected_absent_keys", [])]
        missing = [identifier for identifier in expected_present if identifier not in ids]
        unexpected = [identifier for identifier in expected_absent if identifier in ids]
        issues = sum(len(metadata_issues(row, filters, iso_now())) for row in rows)
        checks.append({"check_id": case["id"], "query": case.get("query"), "expected_filters": filters,
                       "expected_present_ids": expected_present, "expected_absent_ids": expected_absent,
                       "missing_expected_ids": missing, "unexpected_present_ids": unexpected,
                       "returned_count": len(rows), "metadata_issue_count": issues,
                       "actual_scope_counts": dict(Counter(row["scope"] for row in rows)),
                       "actual_category_counts": dict(Counter(row["category"] for row in rows)),
                       "actual_project_counts": dict(Counter(str(row["project"]) for row in rows)),
                       "passed": not missing and not unexpected and issues == 0})

    report = {
        "schema_version": 1, "evaluated_at": iso_now(),
        "corpus": {"database_path": str(database), "manifest_path": str(manifest_path),
                   "query_suite_path": str(queries_path), "query_suite_sha256": hashlib.sha256(queries_path.read_bytes()).hexdigest(),
                   "memory_count": len(records), "generated_record_count": manifest.get("record_count"),
                   "seed": manifest.get("seed"), "snapshot_preparation": manifest.get("snapshot_preparation", "Not described in the supplied manifest. Snapshot preparation is outside measured retrieval operations.")},
        "evaluation": {"query_count": len(labelled), "distinct_target_facts": len({key for case in labelled for key in case["target_keys"]}),
                       "method": "Hand-authored, predeclared queries identify designated relevant synthetic fixture facts by natural content. Hit@k tests whether any labelled target appears by rank k. MRR@5 is 1/rank of the first target, or zero when absent in five results.",
                       "limits": ["Synthetic authored cases are diagnostics, not a representative sample of human queries or real-world accuracy.",
                                  "Several queries reuse each fact; query observations are correlated. No confidence interval is reported.",
                                  "Labels identify specific target facts, not every potentially relevant background item; no precision, recall, or nDCG is claimed.",
                                  "Literal, prefix, malformed, contextual, and paraphrase cases are reported separately; the aggregate depends on this deliberately selected query mix.",
                                  "FTS5 performs lexical matching. Synonyms and zero lexical overlap are not semantic retrieval.",
                                  "Rows with equal ranking scores use random UUID order, so tied ranks may change when the corpus is regenerated.",
                                  "Metadata invariants inspect returned samples, with explicit lifecycle/context checks; they do not exhaust every possible query."],
                       "surface_overlap_method": "Accent-folded, casefolded Unicode word tokens compared with target content/category/project; this is a diagnostic approximation, not SQLite's tokenizer.",
                       "label_notes": suite.get("label_notes", "")},
        "overall": metrics(results),
        "retrieval_errors": sum(row["retrieval_error"] is not None for row in results),
        "lexical": metrics([row for row in results if not row["group"].startswith("paraphrase")]),
        "groups": [{"name": name, **metrics([row for row in results if row["group"] == name])} for name in dict.fromkeys(row["group"] for row in results)],
        "filter_checks": {"evaluated_rows": returned_rows, "project_leaks": leak_counts["project"],
                          "scope_leaks": leak_counts["scope"], "category_leaks": leak_counts["category"],
                          "expired_leaks": leak_counts["expired"], "superseded_leaks": leak_counts["superseded"],
                          "metadata_mismatches": leak_counts["metadata_mismatch"], "unknown_ids": leak_counts["unknown_id"],
                          "passed_checks": sum(case["passed"] for case in checks), "total_checks": len(checks),
                          "checks": checks, "leaked_rows": leaked_rows},
        "diagnostics": {"broad_queries": [row for row in diagnostics if row["kind"] == "broad"],
                        "negative_and_validation": [row for row in diagnostics if row["kind"] != "broad"]},
        "failures": failures, "query_results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_temporary = output_dir / "quality.json.tmp"
    json_temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    json_temporary.replace(output_dir / "quality.json")
    fields = ("query_id", "group", "query", "filters", "target_keys", "expected_ids", "returned_ids", "returned_count", "hit_at_1", "hit_at_5", "reciprocal_rank_at_5", "first_relevant_rank", "latency_ms", "retrieval_error", "notes")
    csv_temporary = output_dir / "query_results.csv.tmp"
    with csv_temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items() if key in fields})
    csv_temporary.replace(output_dir / "query_results.csv")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Existing isolated benchmark database; no default personal database.")
    parser.add_argument("--manifest", required=True, type=Path, help="Fixture manifest written by the corpus generator.")
    parser.add_argument("--queries", required=True, type=Path, help="Frozen labelled query suite JSON.")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = evaluate(args.database.resolve(), args.manifest.resolve(), args.queries.resolve(), args.output_dir.resolve())
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"overall": report["overall"], "groups": report["groups"],
                      "invariant_failures": len(report["filter_checks"]["leaked_rows"]),
                      "passed_filter_checks": report["filter_checks"]["passed_checks"],
                      "total_filter_checks": report["filter_checks"]["total_checks"]}, indent=2))
    return int(bool(report["filter_checks"]["leaked_rows"]) or report["retrieval_errors"] > 0 or report["filter_checks"]["passed_checks"] != report["filter_checks"]["total_checks"] or any(case.get("passed") is False for case in report["diagnostics"]["negative_and_validation"]))


if __name__ == "__main__":
    raise SystemExit(main())
