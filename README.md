# Lam Memory Layer

External, cue-driven continuity memory for AI work sessions.

LML is a local memory controller for agents that need project continuity across
fresh sessions, tools, and work surfaces. It is not a persona prompt, a
transcript dump, or a replacement for a first-party memory service. It stores
structured memory nodes, typed edges, cues, provenance, priority, confidence,
timeline evidence, and reviewable candidates, then retrieves a small
task-relevant context packet before work.

## What It Does

- Stores memory as structured graph nodes, not plain text files.
- Expands cues into related entities, timeline evidence, semantic neighbors,
  and project scope.
- Builds compact context packets with provenance and uncertainty labels.
- Uses hierarchical / coarse-to-fine retrieval: field summary first,
  checkpoint nodes next, episodes only when needed, and source pointers for
  evidence-sensitive work.
- Keeps identity, relationship, project, procedural, semantic, and episodic
  memory separate.
- Uses candidate review and branch quorum for durable memory changes.
- Provides a local dashboard with graph, timeline, candidate review, and node
  inspection.
- Exposes a permission-reduced MCP adapter for approved surfaces.

## What It Does Not Do

- It does not access hidden ChatGPT or Codex memory.
- It does not export or clone any proprietary OpenAI memory state.
- It does not write active memory directly from unreviewed conversation text.
- It does not store raw transcripts by default.
- It does not make claims about model consciousness or internal hidden state.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Initialize A Local Vault

```bash
.venv/bin/lml init
.venv/bin/lml retrieve "who am I working with and what project is active?"
.venv/bin/lml context "continue the current project" --scope example-project
```

## Retrieval Levels

LML treats long memory as a navigable stack, not a document to load wholesale:

```text
Level 0: field or dream summary
Level 1: checkpoint / semantic node
Level 2: episode / dated evidence
Level 3: source pointer / full artifact reference
```

Normal cues usually stop at Level 1. Cues that ask for evidence, chronology,
conflict handling, audit, or deeper detail automatically drill down to related
episodes and expose source pointers in the generated context packet.

## Dashboard

```bash
.venv/bin/lml dashboard --open
```

Then open:

```text
http://127.0.0.1:8765
```

## MCP Adapter

Read-only profile:

```bash
.venv/bin/python -m lml.mcp_server --source-branch work-mode
```

Proposal-enabled profile:

```bash
.venv/bin/python -m lml.mcp_server --source-branch chatgpt-cloud --allow-proposals
```

Proposal tools create pending candidates only. They do not bypass review,
dream cycles, owner-controlled boundaries, or branch quorum.

## Public Safety Boundary

This repository intentionally excludes personal memory, private seeds, runtime
SQLite databases, tunnel credentials, local account identifiers, screenshots,
logs, and any private relationship or project history. Use `examples/` as a
template for your own vault.

## Status

Experimental. Designed for local-first research and personal-agent continuity.
