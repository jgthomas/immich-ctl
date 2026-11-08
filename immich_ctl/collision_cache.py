"""Small, disposable records of successful checks against immutable source ZIPs."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from immich_ctl import takeout


def archive_state(paths: list[Path]) -> list[dict[str, Any]]:
    result = []
    for path in paths:
        info = path.stat()
        result.append(
            dict(
                path=str(path),
                device=info.st_dev,
                inode=info.st_ino,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                ctime_ns=info.st_ctime_ns,
            )
        )
    return result


def fingerprint(state: list[dict[str, Any]]) -> dict[str, Any] | None:
    # Changes to the checker or media classification invalidate previous passes.
    try:
        policy = hashlib.sha256()
        for path in (
            Path(__file__),
            Path(takeout.__file__),
            Path(__file__).with_name("takeout_safety.py"),
        ):
            policy.update(path.read_bytes())
        return {"version": 1, "policy": policy.hexdigest(), "archives": state}
    except OSError:
        return None


def record_path(directory: Path, identity: dict[str, Any]) -> Path:
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return directory / (key + ".json")


def contains(directory: Path, identity: dict[str, Any]) -> bool:
    try:
        with record_path(directory, identity).open("rb") as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            return False
        record = json.loads(payload)
        return (
            isinstance(record, dict)
            and record.get("passed") is True
            and record == {"fingerprint": identity, "passed": True}
        )
    except (OSError, ValueError, UnicodeError):
        return False


def store(directory: Path, identity: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".collision-", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"fingerprint": identity, "passed": True}, stream, sort_keys=True)
            stream.write("\n")
        temporary.replace(record_path(directory, identity))
    except OSError:
        # An unavailable cache must not stop, or weaken, a successful source check.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def default_directory() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", "")).expanduser()
    if not base.is_absolute():
        base = Path.home() / ".cache"
    return base / "immich-ctl" / "collisions"
