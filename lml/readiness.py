from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .store import MemoryStore

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

READINESS_SCHEMA = "lml-tunnel-readiness/v1"
MCP_SERVER_NAME = "lam-memory-layer"


def assess_tunnel_readiness(
    root: str | Path,
    db_path: str | Path,
    parity_report: Mapping[str, Any],
    *,
    codex_home: str | Path = Path.home() / ".codex",
    environment: Mapping[str, str] | None = None,
    confirmations: Mapping[str, bool] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    database = Path(db_path).resolve()
    codex_root = Path(codex_home).expanduser().resolve()
    env = os.environ if environment is None else environment
    confirmed = confirmations or {}

    store_ok = False
    store_detail = "Vault database is missing."
    if database.is_file():
        store = MemoryStore(database)
        store.init()
        stats = store.stats()
        store_ok = stats["nodes"] > 0 and stats["cues"] > 0
        store_detail = (
            f"{stats['nodes']} nodes, {stats['edges']} edges, "
            f"{stats['cues']} cues."
        )

    python_path = root_path / ".venv" / "bin" / "python"
    server_module = root_path / "lml" / "mcp_server.py"
    stdio_ok = (
        python_path.is_file()
        and os.access(python_path, os.X_OK)
        and server_module.is_file()
    )

    registry = _codex_mcp_registry(codex_root / "config.toml", root_path)
    parity_ok = bool(parity_report.get("passed"))
    parity_schema_ok = parity_report.get("schema") == "lml-field-parity/v1"
    parity_source_ok = (
        Path(str(parity_report.get("source_db", ""))).resolve() == database
    )

    tunnel_client = which("tunnel-client")
    runtime_key_present = bool(env.get("CONTROL_PLANE_API_KEY"))
    tunnel_id_present = bool(
        env.get("CONTROL_PLANE_TUNNEL_ID") or env.get("LML_TUNNEL_ID")
    )

    local_checks = [
        _check("vault", store_ok, store_detail),
        _check(
            "stdio_server",
            stdio_ok,
            "Packaged read-only LML MCP stdio command is executable."
            if stdio_ok
            else "The LML MCP Python executable or server module is missing.",
        ),
        _check("codex_registry", registry["ok"], registry["detail"]),
        _check(
            "field_parity",
            parity_ok and parity_schema_ok and parity_source_ok,
            "Direct, HTTP, and MCP retrieval match on a fresh snapshot."
            if parity_ok and parity_schema_ok and parity_source_ok
            else "Fresh cross-interface field parity did not pass.",
        ),
    ]
    activation_checks = [
        _check(
            "tunnel_client",
            bool(tunnel_client),
            "tunnel-client is installed and discoverable."
            if tunnel_client
            else "Install the official tunnel-client before activation.",
        ),
        _check(
            "runtime_key",
            runtime_key_present,
            "Runtime key is available to this process."
            if runtime_key_present
            else "Runtime key has not been supplied to this process.",
        ),
        _check(
            "tunnel_id",
            tunnel_id_present,
            "Tunnel identity is available to this process."
            if tunnel_id_present
            else "CONTROL_PLANE_TUNNEL_ID has not been supplied to this process.",
        ),
        _confirmation_check(
            "platform_permissions",
            confirmed.get("platform_permissions", False),
            "Platform Tunnels Read + Use, plus Manage for creation.",
        ),
        _confirmation_check(
            "workspace_association",
            confirmed.get("workspace_association", False),
            "The tunnel is associated with the selected ChatGPT workspace and "
            "Platform organization.",
        ),
        _confirmation_check(
            "developer_mode",
            confirmed.get("developer_mode", False),
            "Developer-mode app access on the target ChatGPT surface.",
        ),
    ]
    local_ready = all(item["state"] == "passed" for item in local_checks)
    cloud_ready = local_ready and all(
        item["state"] == "passed" for item in activation_checks
    )
    return {
        "schema": READINESS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ready_for_account_authorization": local_ready,
        "ready_for_cloud_roundtrip": cloud_ready,
        "local_checks": local_checks,
        "activation_checks": activation_checks,
        "secret_handling": {
            "values_read_into_report": False,
            "presence_only": [
                "CONTROL_PLANE_API_KEY",
                "CONTROL_PLANE_TUNNEL_ID",
            ],
            "legacy_aliases": ["LML_TUNNEL_ID"],
        },
        "next_action": (
            "Run tunnel-client doctor, connect the developer-mode app, and "
            "verify a real retrieval round trip."
            if cloud_ready
            else (
                "the owner selects the Platform organization and ChatGPT workspace, "
                "then authorizes tunnel setup through the secure OpenAI flow."
                if local_ready
                else "Repair the failed local checks before requesting account access."
            )
        ),
    }


def _codex_mcp_registry(config_path: Path, root: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {"ok": False, "detail": "Codex config.toml is missing."}
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {"ok": False, "detail": "Codex config.toml could not be parsed."}
    entry = config.get("mcp_servers", {}).get(MCP_SERVER_NAME)
    if not isinstance(entry, dict):
        return {"ok": False, "detail": "lam-memory-layer is not registered in Codex."}
    expected_command = str(root / ".venv" / "bin" / "python")
    args = entry.get("args", [])
    read_only = "--allow-proposals" not in args
    command_ok = entry.get("command") == expected_command
    module_ok = _contains_sequence(args, ["-m", "lml.mcp_server"])
    source_ok = _contains_sequence(args, ["--source-branch", "work-mode"])
    ok = command_ok and module_ok and source_ok and read_only
    return {
        "ok": ok,
        "detail": (
            "Global Codex MCP entry is enabled with the read-only work-mode profile."
            if ok
            else "Codex MCP entry does not match the expected read-only work-mode profile."
        ),
    }


def _contains_sequence(items: Any, expected: list[str]) -> bool:
    if not isinstance(items, list):
        return False
    width = len(expected)
    return any(
        items[index : index + width] == expected for index in range(len(items))
    )


def _check(check_id: str, passed: bool, detail: str) -> dict[str, str]:
    return {
        "id": check_id,
        "state": "passed" if passed else "pending",
        "detail": detail,
    }


def _confirmation_check(check_id: str, passed: bool, detail: str) -> dict[str, str]:
    return _check(
        check_id,
        passed,
        detail if passed else f"Not yet confirmed: {detail}",
    )


def write_readiness_report(report: Mapping[str, Any], out: str | Path) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
