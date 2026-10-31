"""Separate duplicate Takeout album titles using their source album dates (UTC)."""

import argparse
import json
import stat
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

from immich_ctl.album_names import dated_names
from immich_ctl.folder_compat import JSON_LIMIT, native_sidecar_issues
from immich_ctl.folder_repair import (
    Change,
    RepairError,
    invalid_constant,
    unique_object,
    write_change,
)
from immich_ctl.folder_validate import ValidationError, inspect_folder
from immich_ctl.takeout import google_photos_path, inspect_archives, media_type
from immich_ctl.takeout_metadata import album_expectation, differences
from immich_ctl.takeout_recover import digest


def repair(
    archives: list[Path],
    folders: list[Path],
    mapping_file: Path,
    *,
    titles: set[str] | None = None,
    dry_run: bool = False,
) -> None:
    archives = sorted({p.expanduser().resolve() for p in archives})
    folders = sorted({p.expanduser().resolve() for p in folders})
    # Do not resolve a mapping symlink into an unrelated file.
    mapping_file = mapping_file.expanduser().absolute()
    if (
        mapping_file.is_symlink()
        or mapping_file.resolve() in archives
        or any(mapping_file.resolve().is_relative_to(folder) for folder in folders)
    ):
        raise RepairError("The mapping must be outside the extracted folders and source ZIPs.")
    _, _, source_titles, _, _, _, coverage = inspect_archives(archives)
    mapping = dated_names(coverage, source_titles, titles)
    if not mapping:
        raise RepairError("No duplicate album titles found.")
    expected_mapping = (json.dumps(mapping, ensure_ascii=True, indent=2) + "\n").encode()
    if mapping_file.exists() and mapping_file.read_bytes() != expected_mapping:
        raise RepairError("Existing mapping differs; choose a new --mapping path.")
    old_titles = {source_titles[d] or d for d in mapping}
    source_albums = {
        d: album_expectation(dict(coverage.album_metadata[d][0], title=source_titles[d] or d))
        for d in mapping
    }

    local = []
    for folder in folders:
        media, warnings = inspect_folder(folder)
        if warnings:
            raise RepairError("Folder inspection needs attention: " + "; ".join(warnings))
        local.extend(media)
    # Match bytes, never renamed filenames or dates, to the source album directory.
    members: dict[str, set[str]] = defaultdict(set)
    for archive_path in archives:
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if relative is None or entry.is_dir() or str(relative.parent) not in mapping:
                    continue
                if media_type(str(relative)) == "Other":
                    continue
                with archive.open(entry) as stream:
                    members[digest(stream)].add(str(relative.parent))

    changes = []
    unresolved = []
    checked = 0
    seen: set[Path] = set()
    for media_path, _ in local:
        if media_path in seen:
            continue
        seen.add(media_path)
        checked += 1
        sidecar = next(
            p
            for p in media_path.parent.iterdir()
            if p.suffix.casefold() == ".json" and p.with_suffix("").name == media_path.name
        )
        with sidecar.open("rb") as stream:
            original = stream.read(JSON_LIMIT + 1)
        if len(original) > JSON_LIMIT:
            raise RepairError("Oversized JSON sidecar: " + str(sidecar))
        data = json.loads(
            original, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
        issues = native_sidecar_issues(media_path, data, payload=original)
        if issues:
            raise RepairError(str(sidecar) + ": " + "; ".join(issues))
        albums = data.get("albums", [])
        if not isinstance(albums, list) or any(
            not isinstance(a, dict) or not isinstance(a.get("title"), str) for a in albums
        ):
            raise RepairError("Invalid native albums: " + str(sidecar))
        relevant = [a for a in albums if a["title"] in old_titles | set(mapping.values())]
        with media_path.open("rb") as stream:
            checksum = digest(stream)
        directories = members.get(checksum, set())
        if not relevant and not directories:
            continue
        remaining = list(relevant)
        for directory in sorted(directories):
            expected = source_albums[directory]
            matches = [
                a
                for a in remaining
                if a["title"] in {expected["title"], mapping[directory]}
                and not differences({k: v for k, v in expected.items() if k != "title"}, a)
                and all(k in expected or k == "title" for k in a)
            ]
            if not matches:
                unresolved.append(str(media_path))
                break
            match = matches[0]
            remaining.remove(match)
            match["title"] = mapping[directory]
        if remaining or not directories:
            unresolved.append(str(media_path))
            continue
        updated = (json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode()
        # Avoid rewriting an already repaired sidecar, including its formatting.
        if json.loads(original) == data:
            continue
        if len(updated) > JSON_LIMIT:
            raise RepairError("Rewritten JSON exceeds size limit: " + str(sidecar))
        changes.append(
            Change(media_path, sidecar, original, updated, "", stat.S_IMODE(sidecar.stat().st_mode))
        )
    for directory, title in mapping.items():
        print(json.dumps({"source_directory": directory, "new_title": title}))
    if unresolved:
        print(f"Media checked: {checked}; planned sidecar updates: {len(changes)}")
        raise RepairError(
            "No files changed. Unresolved source album identity or metadata: "
            + json.dumps(sorted(set(unresolved)))
        )
    if not dry_run:
        if not mapping_file.exists():
            with mapping_file.open("xb") as stream:
                mapping_file.chmod(0o600)
                stream.write(expected_mapping)
        for change in changes:
            write_change(change)
    print(
        f"Media checked: {checked}; sidecars {'would update' if dry_run else 'updated'}: {len(changes)}"
    )
    print("Mapping: " + str(mapping_file))
    if dry_run:
        print("Dry run: no files changed or mapping written.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path, metavar="ZIP")
    parser.add_argument(
        "--extracted", required=True, action="append", type=Path, metavar="DIRECTORY"
    )
    parser.add_argument(
        "--mapping",
        required=True,
        type=Path,
        help="New/reusable JSON mapping outside upload folders",
    )
    parser.add_argument(
        "--title", action="append", help="Only this duplicate title; repeat as needed"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        repair(
            args.archives,
            args.extracted,
            args.mapping,
            titles=set(args.title) if args.title else None,
            dry_run=args.dry_run,
        )
    except (RepairError, ValidationError, ValueError, OSError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
