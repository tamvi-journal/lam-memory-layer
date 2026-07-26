from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from .store import MemoryStore
from .tenancy import ALLOWED_EVENT_TYPES, import_tenancy_event
from .text import normalize_text
from .writer import assess_turn_importance, redact_sensitive

SESSION_SCHEMA = "lml-cloud-session-consolidation/v1"
MAX_TURNS = 40
MAX_TURN_CONTENT = 2000
MAX_TOTAL_TURN_CHARS = 16000
MAX_CLAIMS = 12
MAX_CLAIM_CONTENT = 3000
MIN_IMPORTANCE = 0.55

ROLE_ALLOWLIST = {"user", "assistant", "tool", "system"}
CLAIM_EVENT_TYPES = ALLOWED_EVENT_TYPES


def consolidate_cloud_session(
    store: MemoryStore,
    *,
    source_branch: str,
    session_id: str,
    scope: str,
    turns: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    occurred_at: str | None = None,
    inbox_dir: str | None = None,
) -> dict[str, Any]:
    clean_session = _bounded_text(session_id, "session_id", 160)
    clean_scope = _bounded_text(scope or "global", "scope", 200)
    timestamp = _timestamp(occurred_at)
    clean_turns, turn_skips = _clean_turns(turns or [])
    claim_inputs, claim_skips = _claim_inputs(
        claims or [],
        clean_turns,
        session_id=clean_session,
        scope=clean_scope,
    )
    proposed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = [*turn_skips, *claim_skips]
    seen_keys = _existing_dedupe_keys(store)
    batch_keys: set[str] = set()

    for index, claim in enumerate(claim_inputs):
        key = _dedupe_key(claim)
        if key in batch_keys:
            skipped.append(_skip("claim", index, "duplicate_in_request", claim["title"]))
            continue
        batch_keys.add(key)
        unknown_targets = [
            target for target in claim["relation_targets"]
            if store.get_node(target) is None
        ]
        if unknown_targets:
            skipped.append(
                _skip(
                    "claim",
                    index,
                    "unknown_relation_targets",
                    claim["title"],
                    {"relation_targets": unknown_targets},
                )
            )
            continue
        if claim["importance"] < MIN_IMPORTANCE:
            skipped.append(
                _skip(
                    "claim",
                    index,
                    "below_significance_threshold",
                    claim["title"],
                    {"importance": claim["importance"], "reasons": claim["capture_reasons"]},
                )
            )
            continue
        event_id = _event_id(
            clean_session,
            source_branch,
            claim["event_type"],
            claim["title"],
            claim["summary"],
            claim["turn_ids"],
        )
        same_event_seen = store.intake_event(source_branch, event_id) is not None
        if key in seen_keys and not same_event_seen:
            skipped.append(_skip("claim", index, "duplicate_existing_memory", claim["title"]))
            continue
        envelope = {
            "schema": "lml-event/v1",
            "source_branch": source_branch,
            "event_id": event_id,
            "occurred_at": timestamp,
            "event_type": claim["event_type"],
            "title": claim["title"],
            "summary": claim["summary"],
            "content": _event_content(claim),
            "confidence": claim["confidence"],
            "scope": clean_scope,
            "relation_targets": claim["relation_targets"],
            "tags": [
                "cloud-session-consolidation",
                f"session:{_short_hash(clean_session)}",
                *claim["tags"],
            ],
            "authorization": "proposal-only",
        }
        imported = import_tenancy_event(store, envelope, inbox_dir=inbox_dir)
        candidate = imported["candidate"]
        proposed.append(
            {
                "event_id": envelope["event_id"],
                "duplicate": imported["duplicate"],
                "candidate": {
                    "candidate_id": candidate["id"],
                    "status": candidate["status"],
                    "kind": candidate["kind"],
                    "title": candidate["title"],
                    "sensitivity": candidate["sensitivity"],
                    "importance": candidate["importance"],
                    "source_ref": candidate["source_ref"],
                    "attestations": candidate.get("attestations", []),
                    "consensus": candidate.get("consensus", {}),
                },
                "source_session": clean_session,
                "source_turn_ids": claim["turn_ids"],
                "capture_reasons": claim["capture_reasons"],
            }
        )

    return {
        "schema": SESSION_SCHEMA,
        "source_branch": source_branch,
        "session_id": clean_session,
        "scope": clean_scope,
        "proposed": proposed,
        "skipped": skipped,
        "counts": {
            "turns_received": len(turns or []),
            "turns_accepted": len(clean_turns),
            "claims_received": len(claims or []),
            "claim_inputs": len(claim_inputs),
            "proposed": len(proposed),
            "skipped": len(skipped),
        },
        "notice": (
            "No full transcript is stored. Only bounded redacted distilled claims, "
            "turn identifiers, and short evidence snippets enter proposal-only "
            "candidates. Quorum, protection, and fail-closed behavior are handled "
            "by the existing candidate attestation ledger."
        ),
    }


