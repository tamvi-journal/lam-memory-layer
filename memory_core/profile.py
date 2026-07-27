from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MemoryProfile:
    name: str
    packet_title: str
    bootstrap_record_ids: tuple[str, ...] = ()
    cue_aliases: tuple[tuple[str, str, float], ...] = ()
    section_order: tuple[str, ...] = ()
    section_labels: dict[str, str] = field(default_factory=dict)
    instructions_by_surface: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    default_instructions: tuple[str, ...] = ()
    history_markers: tuple[str, ...] = (
        "history",
        "historical",
        "timeline",
        "changed",
        "change",
        "evolved",
        "evolution",
        "before",
        "previous",
    )

    def instructions(self, surface: str) -> tuple[str, ...]:
        return self.instructions_by_surface.get(
            surface, self.default_instructions
        )
