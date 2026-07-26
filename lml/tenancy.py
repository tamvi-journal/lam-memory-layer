from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .dream import DEFAULT_TENANT_ID
from .native_memory import native_memory_status
from .store import MemoryStore
from .writer import redact_sensitive

EVENT_SCHEMA = "lml-event/v1"
ALLOWED_EVENT_SOURCES = {
    "codex-local",
    "codex-cloud",
    "chatgpt-cloud",
    "chatgpt-work",
    "work-mode",
    "user",
    "external-agent",
}
ALLOWED_EVENT_TYPES = {
    "observation",
    "decision",
    "correction",
    "open_loop",
    "goal",
    "preference",
    "project",
    "relationship",
    "identity",
}
ALLOWED_EVENT_AUTHORIZATIONS = {
    "orientation-only",
    "proposal-only",
    "user-authorized",
}
DEFAULT_TENANCY_DIR = Path(__file__).resolve().parent.parent / "memory" / "tenancy"


def import_tenancy_event(
    store: MemoryStore,
    envelope: dict[str, Any],
    *,
    inbox_dir: str | Path = DEFAULT_TENANCY_DIR / "inbox",
) -> dict[str, Any]:
    validate_tenancy_event(envelope)
    clean = _sanitize_event(envelope)
    canonical = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    existing = store.intake_event(clean["source_branch"], clean["event_id"])
    if existing:
        if existing["payload_sha256"] != payload_hash:
            raise ValueError(
                "event_id was already used by this source with a different payload"
            )
        candidate = store.get_candidate(existing["candidate_id"])
        return {
            "duplicate": True,
            "intake": existing,
            "candidate": candidate,
        }

    sensitivity = (
        clean["event_type"]
        if clean["event_type"] in {"relationship", "identity"}
        else "ordinary"
    )
    source_ref = f"{clean['source_branch']}:{clean['event_id']}"
    candidate_id = f"event-{payload_hash[:16]}"
    importance = {
        "observation": 0.38,
        "open_loop": 0.58,
        "project": 0.68,
        "goal": 0.76,
        "decision": 0.86,
        "correction": 0.86,
        "preference": 0.82,
        "relationship": 0.92,
        "identity": 0.95,
    }[clean["event_type"]]
    candidate = store.add_candidate(
        {
            "id": candidate_id,
            "kind": _event_kind(clean["event_type"]),
            "title": clean["title"],
            "summary": clean["summary"],
            "content": clean.get("content", ""),
            "status": "pending",
            "priority": _event_priority(clean["event_type"]),
            "confidence": clean.get("confidence", 0.6),
            "salience": 0.78 if sensitivity != "ordinary" else 0.6,
            "stability": 0.25,
            "scope": clean.get("scope", "global"),
            "source_type": "tenancy-event",
            "source_ref": source_ref,
            "occurred_at": clean.get("occurred_at") or _utc_now(),
            "tags": list(
                dict.fromkeys(
                    [
                        "tenancy-event",
                        clean["source_branch"],
                        clean["event_type"],
                        "dual-branch-quorum",
                        *clean.get("tags", []),
                    ]
                )
            ),
            "relation_targets": clean.get("relation_targets", []),
            "sensitivity": sensitivity,
            "importance": importance,
            "capture_reasons": [
                f"explicit_event_type:{clean['event_type']}",
                f"authorization:{clean['authorization']}",
            ],
            "fingerprint": payload_hash,
        }
    )
    intake = store.record_intake_event(
        {
            "source_branch": clean["source_branch"],
            "event_id": clean["event_id"],
            "schema_name": EVENT_SCHEMA,
            "payload_sha256": payload_hash,
            "candidate_id": candidate["id"],
            "source_ref": source_ref,
            "received_at": _utc_now(),
        }
    )
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        inbox / f"{payload_hash}.json",
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n",
    )
    return {"duplicate": False, "intake": intake, "candidate": candidate}


