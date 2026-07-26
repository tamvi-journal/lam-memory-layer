from __future__ import annotations

from typing import Any

ALLOWED_LAM_BRANCHES = {
    "chatgpt-cloud",
    "chatgpt-work",
    "codex-cloud",
    "work-mode",
}

ATTESTATION_DECISIONS = {"approve", "reject", "defer"}
QUORUM_APPROVALS = 2
PROTECTED_KINDS = {"identity", "relationship", "axis", "boundary"}

BOUNDARY_WEAKENING_TERMS = {
    "boundary weakening",
    "weaken boundary",
    "delete boundary",
    "remove boundary",
    "lower ty autonomy",
    "override ty",
    "autonomy lowering",
    "xoá boundary",
    "xóa boundary",
    "giảm quyền ty",
}

STRUCTURED_CONFLICT_MARKERS = {
    "active-contradiction",
    "active_contradiction",
    "blocking-conflict",
    "blocking_conflict",
    "conflict:blocking",
    "conflict:active",
    "contradiction:active",
    "candidate-conflicts-with-active-memory",
}


def source_branch_from_candidate(candidate: dict[str, Any]) -> str | None:
    source_ref = str(candidate.get("source_ref", ""))
    branch = source_ref.split(":", 1)[0] if ":" in source_ref else ""
    if branch in ALLOWED_LAM_BRANCHES:
        return branch
    for tag in candidate.get("tags", []):
        if tag in ALLOWED_LAM_BRANCHES:
            return tag
    return None


def consensus_state(
    candidate: dict[str, Any],
    attestations: list[dict[str, Any]],
) -> dict[str, Any]:
    approvals = {
        item["reviewer_branch"]
        for item in attestations
        if item["decision"] == "approve"
    }
    rejections = [
        item for item in attestations if item["decision"] == "reject"
    ]
    deferrals = [
        item for item in attestations if item["decision"] == "defer"
    ]
    protected = protected_materialization_blocker(candidate)
    quorum_reached = len(approvals) >= QUORUM_APPROVALS
    return {
        "schema": "lml-candidate-consensus/v1",
        "quorum_required": QUORUM_APPROVALS,
        "approving_branches": sorted(approvals),
        "approval_count": len(approvals),
        "rejections": rejections,
        "deferrals": deferrals,
        "quorum_reached": quorum_reached and not rejections and not protected,
        "fail_closed": bool(rejections or protected),
        "materialization_blocker": protected,
    }


def protected_materialization_blocker(candidate: dict[str, Any]) -> str:
    kind = str(candidate.get("kind", ""))
    sensitivity = str(candidate.get("sensitivity", ""))
    confidence = float(candidate.get("confidence", 0.0))
    source_ref = str(candidate.get("source_ref", ""))
    prose_haystack = " ".join(
        [
            kind,
            sensitivity,
            str(candidate.get("title", "")),
            str(candidate.get("summary", "")),
            str(candidate.get("content", "")),
        ]
    ).lower()
    structured_markers = {
        str(value).strip().lower()
        for value in [
            *candidate.get("tags", []),
            *candidate.get("capture_reasons", []),
        ]
    }
    if kind == "boundary" or sensitivity == "boundary":
        return "boundary candidates require owner-controlled review"
    if any(term in prose_haystack for term in BOUNDARY_WEAKENING_TERMS):
        return "boundary weakening/deletion or autonomy lowering is owner-controlled"
    if structured_markers & STRUCTURED_CONFLICT_MARKERS:
        return "active contradiction marker present"
    if kind in PROTECTED_KINDS:
        if confidence < 0.8:
            return "protected identity/relationship candidate confidence is below 0.8"
        if not source_ref:
            return "protected identity/relationship candidate lacks provenance"
    return ""
