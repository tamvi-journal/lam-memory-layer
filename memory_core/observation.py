from __future__ import annotations

from typing import Any

from .profile import MemoryProfile
from .retrieval import CueDrivenRetriever
from .store import MemoryStore


def validate_store(store: MemoryStore) -> dict[str, Any]:
    store.initialize()
    with store.connect(readonly=True) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [
            dict(row) for row in conn.execute("PRAGMA foreign_key_check")
        ]
        duplicate_current = conn.execute(
            "SELECT record_id,COUNT(*) AS count FROM memory_current_v3 "
            "GROUP BY record_id HAVING count>1"
        ).fetchall()
        orphan_current = conn.execute(
            "SELECT v.revision_id FROM memory_current_v3 v "
            "LEFT JOIN memory_records_v3 r ON r.record_id=v.record_id "
            "WHERE r.record_id IS NULL"
        ).fetchall()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    checks = {
        "integrity_ok": integrity == "ok",
        "foreign_keys_ok": not foreign_keys,
        "one_current_revision_per_record": not duplicate_current,
        "no_orphan_current_revision": not orphan_current,
        "schema_application_id": (
            store.schema_info()["application_id"] != 0
        ),
        "schema_version_current": (
            store.schema_info()["user_version"] == 3
        ),
        "memory_dream_summary_pipeline_present": {
            "memory_episodes_v3",
            "memory_dream_runs_v3",
            "memory_dream_proposals_v3",
        }.issubset(tables),
    }
    return {
        "schema": "memory-core-doctor/v1",
        "passed": all(checks.values()),
        "checks": checks,
        "foreign_key_errors": foreign_keys,
    }


def evaluate_cue_contract(
    store: MemoryStore,
    profile: MemoryProfile,
    cases: list[dict[str, Any]],
    *,
    surface: str = "evaluation",
    limit: int = 10,
) -> dict[str, Any]:
    retriever = CueDrivenRetriever(store, profile)
    results: list[dict[str, Any]] = []
    for case in cases:
        hits = retriever.retrieve(
            case["cue"],
            scope=case.get("scope", "global"),
            surface=surface,
            limit=limit,
        )
        selected = {hit.revision["record_id"] for hit in hits}
        required = set(case.get("required_ids", []))
        results.append(
            {
                "name": case.get("name", case["cue"]),
                "cue": case["cue"],
                "required_ids": sorted(required),
                "missing": sorted(required - selected),
                "passed": not (required - selected),
            }
        )
    return {
        "schema": "memory-core-cue-evaluation/v1",
        "passed": all(item["passed"] for item in results),
        "cases": results,
    }
