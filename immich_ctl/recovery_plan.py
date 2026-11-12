"""Private companion-recovery plans bound to unchanged local inputs."""

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from immich_ctl.takeout_recover import RecoveryError

VERSION = 1  # Bump when companion matching/metadata policy changes.
LIMIT = 64 * 1024 * 1024


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryError("Recovery plan inputs must be regular files: " + str(path))
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def snapshot(paths: list[Path], folders: list[Path]) -> dict[str, Any]:
    """Stat media, hash small JSON, and retain the full roster to detect added files."""
    archives = {str(path): fingerprint(path) for path in paths}
    extracted = {}
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            raise RecoveryError("Recovery plan folder is missing or unsafe: " + str(folder))
        files = {}
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise RecoveryError("Recovery plan inputs must not contain symlinks.")
            if path.is_dir():
                continue
            before = fingerprint(path)
            row: dict[str, Any] = {"stat": before}
            if path.suffix.lower() == ".json":
                row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                if fingerprint(path) != before:
                    raise RecoveryError("Recovery inputs changed while checking the plan.")
            files[str(path.relative_to(folder))] = row
        extracted[str(folder)] = files
    return {"archives": archives, "folders": extracted}


def unchanged(expected: dict[str, Any], paths: list[Path], folders: list[Path]) -> None:
    if snapshot(paths, folders) != expected:
        raise RecoveryError("Recovery inputs changed; create a new dry-run plan.")


def destination(path: Path, paths: list[Path], folders: list[Path], output: Path) -> Path:
    path = path.expanduser().absolute()
    manifest = output.with_name(output.name + ".manifest.json")
    if (
        path.resolve() != path
        or not path.parent.is_dir()
        or path in paths
        or path in (output, manifest)
        or output in path.parents
        or any(path == folder or folder in path.parents for folder in folders)
    ):
        raise RecoveryError("Plan must be separate from source, extracted and output files.")
    return path


def save(path: Path, inputs: dict[str, Any], report: dict[str, Any]) -> None:
    payload = {"version": VERSION, "inputs": inputs, "report": report}
    data = encoded({"payload": payload, "sha256": hashlib.sha256(encoded(payload)).hexdigest()})
    if len(data) > LIMIT:
        raise RecoveryError("Recovery plan is too large.")
    # Exclusive creation also rejects existing files, symlinks and hard links.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data + b"\n")
    except BaseException:
        path.unlink()
        raise


def load(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise RecoveryError("Recovery plan must be a regular file.")
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    try:
        data = json.loads(raw)
        payload = data["payload"]
        if (
            len(raw) > LIMIT
            or payload["version"] != VERSION
            or data["sha256"] != hashlib.sha256(encoded(payload)).hexdigest()
            or not isinstance(payload["inputs"], dict)
            or not isinstance(payload["report"], dict)
        ):
            raise ValueError()
        return payload["inputs"], payload["report"]
    except (ValueError, TypeError, KeyError):
        raise RecoveryError(
            "Invalid or incompatible recovery plan; create a new dry run."
        ) from None
