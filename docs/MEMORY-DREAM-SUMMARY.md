# Memory–Dream–Summary Pipeline

## Contract

The generic pipeline is:

```text
experience
  -> raw episode archive
  -> validated semantic intake
  -> governed dream / consolidation
  -> deterministic summary projection
  -> bounded recall
```

SQLite is canonical throughout. A consumer may project current memory into
another product's files, but those files are disposable views and are never
parsed back as memory truth.

## Raw archive

`EpisodeArchive.capture()` stores:

- a stable episode ID and idempotency key;
- source identity and hashes;
- observation time, actor and surface;
- title, summary and a bounded excerpt;
- a small structured payload;
- privacy and explicit transcript-inclusion metadata.

Payload keys such as `messages`, `turns`, `conversation` and `transcript` fail
closed unless `include_transcript=True`. Transcript storage is never the
default.

Every episode can produce canonical evidence for governed intake through
`EpisodeArchive.as_evidence()`.

## Governed dream

`GovernedDream.run()` accepts a batch of consolidation proposals plus their
source episode IDs. It:

1. resolves each episode to provenance-bearing evidence;
2. submits each semantic proposal through `ValidatedIntake`;
3. optionally applies telemetry-only maintenance;
4. verifies that every pre-existing revision retains its semantic hash;
5. appends an immutable dream-run and proposal audit record.

Dreaming cannot update a semantic row in place. A correction, refinement or
supersession produces a new immutable revision under the existing governance
law. Reusing an idempotency key with changed input fails closed.

## Summary and Hermes projection

`SummaryProjector` selects current revisions from SQLite and renders a
deterministic, framing-inclusive hard-budgeted document. No timestamp or
untracked external state enters the projection.

`HermesProjection` writes:

```text
HERMES_HOME/
  memories/
    MEMORY.md
    USER.md
    .agent-memory-core-projection.json
```

The Markdown files state that they are generated projections. The manifest
records the canonical database and file hashes. `verify()` compares the files
with a fresh database render.

A Hermes host may submit a new memory proposal through its consumer adapter.
The adapter must govern the proposal first and regenerate the files only after
intake returns. Editing the Markdown files never updates SQLite.

## Tenancy

`MemoryTenancy.at(root, tenant_id=...)` creates one isolated database and one
optional Hermes home. The tenant ID is persisted in database metadata; trying
to reopen the same database under a different tenant fails.

The class supplies isolation, not identity. Profiles, seeds, schedules,
authority and final action gates remain consumer-owned.

## Read and rollback boundaries

Episode lookup, summary rendering and read-only recall do not initialize or
write the database. File projection and schema initialization are explicit
write operations.

Because semantic history, episodes and dream audits are append-only, rollback
means returning to an earlier application deployment or isolating the pilot
database. It never means deleting or rewriting historical rows in place.
