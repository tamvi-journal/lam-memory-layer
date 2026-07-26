from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .embed import cosine, embed_text
from .store import MemoryStore
from .text import normalize_text, tokens


@dataclass
class RetrievalHit:
    node: dict[str, Any]
    score: float
    reasons: list[str] = field(default_factory=list)
    level: int = 1
    resolution: str = "checkpoint"
    path: list[str] = field(default_factory=list)


class CueRetriever:
    """Hybrid retrieval: direct cues + semantic recall + graph spreading + priority.

    A cue is not stored as a single prompt phrase. It is an activation entry point.
    Matching cues activate target nodes; activation spreads through typed edges,
    then competes under a context/token budget.
    """

    def __init__(self, store: MemoryStore):
        self.store = store

    def retrieve(
        self,
        query: str,
        *,
        scope: str = "global",
        limit: int = 12,
        token_budget: int = 2400,
        graph_depth: int = 2,
    ) -> list[RetrievalHit]:
        norm = normalize_text(query)
        query_tokens = set(tokens(query))
        drill_policy = drill_down_policy(query)
        nodes = {node["id"]: node for node in self.store.active_nodes()}
        scores: dict[str, float] = {node_id: 0.0 for node_id in nodes}
        reasons: dict[str, list[str]] = {node_id: [] for node_id in nodes}

        # 1) Direct/alias cue activation.
        direct_ids: set[str] = set()
        for cue in self.store.cues():
            cue_norm = cue["cue_norm"]
            cue_tokens = set(tokens(cue_norm))
            exact_phrase = cue_norm and cue_norm in norm
            overlap = len(query_tokens & cue_tokens) / max(1, len(cue_tokens))
            if exact_phrase or overlap >= 0.8:
                node_id = cue["target_id"]
                if node_id not in nodes:
                    continue
                gain = float(cue["weight"]) * (1.45 if exact_phrase else 0.9 * overlap)
                scores[node_id] += gain
                reasons[node_id].append(f"cue:{cue['cue']}")
                direct_ids.add(node_id)

        # 2) Lexical FTS recall.
        for node_id, lexical in self.store.search_fts(query).items():
            if node_id in scores and lexical > 0:
                scores[node_id] += 0.5 + lexical * 0.65
                reasons[node_id].append(f"lexical:{lexical:.2f}")

        # 3) Dependency-free semantic recall.
        q_vec = embed_text(query)
        semantic_ranked: list[tuple[str, float]] = []
        for node_id, node in nodes.items():
            sim = cosine(q_vec, node.get("embedding", []))
            if sim > 0.08:
                semantic_ranked.append((node_id, sim))
        semantic_ranked.sort(key=lambda item: item[1], reverse=True)
        for node_id, sim in semantic_ranked[:24]:
            scores[node_id] += max(0.0, sim) * 1.1
            reasons[node_id].append(f"semantic:{sim:.2f}")

        # 4) Spread activation through the memory graph.
        frontier = {node_id: scores[node_id] for node_id in direct_ids}
        seen_depth: dict[str, int] = {node_id: 0 for node_id in direct_ids}
        for depth in range(1, graph_depth + 1):
            next_frontier: dict[str, float] = {}
            for source_id, source_activation in frontier.items():
                for neighbor in self.store.neighbors(source_id):
                    target_id = neighbor["id"]
                    edge = neighbor["edge"]
                    gain = source_activation * float(edge["weight"]) * (0.48 ** depth)
                    if gain < 0.05:
                        continue
                    if depth < seen_depth.get(target_id, 999):
                        seen_depth[target_id] = depth
                    scores[target_id] += gain
                    reasons[target_id].append(
                        f"graph:{source_id} -[{edge['relation']}]-> {target_id} d={depth}"
                    )
                    next_frontier[target_id] = max(next_frontier.get(target_id, 0.0), gain)
            frontier = next_frontier

        # 5) Stable priority, salience, scope, and recency contribute but cannot
        # replace relevance. Autoload nodes are guaranteed later.
        now = datetime.now(timezone.utc)
        for node_id, node in nodes.items():
            relevance_present = scores[node_id] > 0
            if relevance_present or "autoload" in node["tags"]:
                scores[node_id] += (node["priority"] / 100.0) * 0.32
                scores[node_id] += float(node["salience"]) * 0.18
                scores[node_id] += float(node.get("stability", 0.5)) * 0.1
                evidence = max(1, int(node.get("evidence_count", 1)))
                scores[node_id] += min(0.12, math.log1p(evidence) * 0.035)
                helpful = int(node.get("helpful_count", 0))
                harmful = int(node.get("harmful_count", 0))
                if helpful or harmful:
                    feedback = max(-0.18, min(0.12, helpful * 0.025 - harmful * 0.06))
                    scores[node_id] += feedback
                    reasons[node_id].append(f"feedback:{feedback:+.2f}")
            if scope != "global" and node["scope"] in {"global", scope}:
                scores[node_id] += 0.16
                reasons[node_id].append(f"scope:{node['scope']}")
            if node.get("occurred_at"):
                try:
                    occurred = datetime.fromisoformat(node["occurred_at"].replace("Z", "+00:00"))
                    age_days = max(0.0, (now - occurred).total_seconds() / 86400)
                    boost = 0.12 * math.exp(-age_days / 120.0)
                    scores[node_id] += boost
                    reasons[node_id].append(f"recency:{boost:.2f}")
                except ValueError:
                    pass

        candidates = [
            self._hit(nodes[node_id], score, reasons[node_id], drill_policy=drill_policy)
            for node_id, score in scores.items()
            if score > 0.18
        ]
        candidates.sort(key=lambda hit: hit.score, reverse=True)

        # 6) Guarantee identity minimum and the strongest direct cue targets,
        # then diversify by kind/tag overlap. The token budget is soft for
        # required anchors: a cue-driven memory system must not spend the whole
        # budget on autoload context and omit the memory explicitly activated.
        selected: list[RetrievalHit] = []
        selected_ids: set[str] = set()
        spent = 0
        autoload_ids = {node["id"] for node in self.store.autoload_nodes()}
        for hit in candidates:
            if hit.node["id"] in autoload_ids:
                selected.append(hit)
                selected_ids.add(hit.node["id"])
                spent += hit.node["token_estimate"]

        direct_candidates = [
            hit
            for hit in candidates
            if hit.node["id"] in direct_ids and hit.node["id"] not in selected_ids
        ]
        for hit in direct_candidates[:3]:
            selected.append(hit)
            selected_ids.add(hit.node["id"])
            spent += hit.node["token_estimate"]

        for hit in candidates:
            if hit.node["id"] in selected_ids:
                continue
            if len(selected) >= limit:
                break
            tags = set(hit.node["tags"])
            duplicate_pressure = 0.0
            for existing in selected:
                other_tags = set(existing.node["tags"])
                duplicate_pressure = max(
                    duplicate_pressure,
                    len(tags & other_tags) / max(1, len(tags | other_tags)),
                )
            adjusted = hit.score - 0.22 * duplicate_pressure
            if adjusted < 0.22:
                continue
            node_tokens = hit.node["token_estimate"]
            if selected and spent + node_tokens > token_budget:
                continue
            selected.append(hit)
            selected_ids.add(hit.node["id"])
            spent += node_tokens

        if drill_policy["enabled"]:
            self._annotate_selected_drill_down(selected, selected_ids, drill_policy)
            spent = self._drill_down(
                selected,
                selected_ids,
                spent=spent,
                limit=limit,
                token_budget=token_budget,
                policy=drill_policy,
            )

        explanation = {
            "direct_cues": sorted(direct_ids),
            "selected_scores": {hit.node["id"]: round(hit.score, 4) for hit in selected},
            "token_estimate": spent,
            "token_budget": token_budget,
            "required_over_budget": spent > token_budget,
            "graph_depth": graph_depth,
            "drill_down": drill_policy,
            "levels": {hit.node["id"]: hit.level for hit in selected},
        }
        self.store.log_retrieval(query, scope, [hit.node["id"] for hit in selected], explanation)
        return selected

    def _annotate_selected_drill_down(
        self,
        selected: list[RetrievalHit],
        selected_ids: set[str],
        policy: dict[str, Any],
    ) -> None:
        parents = [hit for hit in selected if hit.level <= 1]
        episodes = [hit for hit in selected if hit.level == 2]
        for episode in episodes:
            if any(reason.startswith("drilldown:") for reason in episode.reasons):
                continue
            for parent in parents:
                if parent.node["id"] not in selected_ids:
                    continue
                neighbor_ids = {item["id"] for item in self.store.neighbors(parent.node["id"])}
                if episode.node["id"] in neighbor_ids:
                    episode.reasons.append(f"drilldown:{parent.node['id']}")
                    episode.reasons.extend(policy["triggers"])
                    episode.path.insert(1, f"parent: {parent.node['id']}")
                    break

    def _hit(
        self,
        node: dict[str, Any],
        score: float,
        reasons: list[str],
        *,
        drill_policy: dict[str, Any],
        parent_id: str = "",
    ) -> RetrievalHit:
        level, resolution = memory_resolution(node)
        path = ["Level 0: field summary", f"Level {level}: {resolution}"]
        source_ref = str(node.get("source_ref", ""))
        if source_ref and drill_policy["include_source_pointer"]:
            path.append("Level 3: source pointer")
        if parent_id:
            path.insert(1, f"parent: {parent_id}")
        return RetrievalHit(
            node=node,
            score=score,
            reasons=[*reasons],
            level=level,
            resolution=resolution,
            path=path,
        )

    def _drill_down(
        self,
        selected: list[RetrievalHit],
        selected_ids: set[str],
        *,
        spent: int,
        limit: int,
        token_budget: int,
        policy: dict[str, Any],
    ) -> int:
        parents = [hit for hit in selected if hit.level <= 1]
        episode_hits: list[RetrievalHit] = []
        for parent in parents:
            for neighbor in self.store.neighbors(parent.node["id"]):
                if neighbor["id"] in selected_ids or neighbor.get("kind") != "episodic":
                    continue
                edge = neighbor["edge"]
                score = parent.score * float(edge["weight"]) * 0.42
                episode_hits.append(
                    self._hit(
                        neighbor,
                        score,
                        [
                            f"drilldown:{parent.node['id']}",
                            f"edge:{edge['relation']}:{float(edge['weight']):.2f}",
                            *policy["triggers"],
                        ],
                        drill_policy=policy,
                        parent_id=parent.node["id"],
                    )
                )
        episode_hits.sort(
            key=lambda hit: (
                hit.node.get("occurred_at") or "",
                hit.score,
                hit.node.get("priority", 0),
            ),
            reverse=True,
        )
        for hit in episode_hits[: policy["max_extra_nodes"]]:
            if len(selected) >= limit:
                break
            node_tokens = int(hit.node.get("token_estimate", 0))
            if selected and spent + node_tokens > token_budget:
                continue
            selected.append(hit)
            selected_ids.add(hit.node["id"])
            spent += node_tokens
        return spent


