from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter
from typing import Any

from .embed import cosine, embed_text
from .store import MemoryStore


def consolidate_candidates(
    store: MemoryStore,
    *,
    min_evidence: int = 3,
    similarity_threshold: float = 0.34,
) -> list[dict[str, Any]]:
    anchored = [
        node
        for node in store.active_nodes()
        if node["kind"] == "episodic" and "candidate-approved" in node["tags"]
    ]
    episodes = [*anchored, *_held_observations(store)]
    clusters = _cluster_episodes(episodes, similarity_threshold)
    proposals: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < min_evidence:
            continue
        proposal = _semantic_proposal(store, cluster)
        proposals.append(store.add_candidate(proposal))
    _reinforce_co_retrieval(store)
    return proposals


def _cluster_episodes(
    episodes: list[dict[str, Any]], threshold: float
) -> list[list[dict[str, Any]]]:
    remaining = {episode["id"]: episode for episode in episodes}
    clusters: list[list[dict[str, Any]]] = []
    while remaining:
        _, seed = remaining.popitem()
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            centroid = _centroid([item["embedding"] for item in cluster])
            for node_id, candidate in list(remaining.items()):
                same_scope = candidate["scope"] == seed["scope"]
                shared_tags = bool(set(candidate["tags"]) & set(seed["tags"]))
                if same_scope and shared_tags and cosine(centroid, candidate["embedding"]) >= threshold:
                    cluster.append(remaining.pop(node_id))
                    changed = True
        clusters.append(cluster)
    return clusters


def _semantic_proposal(
    store: MemoryStore, cluster: list[dict[str, Any]]
) -> dict[str, Any]:
    scope = Counter(node["scope"] for node in cluster).most_common(1)[0][0]
    tags = Counter(
        tag
        for node in cluster
        for tag in node["tags"]
        if tag not in {"codex-turn", "episode", "candidate-approved", scope}
    )
    top_tags = [tag for tag, _ in tags.most_common(5)]
    evidence_ids = sorted(node["id"] for node in cluster)
    fingerprint = hashlib.sha256(
        ("semantic\n" + "\n".join(evidence_ids)).encode("utf-8")
    ).hexdigest()
    titles = [node["title"].split(": ", 1)[-1] for node in cluster[:5]]
    title = f"Recurring pattern in {scope}: {titles[0][:70]}"
    summary = (
        f"Across {len(cluster)} distinct task episodes in {scope}, a recurring "
        f"pattern appears around {', '.join(top_tags) if top_tags else 'the same working concern'}. "
        "This is a consolidation candidate, not an accepted belief."
    )
    relation_targets = sorted(
        {
            edge["dst_id"] if edge["src_id"] in evidence_ids else edge["src_id"]
            for edge in store.edges()
            if edge["relation"] == "supports"
            and (edge["src_id"] in evidence_ids or edge["dst_id"] in evidence_ids)
        }
        | {
            target
            for node in cluster
            for target in node.get("relation_targets", [])
        }
    )
    importance = sum(float(node.get("importance", 0.6)) for node in cluster) / len(
        cluster
    )
    return {
        "id": fingerprint[:20],
        "kind": "semantic",
        "title": title,
        "summary": summary,
        "content": "\n".join(f"- {node['title']}: {node['summary']}" for node in cluster),
        "status": "pending",
        "priority": min(88, 60 + int(math.log2(len(cluster) + 1) * 7)),
        "confidence": min(0.9, 0.48 + len(cluster) * 0.07),
        "salience": min(0.9, 0.5 + len(cluster) * 0.05),
        "stability": min(0.88, 0.35 + len(cluster) * 0.08),
        "evidence_count": len(cluster),
        "scope": scope,
        "source_type": "lml-consolidation",
        "source_ref": " + ".join(evidence_ids),
        "tags": ["dreaming-candidate", *top_tags],
        "relation_targets": relation_targets,
        "sensitivity": (
            "relational"
            if {"relationship", "relationship-context", "lam", "ty"} & set(top_tags)
            else "ordinary"
        ),
        "importance": min(0.92, max(0.62, importance + 0.12)),
        "capture_reasons": [
            "recurring_pattern",
            f"evidence_count:{len(cluster)}",
        ],
        "fingerprint": fingerprint,
    }


def _held_observations(store: MemoryStore) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for candidate in store.candidates(status="held", limit=500):
        joined = "\n".join(
            [
                candidate["title"],
                candidate["summary"],
                candidate["content"],
                " ".join(candidate["tags"]),
            ]
        )
        observations.append(
            {
                "id": f"held-{candidate['id']}",
                "kind": "episodic",
                "title": candidate["title"],
                "summary": candidate["summary"],
                "content": candidate["content"],
                "scope": candidate["scope"],
                "tags": [*candidate["tags"], "held-observation"],
                "embedding": embed_text(joined),
                "relation_targets": candidate["relation_targets"],
                "importance": candidate["importance"],
            }
        )
    return observations


def _reinforce_co_retrieval(store: MemoryStore) -> None:
    autoload_ids = {node["id"] for node in store.autoload_nodes()}
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT result_ids_json, explanation_json "
            "FROM retrieval_log ORDER BY id DESC LIMIT 250"
        ).fetchall()
    pair_counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        ids = sorted(set(json.loads(row["result_ids_json"] or "[]")))
        explanation = json.loads(row["explanation_json"] or "{}")
        direct_ids = set(explanation.get("direct_cues", []))
        cue_specific = [
            node_id
            for node_id in ids
            if node_id not in autoload_ids or node_id in direct_ids
        ]
        pair_counts.update(itertools.combinations(cue_specific[:12], 2))
    with store.connect() as conn:
        conn.execute("DELETE FROM memory_edges WHERE relation='co-retrieved'")
    for (left, right), count in pair_counts.items():
        if count < 3:
            continue
        weight = min(0.72, 0.2 + math.log1p(count) * 0.1)
        store.add_edge(
            left,
            right,
            "co-retrieved",
            round(weight, 4),
            f"{count} co-retrievals in recent log",
        )


def _centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    result = [0.0] * size
    for vector in vectors:
        for index, value in enumerate(vector[:size]):
            result[index] += value
    return [value / len(vectors) for value in result]
