from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .profile import MemoryProfile
from .store import MemoryStore
from .text import normalize_text, tokens


@dataclass
class MemoryHit:
    revision: dict[str, Any]
    score: float
    reasons: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


class CueDrivenRetriever:
    def __init__(self, store: MemoryStore, profile: MemoryProfile):
        self.store = store
        self.profile = profile

    def retrieve(
        self,
        query: str,
        *,
        scope: str = "global",
        surface: str = "local",
        limit: int = 10,
        token_budget: int = 1800,
        include_history: bool | None = None,
    ) -> list[MemoryHit]:
        normalized = normalize_text(query)
        query_tokens = set(tokens(query))
        revisions = {
            item["record_id"]: item
            for item in self.store.current_view()
            if item["authority_status"] != "non_authoritative"
            and item["scope"] in {"global", scope}
        }
        scores = {record_id: 0.0 for record_id in revisions}
        reasons = {record_id: [] for record_id in revisions}
        direct: set[str] = set()

        with self.store.connect() as conn:
            cues = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM memory_cues_v2 WHERE profile=? "
                    "AND scope IN ('global',?) ORDER BY weight DESC",
                    (self.profile.name, scope),
                )
            ]
            relations = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM memory_relations_v2 WHERE status='active'"
                )
            ]
        cues.extend(
            {
                "cue": cue,
                "cue_norm": normalize_text(cue),
                "target_record_id": target,
                "weight": weight,
            }
            for cue, target, weight in self.profile.cue_aliases
        )
        for cue in cues:
            target = cue["target_record_id"]
            if target not in revisions:
                continue
            cue_tokens = set(tokens(cue["cue_norm"]))
            exact = bool(cue["cue_norm"] and cue["cue_norm"] in normalized)
            overlap = len(query_tokens & cue_tokens) / max(1, len(cue_tokens))
            if exact or overlap >= 0.8:
                gain = float(cue["weight"]) * (
                    1.45 if exact else 0.9 * overlap
                )
                scores[target] += gain
                reasons[target].append(f"cue:{cue['cue']}")
                direct.add(target)

        for record_id, revision in revisions.items():
            memory_tokens = set(
                tokens(
                    "\n".join(
                        [
                            revision["title"],
                            revision["summary"],
                            revision["content"],
                        ]
                    )
                )
            )
            if query_tokens and memory_tokens:
                overlap = len(query_tokens & memory_tokens) / max(
                    1, len(query_tokens)
                )
                if overlap:
                    scores[record_id] += min(1.0, overlap) * 0.9
                    reasons[record_id].append(f"lexical:{overlap:.2f}")

        for record_id in self.profile.bootstrap_record_ids:
            if record_id in revisions:
                scores[record_id] += 0.34
                reasons[record_id].append("bootstrap")

        frontier = {record_id: scores[record_id] for record_id in direct}
        for depth in (1, 2):
            next_frontier: dict[str, float] = {}
            for source_id, activation in frontier.items():
                for relation in relations:
                    if relation["from_record_id"] == source_id:
                        target = relation["to_record_id"]
                    elif relation["to_record_id"] == source_id:
                        target = relation["from_record_id"]
                    else:
                        continue
                    if target not in revisions:
                        continue
                    gain = activation * float(relation["weight"]) * (0.46**depth)
                    if gain < 0.05:
                        continue
                    scores[target] += gain
                    reasons[target].append(
                        f"graph:{source_id}-[{relation['relation_type']}]->"
                        f"{target}:d{depth}"
                    )
                    next_frontier[target] = max(
                        next_frontier.get(target, 0.0), gain
                    )
            frontier = next_frontier

        for record_id, revision in revisions.items():
            if scores[record_id] <= 0:
                continue
            scores[record_id] += float(revision["confidence"]) * 0.28
            scores[record_id] += float(revision["salience"]) * 0.14
            scores[record_id] += float(revision["stability"]) * 0.12
            scores[record_id] += float(revision["accessibility"]) * 0.08

        ranked = [
            MemoryHit(revisions[record_id], score, reasons[record_id])
            for record_id, score in scores.items()
            if score >= 0.24
        ]
        ranked.sort(key=lambda item: (-item.score, item.revision["record_id"]))
        wants_history = (
            include_history
            if include_history is not None
            else any(
                normalize_text(marker) in normalized
                for marker in self.profile.history_markers
            )
        )
        selected: list[MemoryHit] = []
        spent = 0
        for hit in ranked:
            estimate = max(
                1,
                len(
                    (
                        hit.revision["title"]
                        + hit.revision["summary"]
                        + hit.revision["content"]
                    ).split()
                )
                * 2,
            )
            if selected and spent + estimate > token_budget:
                continue
            if len(selected) >= limit:
                break
            if wants_history:
                hit.history = self.store.historical_view(
                    hit.revision["record_id"]
                )
            selected.append(hit)
            spent += estimate
            self.store.record_access(
                cue=query,
                record_id=hit.revision["record_id"],
                revision_id=hit.revision["revision_id"],
                retrieval_reason=",".join(hit.reasons[:5]),
                rank=len(selected),
                surface=surface,
            )
        return selected
