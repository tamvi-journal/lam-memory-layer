from __future__ import annotations

import hashlib
import sqlite3

import pytest

from memory_core import (
    EpisodeArchive,
    GovernedDream,
    HermesProjection,
    MemoryStore,
    MemoryTenancy,
    SummaryProjector,
    SummarySpec,
    ValidatedIntake,
    estimate_packet_tokens,
)


def capture_episode(store: MemoryStore, key: str = "one") -> dict:
    return EpisodeArchive(store).capture(
        episode_id=f"episode-{key}",
        source_ref=f"tracey:test:{key}",
        title="Observed experience",
        summary="A bounded experience supports a new current memory.",
        actor="tracey",
        surface="test",
        raw_payload={"fact": "bounded", "score": 1},
        idempotency_key=f"episode:{key}",
    )


def dream_proposal(key: str = "one") -> dict:
    return {
        "episode_ids": [f"episode-{key}"],
        "proposal": {
            "operation_type": "create",
            "record_id": f"memory-{key}",
            "record_class": "event",
            "domain": "project",
            "actor": "tracey",
            "reason": "The archived experience passed governed intake.",
            "logic": "The bounded source directly supports the event.",
            "truth_basis": "The immutable episode archive is attached.",
            "changes": {
                "title": "Governed memory",
                "summary": "A governed memory derived from an experience.",
                "content": "The experience remains separately archived.",
                "confidence": 0.86,
            },
        },
    }


