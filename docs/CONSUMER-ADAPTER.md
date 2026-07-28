# Consumer adapter contract

Agent Memory Core owns memory mechanics. A consumer adapter owns identity,
authority and product integration.

## Minimum adapter

```python
from memory_core import MemoryProfile, MemoryRuntime

profile = MemoryProfile(
    name="my-agent",
    packet_title="MY AGENT MEMORY",
    bootstrap_record_ids=("agent-axis",),
    cue_aliases=(("reasoning", "agent-axis", 1.8),),
    default_instructions=(
        "Treat memory as candidate context, not truth authority.",
    ),
)

memory = MemoryRuntime(
    "runtime_state/memory.sqlite3",
    profile,
    surface="local",
)
```

The consumer then supplies its own provenance-bearing seed proposals and sends
retrieved packets through its existing reasoning, verification and permission
boundary.

## Ownership boundary

| Memory Core | Consumer adapter |
|---|---|
| revisions and history | identity and axis seeds |
| evidence and operation ledger | approved evidence sources |
| cue and relation retrieval | aliases and bootstrap anchors |
| bounded packet rendering | final reasoning authority |
| audited maintenance primitive | dream/maintenance policy |
| generic validation | permissions and protected authorization |

## Integration order

1. Define a thin update law, not a personality script.
2. Attach independent sources and an explicit falsifier to axis seeds.
3. Bootstrap idempotently into a private runtime database.
4. Retrieve into an explicitly non-authoritative candidate-context boundary.
5. Keep the host's verification, action gate and owner permissions unchanged.
6. Run `doctor()` plus consumer-specific cue contracts.
7. Add maintenance only after semantic revision behavior is verified.

Do not share a consumer database, private seeds or identity profile merely
because two agents use the same core package. Reuse the mechanism; keep each
trajectory's evidence and authority separate.
