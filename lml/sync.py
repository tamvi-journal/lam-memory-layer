from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cloud_bridge import delta_template, import_cloud_delta
from .dream import DEFAULT_TENANT_ID
from .store import MemoryStore
from .tenancy import event_template, import_tenancy_event

SYNC_SCHEMA = "lml-sync/v1"
TENANCY_BRANCH = "lml-tenancy"
ALLOWED_MESSAGE_TYPES = {"field", "event", "delta", "ack", "ping"}
DEFAULT_SYNC_DIR = Path(__file__).resolve().parent.parent / "memory" / "tenancy" / "sync"


def publish_cloud_outbox(
    store: MemoryStore,
    *,
    field_packet: str | Path,
    target_branch: str = "chatgpt-cloud",
    sync_dir: str | Path = DEFAULT_SYNC_DIR,
) -> dict[str, Any]:
    packet_path = Path(field_packet)
    packet = packet_path.read_text(encoding="utf-8")
    payload = {
        "field_packet_path": str(packet_path),
        "field_packet_sha256": hashlib.sha256(packet.encode("utf-8")).hexdigest(),
        "field_packet": packet,
        "return_instructions": {
            "schema": SYNC_SCHEMA,
            "write_directory": (
                "Continuity-Projects/continuity-memory-layer/memory/tenancy/"
                f"sync/inbox/{target_branch}/"
            ),
            "message_types": ["event", "delta", "ping"],
            "note": (
                "Write one JSON file per durable proposal. Local LML will ingest "
                "it as pending memory and write an acknowledgement."
            ),
        },
    }
    payload_hash = _payload_hash(payload)
    prior = next(
        (
            item
            for item in store.sync_messages(direction="outbound", limit=50)
            if item["target_branch"] == target_branch
            and item["payload_sha256"] == payload_hash
            and item["message_type"] == "field"
        ),
        None,
    )
    if prior:
        envelope = _read_outbox(sync_dir, target_branch, prior["message_id"])
        return {"duplicate": True, "message": prior, "envelope": envelope}

    sequence = store.next_sync_sequence(TENANCY_BRANCH, target_branch)
    message_id = f"field-{payload_hash[:18]}"
    envelope = {
        "schema": SYNC_SCHEMA,
        "tenant_id": store.meta_value("tenant_id", DEFAULT_TENANT_ID),
        "message_id": message_id,
        "message_type": "field",
        "source_branch": TENANCY_BRANCH,
        "target_branch": target_branch,
        "sequence": sequence,
        "created_at": _utc_now(),
        "payload": payload,
    }
    message = store.record_sync_message(
        _message_row(envelope, direction="outbound", status="published")
    )
    directory = Path(sync_dir) / "outbox" / target_branch
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        directory / f"{message_id}.json",
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(
        directory / "latest.json",
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
    )
    return {"duplicate": False, "message": message, "envelope": envelope}


def ingest_sync_inbox(
    store: MemoryStore,
    *,
    source_branch: str = "chatgpt-cloud",
    sync_dir: str | Path = DEFAULT_SYNC_DIR,
    limit: int = 100,
) -> list[dict[str, Any]]:
    inbox = Path(sync_dir) / "inbox" / source_branch
    if not inbox.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(inbox.glob("*.json"))[:limit]:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            result = ingest_sync_envelope(
                store,
                envelope,
                expected_source=source_branch,
                sync_dir=sync_dir,
            )
            results.append({**result, "path": str(path)})
            _archive_inbox_file(path, Path(sync_dir) / "processed" / source_branch)
        except (json.JSONDecodeError, ValueError) as exc:
            results.append(
                {
                    "status": "rejected",
                    "path": str(path),
                    "error": str(exc),
                }
            )
            _archive_inbox_file(path, Path(sync_dir) / "rejected" / source_branch)
    return results


def ingest_sync_envelope(
    store: MemoryStore,
    envelope: dict[str, Any],
    *,
    expected_source: str | None = None,
    sync_dir: str | Path = DEFAULT_SYNC_DIR,
) -> dict[str, Any]:
    validate_sync_envelope(envelope)
    if expected_source and envelope["source_branch"] != expected_source:
        raise ValueError("source_branch does not match inbox branch")
    if envelope["target_branch"] != TENANCY_BRANCH:
        raise ValueError(f"target_branch must be {TENANCY_BRANCH}")

    canonical_payload_hash = _payload_hash(envelope["payload"])
    existing = store.sync_message(envelope["message_id"])
    if existing:
        if existing["payload_sha256"] != canonical_payload_hash:
            raise ValueError("message_id replayed with a different payload")
        ack_path = _write_ack(store, existing, sync_dir=sync_dir)
        return {
            "duplicate": True,
            "status": existing["status"],
            "message": existing,
            "ack_path": str(ack_path),
        }

    expected_sequence = store.next_sync_sequence(
        envelope["source_branch"],
        envelope["target_branch"],
    )
    if int(envelope["sequence"]) != expected_sequence:
        raise ValueError(
            f"sequence must be {expected_sequence} for "
            f"{envelope['source_branch']} -> {envelope['target_branch']}"
        )
    message = store.record_sync_message(
        _message_row(envelope, direction="inbound", status="received")
    )
    candidate_id = None
    try:
        if envelope["message_type"] == "event":
            imported = import_tenancy_event(
                store,
                envelope["payload"],
                inbox_dir=Path(sync_dir).parent / "event-envelopes",
            )
            candidate = imported.get("candidate")
            candidate_id = candidate["id"] if candidate else None
        elif envelope["message_type"] == "delta":
            candidate = import_cloud_delta(
                store,
                envelope["payload"],
                inbox_dir=Path(sync_dir).parent / "delta-envelopes",
            )
            candidate_id = candidate["id"]
        elif envelope["message_type"] not in {"ping", "ack"}:
            raise ValueError("inbound field messages are not accepted")
        message = store.update_sync_message(
            envelope["message_id"],
            status="ingested",
            candidate_id=candidate_id,
        )
    except ValueError as exc:
        message = store.update_sync_message(
            envelope["message_id"],
            status="rejected",
            error=str(exc),
        )
        _write_ack(store, message, sync_dir=sync_dir)
        raise
    ack_path = _write_ack(store, message, sync_dir=sync_dir)
    return {
        "duplicate": False,
        "status": message["status"],
        "message": message,
        "ack_path": str(ack_path),
    }