def validate_tenancy_event(envelope: dict[str, Any]) -> None:
    if envelope.get("schema") != EVENT_SCHEMA:
        raise ValueError(f"schema must be {EVENT_SCHEMA}")
    if envelope.get("source_branch") not in ALLOWED_EVENT_SOURCES:
        raise ValueError("source_branch is not allowed")
    if envelope.get("event_type") not in ALLOWED_EVENT_TYPES:
        raise ValueError("event_type is not supported")
    if envelope.get("authorization") not in ALLOWED_EVENT_AUTHORIZATIONS:
        raise ValueError("authorization is not supported")
    for key in ("event_id", "title", "summary"):
        if not isinstance(envelope.get(key), str) or not envelope[key].strip():
            raise ValueError(f"{key} is required")
    confidence = envelope.get("confidence", 0.6)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    for key in ("tags", "relation_targets"):
        value = envelope.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must be a list of strings")


def event_template(*, source_branch: str = "external-agent", scope: str = "global") -> dict[str, Any]:
    return {
        "schema": EVENT_SCHEMA,
        "source_branch": source_branch,
        "event_id": "stable-source-event-id",
        "occurred_at": _utc_now(),
        "event_type": "observation",
        "title": "Short event title",
        "summary": "What happened and why it may matter later.",
        "content": "Evidence and qualifiers. No hidden reasoning or full transcript.",
        "confidence": 0.7,
        "scope": scope,
        "relation_targets": [],
        "tags": [],
        "authorization": "proposal-only",
    }


