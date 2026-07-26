from lml.context import ContextPacketBuilder
from lml.retrieval import CueRetriever, drill_down_policy
from lml.store import MemoryNode, MemoryStore


def _store(tmp_path):
    store = MemoryStore(tmp_path / "memory" / "lml.sqlite3")
    store.init()
    store.upsert_node(
        MemoryNode(
            id="project-checkpoint",
            kind="semantic",
            title="Project checkpoint",
            summary="A stable project-level checkpoint.",
            content="The project should retrieve this checkpoint before work.",
            priority=80,
            confidence=0.9,
            source_ref="docs/project.md#checkpoint",
            tags=["checkpoint"],
        )
    )
    store.upsert_node(
        MemoryNode(
            id="project-episode",
            kind="episodic",
            title="Project episode",
            summary="A dated event that supports the checkpoint.",
            content="This episode is lower-level evidence for the checkpoint.",
            priority=60,
            confidence=0.8,
            source_ref="logs/project-event.md#2026-01-01",
            occurred_at="2026-01-01T00:00:00+00:00",
            tags=["episode"],
        )
    )
    store.add_cue("project alpha", "project-checkpoint", weight=1.0)
    store.add_edge("project-checkpoint", "project-episode", "supported-by", weight=1.0)
    return store


def test_plain_cue_can_stay_at_checkpoint_level(tmp_path):
    hits = CueRetriever(_store(tmp_path)).retrieve("project alpha", limit=1)

    assert [hit.node["id"] for hit in hits] == ["project-checkpoint"]
    assert hits[0].level == 1
    assert hits[0].resolution == "checkpoint"


def test_evidence_cue_drills_down_to_episode(tmp_path):
    hits = CueRetriever(_store(tmp_path)).retrieve(
        "project alpha evidence source timeline", limit=4
    )

    ids = [hit.node["id"] for hit in hits]
    episode = next(hit for hit in hits if hit.node["id"] == "project-episode")
    assert "project-checkpoint" in ids
    assert "project-episode" in ids
    assert episode.level == 2
    assert "drilldown:project-checkpoint" in episode.reasons
    assert "Level 3: source pointer" in episode.path


def test_context_packet_exposes_memory_path(tmp_path):
    packet = ContextPacketBuilder(CueRetriever(_store(tmp_path))).build(
        "project alpha evidence", limit=4
    )

    assert "## Retrieval path" in packet
    assert "hierarchical / coarse-to-fine retrieval" in packet
    assert "Level 1: checkpoint" in packet
    assert "Level 2: episode" in packet
    assert "Level 3: source pointer" in packet


def test_drill_down_policy_is_triggered_by_detail_requests():
    policy = drill_down_policy("need chronology, conflict, and source evidence")

    assert policy["enabled"] is True
    assert {"timeline", "conflict", "evidence"} <= set(policy["triggers"])