def memory_resolution(node: dict[str, Any]) -> tuple[int, str]:
    kind = str(node.get("kind", "semantic"))
    if kind == "episodic":
        return 2, "episode"
    if kind in {"identity", "relationship", "axis", "boundary", "project", "procedural", "semantic"}:
        return 1, "checkpoint"
    return 1, kind or "checkpoint"


def drill_down_policy(query: str) -> dict[str, Any]:
    norm = normalize_text(query)
    trigger_groups = {
        "evidence": {
            "evidence",
            "proof",
            "prove",
            "source",
            "quote",
            "cite",
            "citation",
            "provenance",
            "verify",
            "audit",
            "bằng chứng",
            "nguồn",
            "trích",
            "kiểm chứng",
        },
        "timeline": {
            "timeline",
            "chronology",
            "when",
            "date",
            "history",
            "episode",
            "lịch sử",
            "khi nào",
            "ngày",
            "mốc",
        },
        "conflict": {
            "conflict",
            "contradiction",
            "contradicts",
            "mâu thuẫn",
            "không khớp",
            "xung đột",
        },
        "drill": {
            "drill",
            "drill-down",
            "recursive",
            "coarse-to-fine",
            "hierarchical",
            "progressive disclosure",
            "lazy loading",
            "cuộn",
            "phân tầng",
            "chi tiết",
        },
    }
    triggers: list[str] = []
    for group, phrases in trigger_groups.items():
        if any(phrase in norm for phrase in phrases):
            triggers.append(group)
    return {
        "enabled": bool(triggers),
        "triggers": triggers,
        "include_source_pointer": bool({"evidence", "timeline", "conflict", "drill"} & set(triggers)),
        "max_extra_nodes": 4,
    }