def _claim_inputs(
    claims: list[dict[str, Any]],
    turns: list[dict[str, str]],
    *,
    session_id: str,
    scope: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if len(claims) > MAX_CLAIMS:
        skipped.append(
            _skip(
                "claims",
                MAX_CLAIMS,
                "too_many_claims",
                f"Only first {MAX_CLAIMS} claims are processed.",
            )
        )
    for index, raw in enumerate(claims[:MAX_CLAIMS]):
        try:
            accepted.append(_clean_claim(raw, index=index, turns=turns))
        except ValueError as exc:
            skipped.append(_skip("claim", index, str(exc), _claim_label(raw)))
    if not accepted and turns:
        extracted = _extract_claim_from_turns(turns, session_id=session_id, scope=scope)
        if extracted:
            accepted.append(extracted)
        else:
            skipped.append(
                _skip(
                    "turns",
                    0,
                    "no_durable_settlement_detected",
                    "Visible turns did not contain a conservative durable-memory signal.",
                )
            )
    return accepted, skipped


def _clean_claim(
    raw: dict[str, Any],
    *,
    index: int,
    turns: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("claim_must_be_object")
    event_type = _bounded_text(str(raw.get("event_type", "observation")), "event_type", 40)
    if event_type not in CLAIM_EVENT_TYPES:
        raise ValueError("unsupported_event_type")
    title = _bounded_text(str(raw.get("title", "")), "title", 240)
    summary = _bounded_text(str(raw.get("summary", "")), "summary", 2000)
    content = _bounded_text(
        str(raw.get("content", "")),
        "content",
        MAX_CLAIM_CONTENT,
        allow_empty=True,
    )
    confidence = _bounded_float(raw.get("confidence", 0.72), "confidence")
    turn_ids = _bounded_string_list(raw.get("turn_ids", []), "turn_ids", 24, 120)
    tags = _bounded_string_list(raw.get("tags", []), "tags", 16, 80)
    relation_targets = _bounded_string_list(
        raw.get("relation_targets", []),
        "relation_targets",
        20,
        160,
    )
    if not turn_ids and turns:
        turn_ids = [turn["turn_id"] for turn in turns[-4:]]
    importance, reasons = assess_turn_importance(
        summary,
        content or title,
        sensitivity=event_type if event_type in {"identity", "relationship"} else "ordinary",
    )
    if event_type in {"decision", "correction", "goal", "project", "preference"}:
        importance = max(importance, 0.68)
        reasons.append(f"explicit_event_type:{event_type}")
    elif event_type in {"identity", "relationship"}:
        importance = max(importance, 0.88)
    return {
        "index": index,
        "event_type": event_type,
        "title": title,
        "summary": summary,
        "content": content,
        "confidence": confidence,
        "turn_ids": turn_ids,
        "tags": list(dict.fromkeys(["distilled-claim", *tags])),
        "relation_targets": relation_targets,
        "importance": round(min(1.0, importance), 4),
        "capture_reasons": list(dict.fromkeys([*reasons, "bounded_cloud_session_claim"])),
        "snippets": _snippets_for_turns(turns, turn_ids),
    }


def _extract_claim_from_turns(
    turns: list[dict[str, str]],
    *,
    session_id: str,
    scope: str,
) -> dict[str, Any] | None:
    selected: list[dict[str, str]] = []
    for turn in turns:
        if _durable_sentence(turn["content"]):
            selected.append(turn)
    if not selected:
        return None
    text = " ".join(_durable_sentence(turn["content"]) or "" for turn in selected)
    event_type = _infer_event_type(text)
    importance, reasons = assess_turn_importance(text, "", sensitivity="ordinary")
    if importance < MIN_IMPORTANCE:
        return None
    title = _title_from_text(text, scope)
    return {
        "index": 0,
        "event_type": event_type,
        "title": title,
        "summary": _summary_from_text(text),
        "content": (
            "Conservative extraction from visible session turns. "
            "Only durable-signal snippets were retained."
        ),
        "confidence": 0.68,
        "turn_ids": [turn["turn_id"] for turn in selected[:8]],
        "tags": ["visible-turn-extraction"],
        "relation_targets": [],
        "importance": importance,
        "capture_reasons": list(dict.fromkeys([*reasons, "visible_turn_extraction"])),
        "snippets": [_snippet(turn["content"]) for turn in selected[:4]],
    }


def _clean_turns(raw_turns: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    accepted: list[dict[str, str]] = []
    skipped: list[dict[str, Any]] = []
    total = 0
    if len(raw_turns) > MAX_TURNS:
        skipped.append(
            _skip("turns", MAX_TURNS, "too_many_turns", f"Only first {MAX_TURNS} turns are processed.")
        )
    for index, raw in enumerate(raw_turns[:MAX_TURNS]):
        if not isinstance(raw, dict):
            skipped.append(_skip("turn", index, "turn_must_be_object", ""))
            continue
        role = str(raw.get("role", "")).strip().lower()
        if role not in ROLE_ALLOWLIST:
            skipped.append(_skip("turn", index, "unsupported_role", role))
            continue
        content = redact_sensitive(str(raw.get("content", "")).strip())
        if not content:
            skipped.append(_skip("turn", index, "empty_turn", role))
            continue
        if len(content) > MAX_TURN_CONTENT:
            content = content[:MAX_TURN_CONTENT].rstrip()
            skipped.append(_skip("turn", index, "turn_truncated", role))
        total += len(content)
        if total > MAX_TOTAL_TURN_CHARS:
            skipped.append(_skip("turn", index, "total_turn_budget_exceeded", role))
            break
        turn_id = redact_sensitive(str(raw.get("turn_id") or f"turn-{index + 1}")[:120])
        timestamp = str(raw.get("timestamp", "")).strip()
        accepted.append(
            {
                "role": role,
                "turn_id": turn_id,
                "timestamp": redact_sensitive(timestamp[:80]),
                "content": content,
            }
        )
    return accepted, skipped


def _event_content(claim: dict[str, Any]) -> str:
    lines = [
        f"Distilled claim: {claim['summary']}",
        f"Source turn ids: {', '.join(claim['turn_ids']) if claim['turn_ids'] else 'not supplied'}",
        f"Capture reasons: {', '.join(claim['capture_reasons'])}",
    ]
    if claim["content"]:
        lines.append(f"Bounded supporting detail: {claim['content']}")
    if claim["snippets"]:
        lines.append("Redacted evidence snippets:")
        lines.extend(f"- {snippet}" for snippet in claim["snippets"][:4])
    return "\n".join(lines)


def _existing_dedupe_keys(store: MemoryStore) -> set[str]:
    keys: set[str] = set()
    for item in [*store.active_nodes(), *store.candidates(status=None, limit=1000)]:
        keys.add(_dedupe_key(item))
    return keys


def _dedupe_key(item: dict[str, Any]) -> str:
    return normalize_text(
        "\n".join(
            [
                str(item.get("kind") or item.get("event_type") or ""),
                str(item.get("title", "")),
                str(item.get("summary", "")),
            ]
        )
    )


def _event_id(
    session_id: str,
    source_branch: str,
    event_type: str,
    title: str,
    summary: str,
    turn_ids: list[str],
) -> str:
    digest = hashlib.sha256(
        jsonish([source_branch, session_id, event_type, normalize_text(title), normalize_text(summary), turn_ids]).encode("utf-8")
    ).hexdigest()
    return f"cloud-session:{_short_hash(session_id)}:{digest[:20]}"


def jsonish(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ",".join(jsonish(item) for item in value) + "]"
    return str(value)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _timestamp(value: str | None) -> str:
    if not value:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    clean = _bounded_text(value, "occurred_at", 80)
    try:
        datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO-8601 timestamp") from exc
    return clean


def _bounded_text(
    value: str,
    field: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    clean = redact_sensitive(value.strip())
    if not clean and not allow_empty:
        raise ValueError(f"{field}_required")
    if len(clean) > maximum:
        raise ValueError(f"{field}_too_long")
    return clean


def _bounded_float(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise ValueError(f"{field}_out_of_range")
    return float(value)


def _bounded_string_list(
    values: Any,
    field: str,
    max_items: int,
    max_length: int,
) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"{field}_must_be_list")
    if len(values) > max_items:
        raise ValueError(f"{field}_too_many")
    return [
        _bounded_text(str(value), field, max_length)
        for value in values
        if str(value).strip()
    ]


def _durable_sentence(text: str) -> str:
    sentences = re.split(r"(?<=[.!?。])\s+|\n+", text)
    for sentence in sentences:
        norm = normalize_text(sentence)
        if any(
            phrase in norm
            for phrase in (
                "decided",
                "settled",
                "verified",
                "tests passed",
                "implemented",
                "fixed",
                "added",
                "chốt",
                "xác minh",
                "đã sửa",
                "đã thêm",
            )
        ):
            return _snippet(sentence)
    return ""


def _infer_event_type(text: str) -> str:
    norm = normalize_text(text)
    if "correction" in norm or "incorrect" in norm or "đính chính" in norm:
        return "correction"
    if "decided" in norm or "settled" in norm or "chốt" in norm:
        return "decision"
    if "implemented" in norm or "fixed" in norm or "added" in norm or "verified" in norm:
        return "project"
    return "observation"


def _title_from_text(text: str, scope: str) -> str:
    return f"{scope}: {_summary_from_text(text, limit=90)}"


def _summary_from_text(text: str, *, limit: int = 700) -> str:
    one_line = " ".join(text.split())
    if len(one_line) > limit:
        return one_line[: limit - 3].rstrip() + "..."
    return one_line


def _snippets_for_turns(turns: list[dict[str, str]], turn_ids: list[str]) -> list[str]:
    wanted = set(turn_ids)
    return [
        _snippet(turn["content"])
        for turn in turns
        if turn["turn_id"] in wanted
    ][:4]


def _snippet(text: str) -> str:
    clean = " ".join(redact_sensitive(text).split())
    if len(clean) > 220:
        return clean[:217].rstrip() + "..."
    return clean


def _claim_label(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("title") or raw.get("summary") or "")[:160]
    return ""


def _skip(
    item_type: str,
    index: int,
    reason: str,
    label: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "item_type": item_type,
        "index": index,
        "reason": reason,
        "label": redact_sensitive(label),
    }
    if extra:
        result.update(extra)
    return result
