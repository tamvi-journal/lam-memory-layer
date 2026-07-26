from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cloud_bridge import cloud_status, export_cloud_field, import_cloud_delta
from .context import ContextPacketBuilder
from .consolidator import consolidate_candidates
from .dashboard import serve_dashboard
from .dream import DEFAULT_TENANT_ID, run_dream_cycle
from .dream_scheduler import dream_scheduler_operation, guarded_dream_cycle
from .evaluation import run_continuity_evaluation
from .ingest import ingest_legacy_ltm
from .native_memory import build_native_memory_digest, native_memory_status
from .preflight import write_preflight_packet
from .retrieval import CueRetriever
from .store import MemoryStore, load_jsonl
from .sync import (
    ingest_sync_envelope,
    ingest_sync_inbox,
    publish_cloud_outbox,
    sync_envelope_template,
    sync_status,
)
from .tenancy import (
    event_template,
    import_tenancy_event,
    tenancy_manifest,
    write_tenancy_manifest,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "memory" / "lml.sqlite3"
SEED_DIR = ROOT / "memory" / "seeds"


def build_store(db: str | Path = DEFAULT_DB) -> MemoryStore:
    store = MemoryStore(db)
    store.init()
    return store


def seed(store: MemoryStore) -> None:
    load_jsonl(store, sorted(SEED_DIR.glob("*.jsonl")))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lml", description="Lam Memory Layer")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize database and load curated seeds")

    retrieve = sub.add_parser("retrieve", help="Show memories activated by a cue")
    retrieve.add_argument("query")
    retrieve.add_argument("--scope", default="global")
    retrieve.add_argument("--limit", type=int, default=12)

    context = sub.add_parser("context", help="Build a context packet")
    context.add_argument("query")
    context.add_argument("--scope", default="global")
    context.add_argument("--limit", type=int, default=12)
    context.add_argument("--budget", type=int, default=2400)
    context.add_argument("--out")

    preflight = sub.add_parser("preflight", help="Build the cold-start cue and context packet")
    preflight.add_argument("task")
    preflight.add_argument("--cwd", default=str(Path.cwd()))
    preflight.add_argument("--scope")
    preflight.add_argument("--limit", type=int, default=12)
    preflight.add_argument("--budget", type=int, default=2400)
    preflight.add_argument("--out", default=str(ROOT / "memory" / "working" / "lam-context-packet.md"))

    ingest = sub.add_parser("ingest-ltm", help="Preview or import checkpoint chunks from legacy LTM")
    ingest.add_argument("path", nargs="?", default=str(ROOT / "memory" / "LTM-Lam.md"))
    ingest.add_argument("--commit", action="store_true")

    dash = sub.add_parser("dashboard", help="Run Obsidian-style memory dashboard")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--open", action="store_true")

    service = sub.add_parser("serve", help="Run the continuity tenancy API and dashboard")
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8765)

    sub.add_parser("stats", help="Show memory graph statistics")

    candidates = sub.add_parser("candidates", help="List or review memory candidates")
    candidates.add_argument("--status", default="pending")
    candidates.add_argument("--approve")
    candidates.add_argument("--reject")
    candidates.add_argument("--attest", choices=["approve", "reject", "defer"])
    candidates.add_argument("--candidate-id")
    candidates.add_argument(
        "--branch",
        default="codex-cloud",
        help="agent branch casting --attest; MCP transports bind this automatically.",
    )
    candidates.add_argument(
        "--review-queue",
        action="store_true",
        help="List pending remote candidates not yet attested by --branch.",
    )
    candidates.add_argument("--note", default="")

    consolidate = sub.add_parser("consolidate", help="Create semantic candidates from episodes")
    consolidate.add_argument("--min-evidence", type=int, default=3)

    feedback = sub.add_parser("feedback", help="Record whether a retrieved memory helped")
    feedback.add_argument("memory_id")
    feedback.add_argument("outcome", choices=["helpful", "harmful"])

    cloud_export = sub.add_parser("cloud-export", help="Export a sanitized field packet for ChatGPT")
    cloud_export.add_argument("query")
    cloud_export.add_argument("--scope", default="global")
    cloud_export.add_argument("--out-dir", default=str(ROOT / "memory" / "cloud"))
    cloud_export.add_argument("--limit", type=int, default=10)
    cloud_export.add_argument("--budget", type=int, default=1800)

    cloud_import = sub.add_parser("cloud-import", help="Import a ChatGPT delta as a pending candidate")
    cloud_import.add_argument("path", help="JSON file, Markdown with a JSON fence, or - for stdin")
    cloud_import.add_argument("--inbox-dir", default=str(ROOT / "memory" / "cloud" / "inbox"))

    cloud_state = sub.add_parser("cloud-status", help="Show cloud bridge packet and inbox status")
    cloud_state.add_argument("--cloud-dir", default=str(ROOT / "memory" / "cloud"))

    dream = sub.add_parser("dream", help="Run temporal regulation and semantic consolidation")
    dream.add_argument("--scope", default="global")
    dream.add_argument("--tenant", default=DEFAULT_TENANT_ID)
    dream.add_argument("--trigger", default="manual")
    dream.add_argument("--dry-run", action="store_true")
    dream.add_argument("--min-evidence", type=int, default=3)
    dream.add_argument("--out", default=str(ROOT / "memory" / "working" / "dream-summary.md"))

    dream_state = sub.add_parser("dream-status", help="Show recent dream cycles and mutations")
    dream_state.add_argument("--limit", type=int, default=5)

    dream_scheduler = sub.add_parser(
        "dream-scheduler",
        help="Inspect, plan, or run the guarded due-aware dream scheduler",
    )
    scheduler_mode = dream_scheduler.add_mutually_exclusive_group()
    scheduler_mode.add_argument("--run-once", action="store_true")
    scheduler_mode.add_argument("--install", action="store_true")
    scheduler_mode.add_argument("--uninstall", action="store_true")
    scheduler_mode.add_argument("--status", action="store_true")
    dream_scheduler.add_argument("--scope", default="global")
    dream_scheduler.add_argument("--tenant", default=DEFAULT_TENANT_ID)
    dream_scheduler.add_argument("--interval-hours", type=float, default=12.0)
    dream_scheduler.add_argument("--wake-seconds", type=int, default=3600)
    dream_scheduler.add_argument("--trigger", default="scheduler")
    dream_scheduler.add_argument("--min-evidence", type=int, default=3)
    dream_scheduler.add_argument("--dry-run", action="store_true")
    dream_scheduler.add_argument("--apply", action="store_true")
    dream_scheduler.add_argument("--label", default="com.tamvi.lml-dreaming")
    dream_scheduler.add_argument(
        "--out",
        default=str(ROOT / "memory" / "working" / "dream-scheduler-report.json"),
    )
    dream_scheduler.add_argument(
        "--summary-out",
        default=str(ROOT / "memory" / "working" / "dream-summary.md"),
    )

    evaluate = sub.add_parser(
        "eval-continuity",
        help="Run a non-destructive continuity quality evaluation",
    )
    evaluate.add_argument(
        "--cwd",
        default=str(ROOT),
        help="Workspace path used to infer evaluation scope",
    )

    parity = sub.add_parser(
        "verify-parity",
        help="Compare one memory snapshot across direct, HTTP, and MCP retrieval",
    )
    parity.add_argument("--cue", action="append")
    parity.add_argument("--scope", default="lam-continuity-pack")
    parity.add_argument("--limit", type=int, default=12)
    parity.add_argument("--budget", type=int, default=2400)
    parity.add_argument(
        "--out",
        default=str(ROOT / "memory" / "working" / "field-parity-report.json"),
    )

    readiness = sub.add_parser(
        "tunnel-readiness",
        help="Verify local prerequisites without creating a tunnel or reading secrets",
    )
    readiness.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    readiness.add_argument(
        "--out",
        default=str(ROOT / "memory" / "working" / "tunnel-readiness-report.json"),
    )
    readiness.add_argument(
        "--parity-out",
        default=str(ROOT / "memory" / "working" / "field-parity-report.json"),
    )
    readiness.add_argument("--platform-permissions-confirmed", action="store_true")
    readiness.add_argument("--workspace-association-confirmed", action="store_true")
    readiness.add_argument("--developer-mode-confirmed", action="store_true")

    tunnel_runtime = sub.add_parser(
        "tunnel-runtime",
        help="Plan, inspect, or explicitly connect the managed LML tunnel runtime",
    )
    tunnel_mode = tunnel_runtime.add_mutually_exclusive_group()
    tunnel_mode.add_argument("--apply", action="store_true")
    tunnel_mode.add_argument("--status", action="store_true")
    tunnel_runtime.add_argument("--alias", default="lam-memory-layer")
    tunnel_runtime.add_argument("--profile", default="lam-memory-layer")
    tunnel_runtime.add_argument(
        "--out",
        default=str(ROOT / "memory" / "working" / "tunnel-runtime-report.json"),
    )

    native = sub.add_parser("native-memory", help="Inspect or retrieve Codex native local memory")
    native.add_argument("--query")
    native.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    native.add_argument("--limit", type=int, default=6)
    native.add_argument(
        "--out",
        default=str(ROOT / "memory" / "working" / "native-capability-report.json"),
    )

    event = sub.add_parser("event-import", help="Import a runtime event as a pending candidate")
    event.add_argument("path", help="JSON file or - for stdin")
    event.add_argument(
        "--inbox-dir",
        default=str(ROOT / "memory" / "tenancy" / "inbox"),
    )

    event_sample = sub.add_parser("event-template", help="Print the lml-event/v1 contract")
    event_sample.add_argument("--source", default="external-agent")
    event_sample.add_argument("--scope", default="global")

    tenancy = sub.add_parser("tenancy-status", help="Show and refresh the tenancy manifest")
    tenancy.add_argument(
        "--out",
        default=str(ROOT / "memory" / "tenancy" / "manifest.json"),
    )
    tenancy.add_argument("--service-url", default="http://127.0.0.1:8765")

    sync_publish = sub.add_parser("sync-publish", help="Publish the current field to a branch outbox")
    sync_publish.add_argument("--branch", default="chatgpt-cloud")
    sync_publish.add_argument(
        "--packet",
        default=str(ROOT / "memory" / "cloud" / "cloud-field-packet.md"),
    )
    sync_publish.add_argument(
        "--sync-dir",
        default=str(ROOT / "memory" / "tenancy" / "sync"),
    )

    sync_poll = sub.add_parser("sync-poll", help="Ingest branch inbox messages and write ACKs")
    sync_poll.add_argument("--branch", default="chatgpt-cloud")
    sync_poll.add_argument(
        "--sync-dir",
        default=str(ROOT / "memory" / "tenancy" / "sync"),
    )

    sync_state = sub.add_parser("sync-status", help="Show branch cursors and latest messages")
    sync_state.add_argument("--branch", default="chatgpt-cloud")
    sync_state.add_argument(
        "--sync-dir",
        default=str(ROOT / "memory" / "tenancy" / "sync"),
    )

    sync_sample = sub.add_parser("sync-template", help="Print a cloud return envelope")
    sync_sample.add_argument("--type", choices=["event", "delta", "ping"], default="event")
    sync_sample.add_argument("--branch", default="chatgpt-cloud")
    sync_sample.add_argument("--sequence", type=int, default=1)

    args = parser.parse_args(argv)
    store = build_store(args.db)

    if args.command == "init":
        seed(store)
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.command == "retrieve":
        hits = CueRetriever(store).retrieve(args.query, scope=args.scope, limit=args.limit)
        payload = [
            {
                "id": hit.node["id"],
                "kind": hit.node["kind"],
                "title": hit.node["title"],
                "score": round(hit.score, 4),
                "reasons": hit.reasons,
                "summary": hit.node["summary"],
            }
            for hit in hits
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.command == "context":
        packet = ContextPacketBuilder(CueRetriever(store)).build(
            args.query,
            scope=args.scope,
            limit=args.limit,
            token_budget=args.budget,
        )
        if args.out:
            Path(args.out).write_text(packet, encoding="utf-8")
            print(args.out)
        else:
            print(packet)
    elif args.command == "preflight":
        seed(store)
        result = write_preflight_packet(
            CueRetriever(store),
            args.task,
            cwd=args.cwd,
            scope=args.scope,
            out=args.out,
            limit=args.limit,
            token_budget=args.budget,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "ingest-ltm":
        preview = ingest_legacy_ltm(store, args.path, commit=args.commit)
        print(json.dumps({"count": len(preview), "commit": args.commit, "items": preview}, ensure_ascii=False, indent=2))
    elif args.command == "dashboard":
        seed(store)
        serve_dashboard(store, host=args.host, port=args.port, open_browser=args.open)
    elif args.command == "serve":
        seed(store)
        serve_dashboard(store, host=args.host, port=args.port)
    elif args.command == "stats":
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.command == "candidates":
        if args.attest:
            if not args.candidate_id:
                raise SystemExit("--candidate-id is required with --attest")
            result = store.attest_candidate(
                args.candidate_id,
                args.branch,
                args.attest,
                note=args.note,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.approve or args.reject:
            candidate_id = args.approve or args.reject
            decision = "approved" if args.approve else "rejected"
            result = store.review_candidate(candidate_id, decision, note=args.note)
            print(json.dumps(result or {}, ensure_ascii=False, indent=2))
        elif args.review_queue:
            print(
                json.dumps(
                    store.branch_review_queue(args.branch),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            status = None if args.status == "all" else args.status
            print(json.dumps(store.candidates(status=status), ensure_ascii=False, indent=2))
    elif args.command == "consolidate":
        proposals = consolidate_candidates(store, min_evidence=args.min_evidence)
        print(json.dumps(proposals, ensure_ascii=False, indent=2))
    elif args.command == "feedback":
        store.record_feedback(args.memory_id, args.outcome)
        print(json.dumps({"memory_id": args.memory_id, "outcome": args.outcome}, indent=2))
    elif args.command == "cloud-export":
        seed(store)
        result = export_cloud_field(
            store,
            args.query,
            scope=args.scope,
            out_dir=args.out_dir,
            limit=args.limit,
            token_budget=args.budget,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "cloud-import":
        import sys

        raw = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        result = import_cloud_delta(store, raw, inbox_dir=args.inbox_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "cloud-status":
        print(json.dumps(cloud_status(store, cloud_dir=args.cloud_dir), ensure_ascii=False, indent=2))
    elif args.command == "dream":
        seed(store)
        guarded = guarded_dream_cycle(
            store,
            scope=args.scope,
            tenant_id=args.tenant,
            trigger=args.trigger,
            dry_run=args.dry_run,
            summary_out=None if args.dry_run else args.out,
            min_evidence=args.min_evidence,
        )
        print(json.dumps(guarded.as_dict(), ensure_ascii=False, indent=2))
    elif args.command == "dream-status":
        runs = store.dream_runs(limit=args.limit)
        payload = []
        for run in runs:
            payload.append(
                {
                    **run,
                    "mutations": store.dream_mutations(run["id"]),
                }
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.command == "dream-scheduler":
        if args.run_once:
            result = guarded_dream_cycle(
                store,
                scope=args.scope,
                tenant_id=args.tenant,
                trigger=args.trigger,
                dry_run=args.dry_run,
                summary_out=None if args.dry_run else args.summary_out,
                min_evidence=args.min_evidence,
                require_due=True,
                interval_hours=args.interval_hours,
            ).as_dict()
        else:
            mode = "install" if args.install else "uninstall" if args.uninstall else "status"
            result = dream_scheduler_operation(
                ROOT,
                args.db,
                mode=mode,
                label=args.label,
                interval_hours=args.interval_hours,
                wake_seconds=args.wake_seconds,
                dry_run=not args.apply,
            )
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if str(result.get("state", "")).endswith("_failed"):
            raise SystemExit(1)
    elif args.command == "eval-continuity":
        result = run_continuity_evaluation(
            sorted(SEED_DIR.glob("*.jsonl")),
            cwd=args.cwd,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["passed"]:
            raise SystemExit(1)
    elif args.command == "verify-parity":
        from .parity import verify_field_parity

        result = verify_field_parity(
            args.db,
            cues=args.cue,
            scope=args.scope,
            limit=args.limit,
            token_budget=args.budget,
        )
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["passed"]:
            raise SystemExit(1)
    elif args.command == "tunnel-readiness":
        from .parity import verify_field_parity
        from .readiness import assess_tunnel_readiness, write_readiness_report

        parity_result = verify_field_parity(args.db)
        parity_out = Path(args.parity_out)
        parity_out.parent.mkdir(parents=True, exist_ok=True)
        parity_out.write_text(
            json.dumps(parity_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = assess_tunnel_readiness(
            ROOT,
            args.db,
            parity_result,
            codex_home=args.codex_home,
            confirmations={
                "platform_permissions": args.platform_permissions_confirmed,
                "workspace_association": args.workspace_association_confirmed,
                "developer_mode": args.developer_mode_confirmed,
            },
        )
        write_readiness_report(result, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ready_for_account_authorization"]:
            raise SystemExit(1)
    elif args.command == "tunnel-runtime":
        from .tunnel_runtime import tunnel_runtime_operation

        result = tunnel_runtime_operation(
            ROOT,
            apply=args.apply,
            status_only=args.status,
            alias=args.alias,
            profile=args.profile,
        )
        if args.out and (args.apply or args.status):
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["state"] in {
            "pending",
            "started_unverified",
            "inspected_unverified",
        }:
            raise SystemExit(1)
    elif args.command == "native-memory":
        status = native_memory_status(args.codex_home)
        if args.query:
            digest = build_native_memory_digest(
                args.query,
                codex_home=args.codex_home,
                limit=args.limit,
            )
            status["digest"] = digest.as_dict() if digest else None
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif args.command == "event-import":
        import sys

        raw = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        result = import_tenancy_event(store, payload, inbox_dir=args.inbox_dir)
        write_tenancy_manifest(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "event-template":
        print(
            json.dumps(
                event_template(source_branch=args.source, scope=args.scope),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "tenancy-status":
        manifest = write_tenancy_manifest(
            store,
            out=args.out,
            service_url=args.service_url,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    elif args.command == "sync-publish":
        result = publish_cloud_outbox(
            store,
            field_packet=args.packet,
            target_branch=args.branch,
            sync_dir=args.sync_dir,
        )
        write_tenancy_manifest(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "sync-poll":
        result = ingest_sync_inbox(
            store,
            source_branch=args.branch,
            sync_dir=args.sync_dir,
        )
        write_tenancy_manifest(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "sync-status":
        print(
            json.dumps(
                sync_status(store, branch=args.branch, sync_dir=args.sync_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "sync-template":
        print(
            json.dumps(
                sync_envelope_template(
                    message_type=args.type,
                    source_branch=args.branch,
                    sequence=args.sequence,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
