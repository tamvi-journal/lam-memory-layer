from __future__ import annotations

from datetime import datetime, timezone

from .profile import MemoryProfile
from .retrieval import MemoryHit


class PacketRenderer:
    def __init__(self, profile: MemoryProfile):
        self.profile = profile

    def render(
        self,
        query: str,
        hits: list[MemoryHit],
        *,
        scope: str,
        surface: str,
        compact: bool = True,
    ) -> str:
        groups: dict[str, list[MemoryHit]] = {}
        for hit in hits:
            groups.setdefault(hit.revision["domain"], []).append(hit)
        lines = [
            f"# {self.profile.packet_title}",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"Cue: {query}",
            f"Scope: {scope}",
            f"Surface: {surface}",
            "",
            "> Memory is evidence and orientation, not authority.",
            "> Current revisions are shown by default; history is cue-driven.",
            "",
        ]
        ordered = list(self.profile.section_order)
        ordered.extend(sorted(set(groups) - set(ordered)))
        for domain in ordered:
            if domain not in groups:
                continue
            lines.extend(
                [
                    f"## {self.profile.section_labels.get(domain, domain.replace('_', ' ').title())}",
                    "",
                ]
            )
            for hit in groups[domain]:
                revision = hit.revision
                lines.extend(
                    [
                        f"### {revision['title']}",
                        revision["summary"] or revision["content"][:350],
                    ]
                )
                if (
                    not compact
                    and revision["content"]
                    and revision["content"] != revision["summary"]
                ):
                    lines.append(revision["content"])
                lines.append(
                    f"- memory_id: `{revision['record_id']}` | "
                    f"revision: `{revision['revision_id']}` | "
                    f"class: {revision['record_class']} | "
                    f"confidence: {revision['confidence']:.2f}"
                )
                lines.append("- retrieval: " + ", ".join(hit.reasons[:5]))
                if hit.history:
                    lines.append(f"- history: {len(hit.history)} revisions")
                    for old in hit.history:
                        lines.append(
                            f"  - r{old['revision_number']} "
                            f"[{old['revision_status']}]: "
                            f"{old['summary'] or old['title']}"
                        )
                lines.append("")
        instructions = self.profile.instructions(surface)
        if instructions:
            lines.extend(["## Execution instruction", ""])
            lines.extend(instructions)
        return "\n".join(lines).strip() + "\n"
