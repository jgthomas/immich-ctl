"""Repair known JPEG/PNG extension mismatches locally, preserving image bytes."""

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from immich_ctl.folder_compat import JSON_LIMIT, native_sidecar_issues
from immich_ctl.folder_repair import (
    Change,
    RepairError,
    invalid_constant,
    unique_object,
    write_change,
)
from immich_ctl.folder_validate import ValidationError, inspect_folder
from immich_ctl.image_format import corrected_suffix


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class Rename:
    media: Path
    sidecar: Path
    target: Path
    target_sidecar: Path
    original: bytes
    updated: bytes
    sha256: str
    mode: int


def plan(folder: Path) -> tuple[int, list[Rename]]:
    try:
        media, warnings = inspect_folder(folder, check_filetypes=False)
    except ValidationError as error:
        raise RepairError(str(error)) from error
    if warnings:
        raise RepairError("Folder inspection needs attention: " + "; ".join(warnings))
    changes = []
    destinations: set[tuple[Path, str]] = set()
    for path, _ in media:
        suffix = corrected_suffix(path)
        if suffix is None:
            continue
        sidecar = next(
            p
            for p in path.parent.iterdir()
            if p.suffix.casefold() == ".json" and p.with_suffix("").name == path.name
        )
        with sidecar.open("rb") as stream:
            original = stream.read(JSON_LIMIT + 1)
        if len(original) > JSON_LIMIT:
            raise RepairError("Oversized JSON sidecar: " + str(sidecar))
        data = json.loads(
            original, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
        issues = native_sidecar_issues(path, data, payload=original)
        if issues:
            raise RepairError(str(sidecar) + ": " + "; ".join(issues))
        if not isinstance(data.get("fileName"), str) or not data["fileName"]:
            raise RepairError("Missing or invalid native fileName: " + str(sidecar))
        target = path.with_suffix(suffix)
        target_sidecar = target.with_name(target.name + sidecar.suffix)
        data["fileName"] = target.name
        updated = (json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode()
        if len(updated) > JSON_LIMIT:
            raise RepairError("Rewritten JSON exceeds size limit: " + str(sidecar))
        occupied = {p.name.casefold() for p in path.parent.iterdir()}
        for destination in (target, target_sidecar):
            identity = (destination.parent, destination.name.casefold())
            if destination.name.casefold() in occupied or identity in destinations:
                raise RepairError("Destination filename collision: " + str(destination))
            destinations.add(identity)
        changes.append(
            Rename(
                path,
                sidecar,
                target,
                target_sidecar,
                original,
                updated,
                checksum(path),
                stat.S_IMODE(sidecar.stat().st_mode),
            )
        )
    return len(media), changes


def verify_source(change: Rename) -> None:
    if (
        change.media.is_symlink()
        or change.sidecar.is_symlink()
        or change.sidecar.read_bytes() != change.original
        or checksum(change.media) != change.sha256
    ):
        raise RepairError("Source changed since inspection: " + str(change.media))


def write_sidecar(path: Path, payload: bytes, mode: int) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def apply(change: Rename) -> None:
    """Publish each pair without overwrites, rolling back that pair on failure."""
    verify_source(change)
    linked = False
    sidecar_created = False
    try:
        # Link within the same directory: no media copying/recompression or overwrite.
        os.link(change.media, change.target, follow_symlinks=False)
        linked = True
        # Create exclusively here so cleanup can distinguish a file we own from a collision.
        with change.target_sidecar.open("xb") as stream:
            sidecar_created = True
            os.fchmod(stream.fileno(), change.mode)
            stream.write(change.updated)
            stream.flush()
            os.fsync(stream.fileno())
        verify_source(change)
        if checksum(change.target) != change.sha256:
            raise RepairError("Renamed image checksum changed: " + str(change.target))
        change.media.unlink()
        change.sidecar.unlink()
    except BaseException:
        if linked:
            if not change.media.exists():
                os.link(change.target, change.media, follow_symlinks=False)
            if not change.sidecar.exists():
                write_sidecar(change.sidecar, change.original, change.mode)
        if sidecar_created:
            change.target_sidecar.unlink()
        if linked:
            change.target.unlink()
        raise


def repair(folder: Path, manifest: Path, *, dry_run: bool = False) -> dict[str, Any]:
    folder = folder.expanduser().resolve()
    manifest = manifest.expanduser().absolute()
    if manifest.is_symlink() or manifest.resolve().is_relative_to(folder):
        raise RepairError("Manifest must be outside the extraction and must not be a symlink.")
    if not manifest.parent.is_dir():
        raise RepairError("Manifest parent directory must already exist.")
    checked, changes = plan(folder)
    if changes and manifest.exists():
        raise RepairError("Manifest already exists; choose a new path. Nothing changed.")
    rows = [
        {
            "old_path": str(c.media.relative_to(folder)),
            "new_path": str(c.target.relative_to(folder)),
            "old_sidecar": str(c.sidecar.relative_to(folder)),
            "new_sidecar": str(c.target_sidecar.relative_to(folder)),
            "sha256": c.sha256,
            "old_sidecar_sha256": hashlib.sha256(c.original).hexdigest(),
            "new_sidecar_sha256": hashlib.sha256(c.updated).hexdigest(),
        }
        for c in changes
    ]
    report = {"folder": str(folder), "state": "planned", "checked": checked, "changes": rows}
    if not dry_run and changes:
        original = (json.dumps(report, indent=2) + "\n").encode()
        write_sidecar(manifest, original, 0o600)
        for change in changes:
            apply(change)
        report["state"] = "complete"
        updated = (json.dumps(report, indent=2) + "\n").encode()
        write_change(Change(manifest, manifest, original, updated, "", 0o600))
    for row in rows:
        print(json.dumps(row))
    print(f"Files checked: {checked}; {'would rename' if dry_run else 'renamed'}: {len(changes)}")
    if dry_run:
        print("Dry run: no files or manifest written.")
    elif changes:
        print("Manifest: " + json.dumps(str(manifest)))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument(
        "--manifest", required=True, type=Path, help="New JSON plan outside upload folders"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        repair(args.folder, args.manifest, dry_run=args.dry_run)
    except (RepairError, ValidationError, OSError, ValueError) as error:
        print("Error: " + json.dumps(str(error)), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
