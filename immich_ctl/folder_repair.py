"""Repair native fileName aliases without changing media or other metadata."""

import argparse
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from immich_ctl.folder_compat import JSON_LIMIT, native_sidecar_issues
from immich_ctl.folder_validate import ValidationError, inspect_folder


class RepairError(Exception):
    """The folder cannot be repaired without guessing or overwriting changed data."""


@dataclass(frozen=True)
class Change:
    media: Path
    sidecar: Path
    original: bytes
    updated: bytes
    old_name: str
    mode: int


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RepairError("Duplicate JSON keys; repair would lose metadata.")
        value[key] = item
    return value


def invalid_constant(value: str) -> None:
    raise RepairError("Non-standard JSON number: " + value)


def plan(folder: Path) -> tuple[int, list[Change]]:
    """Validate every media/sidecar pair before any writes are attempted."""
    files, warnings = inspect_folder(folder)
    if warnings:
        raise RepairError("Folder inspection needs attention: " + "; ".join(warnings))
    changes = []
    for media, _ in files:
        # inspect_folder already requires one sidecar with an exact media basename.
        sidecar = next(
            p
            for p in media.parent.iterdir()
            if p.suffix.casefold() == ".json" and p.with_suffix("").name == media.name
        )
        with sidecar.open("rb") as stream:
            original = stream.read(JSON_LIMIT + 1)
        if len(original) > JSON_LIMIT:
            raise RepairError("Sidecar exceeds the JSON size limit: " + str(sidecar))
        try:
            data = json.loads(
                original, object_pairs_hook=unique_object, parse_constant=invalid_constant
            )
        except (ValueError, UnicodeError):
            raise RepairError("Invalid JSON sidecar: " + str(sidecar)) from None
        if not isinstance(data, dict):
            raise RepairError("Sidecar must contain a JSON object: " + str(sidecar))
        issues = native_sidecar_issues(media, data, payload=original)
        if issues:
            raise RepairError(str(sidecar) + ": " + "; ".join(issues))
        name = data.get("fileName")
        if not isinstance(name, str) or not name:
            raise RepairError("Missing or invalid native fileName: " + str(sidecar))
        if name == media.name:
            continue
        data["fileName"] = media.name
        updated = (json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode()
        if len(updated) > JSON_LIMIT:
            raise RepairError("Rewritten sidecar would exceed the JSON size limit: " + str(sidecar))
        changes.append(
            Change(media, sidecar, original, updated, name, stat.S_IMODE(sidecar.stat().st_mode))
        )
    return len(files), changes


def write_change(change: Change) -> None:
    """Replace one complete JSON file atomically, preserving its permissions."""
    if change.sidecar.is_symlink() or change.sidecar.read_bytes() != change.original:
        raise RepairError("Sidecar changed since inspection: " + str(change.sidecar))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=change.sidecar.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), change.mode)
            stream.write(change.updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, change.sidecar)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def repair(folder: Path, *, dry_run: bool = False) -> None:
    folder = folder.expanduser().resolve()
    checked, changes = plan(folder)
    for change in changes:
        if not dry_run:
            write_change(change)
        print(
            json.dumps(
                {
                    "action": "would_update" if dry_run else "updated",
                    "sidecar": str(change.sidecar.relative_to(folder)),
                    "old_fileName": change.old_name,
                    "new_fileName": change.media.name,
                },
                ensure_ascii=True,
            )
        )
    action = "would update" if dry_run else "updated"
    print(
        f"Files checked: {checked}; {action}: {len(changes)}; already matching: {checked - len(changes)}"
    )
    if dry_run:
        print("Dry run: no files changed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Normal or recovered immich-go extraction")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = parser.parse_args(argv)
    try:
        repair(args.folder, dry_run=args.dry_run)
    except (RepairError, ValidationError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
