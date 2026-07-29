from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import MemoryStore


@dataclass(frozen=True)
class MemoryTenancy:
    tenant_id: str
    root: Path
    database_path: Path
    hermes_home: Path

    @classmethod
    def at(
        cls,
        root: str | Path,
        *,
        tenant_id: str,
    ) -> "MemoryTenancy":
        if not tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        resolved = Path(root)
        return cls(
            tenant_id=tenant_id,
            root=resolved,
            database_path=resolved / "memory.sqlite3",
            hermes_home=resolved / "hermes-home",
        )

    def initialize(self) -> dict[str, Any]:
        store = MemoryStore(self.database_path)
        schema = store.initialize()
        with store.connect() as conn:
            prior = conn.execute(
                "SELECT value FROM memory_meta_v3 WHERE key='tenant_id'"
            ).fetchone()
            if prior and prior["value"] != self.tenant_id:
                raise ValueError(
                    "database belongs to a different memory tenancy"
                )
            conn.execute(
                "INSERT INTO memory_meta_v3(key,value) VALUES('tenant_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.tenant_id,),
            )
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        return {
            "schema": "agent-memory-tenancy/v1",
            "tenant_id": self.tenant_id,
            "database_path": str(self.database_path.resolve()),
            "hermes_home": str(self.hermes_home.resolve()),
            "database": schema,
        }