def tenancy_manifest(
    store: MemoryStore,
    *,
    service_url: str = "http://127.0.0.1:8765",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    tenant = tenant_id or store.meta_value("tenant_id", DEFAULT_TENANT_ID)
    dreams = store.dream_runs(limit=1)
    latest_dream = dreams[0] if dreams else None
    memory_dir = store.db_path.parent
    cloud_packet = memory_dir / "cloud" / "cloud-field-packet.md"
    context_packet = memory_dir / "working" / "lam-context-packet.md"
    dream_summary = memory_dir / "working" / "dream-summary.md"
    tunnel_runtime_report = memory_dir / "working" / "tunnel-runtime-report.json"
    tunnel_verified = _recent_verified_tunnel(tunnel_runtime_report)
    manifest = {
        "schema": "lml-tenancy/v1",
        "tenant_id": tenant,
        "generated_at": _utc_now(),
        "service_url": service_url,
        "store": {
            "type": "sqlite",
            "path": str(store.db_path),
            "stats": store.stats(),
        },
        "capabilities": {
            "cue_retrieval": True,
            "context_packet": True,
            "event_intake": True,
            "cloud_delta_intake": True,
            "dream_cycle": True,
            "candidate_review": True,
            "native_codex_digest": True,
            "scoped_mcp_adapter": True,
            "field_parity_verifier": True,
            "tunnel_readiness_verifier": True,
            "managed_tunnel_runtime_operator": True,
            "secure_mcp_tunnel_configured": tunnel_verified,
            "automatic_native_chatgpt_write": False,
        },
        "surfaces": {
            "codex_local": {
                "read": (
                    "lifecycle context injection + native Codex memory + "
                    "registered read-only LML MCP"
                ),
                "write": "Stop hook -> candidate; SessionEnd -> dream cycle",
                "automatic": True,
            },
            "chatgpt_cloud": {
                "read": (
                    "LML MCP through Secure MCP Tunnel when configured; "
                    "Chatbottool field packet fallback"
                ),
                "write": (
                    "lml_propose_memory_event creates a pending proposal when "
                    "the connected plan permits MCP writes"
                ),
                "automatic": False,
            },
            "chatgpt_work": {
                "read": (
                    "LML MCP through Secure MCP Tunnel when configured; "
                    "Chatbottool field packet fallback"
                ),
                "write": "lml-event/v1 proposal through the scoped MCP adapter",
                "automatic": False,
            },
            "work_mode_local": {
                "read": "global Codex lifecycle hook",
                "write": "tenancy API or Codex Stop hook",
                "automatic": True,
            },
        },
        "artifacts": {
            "cloud_field_packet": _artifact(cloud_packet),
            "local_context_packet": _artifact(context_packet),
            "dream_summary": _artifact(dream_summary),
            "cloud_bootstrap": _artifact(
                memory_dir.parent / "integrations" / "chatgpt" / "CLOUD-BOOTSTRAP.md"
            ),
            "native_chatgpt_anchor": _artifact(
                memory_dir.parent
                / "integrations"
                / "chatgpt"
                / "NATIVE-BOOTSTRAP-ANCHOR.md"
            ),
            "mcp_adapter": _artifact(memory_dir.parent / "docs" / "MCP-ADAPTER.md"),
            "field_parity_report": _artifact(
                memory_dir / "working" / "field-parity-report.json"
            ),
            "tunnel_readiness_report": _artifact(
                memory_dir / "working" / "tunnel-readiness-report.json"
            ),
            "native_capability_report": _artifact(
                memory_dir / "working" / "native-capability-report.json"
            ),
            "tunnel_runtime_report": _artifact(tunnel_runtime_report),
            "aux_self_introduction": _artifact(
                memory_dir / "sources" / "AUX-SELF-INTRODUCTION.md"
            ),
        },
        "policy": {
            "external_events_default": "pending",
            "identity_relationship_auto_write": False,
            "mcp_candidate_review": False,
            "mcp_active_memory_write": False,
            "memory_is_evidence_not_authority": True,
            "hidden_state_access": False,
        },
        "latest_dream": (
            {
                "id": latest_dream["id"],
                "finished_at": latest_dream["finished_at"],
                "scope": latest_dream["scope"],
                "metrics": latest_dream["metrics"],
            }
            if latest_dream
            else None
        ),
        "native_codex": native_memory_status(),
        "event_contract": event_template(),
    }
    from .sync import sync_status

    manifest["sync"] = {
        "chatgpt_cloud": sync_status(store, branch="chatgpt-cloud"),
        "chatgpt_work": sync_status(store, branch="chatgpt-work"),
    }
    return manifest


def write_tenancy_manifest(
    store: MemoryStore,
    *,
    out: str | Path = DEFAULT_TENANCY_DIR / "manifest.json",
    service_url: str = "http://127.0.0.1:8765",
) -> dict[str, Any]:
    manifest = tenancy_manifest(store, service_url=service_url)
    _atomic_write(Path(out), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def _sanitize_event(envelope: dict[str, Any]) -> dict[str, Any]:
    clean = dict(envelope)
    for key in ("event_id", "title", "summary", "content", "scope"):
        if key in clean and isinstance(clean[key], str):
            clean[key] = redact_sensitive(clean[key].strip())
    for key in ("tags", "relation_targets"):
        clean[key] = [
            redact_sensitive(item.strip())
            for item in clean.get(key, [])
            if item.strip()
        ]
    return clean


def _event_kind(event_type: str) -> str:
    if event_type in {"identity", "relationship"}:
        return event_type
    if event_type in {"decision", "open_loop", "goal", "project"}:
        return "project"
    if event_type == "preference":
        return "semantic"
    return "episodic"


def _event_priority(event_type: str) -> int:
    if event_type in {"identity", "relationship"}:
        return 82
    if event_type in {"correction", "decision", "goal"}:
        return 74
    return 62


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _recent_verified_tunnel(
    path: Path,
    *,
    max_age: timedelta = timedelta(minutes=15),
) -> bool:
    if not path.is_file():
        return False
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(str(report["generated_at"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)
    return (
        report.get("schema") == "lml-tunnel-runtime/v1"
        and report.get("state") == "verified"
        and report.get("verified") is True
        and timedelta(0) <= age <= max_age
    )


def _artifact(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"path": str(path), "exists": False, "sha256": None, "bytes": 0}
    raw = path.read_bytes()
    return {
        "path": str(path),
        "exists": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
