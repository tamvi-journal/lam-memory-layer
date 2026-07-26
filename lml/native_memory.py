from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .text import normalize_text, tokens
from .writer import redact_sensitive

DEFAULT_CODEX_HOME = Path.home() / ".codex"
CONTINUITY_TERMS = {
    "lam",
    "ty",
    "continuity",
    "memory",
    "tam",
    "vi",
    "aux",
    "tracey",
    "field",
    "trajectory",
    "relationship-context",
}


@dataclass(frozen=True)
class NativeMemoryDigest:
    source_path: str
    source_paths: tuple[str, ...]
    source_sha256: str
    source_modified_at: str
    excerpts: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_paths": list(self.source_paths),
            "source_sha256": self.source_sha256,
            "source_modified_at": self.source_modified_at,
            "excerpts": self.excerpts,
        }


def native_memory_status(codex_home: str | Path = DEFAULT_CODEX_HOME) -> dict[str, Any]:
    home = Path(codex_home)
    config = home / "config.toml"
    summary = home / "memories" / "memory_summary.md"
    memory_dir = home / "memories"
    chronicle_dir = home / "memories_extensions" / "chronicle"
    config_text = config.read_text(encoding="utf-8", errors="replace") if config.exists() else ""
    disable_external = _toml_bool(
        config_text,
        "disable_on_external_context",
        section="memories",
    )
    native_files = _markdown_files(memory_dir)
    chronicle_files = _markdown_files(chronicle_dir)
    return {
        "codex_home": str(home),
        "native_store": "codex-local-generated",
        "bridge_access": "read-only",
        "feature_enabled": _toml_bool(config_text, "memories", section="features"),
        "generate_memories": _toml_bool(config_text, "generate_memories", section="memories"),
        "use_memories": _toml_bool(config_text, "use_memories", section="memories"),
        "disable_on_external_context": disable_external,
        "external_context_generation_allowed": (
            not disable_external if disable_external is not None else None
        ),
        "summary_exists": summary.exists(),
        "summary_path": str(summary),
        "summary_bytes": summary.stat().st_size if summary.exists() else 0,
        "memory_markdown_files": len(native_files),
        "chronicle": {
            "config_enabled": _toml_bool(
                config_text,
                "chronicle",
                section="features",
            ),
            "directory_exists": chronicle_dir.is_dir(),
            "path": str(chronicle_dir),
            "memory_markdown_files": len(chronicle_files),
        },
    }


def build_native_memory_digest(
    cue: str,
    *,
    codex_home: str | Path = DEFAULT_CODEX_HOME,
    limit: int = 6,
    excerpt_limit: int = 520,
) -> NativeMemoryDigest | None:
    home = Path(codex_home)
    sources = _native_sources(home)
    if not sources:
        return None
    cue_tokens = set(tokens(cue))
    ranked: list[tuple[float, int, dict[str, str]]] = []
    source_payloads: list[tuple[Path, str, str]] = []
    source_order = 0
    for source_path, source_type in sources:
        raw = source_path.read_text(encoding="utf-8", errors="replace")
        source_payloads.append((source_path, source_type, raw))
        for section in _sections(raw):
            text_tokens = set(tokens(f"{section['heading']} {section['body']}"))
            overlap = len(cue_tokens & text_tokens) / max(1, len(cue_tokens))
            continuity_overlap = len(CONTINUITY_TERMS & text_tokens) / len(
                CONTINUITY_TERMS
            )
            profile_boost = (
                0.35
                if source_type == "codex-native-summary"
                and section["heading"] in {"User Profile", "User preferences"}
                else 0.0
            )
            score = overlap * 1.4 + continuity_overlap * 0.9 + profile_boost
            if score > 0.08:
                ranked.append(
                    (
                        score,
                        source_order,
                        {
                            **section,
                            "source_path": str(source_path),
                            "source_type": source_type,
                        },
                    )
                )
            source_order += 1
    ranked.sort(key=lambda item: (-item[0], item[1]))

    excerpts = []
    for score, _, section in ranked[:limit]:
        body = redact_sensitive(" ".join(section["body"].split()))
        if len(body) > excerpt_limit:
            body = body[: excerpt_limit - 3].rstrip() + "..."
        excerpts.append(
            {
                "heading": section["heading"],
                "text": body,
                "relevance": round(score, 4),
                "source": (
                    "Codex Chronicle local memory"
                    if section["source_type"] == "codex-chronicle"
                    else "Codex native local memory summary"
                ),
                "source_path": section["source_path"],
                "source_type": section["source_type"],
                "authority": "evidence-only",
            }
        )
    hasher = hashlib.sha256()
    for source_path, source_type, raw in source_payloads:
        hasher.update(source_type.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(source_path).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(raw.encode("utf-8"))
        hasher.update(b"\0")
    source_paths = tuple(str(item[0]) for item in source_payloads)
    modified_at = max(item[0].stat().st_mtime_ns for item in source_payloads)
    return NativeMemoryDigest(
        source_path=source_paths[0],
        source_paths=source_paths,
        source_sha256=hasher.hexdigest(),
        source_modified_at=str(modified_at),
        excerpts=excerpts,
    )


def _native_sources(home: Path, *, chronicle_limit: int = 24) -> list[tuple[Path, str]]:
    summary = home / "memories" / "memory_summary.md"
    sources: list[tuple[Path, str]] = []
    if summary.is_file():
        sources.append((summary, "codex-native-summary"))
    chronicle_dir = home / "memories_extensions" / "chronicle"
    chronicle_files = sorted(
        _markdown_files(chronicle_dir),
        key=lambda path: (-path.stat().st_mtime_ns, str(path)),
    )
    sources.extend(
        (path, "codex-chronicle") for path in chronicle_files[:chronicle_limit]
    )
    return sources


def _markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*.md") if path.is_file())


def _sections(text: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    heading = "Preamble"
    buffer: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^##+\s+(.+?)\s*$", line)
        if match:
            if any(item.strip() for item in buffer):
                result.append({"heading": heading, "body": "\n".join(buffer).strip()})
            heading = match.group(1).strip()
            buffer = []
        else:
            buffer.append(line)
    if any(item.strip() for item in buffer):
        result.append({"heading": heading, "body": "\n".join(buffer).strip()})
    return result


def _toml_bool(text: str, key: str, *, section: str) -> bool | None:
    active_section = ""
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^\[([^\]]+)\]$", line)
        if match:
            active_section = normalize_text(match.group(1))
            continue
        if active_section != normalize_text(section):
            continue
        value_match = re.match(rf"^{re.escape(key)}\s*=\s*(true|false)\s*$", line, re.I)
        if value_match:
            return value_match.group(1).lower() == "true"
    return None
