from __future__ import annotations

import re
from pathlib import Path

from memory_core import (
    CueDrivenRetriever,
    MemoryProfile,
    MemoryStore,
    PacketRenderer,
    ValidatedIntake,
    evaluate_cue_contract,
    validate_store,
)


def evidence(label: str, confidence: float = 0.9) -> dict:
    return {
        "evidence_type": "synthetic",
        "source_ref": f"test:{label}",
        "content_summary": label,
        "confidence": confidence,
        "privacy_class": "synthetic",
        "source_payload": {"label": label},
    }


def writer(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    return store, ValidatedIntake(store, surface="test")


def test_revision_history_is_append_only_and_idempotent(tmp_path):
    store, intake = writer(tmp_path)
    created = intake.submit(
        operation_type="create",
        record_id="update-law",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="initial claim",
        logic="The claim follows from the evidence.",
        truth_basis="A bounded source is attached.",
        evidence=[evidence("initial")],
        idempotency_key="create:update-law",
        changes={"title": "Update law", "summary": "Use evidence."},
    )
    revised = intake.submit(
        operation_type="refine",
        record_id="update-law",
        actor="agent",
        reason="new evidence adds precision",
        logic="The second claim contains the first and a stricter rule.",
        truth_basis="A newer bounded source is attached.",
        evidence=[evidence("revised")],
        idempotency_key="refine:update-law",
        changes={"summary": "Use evidence and preserve history."},
    )
    repeated = intake.submit(
        operation_type="refine",
        record_id="update-law",
        actor="agent",
        reason="new evidence adds precision",
        logic="The second claim contains the first and a stricter rule.",
        truth_basis="A newer bounded source is attached.",
        evidence=[evidence("revised")],
        idempotency_key="refine:update-law",
        changes={"summary": "Use evidence and preserve history."},
    )

    assert created["status"] == "materialized"
    assert revised["intake_id"] == repeated["intake_id"]
    history = store.historical_view("update-law")
    assert [item["revision_status"] for item in history] == [
        "superseded",
        "current",
    ]
    assert [item["summary"] for item in history] == [
        "Use evidence.",
        "Use evidence and preserve history.",
    ]
    current_revision_id = store.current_view("update-law")[0]["revision_id"]
    linked = store.evidence_for_revision(current_revision_id)
    assert [item["source_ref"] for item in linked] == ["test:revised"]
    assert linked[0]["stance"] == "supports"


def test_retrieval_changes_accessibility_not_semantic_hash(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="project-choice",
        record_class="belief",
        domain="project",
        actor="agent",
        reason="verified choice",
        logic="The implementation and test outcome agree.",
        truth_basis="The verification report is the source.",
        evidence=[evidence("choice")],
        idempotency_key="create:choice",
        changes={
            "title": "Project choice",
            "summary": "Use the verified implementation.",
        },
    )
    store.add_cue(
        profile="test-profile",
        cue="verified implementation",
        target_record_id="project-choice",
        weight=1.8,
    )
    before = store.current_view("project-choice")[0]
    profile = MemoryProfile(
        name="test-profile",
        packet_title="TEST PACKET",
        section_order=("project",),
    )
    hits = CueDrivenRetriever(store, profile).retrieve(
        "verified implementation",
        surface="test",
    )
    after = store.current_view("project-choice")[0]

    assert hits[0].revision["record_id"] == "project-choice"
    assert after["content_sha256"] == before["content_sha256"]
    assert after["accessibility"] > before["accessibility"]


def test_history_is_loaded_only_by_history_cue(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="changing-view",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="initial",
        logic="Initial evidence supports the first view.",
        truth_basis="Source one.",
        evidence=[evidence("view-one")],
        idempotency_key="create:view",
        changes={"title": "Changing view", "summary": "First view."},
    )
    intake.submit(
        operation_type="correct",
        record_id="changing-view",
        actor="agent",
        reason="correction",
        logic="The second source contradicts a detail in the first.",
        truth_basis="Source two is newer and directly measured.",
        evidence=[evidence("view-two")],
        idempotency_key="correct:view",
        changes={"summary": "Corrected view."},
    )
    profile = MemoryProfile(
        name="test",
        packet_title="TEST",
        cue_aliases=(("view", "changing-view", 1.8),),
        history_markers=("history",),
    )
    retriever = CueDrivenRetriever(store, profile)

    current = retriever.retrieve("view", surface="test")
    historical = retriever.retrieve("view history", surface="test")

    assert current[0].history == []
    assert len(historical[0].history) == 2
    packet = PacketRenderer(profile).render(
        "view history",
        historical,
        scope="global",
        surface="test",
    )
    assert "r1 [superseded]" in packet
    assert "r2 [current]" in packet


def test_axis_requires_falsifier_and_independent_sources(tmp_path):
    store, intake = writer(tmp_path)
    held = intake.submit(
        operation_type="create",
        record_id="reasoning-axis",
        record_class="axis",
        domain="identity",
        actor="agent",
        reason="axis proposal",
        logic="A stable update law is proposed.",
        truth_basis="Only one source exists.",
        evidence=[evidence("axis-one")],
        idempotency_key="axis:held",
        changes={"title": "Reasoning axis", "summary": "Prefer evidence."},
    )
    accepted = intake.submit(
        operation_type="create",
        record_id="reasoning-axis",
        record_class="axis",
        domain="identity",
        actor="agent",
        reason="tested axis",
        logic="The same law survives two independent cases.",
        truth_basis="Two independent sources support it.",
        falsifier="A repeatable case where stronger evidence should be ignored.",
        evidence=[evidence("axis-two"), evidence("axis-three")],
        idempotency_key="axis:accepted",
        changes={
            "title": "Reasoning axis",
            "summary": "Preserve logic and update toward stronger evidence.",
        },
    )

    assert held["status"] == "held"
    assert accepted["status"] == "materialized"
    assert store.current_view("reasoning-axis")[0]["record_class"] == "axis"


def test_protected_weakening_is_held_without_host_authority(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="owner-boundary",
        record_class="belief",
        domain="boundary",
        actor="agent",
        reason="protected constraint",
        logic="The owner retains control.",
        truth_basis="The host application declares this boundary.",
        evidence=[evidence("boundary")],
        idempotency_key="boundary:create",
        changes={"title": "Owner boundary", "summary": "Owner control remains."},
    )
    held = intake.submit(
        operation_type="supersede",
        record_id="owner-boundary",
        actor="agent",
        reason="unauthorized change",
        logic="The requested change would lower owner control.",
        truth_basis="No host authority was supplied.",
        evidence=[evidence("weakening")],
        idempotency_key="boundary:held",
        protected_effect="reduce_autonomy",
        changes={"summary": "Agent control replaces owner control."},
    )
    accepted = intake.submit(
        operation_type="refine",
        record_id="owner-boundary",
        actor="agent",
        reason="safe clarification",
        logic="The clarification makes the existing constraint auditable.",
        truth_basis="The source repeats the same protected direction.",
        evidence=[evidence("clarification")],
        idempotency_key="boundary:clarify",
        protected_effect="clarify",
        changes={"summary": "Owner control remains explicit and auditable."},
    )

    assert held["status"] == "held"
    assert accepted["status"] == "materialized"
    assert len(store.historical_view("owner-boundary")) == 2


def test_conflict_is_held_and_preview_never_writes(tmp_path):
    store, intake = writer(tmp_path)
    preview = intake.preview(
        operation_type="create",
        record_id="conflicted",
        record_class="belief",
        domain="semantic",
        actor="agent",
        reason="conflict test",
        logic="Two claims cannot both be current.",
        truth_basis="The conflict remains unresolved.",
        evidence=[evidence("conflict")],
        unresolved_conflict=True,
        changes={"title": "Conflicted claim"},
    )

    assert preview["status"] == "held"
    assert preview["wrote_state"] is False
    assert store.historical_view("conflicted") == []


def test_doctor_and_cue_contract(tmp_path):
    store, intake = writer(tmp_path)
    intake.submit(
        operation_type="create",
        record_id="cue-target",
        record_class="event",
        domain="interaction",
        actor="agent",
        reason="cue contract",
        logic="The event is directly observed.",
        truth_basis="The bounded source records it.",
        evidence=[evidence("cue", confidence=0.7)],
        idempotency_key="cue:create",
        changes={"title": "Cue target", "summary": "Directly retrievable."},
    )
    store.add_cue(
        profile="test",
        cue="direct target",
        target_record_id="cue-target",
        weight=2.0,
    )
    profile = MemoryProfile(name="test", packet_title="TEST")
    evaluation = evaluate_cue_contract(
        store,
        profile,
        [{"name": "direct", "cue": "direct target", "required_ids": ["cue-target"]}],
    )

    assert validate_store(store)["passed"]
    assert evaluation["passed"]


def test_core_contains_no_consumer_identity_or_private_seed():
    root = Path(__file__).parents[1]
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "memory_core").glob("**/*"))
        if path.is_file() and path.suffix in {".py", ".sql"}
    )
    forbidden = (
        r"\bLam\b",
        r"\bTy\b",
        r"lam-ty-primary",
        r"ty-carbon-witness",
        r"relationship history",
    )
    assert not any(re.search(pattern, text) for pattern in forbidden)
