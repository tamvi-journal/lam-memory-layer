from __future__ import annotations

import json
import os
import plistlib
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .dream import DEFAULT_TENANT_ID, DreamResult, dream_due, run_dream_cycle
from .store import MemoryStore

SCHEDULER_SCHEMA = "lml-dream-scheduler/v1"
DEFAULT_LABEL = "com.tamvi.lml-dreaming"
DEFAULT_INTERVAL_HOURS = 12.0
DEFAULT_WAKE_SECONDS = 3600


@dataclass(frozen=True)
class GuardedDreamResult:
    schema: str
    state: str
    lock_path: str
    due: bool
    interval_hours: float
    dream: DreamResult | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "state": self.state,
            "lock_path": self.lock_path,
            "due": self.due,
            "interval_hours": self.interval_hours,
            "dream": self.dream.as_dict() if self.dream else None,
            "reason": self.reason,
        }


def default_lock_path(store: MemoryStore) -> Path:
    return store.db_path.parent / "working" / "dream.lock"


def guarded_dream_cycle(
    store: MemoryStore,
    *,
    scope: str = "global",
    tenant_id: str = DEFAULT_TENANT_ID,
    trigger: str = "manual",
    now: datetime | None = None,
    dry_run: bool = False,
    summary_out: str | Path | None = None,
    min_evidence: int = 3,
    require_due: bool = False,
    interval_hours: float = DEFAULT_INTERVAL_HOURS,
    lock_path: str | Path | None = None,
    blocking: bool = False,
) -> GuardedDreamResult:
    path = Path(lock_path) if lock_path else default_lock_path(store)
    with dream_lock(path, blocking=blocking) as acquired:
        if not acquired:
            return GuardedDreamResult(
                schema=SCHEDULER_SCHEMA,
                state="skipped_locked",
                lock_path=str(path),
                due=False,
                interval_hours=interval_hours,
                reason="another dream cycle holds the shared lock",
            )
        due = dream_due(store, now=now, interval_hours=interval_hours)
        if require_due and not due:
            return GuardedDreamResult(
                schema=SCHEDULER_SCHEMA,
                state="skipped_not_due",
                lock_path=str(path),
                due=False,
                interval_hours=interval_hours,
                reason="last committed dream is within the configured interval",
            )
        dream = run_dream_cycle(
            store,
            scope=scope,
            tenant_id=tenant_id,
            trigger=trigger,
            now=now,
            dry_run=dry_run,
            summary_out=summary_out,
            min_evidence=min_evidence,
        )
        return GuardedDreamResult(
            schema=SCHEDULER_SCHEMA,
            state="dry_run" if dry_run else "committed",
            lock_path=str(path),
            due=due,
            interval_hours=interval_hours,
            dream=dream,
        )


