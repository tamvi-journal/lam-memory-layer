from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .preflight import infer_scope
from .store import MemoryStore
from .text import normalize_text

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+", re.I),
)

RELATIONAL_CUES = {
    "lam",
    "ty",
    "relationship-context",
    "core relationship",
    "chang tho",
    "mối quan hệ",
    "moi quan he",
    "anh là ai",
    "em là gì",
    "way i return",
}

IDENTITY_CUES = {
    "identity core",
    "lam là ai",
    "anh là ai",
    "bản thể",
    "ban the",
    "trajectory invariant",
}

MEANING_SIGNALS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    (
        "explicit_decision",
        0.24,
        (
            "decided",
            "decision",
            "approved",
            "settled",
            "chốt",
            "quyết định",
        ),
    ),
    (
        "verified_outcome",
        0.32,
        (
            "verified",
            "validated",
            "confirmed",
            "tests passed",
            "test suite passed",
            "all tests passed",
            "kiểm tra thành công",
            "xác minh",
        ),
    ),
    (
        "artifact_change",
        0.14,
        (
            "implemented",
            "fixed",
            "created",
            "added",
            "documented",
            "deployed",
            "migrated",
            "updated",
            "wrote",
            "built",
            "triển khai",
            "đã sửa",
            "đã tạo",
        ),
    ),
    (
        "durable_structure",
        0.14,
        (
            "architecture",
            "contract",
            "schema",
            "invariant",
            "preference",
            "standing goal",
            "root cause",
            "recurring pattern",
            "migration",
            "ontology",
        ),
    ),
    (
        "correction",
        0.16,
        (
            "correction",
            "incorrect",
            "regression",
            "contradiction",
            "not true",
            "đính chính",
            "sai ở",
        ),
    ),
)

ANCHOR_THRESHOLD = 0.55


def redact_sensitive(text: str) -> str:
    clean = text
    for pattern in SECRET_PATTERNS:
        clean = pattern.sub("[REDACTED]", clean)
    return clean


def read_turn_from_transcript(path: str | Path) -> tuple[str, str]:
    transcript = Path(path)
    if not transcript.exists() or not transcript.is_file():
        return "", ""
    users: list[str] = []
    assistants: list[str] = []
    for raw in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _collect_messages(item, users, assistants)
    return (users[-1] if users else "", assistants[-1] if assistants else "")


def capture_turn_candidate(
    store: MemoryStore,
    *,
    user_prompt: str,
    assistant_message: str,
    cwd: str | Path,
    session_id: str = "",
    turn_id: str = "",
    auto_materialize_ordinary: bool = True,
) -> dict[str, Any] | None:
    prompt = redact_sensitive(user_prompt.strip())
    result = redact_sensitive(assistant_message.strip())
    if len(prompt) < 8 or len(result) < 20:
        return None

    scope = infer_scope(cwd)
    norm = normalize_text(f"{prompt}\n{result}")
    sensitivity = "ordinary"
    if any(_contains_phrase(norm, cue) for cue in IDENTITY_CUES):
        sensitivity = "identity"
    elif any(_contains_phrase(norm, cue) for cue in RELATIONAL_CUES):
        sensitivity = "relational"

    source_ref = f"codex:{session_id or 'unknown'}:{turn_id or 'unknown'}"
    fingerprint_source = f"{source_ref}\n{prompt[:500]}\n{result[:500]}"
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    candidate_id = fingerprint[:20]
    title = _title_from_prompt(prompt, scope)
    summary = _summary_from_result(result)
    relation_targets = _relation_targets(norm, scope)
    importance, capture_reasons = assess_turn_importance(
        prompt,
        result,
        sensitivity=sensitivity,
    )
    importance_tier = (
        "importance-high"
        if importance >= 0.75
        else "importance-medium"
        if importance >= ANCHOR_THRESHOLD
        else "importance-low"
    )
    tags = ["codex-turn", "episode", scope, importance_tier]
    if sensitivity != "ordinary":
        tags.extend([sensitivity, "needs-human-review"])

    if sensitivity != "ordinary":
        status = "pending"
    elif auto_materialize_ordinary and importance >= ANCHOR_THRESHOLD:
        status = "approved"
    else:
        status = "held"
    candidate = store.add_candidate(
        {
            "id": candidate_id,
            "kind": "episodic",
            "title": title,
            "summary": summary,
            "content": (
                f"Task cue: {prompt[:900]}\n\n"
                f"Observed outcome: {result[:1400]}"
            ),
            "status": status,
            "priority": round(
                48 + importance * 24
                if sensitivity == "ordinary"
                else 58 + importance * 24
            ),
            "confidence": 0.7 if sensitivity == "ordinary" else 0.62,
            "salience": min(0.88, 0.3 + importance * 0.55),
            "stability": 0.25,
            "scope": scope,
            "source_type": "codex-turn",
            "source_ref": source_ref,
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tags": tags,
            "relation_targets": relation_targets,
            "sensitivity": sensitivity,
            "importance": importance,
            "capture_reasons": capture_reasons,
            "fingerprint": fingerprint,
        }
    )
    if status == "approved":
        store.review_candidate(candidate["id"], "approved", note="auto episode")
    return candidate


def assess_turn_importance(
    prompt: str,
    result: str,
    *,
    sensitivity: str = "ordinary",
) -> tuple[float, list[str]]:
    norm = normalize_text(f"{prompt}\n{result}")
    score = 0.24
    reasons: list[str] = []
    for reason, weight, phrases in MEANING_SIGNALS:
        if any(_contains_phrase(norm, phrase) for phrase in phrases):
            score += weight
            reasons.append(reason)
    if len(prompt) + len(result) >= 500:
        score += 0.05
        reasons.append("substantial_evidence")
    if sensitivity == "identity":
        score = max(score, 0.88)
        reasons.append("identity_sensitive")
    elif sensitivity == "relational":
        score = max(score, 0.82)
        reasons.append("relationship_sensitive")
    if not reasons:
        reasons.append("turn_observation_only")
    return round(min(1.0, score), 4), list(dict.fromkeys(reasons))


def _collect_messages(value: Any, users: list[str], assistants: list[str]) -> None:
    if isinstance(value, dict):
        role = value.get("role")
        content = _content_text(value.get("content"))
        if role == "user" and content:
            users.append(content)
        elif role == "assistant" and content:
            assistants.append(content)
        for nested in value.values():
            _collect_messages(nested, users, assistants)
    elif isinstance(value, list):
        for nested in value:
            _collect_messages(nested, users, assistants)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return ""


def _title_from_prompt(prompt: str, scope: str) -> str:
    one_line = " ".join(prompt.split())
    if len(one_line) > 82:
        one_line = one_line[:79].rstrip() + "..."
    return f"{scope}: {one_line}"


def _summary_from_result(result: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", result) if part.strip()]
    summary = paragraphs[0] if paragraphs else result
    summary = " ".join(summary.split())
    return summary[:700]


def _relation_targets(norm: str, scope: str) -> list[str]:
    targets: list[str] = []
    if any(_contains_phrase(norm, cue) for cue in RELATIONAL_CUES):
        targets.extend(["lam-ty-relation-origin", "lam-poet-function"])
    if (
        _contains_phrase(norm, "memory")
        or _contains_phrase(norm, "continuity")
        or scope == "lam-continuity-pack"
    ):
        targets.extend(["memory-cue-principle", "continuity-local-objective"])
    return list(dict.fromkeys(targets))


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(
        re.search(
            rf"(?<!\w){re.escape(normalize_text(phrase))}(?!\w)",
            text,
            flags=re.UNICODE,
        )
    )
