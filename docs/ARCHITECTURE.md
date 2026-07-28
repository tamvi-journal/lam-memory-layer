# Architecture

## Layers

1. `MemoryStore` owns records, immutable revisions, evidence, operations,
   cues, relations, and access telemetry.
2. `ValidatedIntake` decides whether a semantic proposal materializes, is
   held, or is a no-op.
3. `CueDrivenRetriever` activates current revisions from explicit cues,
   lexical overlap, bounded bootstrap anchors, and graph relations.
4. `PacketRenderer` turns selected revisions into a bounded context packet.
5. Profiles provide all consumer-specific names, anchors, aliases, sections,
   and execution instructions.
6. Adapters outside this repository own migration, transport, scheduling,
   consolidation, and product lifecycle hooks.

## Current versus history

A record is the stable subject of a memory. A revision is a claim about that
subject at a point in time.

- exactly one revision may be current;
- a semantic update supersedes the previous revision;
- invalidation keeps the final revision in history;
- retrieval uses current revisions by default;
- history is loaded only when the cue or caller asks for it.

## Dynamic memory

Accessibility is telemetry, not meaning. Retrieval may increase accessibility
without changing the semantic hash.

Confidence is part of the semantic claim and therefore changes through a new
revision. Salience, stability and accessibility can be updated through
`MemoryStore.apply_maintenance()`. That path is transactional, idempotent,
records a `maintenance` operation, verifies that the semantic hash did not
change and rejects all other fields. Host applications still decide when a
maintenance run is justified.

## Authority

The core does not know the identity of the human owner or the agent. A host
passes `protected_authorized=True` only after resolving its own permission
boundary.

The default policy:

- holds unresolved conflicts;
- requires stronger evidence and a falsifier for axis changes;
- allows ordinary evidence-backed event and belief evolution;
- fails closed on protected weakening or undeclared protected effects.

## Extraction boundary

The core is intentionally free of:

- agent-specific memory;
- relationship data;
- private seeds;
- cloud or local product routing;
- transcript ingestion;
- a fixed identity ontology.

Consumers should keep those in separate profile and adapter packages.
