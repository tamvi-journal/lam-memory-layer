from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .writer import redact_sensitive

RUNTIME_SCHEMA = "lml-tunnel-runtime/v1"
DEFAULT_ALIAS = "lam-memory-layer"
DEFAULT_PROFILE = "lam-memory-layer"


def tunnel_runtime_operation(
    root: str | Path,
    *,
    apply: bool = False,
    status_only: bool = False,
    alias: str = DEFAULT_ALIAS,
    profile: str = DEFAULT_PROFILE,
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    env = os.environ if environment is None else environment
    tunnel_client = which("tunnel-client")
    python = root_path / ".venv" / "bin" / "python"
    tunnel_id = env.get("CONTROL_PLANE_TUNNEL_ID", "")
    runtime_key_present = bool(env.get("CONTROL_PLANE_API_KEY"))
    tunnel_id_present = bool(tunnel_id)
    checks = {
        "tunnel_client": bool(tunnel_client),
        "mcp_command": python.is_file() and os.access(python, os.X_OK),
        "runtime_key": runtime_key_present,
        "tunnel_id": tunnel_id_present,
    }
    mcp_command = (
        f"{python} -m lml.mcp_server --source-branch chatgpt-cloud --allow-proposals"
    )
    plan = {
        "schema": RUNTIME_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alias": alias,
        "profile": profile,
        "mode": "status" if status_only else ("apply" if apply else "plan"),
        "checks": checks,
        "ready_to_apply": all(checks.values()),
        "mcp_command": mcp_command,
        "credential_refs": {
            "runtime_key": "env:CONTROL_PLANE_API_KEY",
            "tunnel_id": "env:CONTROL_PLANE_TUNNEL_ID",
        },
        "secret_values_reported": False,
    }
    if not tunnel_client:
        return {**plan, "state": "pending", "detail": "tunnel-client is missing."}
    if status_only:
        status = _run_json(
            [tunnel_client, "runtimes", "status", alias, "--json"],
            runner=runner,
            environment=env,
            tunnel_id=tunnel_id,
        )
        verified = _runtime_verified(status)
        return {
            **plan,
            "state": "verified" if verified else "inspected_unverified",
            "verified": verified,
            "status": status,
        }
    if not apply:
        return {
            **plan,
            "state": "planned",
            "detail": (
                "Ready for explicit apply authorization."
                if plan["ready_to_apply"]
                else "Account credentials or local runtime prerequisites are pending."
            ),
        }
    if not all(checks.values()):
        missing = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(f"tunnel runtime prerequisites are missing: {', '.join(missing)}")

    connect = _run_json(
        [
            tunnel_client,
            "runtimes",
            "connect",
            "--json",
            "--alias",
            alias,
            "--profile",
            profile,
            "--tunnel-id",
            tunnel_id,
            "--runtime-api-key",
            "env:CONTROL_PLANE_API_KEY",
            "--mcp-command",
            mcp_command,
        ],
        runner=runner,
        environment=env,
        tunnel_id=tunnel_id,
    )
    status = _run_json(
        [tunnel_client, "runtimes", "status", alias, "--json"],
        runner=runner,
        environment=env,
        tunnel_id=tunnel_id,
    )
    verified = _runtime_verified(status)
    return {
        **plan,
        "state": "verified" if verified else "started_unverified",
        "verified": verified,
        "connect": connect,
        "status": status,
    }


def _runtime_verified(status: Mapping[str, Any]) -> bool:
    runtime = status.get("runtime", status)
    if not isinstance(runtime, Mapping):
        return False
    return all(
        bool(runtime.get(field))
        for field in ("process_running", "healthy", "ready")
    )


def _run_json(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    environment: Mapping[str, str],
    tunnel_id: str,
) -> dict[str, Any]:
    result = runner(
        list(command),
        text=True,
        capture_output=True,
        check=False,
        env=dict(environment),
    )
    stdout = _redact_output(result.stdout, tunnel_id)
    stderr = _redact_output(result.stderr, tunnel_id)
    if result.returncode != 0:
        detail = stderr or stdout or f"exit {result.returncode}"
        raise RuntimeError(f"tunnel-client failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"output": stdout}
    return _redact_payload(payload, tunnel_id)


def _redact_payload(value: Any, tunnel_id: str) -> Any:
    if isinstance(value, dict):
        return {key: _redact_payload(item, tunnel_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, tunnel_id) for item in value]
    if isinstance(value, str):
        return _redact_output(value, tunnel_id)
    return value


def _redact_output(text: str, tunnel_id: str) -> str:
    clean = redact_sensitive(text or "")
    if tunnel_id:
        clean = clean.replace(tunnel_id, "<tunnel-id>")
    clean = re.sub(r"\btunnel_[A-Za-z0-9_-]{12,}\b", "<tunnel-id>", clean)
    return clean.strip()
