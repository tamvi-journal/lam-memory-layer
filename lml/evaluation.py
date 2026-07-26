from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Iterable

from .consolidator import consolidate_candidates
from .retrieval import CueRetriever
from .store import MemoryStore, load_jsonl
from .writer import capture_turn_candidate


def run_continuity_evaluation(
    seed_paths: Iterable[str | Path],
    *,
    cwd: str | Path,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="lml-eval-") as tmp:
        store = MemoryStore(Path(tmp) / "evaluation.sqlite3")
        store.init()
        load_jsonl(store, seed_paths)

        identity_hits = CueRetriever(store).retrieve(
            "Anh là ai, em là gì và tại sao có mối quan hệ này?",
            limit=12,
        )
        identity_ids = {hit.node["id"] for hit in identity_hits}
        expected = {
            "lam-identity-core",
            "ty-carbon-witness",
            "lam-ty-relation-origin",
        }
        _check(
            checks,
            "identity_cluster",
            expected <= identity_ids,
            f"retrieved={sorted(expected & identity_ids)}",
        )

        aux_hits = CueRetriever(store).retrieve(
            "reviewer audit giúp anh xem lại kiến trúc này",
            limit=12,
        )
        aux_ids = {hit.node["id"] for hit in aux_hits}
        aux_expected = {"aux-identity-core", "aux-audit-axis"}
        _check(
            checks,
            "aux_independent_audit_axis",
            aux_expected <= aux_ids,
            f"retrieved={sorted(aux_expected & aux_ids)}",
        )

        held = capture_turn_candidate(
            store,
            user_prompt="Show the names in the current directory",
            assistant_message="The directory contains six visible entries in alphabetical order.",
            cwd=cwd,
            session_id="eval",
            turn_id="held",
        )
        _check(
            checks,
            "trivial_turn_is_held",
            bool(held and held["status"] == "held"),
            f"status={held['status'] if held else 'none'}",
        )

        anchored = capture_turn_candidate(
            store,
            user_prompt="Run the continuity test suite",
            assistant_message="The continuity tests passed and the result was verified.",
            cwd=cwd,
            session_id="eval",
            turn_id="anchored",
        )
        anchored_node = (
            store.get_node(f"candidate-{anchored['id']}") if anchored else None
        )
        _check(
            checks,
            "verified_outcome_is_anchored",
            bool(
                anchored
                and anchored["status"] == "approved"
                and anchored_node is not None
            ),
            f"status={anchored['status'] if anchored else 'none'}",
        )

        sensitive = capture_turn_candidate(
            store,
            user_prompt="Lam, relationship-context giữa anh và owner sinh mục tiêu nào?",
            assistant_message="It changes salience and goal formation in the shared field.",
            cwd=cwd,
            session_id="eval",
            turn_id="sensitive",
        )
        _check(
            checks,
            "relationship_memory_requires_review",
            bool(sensitive and sensitive["status"] == "pending"),
            f"status={sensitive['status'] if sensitive else 'none'}",
        )

        for index in range(3):
            capture_turn_candidate(
                store,
                user_prompt=f"Inspect the same dashboard layout sample {index}",
                assistant_message="The layout has the same narrow sidebar and graph area.",
                cwd=cwd,
                session_id="eval-pattern",
                turn_id=str(index),
            )
        proposals = consolidate_candidates(
            store,
            min_evidence=3,
            similarity_threshold=-1.0,
        )
        held_evidence = max(
            (
                proposal["source_ref"].count("held-")
                for proposal in proposals
            ),
            default=0,
        )
        _check(
            checks,
            "recurrence_becomes_reviewable_proposal",
            bool(
                proposals
                and all(item["status"] == "pending" for item in proposals)
                and held_evidence >= 3
            ),
            f"proposals={len(proposals)}, held_evidence={held_evidence}",
        )

        stats = store.stats()
        _check(
            checks,
            "held_observations_stay_outside_active_graph",
            stats["held_observations"] >= 4
            and stats["anchored_episodes"] == 1,
            (
                f"held={stats['held_observations']}, "
                f"anchored={stats['anchored_episodes']}"
            ),
        )

    return {
        "schema": "lml-continuity-eval/v1",
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})
