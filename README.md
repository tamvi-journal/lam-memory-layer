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