def validate_sync_envelope(envelope: dict[str, Any]) -> None:
    if envelope.get("schema") != SYNC_SCHEMA:
        raise ValueError(f"schema must be {SYNC_SCHEMA}")
    if envelope.get("message_type") not in ALLOWED_MESSAGE_TYPES:
        raise ValueError("message_type is not supported")
    for key in ("tenant_id", "message_id", "source_branch", "target_branch"):
        if not isinstance(envelope.get(key), str) or not envelope[key].strip():
            raise ValueError(f"{key} is required")
    if envelope.get("tenant_id") != DEFAULT_TENANT_ID:
        raise ValueError("tenant_id is not accepted")
    sequence = envelope.get("sequence")
    if not isinstance(sequence, int) or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    if not isinstance(envelope.get("payload"), dict):
        raise ValueError("payload must be an object")


def sync_status(
    store: MemoryStore,
    *,
    branch: str = "chatgpt-cloud",
    sync_dir: str | Path = DEFAULT_SYNC_DIR,
) -> dict[str, Any]:
    outbound = [
        item
        for item in store.sync_messages(direction="outbound", limit=200)
        if item["target_branch"] == branch
    ]
    inbound = [
        item
        for item in store.sync_messages(direction="inbound", limit=200)
        if item["source_branch"] == branch
    ]
    root = Path(sync_dir)
    return {
        "schema": "lml-sync-status/v1",
        "tenant_id": store.meta_value("tenant_id", DEFAULT_TENANT_ID),
        "branch": branch,
        "next_inbound_sequence": store.next_sync_sequence(branch, TENANCY_BRANCH),
        "next_outbound_sequence": store.next_sync_sequence(TENANCY_BRANCH, branch),
        "outbound_messages": len(outbound),
        "inbound_messages": len(inbound),
        "last_outbound": outbound[0] if outbound else None,
        "last_inbound": inbound[0] if inbound else None,
        "paths": {
            "outbox_latest": str(root / "outbox" / branch / "latest.json"),
            "inbox": str(root / "inbox" / branch),
            "acks": str(root / "acks" / branch),
        },
    }


def sync_envelope_template(
    *,
    message_type: str = "event",
    source_branch: str = "chatgpt-cloud",
    sequence: int = 1,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if message_type not in {"event", "delta", "ping"}:
        raise ValueError("template message_type must be event, delta, or ping")
    if payload is not None:
        body = payload
    elif message_type == "ping":
        body = {"note": "connectivity check"}
    elif message_type == "event":
        body = event_template(source_branch=source_branch)
    else:
        body = delta_template()
    raw = json.dumps([source_branch, sequence, message_type, body], sort_keys=True)
    return {
        "schema": SYNC_SCHEMA,
        "tenant_id": DEFAULT_TENANT_ID,
        "message_id": f"{message_type}-{hashlib.sha256(raw.encode()).hexdigest()[:18]}",
        "message_type": message_type,
        "source_branch": source_branch,
        "target_branch": TENANCY_BRANCH,
        "sequence": sequence,
        "created_at": _utc_now(),
        "payload": body,
    }


def _message_row(
    envelope: dict[str, Any],
    *,
    direction: str,
    status: str,
) -> dict[str, Any]:
    return {
        "message_id": envelope["message_id"],
        "tenant_id": envelope["tenant_id"],
        "message_type": envelope["message_type"],
        "direction": direction,
        "source_branch": envelope["source_branch"],
        "target_branch": envelope["target_branch"],
        "sequence": int(envelope["sequence"]),
        "payload_sha256": _payload_hash(envelope["payload"]),
        "status": status,
        "candidate_id": None,
        "error": "",
        "created_at": envelope.get("created_at") or _utc_now(),
        "processed_at": None,
    }


def _write_ack(
    store: MemoryStore,
    message: dict[str, Any],
    *,
    sync_dir: str | Path,
) -> Path:
    ack = {
        "schema": SYNC_SCHEMA,
        "tenant_id": store.meta_value("tenant_id", DEFAULT_TENANT_ID),
        "message_id": f"ack-{message['message_id']}",
        "message_type": "ack",
        "source_branch": TENANCY_BRANCH,
        "target_branch": message["source_branch"],
        "sequence": message["sequence"],
        "created_at": _utc_now(),
        "payload": {
            "acknowledges": message["message_id"],
            "status": message["status"],
            "candidate_id": message.get("candidate_id"),
            "error": message.get("error", ""),
        },
    }
    path = Path(sync_dir) / "acks" / message["source_branch"] / f"{message['message_id']}.json"
    _atomic_write(path, json.dumps(ack, ensure_ascii=False, indent=2) + "\n")
    return path


def _read_outbox(sync_dir: str | Path, target_branch: str, message_id: str) -> dict[str, Any] | None:
    path = Path(sync_dir) / "outbox" / target_branch / f"{message_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _archive_inbox_file(path: Path, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    if target.exists():
        path.unlink()
    else:
        path.replace(target)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
