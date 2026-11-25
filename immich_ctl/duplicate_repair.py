"""Consolidate identical extracted media when one copy preserves all album metadata."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from immich_ctl.folder_compat import JSON_LIMIT, album_duplicate_groups, native_sidecar_issues
from immich_ctl.folder_repair import RepairError, invalid_constant
from immich_ctl.folder_validate import ValidationError, inspect_folder, timestamp
from immich_ctl.local_hashes import fingerprint, hash_file

# immich-go v0.32.0 adapters/shared/bannedFiles.go. The folder wrapper uses
# the defaults and clears environment overrides.
EXCLUDED_DIRECTORIES = {
    "@eadir",
    "@__thumb",
    "lightroom catalog",
    "thumbnails",
    ".spotlight-v100",
    ".photostructure",
    "recently deleted",
}


def upload_eligible(path: Path) -> bool:
    name = path.name.casefold()
    return (
        not any(part.casefold() in EXCLUDED_DIRECTORIES for part in path.parts[:-1])
        and name != ".ds_store"
        and not name.startswith("._")
        and not fnmatchcase(name, "synofile_thumb_*.*")
    )


def unique_native_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    folded = set()
    for key, value in pairs:
        normalized = key.casefold()
        if normalized in folded:
            raise RepairError(f"Duplicate or case-colliding JSON key: {key}")
        folded.add(normalized)
        result[key] = value
    return result


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def read_sidecar(media: Path) -> tuple[Path, bytes, dict[str, Any], list[int]]:
    (sidecar,) = (
        path
        for path in media.parent.iterdir()
        if path.suffix.casefold() == ".json" and path.with_suffix("").name == media.name
    )
    before = fingerprint(sidecar)
    with sidecar.open("rb") as stream:
        raw = stream.read(JSON_LIMIT + 1)
    if len(raw) > JSON_LIMIT:
        raise RepairError(f"Oversized sidecar: {sidecar}")
    data = json.loads(raw, object_pairs_hook=unique_native_object, parse_constant=invalid_constant)
    if (
        not isinstance(data, dict)
        or native_sidecar_issues(media, data, payload=raw)
        or data.get("fileName") != media.name
    ):
        raise RepairError(f"Sidecar needs filename or compatibility repair: {sidecar}")
    return sidecar, raw, data, before


def album_payloads(data: dict[str, Any], path: Path) -> set[str]:
    albums = data.pop("albums", [])
    if not isinstance(albums, list) or any(
        not isinstance(album, dict)
        or not isinstance(album.get("title"), str)
        or not album["title"].strip()
        for album in albums
    ):
        raise RepairError(f"Invalid albums in {path}")
    if len({album["title"] for album in albums}) != len(albums):
        raise RepairError(f"Repeated album titles in {path}")
    return {canonical(album) for album in albums}


def filename_index(folder: Path, metadata: dict[Path, dict[str, Any]]) -> dict[str, set[Path]]:
    index: defaultdict[str, set[Path]] = defaultdict(set)
    for path, sidecar in metadata.items():
        if not upload_eligible(path.relative_to(folder)):
            continue
        index[path.name.casefold()].add(path)
        for key, value in sidecar.items():
            if key.casefold() == "filename" and isinstance(value, str) and value:
                index[value.casefold()].add(path)
    return dict(index)


def collision_free(
    media: Path,
    copies: set[Path],
    peers: set[Path],
    metadata: dict[Path, dict[str, Any]],
    checksum: str,
    snapshots: dict[Path, list[int]],
) -> bool:
    captured = timestamp(metadata[media].get("dateTaken"))
    for peer in sorted(peers - copies):
        dates = [
            timestamp(value)
            for key, value in metadata[peer].items()
            if key.casefold() == "datetaken"
        ]
        # Missing or ambiguous timestamps cannot rule out a filename match.
        if captured is not None and len(dates) == 1 and dates[0] is not None:
            if abs((captured - dates[0]).total_seconds()) > 5:
                continue
        before = fingerprint(peer)
        if hash_file(peer, before)["sha256"] != checksum:
            return False
        snapshots[peer] = before
    return True


def plan(folder: Path) -> tuple[int, list[dict[str, Any]], dict[Path, list[int]]]:
    files, warnings = inspect_folder(folder)
    if warnings:
        raise RepairError("Folder inspection needs attention: " + "; ".join(warnings))
    metadata = {path: data for path, data in files if data is not None}
    names = filename_index(folder, metadata)
    snapshots: dict[Path, list[int]] = {}
    groups = []
    for copies in album_duplicate_groups(list(metadata), metadata):
        entries = []
        album_sets = []
        fields = set()
        checksums = set()
        for media in copies:
            snapshots[media] = fingerprint(media)
            checksums.add(hash_file(media, snapshots[media])["sha256"])
            sidecar, raw, data, snapshots_sidecar = read_sidecar(media)
            snapshots[sidecar] = snapshots_sidecar
            albums = album_payloads(data, sidecar)
            album_sets.append(albums)
            del data["fileName"]
            fields.add(canonical(data))
            entries.append(
                {
                    "media": str(media.relative_to(folder)),
                    "sidecar": str(sidecar.relative_to(folder)),
                    "sidecar_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        combined = set().union(*album_sets)
        paths = [entry["media"] for entry in entries]
        if len(checksums) != 1 or len(fields) != 1 or not combined:
            raise RepairError(f"Duplicate group requires manual review: {paths}")
        checksum = checksums.pop()
        copy_set = set(copies)
        keep = next(
            (
                i
                for i, media in enumerate(copies)
                if album_sets[i] == combined
                and upload_eligible(media.relative_to(folder))
                and collision_free(
                    media,
                    copy_set,
                    names.get(media.name.casefold(), set()),
                    metadata,
                    checksum,
                    snapshots,
                )
            ),
            None,
        )
        if keep is None:
            raise RepairError(
                f"Duplicate group requires manual review: no uploadable, collision-free copy "
                f"with every album: {paths}"
            )
        groups.append(
            {
                "sha256": checksum,
                "albums": [json.loads(album)["title"] for album in sorted(combined)],
                "retain": entries.pop(keep),
                "redundant": entries,
            }
        )
    verify_inputs(snapshots)
    return len(files), groups, snapshots


def verify_inputs(snapshots: dict[Path, list[int]]) -> None:
    for path, before in snapshots.items():
        if path.resolve() != path or fingerprint(path) != before:
            raise RepairError(f"Input changed during inspection: {path}")


def apply(
    folder: Path, backup: Path, groups: list[dict[str, Any]], snapshots: dict[Path, list[int]]
) -> None:
    verify_inputs(snapshots)
    backup.mkdir(mode=0o700)
    moved: list[tuple[Path, Path]] = []
    manifest = {"source": str(folder), "groups": groups}
    with (backup / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    try:
        for group in groups:
            for entry in group["redundant"]:
                for field in ("media", "sidecar"):
                    source = folder / entry[field]
                    target = backup / entry[field]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    verify_inputs({source: snapshots[source]})
                    source.rename(target)
                    moved.append((source, target))
                    expected = group["sha256"] if field == "media" else entry["sidecar_sha256"]
                    if hash_file(target, fingerprint(target))["sha256"] != expected:
                        raise RepairError(f"Moved {field} differs from the repair plan: {source}")
        for group in groups:
            verify_inputs(
                {
                    folder / group["retain"][field]: snapshots[folder / group["retain"][field]]
                    for field in ("media", "sidecar")
                }
            )
    except BaseException as error:
        for source, target in reversed(moved):
            if source.exists() or source.is_symlink():
                raise RepairError(
                    f"Cannot restore {source}; original files are in {backup}"
                ) from error
            target.rename(source)
        raise


def repair(folder: Path, *, dry_run: bool) -> None:
    folder = folder.expanduser().resolve()
    checked, groups, snapshots = plan(folder)
    backup = folder.with_name(folder.name + "-duplicate-backup")
    if groups and (backup.exists() or backup.is_symlink()):
        raise RepairError(f"Backup already exists: {backup}")
    for group in groups:
        print(json.dumps(group, ensure_ascii=True))
    count = sum(len(group["redundant"]) for group in groups)
    print(f"Files checked: {checked}; duplicate groups: {len(groups)}; redundant files: {count}")
    if count:
        print(f"Backup: {backup}")
    if dry_run:
        print("Dry run: no files moved.")
    elif groups:
        apply(folder, backup, groups, snapshots)
        print(f"Moved {count} redundant files and their sidecars.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder", type=Path, help="Prepared extraction with aligned sidecar filenames"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without moving files")
    args = parser.parse_args(argv)
    try:
        repair(args.folder, dry_run=args.dry_run)
    except (RepairError, ValidationError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
