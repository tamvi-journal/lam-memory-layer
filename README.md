# Agent Memory Core

An experimental, profile-driven memory engine for agents that need:

- current and historical views;
- immutable semantic revisions;
- provenance-bearing evidence;
- cue-driven retrieval with bounded packets;
- dynamic accessibility without silent meaning changes;
- validated self-managed intake;
- slow, evidence-heavy axis evolution;
- fail-closed protected boundaries.

The package contains no agent identity, relationship history, private memory,
provider prompt, or product-specific transport. Those belong in profiles and
adapters owned by the consuming project.

## Why this exists

Agent memory should be able to evolve without quietly rewriting its own past.
This library separates four concerns that are often collapsed together:

- a stable record identity;
- append-only semantic revisions;
- immutable evidence and provenance;
- dynamic retrieval telemetry.

The result is a reusable mechanism, not a prebuilt identity or autonomous
agent.

## Install from source

```bash
git clone https://github.com/tamvi-journal/agent-memory-core.git
cd agent-memory-core
python3 -m pip install -e .
```

## Update law

The core follows four rules:

1. Current evidence may revise the current model.
2. Revision creates history; it does not overwrite history.
3. Retrieval may change accessibility, not semantic content.
4. Protected weakening requires authority supplied by the host application.

## Minimal use

```python
from memory_core import MemoryStore, ValidatedIntake

store = MemoryStore("memory.sqlite3")
writer = ValidatedIntake(store, surface="local")

result = writer.submit(
    operation_type="create",
    record_id="project-decision",
    record_class="belief",
    domain="project",
    actor="agent",
    reason="A verified decision should become current context.",
    logic="The implementation and test outcome agree.",
    truth_basis="The source artifact and verification report are linked.",
    evidence=[{
        "source_ref": "report:verified",
        "content_summary": "The consuming path passed.",
        "confidence": 0.95,
    }],
    idempotency_key="decision:verified:v1",
    changes={
        "title": "Verified project decision",
        "summary": "Use the tested path as the current implementation.",
    },
)
```

Use `MemoryProfile`, `CueDrivenRetriever`, and `PacketRenderer` to supply the
consumer-specific bootstrap anchors, aliases, sections, and instructions.

## Verify

```bash
python3 -W error::ResourceWarning -m pytest -q
python3 -m pip wheel . --no-deps
```

## Boundaries

This repository deliberately does not implement:

- transcript dumping;
- hidden-state access;
- identity or personality templates;
- automatic authority over protected constraints;
- cross-product transport;
- a universal ontology.

Those are application decisions, not generic memory mechanics.
