from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .cloud_bridge import import_cloud_delta
from .context import ContextPacketBuilder
from .dream_scheduler import guarded_dream_cycle
from .retrieval import CueRetriever
from .store import MemoryStore
from .sync import ingest_sync_envelope, ingest_sync_inbox, publish_cloud_outbox, sync_status
from .tenancy import import_tenancy_event, tenancy_manifest, write_tenancy_manifest

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(
    store: MemoryStore,
    *,
    token: str = "",
    service_url: str = "http://127.0.0.1:8765",
):
    memory_root = store.db_path.parent
    manifest_out = memory_root / "tenancy" / "manifest.json"
    tenancy_inbox = memory_root / "tenancy" / "inbox"
    cloud_inbox = memory_root / "cloud" / "inbox"
    sync_dir = memory_root / "tenancy" / "sync"
    working_dir = memory_root / "working"

    def refresh_manifest() -> None:
        write_tenancy_manifest(
            store,
            out=manifest_out,
            service_url=service_url,
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self.send_json(
                    {
                        "ok": True,
                        "service": "lml-continuity-tenancy",
                        "tenant_id": store.meta_value("tenant_id", "local-continuity-primary"),
                    }
                )
                return
            if token and parsed.path.startswith("/api/") and not self.authorized():
                self.send_json({"error": "unauthorized"}, status=401)
                return
            if parsed.path == "/api/tenancy":
                self.send_json(tenancy_manifest(store, service_url=service_url))
                return
            if parsed.path == "/api/dreams":
                limit = _bounded_int(
                    parse_qs(parsed.query).get("limit", ["20"])[0],
                    default=20,
                    minimum=1,
                    maximum=100,
                )
                self.send_json({"items": store.dream_runs(limit=limit)})
                return
            if parsed.path == "/api/intake-events":
                limit = _bounded_int(
                    parse_qs(parsed.query).get("limit", ["50"])[0],
                    default=50,
                    minimum=1,
                    maximum=200,
                )
                self.send_json({"items": store.intake_events(limit=limit)})
                return
            if parsed.path == "/api/sync":
                branch = parse_qs(parsed.query).get("branch", ["chatgpt-cloud"])[0]
                self.send_json(sync_status(store, branch=branch, sync_dir=sync_dir))
                return
            if parsed.path == "/api/state":
                query = parse_qs(parsed.query).get("q", [""])[0]
                nodes = store.active_nodes()
                hit_map = {}
                if query:
                    hits = CueRetriever(store).retrieve(
                        query,
                        limit=20,
                        token_budget=6000,
                    )
                    visual_hits = sorted(
                        hits,
                        key=lambda hit: hit.score,
                        reverse=True,
                    )
                    hit_map = {
                        hit.node["id"]: {
                            "score": hit.score,
                            "reasons": hit.reasons,
                            "rank": rank,
                            "primary": rank < 6,
                        }
                        for rank, hit in enumerate(visual_hits)
                    }
                payload_nodes = []
                for node in nodes:
                    node = {key: value for key, value in node.items() if key != "embedding"}
                    node["activation"] = hit_map.get(node["id"], {})
                    payload_nodes.append(node)
                self.send_json(
                    {
                        "stats": store.stats(),
                        "nodes": payload_nodes,
                        "edges": store.edges(),
                        "timeline": store.timeline(),
                        "query": query,
                    }
                )
                return
            if parsed.path == "/api/node":
                node_id = parse_qs(parsed.query).get("id", [""])[0]
                node = store.get_node(node_id)
                if node:
                    node.pop("embedding", None)
                    self.send_json({"node": node, "neighbors": store.neighbors(node_id)})
                else:
                    self.send_error(404, "Memory not found")
                return
            if parsed.path == "/api/context":
                packet_path = working_dir / "lam-context-packet.md"
                if packet_path.exists():
                    self.send_json({"path": str(packet_path), "content": packet_path.read_text(encoding="utf-8")})
                else:
                    self.send_json({"path": str(packet_path), "content": ""})
                return
            if parsed.path == "/api/candidates":
                status = parse_qs(parsed.query).get("status", ["pending"])[0]
                self.send_json(
                    {
                        "items": store.candidates(
                            status=None if status == "all" else status
                        )
                    }
                )
                return
            if parsed.path == "/api/field-states":
                self.send_json({"items": store.field_states(limit=100)})
                return

            relative = "index.html" if parsed.path in {"", "/"} else parsed.path.lstrip("/")
            target = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
                self.send_error(403)
                return
            if not target.exists() or not target.is_file():
                self.send_error(404)
                return
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if token and not self.authorized():
                self.send_json({"error": "unauthorized"}, status=401)
                return
            try:
                body = self.read_json()
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return

            try:
                if parsed.path == "/api/retrieve":
                    query = _required_text(body, "query")
                    scope = str(body.get("scope", "global"))
                    limit = _bounded_int(body.get("limit"), default=12, minimum=1, maximum=50)
                    budget = _bounded_int(
                        body.get("token_budget"),
                        default=2400,
                        minimum=200,
                        maximum=20000,
                    )
                    hits = CueRetriever(store).retrieve(
                        query,
                        scope=scope,
                        limit=limit,
                        token_budget=budget,
                    )
                    self.send_json(
                        {
                            "query": query,
                            "scope": scope,
                            "items": [
                                {
                                    "node": {
                                        key: value
                                        for key, value in hit.node.items()
                                        if key != "embedding"
                                    },
                                    "score": hit.score,
                                    "reasons": hit.reasons,
                                }
                                for hit in hits
                            ],
                        }
                    )
                    return
                if parsed.path == "/api/context":
                    query = _required_text(body, "query")
                    scope = str(body.get("scope", "global"))
                    packet = ContextPacketBuilder(CueRetriever(store)).build(
                        query,
                        scope=scope,
                        limit=_bounded_int(body.get("limit"), default=12, minimum=1, maximum=50),
                        token_budget=_bounded_int(
                            body.get("token_budget"),
                            default=2400,
                            minimum=200,
                            maximum=20000,
                        ),
                        compact=bool(body.get("compact", True)),
                        event_type="tenancy-api",
                    )
                    self.send_json({"query": query, "scope": scope, "packet": packet})
                    return
                if parsed.path == "/api/events":
                    result = import_tenancy_event(
                        store,
                        body,
                        inbox_dir=tenancy_inbox,
                    )
                    refresh_manifest()
                    self.send_json(result, status=200 if result["duplicate"] else 201)
                    return
                if parsed.path == "/api/cloud-deltas":
                    result = import_cloud_delta(
                        store,
                        body,
                        inbox_dir=cloud_inbox,
                    )
                    refresh_manifest()
                    self.send_json({"candidate": result}, status=201)
                    return
                if parsed.path == "/api/dream":
                    guarded = guarded_dream_cycle(
                        store,
                        scope=str(body.get("scope", "global")),
                        trigger=str(body.get("trigger", "tenancy-api"))[:80],
                        dry_run=bool(body.get("dry_run", False)),
                        summary_out=(
                            None
                            if body.get("dry_run")
                            else working_dir / "dream-summary.md"
                        ),
                        min_evidence=_bounded_int(
                            body.get("min_evidence"),
                            default=3,
                            minimum=2,
                            maximum=20,
                        ),
                    )
                    result = guarded.dream
                    if result and not result.dry_run:
                        refresh_manifest()
                    if result:
                        payload = {
                            **result.as_dict(),
                            "guard": {
                                "state": guarded.state,
                                "lock_path": guarded.lock_path,
                                "due": guarded.due,
                                "interval_hours": guarded.interval_hours,
                            },
                        }
                    else:
                        payload = guarded.as_dict()
                    self.send_json(payload)
                    return
                if parsed.path == "/api/sync/ingest":
                    result = ingest_sync_envelope(
                        store,
                        body,
                        sync_dir=sync_dir,
                    )
                    refresh_manifest()
                    self.send_json(result, status=200 if result["duplicate"] else 201)
                    return
                if parsed.path == "/api/sync/poll":
                    branch = str(body.get("branch", "chatgpt-cloud"))
                    result = ingest_sync_inbox(
                        store,
                        source_branch=branch,
                        sync_dir=sync_dir,
                    )
                    refresh_manifest()
                    self.send_json({"items": result})
                    return
                if parsed.path == "/api/sync/publish":
                    packet = str(
                        body.get(
                            "field_packet",
                            memory_root / "cloud" / "cloud-field-packet.md",
                        )
                    )
                    result = publish_cloud_outbox(
                        store,
                        field_packet=packet,
                        target_branch=str(body.get("branch", "chatgpt-cloud")),
                        sync_dir=sync_dir,
                    )
                    refresh_manifest()
                    self.send_json(result, status=200 if result["duplicate"] else 201)
                    return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return

            if not parsed.path.startswith("/api/candidates/"):
                self.send_error(404)
                return
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 4 or parts[0:2] != ["api", "candidates"]:
                self.send_error(404)
                return
            candidate_id, action = parts[2], parts[3]
            if action not in {"approve", "reject"}:
                self.send_error(400, "Unknown candidate action")
                return
            decision = "approved" if action == "approve" else "rejected"
            item = store.review_candidate(
                candidate_id,
                decision,
                note=str(body.get("note", ""))[:1000],
            )
            if not item:
                self.send_error(404, "Candidate not found")
                return
            refresh_manifest()
            self.send_json({"item": item})

        def read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 1_048_576:
                raise ValueError("request body exceeds 1 MiB")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return bool(token) and hmac.compare_digest(header, expected)

        def send_json(self, payload: object, *, status: int = 200) -> None:
            content = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[lml-dashboard] {self.address_string()} {fmt % args}")

    return Handler


def serve_dashboard(
    store: MemoryStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    token: str | None = None,
) -> None:
    auth_token = token if token is not None else os.environ.get("LML_SERVICE_TOKEN", "")
    if not _is_loopback(host) and not auth_token:
        raise ValueError("non-loopback LML service requires LML_SERVICE_TOKEN")
    url = f"http://{host}:{port}"
    write_tenancy_manifest(
        store,
        out=store.db_path.parent / "tenancy" / "manifest.json",
        service_url=url,
    )
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(store, token=auth_token, service_url=url),
    )
    print(f"LML dashboard: {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _required_text(body: dict[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
