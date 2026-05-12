import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


def log_audit(
    log_path: Path,
    command: str,
    args: dict,
    outcome: Literal["success", "failure"],
    case: str | None = None,
    operator: str | None = None,
    details: dict | None = None,
) -> None:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _check_json_serializable("args", args)
    if details is not None:
        _check_json_serializable("details", details)

    if operator is None:
        operator = os.environ.get("USER") or "unknown"

    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "operator": operator,
        "command": command,
        "args": args,
        "outcome": outcome,
    }
    if case is not None:
        entry["case"] = case
    if details is not None:
        entry["details"] = details

    line = json.dumps(entry, separators=(",", ":")) + "\n"

    lock_path = log_path.with_name(log_path.name + ".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with open(log_path, "a", newline="") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _check_json_serializable(name: str, d: dict) -> None:
    for key, value in d.items():
        try:
            json.dumps(value)
        except TypeError as e:
            raise TypeError(
                f"{name}[{key!r}] is not JSON-serializable: {e}"
            ) from e
