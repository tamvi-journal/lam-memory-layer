from __future__ import annotations

import json
from typing import Any

from .episodes import EpisodeArchive
from .governance import ValidatedIntake
from .store import MemoryStore, hash_payload, utc_now


class GovernedDream:
    """Audited consolidation that can only change semantics through intake."""

    def __init__(
        self,
        store: MemoryStore,
        intake: ValidatedIntake,
        *,
        surface: str,
    ):
        self.store = store
        self.intake = intake
        self.surface = surface
        self.episodes = EpisodeArchive(store)

    def run(
        self,
        *,
        dream_run_id: str,
        proposals: list[dict[str, Any]],
        actor: str,
        reason: str,
        idempotency_key: str,
        adjustments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (dream_run_id, actor, reason, idempotency_key)
        ):
            raise ValueError(
                "dream_run_id, actor, reason, and idempotency_key are required"
            )
        normalized = [
            {
                "episode_ids": list(item.get("episode_ids") or []),
                "proposal": dict(item.get("proposal") or {}),
            }
            for item in proposals
        ]
        input_sha256 = hash_payload(
            {"proposals": normalized, "adjustments": adjustments or []}
        )
        self.store.initialize()
        prior = self._run_by_key(idempotency_key)
        if prior:
            if prior["input_sha256"] != input_sha256:
                raise ValueError(
                    "idempotency_key already exists with different dream input"
                )
            return prior

        history_before = self._semantic_history()
        results: list[dict[str, Any]] = []
        for ordinal, item in enumerate(normalized):
            proposal = item["proposal"]
            if not proposal:
                raise ValueError(f"proposal[{ordinal}] is empty")
            episode_ids = item["episode_ids"]
            if not episode_ids:
                raise ValueError(
                    f"proposal[{ordinal}] requires at least one source episode"
                )
            episode_evidence = [
                self.episodes.as_evidence(episode_id)
                for episode_id in episode_ids
            ]
            proposal["evidence"] = [
                *episode_evidence,
                *(proposal.get("evidence") or []),
            ]
            proposal.setdefault(
                "idempotency_key",
                f"{idempotency_key}:proposal:{ordinal}",
            )
            result = self.intake.submit(**proposal)
            results.append(
                {
                    "ordinal": ordinal,
                    "record_id": proposal["record_id"],
                    "episode_ids": episode_ids,
                    "intake": result,
                }
            )

        maintenance = None
        if adjustments:
            maintenance = self.store.apply_maintenance(
                run_id=dream_run_id,
                adjustments=adjustments,
                actor=actor,
                surface=self.surface,
                reason=reason,
                idempotency_key=f"{idempotency_key}:maintenance",
            )

        history_after = self._semantic_history()
        rewritten = any(
            history_after.get(revision_id) != content_sha256
            for revision_id, content_sha256 in history_before.items()
        )
        if rewritten:
            raise RuntimeError("dream rewrote historical semantic payload")
        result_payload = {
            "schema": "agent-memory-governed-dream/v1",
            "dream_run_id": dream_run_id,
            "proposal_results": results,
            "maintenance": maintenance,
            "historical_payload_rewritten": False,
        }
        before_hash = hash_payload(history_before)
        after_hash = hash_payload(history_after)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO memory_dream_runs_v3("
                "dream_run_id,input_sha256,actor,surface,reason,proposal_count,"
                "result_json,semantic_history_before_sha256,"
                "semantic_history_after_sha256,historical_payload_rewritten,"
                "idempotency_key,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dream_run_id,
                    input_sha256,
                    actor,
                    self.surface,
                    reason,
                    len(results),
                    json.dumps(
                        result_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    before_hash,
                    after_hash,
                    0,
                    idempotency_key,
                    utc_now(),
                ),
            )
            for result in results:
                intake = result["intake"]
                conn.execute(
                    "INSERT INTO memory_dream_proposals_v3("
                    "dream_run_id,ordinal,intake_id,record_id,"
                    "episode_ids_json,status,result_json"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        dream_run_id,
                        result["ordinal"],
                        intake.get("intake_id"),
                        result["record_id"],
                        json.dumps(result["episode_ids"], ensure_ascii=False),
                        intake["status"],
                        json.dumps(
                            intake,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
        stored = self._run_by_key(idempotency_key)
        if stored is None:
            raise RuntimeError("dream run did not persist")
        return stored

    def get(self, dream_run_id: str) -> dict[str, Any] | None:
        if not self.store.db_path.exists():
            return None
        with self.store.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM memory_dream_runs_v3 WHERE dream_run_id=?",
                (dream_run_id,),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    def _run_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not self.store.db_path.exists():
            return None
        with self.store.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM memory_dream_runs_v3 WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    def _semantic_history(self) -> dict[str, str]:
        with self.store.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT revision_id,content_sha256 "
                "FROM memory_revisions_v3 ORDER BY revision_id"
            ).fetchall()
        return {row["revision_id"]: row["content_sha256"] for row in rows}

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["result"] = json.loads(row.pop("result_json"))
        row["historical_payload_rewritten"] = bool(
            row["historical_payload_rewritten"]
        )
        return row