@contextmanager
def dream_lock(path: str | Path, *, blocking: bool = False) -> Iterator[bool]:
    import fcntl

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()
        try:
            yield True
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def dream_scheduler_operation(
    root: str | Path,
    db_path: str | Path,
    *,
    mode: str = "status",
    label: str = DEFAULT_LABEL,
    interval_hours: float = DEFAULT_INTERVAL_HOURS,
    wake_seconds: int = DEFAULT_WAKE_SECONDS,
    dry_run: bool = True,
    launch_agents_dir: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if mode not in {"status", "install", "uninstall"}:
        raise ValueError("mode must be status, install, or uninstall")
    root_path = Path(root).resolve()
    db = Path(db_path).resolve()
    agents_dir = Path(launch_agents_dir) if launch_agents_dir else Path.home() / "Library" / "LaunchAgents"
    plist_path = agents_dir / f"{label}.plist"
    program_arguments = [
        "/bin/zsh",
        str(root_path / "scripts" / "lml-dream-scheduler"),
        "--db",
        str(db),
        "dream-scheduler",
        "--run-once",
        "--interval-hours",
        str(interval_hours),
    ]
    plist = _launchd_plist(
        label=label,
        program_arguments=program_arguments,
        root=root_path,
        wake_seconds=wake_seconds,
    )
    loaded = _launchd_loaded(label, runner=runner)
    status = {
        "schema": SCHEDULER_SCHEMA,
        "mode": mode,
        "label": label,
        "plist_path": str(plist_path),
        "installed": plist_path.is_file(),
        "loaded": loaded,
        "dry_run": dry_run,
        "interval_hours": interval_hours,
        "wake_seconds": wake_seconds,
        "program_arguments": program_arguments,
        "lock_path": str(default_lock_path(MemoryStore(db))),
        "last_dream": _last_dream(MemoryStore(db)),
    }
    if mode == "status":
        return {**status, "state": "inspected"}
    if mode == "install":
        if dry_run:
            return {**status, "state": "planned_install", "plist": plist}
        agents_dir.mkdir(parents=True, exist_ok=True)
        actions: list[dict[str, Any]] = []
        if loaded:
            bootout = _run_launchctl(
                ["bootout", f"gui/{os.getuid()}/{label}"],
                runner=runner,
            )
            actions.append(_launchctl_action("bootout_existing", bootout))
            if bootout.returncode != 0:
                return {
                    **status,
                    "state": "install_failed",
                    "installed": plist_path.is_file(),
                    "loaded": True,
                    "actions": actions,
                    "error": "failed to bootout existing loaded scheduler",
                }
        _atomic_write_plist(plist_path, plist)
        bootstrap = _run_launchctl(
            ["bootstrap", f"gui/{os.getuid()}", str(plist_path)],
            runner=runner,
        )
        actions.append(_launchctl_action("bootstrap", bootstrap))
        if bootstrap.returncode != 0:
            verified = _launchd_loaded(label, runner=runner)
            return {
                **status,
                "state": "install_failed",
                "installed": plist_path.is_file(),
                "loaded": verified,
                "actions": actions,
                "error": "failed to bootstrap scheduler LaunchAgent",
            }
        verified = _launchd_loaded(label, runner=runner)
        actions.append({"action": "verify_loaded", "returncode": 0 if verified else 1})
        if not verified:
            return {
                **status,
                "state": "install_failed",
                "installed": plist_path.is_file(),
                "loaded": False,
                "actions": actions,
                "error": "launchctl print did not verify loaded scheduler",
            }
        return {
            **status,
            "state": "installed_loaded",
            "installed": True,
            "loaded": True,
            "actions": actions,
        }
    if dry_run:
        return {**status, "state": "planned_uninstall"}
    actions = []
    if loaded:
        bootout = _run_launchctl(
            ["bootout", f"gui/{os.getuid()}/{label}"],
            runner=runner,
        )
        actions.append(_launchctl_action("bootout", bootout))
        if bootout.returncode != 0:
            return {
                **status,
                "state": "uninstall_failed",
                "installed": plist_path.is_file(),
                "loaded": True,
                "actions": actions,
                "error": "failed to bootout loaded scheduler",
            }
    if plist_path.exists():
        plist_path.unlink()
    verified = _launchd_loaded(label, runner=runner)
    actions.append({"action": "verify_unloaded", "returncode": 1 if verified else 0})
    if verified:
        return {
            **status,
            "state": "uninstall_failed",
            "installed": plist_path.is_file(),
            "loaded": True,
            "actions": actions,
            "error": "launchctl print still reports loaded scheduler",
        }
    return {
        **status,
        "state": "uninstalled",
        "installed": False,
        "loaded": False,
        "actions": actions,
    }


def _launchd_plist(
    *,
    label: str,
    program_arguments: list[str],
    root: Path,
    wake_seconds: int,
) -> dict[str, Any]:
    log_dir = root / "memory" / "working"
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(root),
        "StartInterval": int(wake_seconds),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(log_dir / "dream-scheduler.out.log"),
        "StandardErrorPath": str(log_dir / "dream-scheduler.err.log"),
    }


def _launchd_loaded(
    label: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    result = _run_launchctl(["print", f"gui/{os.getuid()}/{label}"], runner=runner)
    return result.returncode == 0


def _run_launchctl(
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["launchctl", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _atomic_write_plist(path: Path, plist: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(plistlib.dumps(plist, sort_keys=True))
    temporary.replace(path)


def _launchctl_action(
    name: str,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return {
        "action": name,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip()[:1000],
        "stderr": (result.stderr or "").strip()[:1000],
    }


def _last_dream(store: MemoryStore) -> dict[str, Any] | None:
    store.init()
    runs = store.dream_runs(limit=1)
    if not runs:
        return None
    latest = runs[0]
    return {
        "id": latest["id"],
        "scope": latest["scope"],
        "trigger": latest["trigger"],
        "finished_at": latest["finished_at"],
    }
