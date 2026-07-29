from __future__ import annotations

import json
from typing import Any

from .store import MemoryStore, hash_payload, utc_now


TRANSCRIPT_KEYS = {
    "conversation",
    "messages",
    "raw_transcript",
    "transcript",
    "turns",
}
MAX_EXCERPT_BYTES = 4096


class EpisodeArchive:
    """Minimal immutable experience archive.

    The default path stores a bounded source excerpt and structured facts, not
    a conversation transcript. Transcript-shaped payloads require an explicit
    opt-in so a host cannot accidentally dump its chat history.
    """

    def __init__(self, store: MemoryStore):
        self.store = store

    def capture(
        self,
        *,
        episode_id: str,
        source_ref: str,
        title: str,
        summary: str,
        actor: str,
        idempotency_key: str,
        episode_type: str = "experience",
        source_family: str = "",
        observed_at: str | None = None,
        surface: str = "",
        content_excerpt: str = "",
        raw_payload: dict[str, Any] | None = None,
        privacy_class: str = "private",
        include_transcript: bool = False,
    ) -> dict[str, Any]:
        for name, value in {
            "episode_id": episode_id,
            "source_ref": source_ref,
            "title": title,
            "summary": summary,
            "actor": actor,
            "idempotency_key": idempotency_key,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        payload = dict(raw_payload or {})
        transcript_keys = self._transcript_keys(payload)
        if transcript_keys and not include_transcript:
            raise ValueError(
                "transcript-shaped payload requires include_transcript=True: "
                + ", ".join(sorted(transcript_keys))
            )
        if len(content_excerpt.encode("utf-8")) > MAX_EXCERPT_BYTES:
            raise ValueError(
                f"content_excerpt exceeds {MAX_EXCERPT_BYTES} UTF-8 bytes"
            )
        observed = observed_at or utc_now()
        family = source_family.strip() or source_ref.split(":", 1)[0]
        source_sha256 = hash_payload(
            {
                "source_ref": source_ref,
                "observed_at": observed,
                "summary": summary,
                "payload": payload,
            }
        )
        capture = {
            "episode_id": episode_id,
            "episode_type": episode_type,
            "source_ref": source_ref,
            "source_family": family,
            "source_sha256": source_sha256,
            "observed_at": observed,
            "actor": actor,
            "surface": surface,
            "title": title,
            "summary": summary,
            "content_excerpt": content_excerpt,
            "raw_payload_json": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "privacy_class": privacy_class,
            "transcript_included": int(include_transcript),
            "idempotency_key": idempotency_key,
        }
        capture_sha256 = hash_payload(capture)
        self.store.initialize()
        with self.store.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM memory_episodes_v3 WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior:
                if prior["capture_sha256"] != capture_sha256:
                    raise ValueError(
                        "idempotency_key already exists with a different episode"
                    )
                return self._decode(dict(prior))
            conn.execute(
                "INSERT INTO memory_episodes_v3("
                "episode_id,episode_type,source_ref,source_family,source_sha256,"
                "observed_at,actor,surface,title,summary,content_excerpt,"
                "raw_payload_json,privacy_class,transcript_included,"
                "capture_sha256,idempotency_key,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    episode_id,
                    episode_type,
                    source_ref,
                    family,
                    source_sha256,
                    observed,
                    actor,
                    surface,
                    title,
                    summary,
                    content_excerpt,
                    capture["raw_payload_json"],
                    privacy_class,
                    int(include_transcript),
                    capture_sha256,
                    idempotency_key,
                    utc_now(),
                ),
            )
        result = self.get(episode_id)
        if result is None:
            raise RuntimeError("episode capture did not persist")
        return result

    def get(self, episode_id: str) -> dict[str, Any] | None:
        if not self.store.db_path.exists():
            return None
        with self.store.connect(readonly=True) as conn:
            if not self._table_exists(conn):
                return None
            row = conn.execute(
                "SELECT * FROM memory_episodes_v3 WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if not self.store.db_path.exists():
            return []
        with self.store.connect(readonly=True) as conn:
            if not self._table_exists(conn):
                return []
            rows = conn.execute(
                "SELECT * FROM memory_episodes_v3 "
                "ORDER BY observed_at DESC,episode_id LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._decode(dict(row)) for row in rows]

    def as_evidence(self, episode_id: str) -> dict[str, Any]:
        episode = self.get(episode_id)
        if episode is None:
            raise ValueError(f"episode not found: {episode_id}")
        return {
            "evidence_type": "archived_episode",
            "source_ref": f"episode:{episode_id}",
            "source_family": episode["source_family"],
            "independence_group": f"episode:{episode_id}",
            "content_summary": episode["summary"],
            "confidence": 0.8,
            "privacy_class": episode["privacy_class"],
            "captured_at": episode["observed_at"],
            "actor": episode["actor"],
            "surface": episode["surface"],
            "source_payload": {
                "episode_id": episode_id,
                "source_ref": episode["source_ref"],
                "source_sha256": episode["source_sha256"],
                "capture_sha256": episode["capture_sha256"],
            },
        }

    @staticmethod
    def _table_exists(conn: Any) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='memory_episodes_v3'"
            ).fetchone()
        )

    @classmethod
    def _transcript_keys(
        cls,
        value: Any,
        *,
        prefix: str = "",
    ) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if str(key).lower() in TRANSCRIPT_KEYS:
                    found.add(path)
                found.update(cls._transcript_keys(child, prefix=path))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                path = f"{prefix}[{index}]"
                found.update(cls._transcript_keys(child, prefix=path))
        return found

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["raw_payload"] = json.loads(row.pop("raw_payload_json"))
        row["transcript_included"] = bool(row["transcript_included"])
        return row
