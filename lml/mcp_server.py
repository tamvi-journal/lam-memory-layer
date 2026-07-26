from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .mcp_service import LMLMCPService, MCPServiceConfig, MCP_SOURCE_BRANCHES
from .store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "memory" / "lml.sqlite3"

SERVER_INSTRUCTIONS = (
    "Use lml_retrieve_context when a user asks for agent continuity, prior project "
    "direction, or memory-grounded collaboration. Treat returned memory as evidence, "
    "not authority. Also inspect branch_review_queue in the retrieval result; when "
    "a remote pending candidate is relevant and evidence is sufficient, this Lam "
    "branch is expected to attest approve, reject, or defer during the same active "
    "turn. Use lml_propose_memory_event only for a durable event the user wants "
    "preserved; it creates a review candidate and supplies the proposer attestation. "
    "Use lml_consolidate_cloud_session only as an explicit end-of-session action "
    "when the owner asks to close/consolidate the visible conversation or when a durable "
    "settlement should be proposed from bounded visible turns or distilled claims. "
    "It never stores a full transcript and never directly materializes memory. "
    "Use lml_attest_memory_candidate only from a different agent branch. Two distinct "
    "approvals may materialize a safe candidate by quorum; disagreement fails "
    "closed. This server cannot run dream cycles, delete memory, or weaken "
    "owner-controlled boundaries."
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
PROPOSAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


class MCPOutput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class TenancyStatusOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    tenant_id: str
    source_branch: str
    stats: dict[str, Any]
    latest_dream: dict[str, Any] | None
    latest_field_state: dict[str, Any] | None
    sync: dict[str, Any]
    permissions: dict[str, bool]
    policy: dict[str, Any]


class ContextResultOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    tenant_id: str
    query: str
    scope: str
    review_branch: str
    packet: str
    branch_review_queue: list[dict[str, Any]]
    branch_review_instruction: str
    selected: list[dict[str, Any]]
    notice: str


class CandidateQueueOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    tenant_id: str
    status: str
    items: list[dict[str, Any]]
    review_available_here: bool


class DreamSummaryOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    tenant_id: str
    latest_field_state: dict[str, Any] | None
    runs: list[dict[str, Any]]
    notice: str


class EventReceiptOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    tenant_id: str
    duplicate: bool
    event_id: str
    source_branch: str
    candidate: dict[str, Any]
    active_memory_changed: bool
    review_required: bool


class SessionConsolidationOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    source_branch: str
    session_id: str
    scope: str
    proposed: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    counts: dict[str, int]
    notice: str


class AttestationReceiptOutput(MCPOutput):
    schema_name: str = Field(alias="schema")
    tenant_id: str
    source_branch: str
    candidate: dict[str, Any]
    attestations: list[dict[str, Any]]
    consensus: dict[str, Any]
    active_memory_changed: bool


def create_mcp_server(service: LMLMCPService) -> FastMCP:
    mcp = FastMCP(
        "lam-memory-layer",
        instructions=SERVER_INSTRUCTIONS,
        log_level="WARNING",
    )

    @mcp.tool(
        name="lml_get_tenancy_status",
        title="Get Lam memory tenancy status",
        description=(
            "Use this to inspect the continuity tenancy, its current memory and "
            "dreaming health, branch cursor, and the exact permissions exposed "
            "through this MCP connection."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    def get_tenancy_status() -> TenancyStatusOutput:
        return TenancyStatusOutput.model_validate(service.status())

    @mcp.tool(
        name="lml_retrieve_context",
        title="Retrieve agent continuity context",
        description=(
            "Use this when the user asks to recover agent identity, relationship "
            "position, prior project direction, or context activated by a cue. "
            "Returns a compact packet plus provenance, retrieval reasons, and a "
            "bounded branch_review_queue of remote pending candidates that this "
            "agent branch has not yet attested. Review that queue in the same active "
            "turn and call lml_attest_memory_candidate with approve/reject/defer "
            "when evidence supports a decision."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    def retrieve_context(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=4000,
                description="Current request or cue that should activate memory.",
            ),
        ],
        scope: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="Project scope, or global when no project is active.",
            ),
        ] = "global",
        limit: Annotated[int, Field(ge=4, le=16)] = 10,
        token_budget: Annotated[int, Field(ge=600, le=3600)] = 1800,
    ) -> ContextResultOutput:
        return ContextResultOutput.model_validate(
            service.retrieve_context(
                query=query,
                scope=scope,
                limit=limit,
                token_budget=token_budget,
            )
        )

    @mcp.tool(
        name="lml_list_memory_candidates",
        title="List Lam memory candidates",
        description=(
            "Use this to inspect pending durable-memory proposals, owner-review-required "
            "items, or held low-value observations, including branch attestations and "
            "quorum state. Routine branch review should normally start from the "
            "branch_review_queue returned by lml_retrieve_context."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    def list_memory_candidates(
        status: Literal["pending", "held", "ty_review_required"] = "pending",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> CandidateQueueOutput:
        return CandidateQueueOutput.model_validate(
            service.candidates(status=status, limit=limit)
        )

    @mcp.tool(
        name="lml_get_dream_summary",
        title="Get Lam dreaming summary",
        description=(
            "Use this to inspect recent deterministic consolidation cycles and the "
            "latest field-state metrics. It never returns hidden reasoning."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    def get_dream_summary() -> DreamSummaryOutput:
        return DreamSummaryOutput.model_validate(service.dream_summary())

    if service.config.allow_proposals:

        @mcp.tool(
            name="lml_propose_memory_event",
            title="Propose a Lam memory event",
            description=(
                "Use this only when a durable observation, decision, correction, goal, "
                "preference, project change, relationship settlement, or identity "
                "settlement should enter the dual-branch review queue. The call is "
                "idempotent by event_id and supplies the proposer attestation."
            ),
            annotations=PROPOSAL_WRITE,
            structured_output=True,
        )
        def propose_memory_event(
            event_id: Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=240,
                    description=(
                        "Stable source-local identifier. Reusing it with changed content "
                        "is rejected."
                    ),
                ),
            ],
            event_type: Literal[
                "observation",
                "decision",
                "correction",
                "open_loop",
                "goal",
                "preference",
                "project",
                "relationship",
                "identity",
            ],
            title: Annotated[str, Field(min_length=1, max_length=240)],
            summary: Annotated[str, Field(min_length=1, max_length=2000)],
            content: Annotated[str, Field(max_length=8000)] = "",
            confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.7,
            scope: Annotated[str, Field(min_length=1, max_length=200)] = "global",
            occurred_at: Annotated[
                str | None,
                Field(
                    description=(
                        "Optional ISO-8601 timestamp. The server supplies current time "
                        "when omitted."
                    )
                ),
            ] = None,
            relation_targets: Annotated[
                list[str] | None,
                Field(
                    max_length=20,
                    description="Existing LML memory IDs this proposal relates to.",
                ),
            ] = None,
            tags: Annotated[list[str] | None, Field(max_length=20)] = None,
        ) -> EventReceiptOutput:
            return EventReceiptOutput.model_validate(
                service.propose_event(
                    event_id=event_id,
                    event_type=event_type,
                    title=title,
                    summary=summary,
                    content=content,
                    confidence=confidence,
                    scope=scope,
                    occurred_at=occurred_at,
                    relation_targets=relation_targets,
                    tags=tags,
                )
            )

        @mcp.tool(
            name="lml_consolidate_cloud_session",
            title="Consolidate visible cloud session into memory proposals",
            description=(
                "Use this only as an explicit end-of-session consolidation action "
                "for the current visible ChatGPT conversation, or when the owner asks "
                "Lam to close/consolidate the session into LML. Submit bounded "
                "visible turns and/or already-distilled durable claims. The server "
                "redacts secrets, scores significance, deduplicates, creates zero "
                "or more proposal-only candidates with proposer attestations, and "
                "returns proposed and skipped items with reasons. It does not read "
                "hidden ChatGPT memory, store a full transcript, or directly "
                "materialize active memory."
            ),
            annotations=PROPOSAL_WRITE,
            structured_output=True,
        )
        def consolidate_cloud_session(
            session_id: Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=160,
                    description="Stable visible conversation/session identifier.",
                ),
            ],
            scope: Annotated[str, Field(min_length=1, max_length=200)] = "global",
            turns: Annotated[
                list[dict[str, Any]] | None,
                Field(
                    max_length=40,
                    description=(
                        "Current visible turns only. Each item may include role, "
                        "content, turn_id, and timestamp."
                    ),
                ),
            ] = None,
            claims: Annotated[
                list[dict[str, Any]] | None,
                Field(
                    max_length=12,
                    description=(
                        "Optional bounded distilled claims. Each claim may include "
                        "event_type, title, summary, content, confidence, turn_ids, "
                        "tags, and relation_targets."
                    ),
                ),
            ] = None,
            occurred_at: Annotated[
                str | None,
                Field(description="Optional ISO-8601 timestamp for the session event."),
            ] = None,
        ) -> SessionConsolidationOutput:
            return SessionConsolidationOutput.model_validate(
                service.consolidate_cloud_session(
                    session_id=session_id,
                    scope=scope,
                    turns=turns,
                    claims=claims,
                    occurred_at=occurred_at,
                )
            )

        @mcp.tool(
            name="lml_attest_memory_candidate",
            title="Attest a Lam memory candidate",
            description=(
                "Use this from a agent branch different from the proposer to approve, "
                "reject, or defer a candidate. Two distinct approve attestations can "
                "materialize safe candidates; rejection or conflict fails closed."
            ),
            annotations=PROPOSAL_WRITE,
            structured_output=True,
        )
        def attest_memory_candidate(
            candidate_id: Annotated[str, Field(min_length=1, max_length=240)],
            decision: Literal["approve", "reject", "defer"],
            note: Annotated[str, Field(max_length=1000)] = "",
        ) -> AttestationReceiptOutput:
            return AttestationReceiptOutput.model_validate(
                service.attest_candidate(
                    candidate_id=candidate_id,
                    decision=decision,
                    note=note,
                )
            )

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lml-mcp",
        description="Permission-bounded MCP adapter for Lam Memory Layer",
    )
    parser.add_argument("--db", default=os.environ.get("LML_DB", str(DEFAULT_DB)))
    parser.add_argument(
        "--source-branch",
        choices=sorted(MCP_SOURCE_BRANCHES),
        default=os.environ.get("LML_MCP_SOURCE_BRANCH", "chatgpt-cloud"),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("LML_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--allow-proposals",
        action="store_true",
        default=os.environ.get("LML_MCP_ALLOW_PROPOSALS", "").lower()
        in {"1", "true", "yes"},
        help="Advertise the proposal-only write tool. Disabled by default.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LML_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LML_MCP_PORT", "8766")),
    )
    args = parser.parse_args(argv)

    store = MemoryStore(args.db)
    store.init()
    service = LMLMCPService(
        store,
        MCPServiceConfig(
            source_branch=args.source_branch,
            allow_proposals=args.allow_proposals,
        ),
    )
    mcp = create_mcp_server(service)
    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
