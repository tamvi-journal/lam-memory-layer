from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .dashboard import make_handler
from .mcp_service import LMLMCPService, MCPServiceConfig
from .store import MemoryStore

PARITY_SCHEMA = "lml-field-parity/v1"
DEFAULT_PARITY_CASES = (
    {
        "cue": "Anh là ai, em là gì và tại sao có mối quan hệ này?",
        "required_ids": [
            "lam-identity-core",
            "ty-carbon-witness",
            "lam-ty-relation-origin",
        ],
    },
    {
        "cue": "core relationship cue",
        "required_ids": [
            "lam-poet-function",
            "ai-relationship-context-return-pattern",
        ],
    },
    {
        "cue": "reviewer audit giúp anh xem lại kiến trúc này",
        "required_ids": [
            "aux-identity-core",
            "aux-audit-axis",
        ],
    },
)


def verify_field_parity(
    db_path: str | Path,
    *,
    cues: list[str] | None = None,
    scope: str = "lam-continuity-pack",
    limit: int = 12,
    token_budget: int = 2400,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    source = Path(db_path).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    cases = (
        [{"cue": cue, "required_ids": []} for cue in cues]
        if cues
        else [dict(case) for case in DEFAULT_PARITY_CASES]
    )
    with tempfile.TemporaryDirectory(prefix="lml-parity-") as temporary:
        snapshot = Path(temporary) / "parity.sqlite3"
        _backup_database(source, snapshot)
        snapshot_sha256 = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        store = MemoryStore(snapshot)
        store.init()
        handler = make_handler(
            store,
            service_url="http://127.0.0.1:ephemeral",
        )
        handler.log_message = lambda *_args: None
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            result = anyio.run(
                _collect_parity,
                store,
                snapshot,
                base_url,
                cases,
                scope,
                limit,
                token_budget,
                python_executable,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    return {
        "schema": PARITY_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_db": str(source),
        "snapshot_sha256": snapshot_sha256,
        "scope": scope,
        "limit": limit,
        "token_budget": token_budget,
        **result,
    }


async def _collect_parity(
    store: MemoryStore,
    snapshot: Path,
    base_url: str,
    cases: list[dict[str, Any]],
    scope: str,
    limit: int,
    token_budget: int,
    python_executable: str,
) -> dict[str, Any]:
    direct = LMLMCPService(
        store,
        MCPServiceConfig(source_branch="work-mode"),
    )
    parameters = StdioServerParameters(
        command=python_executable,
        args=[
            "-m",
            "lml.mcp_server",
            "--db",
            str(snapshot),
            "--source-branch",
            "work-mode",
        ],
        cwd=Path(__file__).resolve().parent.parent,
    )
    reports: list[dict[str, Any]] = []
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_names = sorted(tool.name for tool in listed.tools)
            read_only_contract = (
                "lml_propose_memory_event" not in tool_names
                and {
                    "lml_get_tenancy_status",
                    "lml_retrieve_context",
                    "lml_list_memory_candidates",
                    "lml_get_dream_summary",
                }
                <= set(tool_names)
            )
            for case in cases:
                cue = str(case["cue"])
                direct_result = direct.retrieve_context(
                    query=cue,
                    scope=scope,
                    limit=limit,
                    token_budget=token_budget,
                )
                http_result = _http_retrieve(
                    base_url,
                    cue=cue,
                    scope=scope,
                    limit=limit,
                    token_budget=token_budget,
                )
                mcp_result = await session.call_tool(
                    "lml_retrieve_context",
                    {
                        "query": cue,
                        "scope": scope,
                        "limit": limit,
                        "token_budget": token_budget,
                    },
                )
                if mcp_result.isError or not mcp_result.structuredContent:
                    raise RuntimeError(f"MCP retrieval failed for cue: {cue}")
                observations = {
                    "direct": _normalize_direct(direct_result["selected"]),
                    "http": _normalize_http(http_result["items"]),
                    "mcp": _normalize_direct(
                        mcp_result.structuredContent["selected"]
                    ),
                }
                reports.append(
                    _compare_case(
                        cue,
                        list(case.get("required_ids", [])),
                        observations,
                    )
                )
    return {
        "passed": read_only_contract and all(report["passed"] for report in reports),
        "mcp_tool_contract": {
            "read_only": read_only_contract,
            "tools": tool_names,
        },
        "surfaces": ["direct", "http", "mcp"],
        "cases": reports,
    }


def _compare_case(
    cue: str,
    required_ids: list[str],
    observations: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    id_sets = {
        surface: {item["memory_id"] for item in items}
        for surface, items in observations.items()
    }
    union = set().union(*id_sets.values())
    intersection = set.intersection(*id_sets.values())
    exact_ids = len({frozenset(ids) for ids in id_sets.values()}) == 1
    provenance_by_surface = {
        surface: {
            item["memory_id"]: (item["source_type"], item["source_ref"])
            for item in items
        }
        for surface, items in observations.items()
    }
    provenance_match = all(
        len(
            {
                provenance_by_surface[surface].get(memory_id)
                for surface in provenance_by_surface
            }
        )
        == 1
        for memory_id in union
    )
    missing_required = {
        surface: sorted(set(required_ids) - ids)
        for surface, ids in id_sets.items()
    }
    required_present = all(not missing for missing in missing_required.values())
    jaccard = len(intersection) / max(1, len(union))
    return {
        "cue": cue,
        "passed": exact_ids and provenance_match and required_present,
        "required_ids": required_ids,
        "exact_ids": exact_ids,
        "provenance_match": provenance_match,
        "required_present": required_present,
        "jaccard": round(jaccard, 4),
        "missing_required": missing_required,
        "observations": {
            surface: {
                "memory_ids": [item["memory_id"] for item in items],
                "provenance": {
                    item["memory_id"]: {
                        "source_type": item["source_type"],
                        "source_ref": item["source_ref"],
                    }
                    for item in items
                },
            }
            for surface, items in observations.items()
        },
    }


def _normalize_direct(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "memory_id": item["memory_id"],
            "source_type": item["source_type"],
            "source_ref": item["source_ref"],
        }
        for item in items
    ]


def _normalize_http(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "memory_id": item["node"]["id"],
            "source_type": item["node"]["source_type"],
            "source_ref": item["node"]["source_ref"],
        }
        for item in items
    ]


def _http_retrieve(
    base_url: str,
    *,
    cue: str,
    scope: str,
    limit: int,
    token_budget: int,
) -> dict[str, Any]:
    body = json.dumps(
        {
            "query": cue,
            "scope": scope,
            "limit": limit,
            "token_budget": token_budget,
        }
    ).encode("utf-8")
    request = Request(
        f"{base_url}/api/retrieve",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _backup_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