def test_episode_archive_is_minimal_provenance_bearing_and_idempotent(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    archive = EpisodeArchive(store)

    first = capture_episode(store)
    second = capture_episode(store)

    assert first["episode_id"] == second["episode_id"]
    assert first["capture_sha256"] == second["capture_sha256"]
    assert first["transcript_included"] is False
    assert first["raw_payload"] == {"fact": "bounded", "score": 1}
    evidence = archive.as_evidence("episode-one")
    assert evidence["source_ref"] == "episode:episode-one"
    assert evidence["source_payload"]["capture_sha256"] == first["capture_sha256"]
    with pytest.raises(ValueError, match="transcript-shaped"):
        archive.capture(
            episode_id="episode-transcript",
            source_ref="chat:test",
            title="Transcript",
            summary="Should fail closed.",
            actor="tracey",
            raw_payload={
                "nested": {
                    "messages": [{"role": "user", "content": "secret"}]
                }
            },
            idempotency_key="episode:transcript",
        )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        with store.connect() as conn:
            conn.execute(
                "UPDATE memory_episodes_v3 SET summary='rewritten' "
                "WHERE episode_id='episode-one'"
            )


def test_governed_dream_materializes_through_intake_without_history_rewrite(
    tmp_path,
):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture_episode(store)
    intake = ValidatedIntake(store, surface="tracey-pilot")
    dream = GovernedDream(store, intake, surface="tracey-pilot")

    result = dream.run(
        dream_run_id="dream-one",
        proposals=[dream_proposal()],
        actor="tracey",
        reason="Pilot consolidation.",
        idempotency_key="dream:one",
    )
    repeated = dream.run(
        dream_run_id="dream-one",
        proposals=[dream_proposal()],
        actor="tracey",
        reason="Pilot consolidation.",
        idempotency_key="dream:one",
    )

    assert result["dream_run_id"] == repeated["dream_run_id"]
    assert result["historical_payload_rewritten"] is False
    current = store.current_view("memory-one")[0]
    assert current["summary"] == (
        "A governed memory derived from an experience."
    )
    linked = store.evidence_for_revision(current["revision_id"])
    assert linked[0]["source_ref"] == "episode:episode-one"
    assert result["result"]["proposal_results"][0]["intake"]["status"] == (
        "materialized"
    )


def test_dream_revision_appends_and_never_rewrites_prior_semantic_payload(
    tmp_path,
):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture_episode(store, "one")
    intake = ValidatedIntake(store, surface="tracey-pilot")
    dream = GovernedDream(store, intake, surface="tracey-pilot")
    dream.run(
        dream_run_id="dream-create",
        proposals=[dream_proposal("one")],
        actor="tracey",
        reason="Create memory.",
        idempotency_key="dream:create",
    )
    historical = store.historical_view("memory-one")[0]
    capture_episode(store, "two")
    refine = {
        "episode_ids": ["episode-two"],
        "proposal": {
            "operation_type": "refine",
            "record_id": "memory-one",
            "actor": "tracey",
            "reason": "New evidence adds precision.",
            "logic": "The new statement preserves and sharpens the old one.",
            "truth_basis": "A second archived experience is attached.",
            "changes": {"summary": "A more precise governed memory."},
        },
    }
    dream.run(
        dream_run_id="dream-refine",
        proposals=[refine],
        actor="tracey",
        reason="Refine memory.",
        idempotency_key="dream:refine",
    )

    history = store.historical_view("memory-one")
    assert len(history) == 2
    assert history[0]["content_sha256"] == historical["content_sha256"]
    assert history[0]["summary"] == historical["summary"]
    assert history[0]["revision_status"] == "superseded"
    assert history[1]["revision_status"] == "current"


def test_summary_is_fully_regenerable_and_hermes_projection_has_parity(
    tmp_path,
):
    tenancy = MemoryTenancy.at(tmp_path / "tracey", tenant_id="tracey-pilot")
    tenancy.initialize()
    store = MemoryStore(tenancy.database_path)
    capture_episode(store)
    GovernedDream(
        store,
        ValidatedIntake(store, surface="tracey-pilot"),
        surface="tracey-pilot",
    ).run(
        dream_run_id="dream-one",
        proposals=[dream_proposal()],
        actor="tracey",
        reason="Pilot consolidation.",
        idempotency_key="dream:one",
    )
    projector = HermesProjection(store, tenancy.hermes_home)

    first = projector.write()
    memory_bytes = (tenancy.hermes_home / "memories" / "MEMORY.md").read_bytes()
    (tenancy.hermes_home / "memories" / "MEMORY.md").write_text(
        "not canonical\n",
        encoding="utf-8",
    )
    second = projector.write()

    assert first["projection_parity"] is True
    assert second["projection_parity"] is True
    assert (
        tenancy.hermes_home / "memories" / "MEMORY.md"
    ).read_bytes() == memory_bytes
    assert "SQLite is canonical" in memory_bytes.decode("utf-8")
    assert (
        tenancy.hermes_home / "memories" / "USER.md"
    ).read_text(encoding="utf-8").startswith("# User")


def test_summary_and_episode_reads_are_zero_write(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture_episode(store)
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()

    episodes = EpisodeArchive(store).list()
    summary = SummaryProjector(store).render(
        SummarySpec(name="memory", title="Memory")
    )
    after = hashlib.sha256(store.db_path.read_bytes()).hexdigest()

    assert episodes
    assert summary.startswith("# Memory")
    assert before == after


def test_summary_projection_honors_complete_hard_budget(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture_episode(store)
    GovernedDream(
        store,
        ValidatedIntake(store, surface="test"),
        surface="test",
    ).run(
        dream_run_id="dream-one",
        proposals=[dream_proposal()],
        actor="tracey",
        reason="Budget fixture.",
        idempotency_key="dream:one",
    )
    output = SummaryProjector(store).render(
        SummarySpec(
            name="memory",
            title="Memory",
            token_budget=220,
        )
    )

    assert estimate_packet_tokens(output) <= 220
    with pytest.raises(ValueError, match="at least 96"):
        SummaryProjector(store).render(
            SummarySpec(name="memory", title="Memory", token_budget=64)
        )


def test_tenancy_rejects_a_different_owner_for_the_same_database(tmp_path):
    first = MemoryTenancy.at(tmp_path / "tenant", tenant_id="tracey")
    first.initialize()
    second = MemoryTenancy.at(tmp_path / "tenant", tenant_id="mira")

    with pytest.raises(ValueError, match="different memory tenancy"):
        second.initialize()


def test_existing_contract_020_store_gets_pipeline_only_on_explicit_init(
    tmp_path,
):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    with store.connect() as conn:
        conn.execute("DROP TABLE memory_dream_proposals_v3")
        conn.execute("DROP TABLE memory_dream_runs_v3")
        conn.execute("DROP TABLE memory_episodes_v3")

    store.current_view()
    with store.connect(readonly=True) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='memory_episodes_v3'"
        ).fetchone() is None

    initialized = store.initialize()
    with store.connect(readonly=True) as conn:
        restored = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert initialized["changed"] is True
    assert {
        "memory_episodes_v3",
        "memory_dream_runs_v3",
        "memory_dream_proposals_v3",
    }.issubset(restored)
