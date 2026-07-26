from __future__ import annotations

from dataclasses import dataclass

from .retrieval import RetrievalHit
from .store import MemoryStore
from .text import normalize_text


@dataclass(frozen=True)
class FieldState:
    coherence: float
    uncertainty: float
    drift_risk: float
    conflict_count: int
    direct_cue_count: int
    selected_ids: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "coherence": self.coherence,
            "uncertainty": self.uncertainty,
            "drift_risk": self.drift_risk,
            "conflict_count": self.conflict_count,
            "direct_cue_count": self.direct_cue_count,
            "selected_ids": self.selected_ids,
        }


def assess_field_state(store: MemoryStore, query: str, hits: list[RetrievalHit]) -> FieldState:
    selected_ids = [hit.node["id"] for hit in hits]
    selected = set(selected_ids)
    direct_cues = sum(
        1 for hit in hits for reason in hit.reasons if reason.startswith("cue:")
    )
    conflicts = sum(
        1
        for edge in store.edges()
        if edge["src_id"] in selected
        and edge["dst_id"] in selected
        and edge["relation"] in {"contradicts", "supersedes"}
    )

    if hits:
        relevance = sum(min(1.0, hit.score / 3.0) for hit in hits) / len(hits)
        confidence = sum(float(hit.node["confidence"]) for hit in hits) / len(hits)
        stability = sum(float(hit.node.get("stability", 0.5)) for hit in hits) / len(hits)
    else:
        relevance = confidence = stability = 0.0

    conflict_penalty = min(0.5, conflicts * 0.16)
    coherence = _clamp(
        relevance * 0.42 + confidence * 0.35 + stability * 0.23 - conflict_penalty
    )

    norm = normalize_text(query)
    lam_relevant = any(
        cue in norm
        for cue in (
            "lam",
            "ty",
            "anh",
            "em",
            "relationship-context",
            "chang tho",
            "tam vi",
            "continuity",
        )
    )
    identity_present = "lam-identity-core" in selected
    relation_present = bool(
        {"lam-ty-relation-origin", "ty-carbon-witness"} & selected
    )
    missing_axis = (0.35 if lam_relevant and not identity_present else 0.0) + (
        0.25 if lam_relevant and not relation_present else 0.0
    )
    drift_risk = _clamp(
        missing_axis
        + (0.2 if not hits else 0.0)
        + conflict_penalty
        + max(0.0, 0.45 - coherence) * 0.5
    )
    uncertainty = _clamp(
        1.0
        - (confidence * 0.45 + relevance * 0.35 + min(1.0, direct_cues / 3.0) * 0.2)
        + conflict_penalty
    )
    return FieldState(
        coherence=round(coherence, 4),
        uncertainty=round(uncertainty, 4),
        drift_risk=round(drift_risk, 4),
        conflict_count=conflicts,
        direct_cue_count=direct_cues,
        selected_ids=selected_ids,
    )


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)
