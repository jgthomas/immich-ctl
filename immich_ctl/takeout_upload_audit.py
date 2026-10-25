"""Read-only warnings for immich-go 0.32.0 matching and retained metadata."""

import base64
import hashlib
import json
import re
import zipfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import UTC, date
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from immich_ctl.folder_compat import JSON_LIMIT, sidecar_state
from immich_ctl.folder_validate import Client, ValidationError, compare, inspect_folder, timestamp
from immich_ctl.takeout import FileKey, google_photos_path, inspect_archives
from immich_ctl.takeout_metadata import duplicate_album_titles, expectation
from immich_ctl.takeout_reconcile import source_rows


def sha1(stream: Any) -> str:
    checksum = hashlib.sha1()
    while chunk := stream.read(1024 * 1024):
        checksum.update(chunk)
    return checksum.hexdigest()


def archive_inputs(
    paths: list[Path], selection: tuple[date, date], include_partner: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    _, memberships, titles, _, warnings, _sidecars, coverage = inspect_archives(paths)
    sidecar_owners: defaultdict[str, set[Any]] = defaultdict(set)
    for owner, names in coverage.item_paths.items():
        for name in names:
            sidecar_owners[name].add(owner)
    rows, _ = source_rows(coverage, selection)
    wanted = {name for row in rows for name in row["sidecars"]}
    metadata: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid = set()
    result = []
    with ExitStack() as stack:
        media: dict[FileKey, tuple[zipfile.ZipFile, zipfile.ZipInfo]] = {}
        for path in paths:
            archive = stack.enter_context(zipfile.ZipFile(path))
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if relative is None or entry.is_dir():
                    continue
                name = str(relative)
                media.setdefault((name, entry.CRC, entry.file_size), (archive, entry))
                if name not in wanted:
                    continue
                if entry.file_size > JSON_LIMIT:
                    invalid.add(name)
                    continue
                try:
                    data = json.loads(archive.read(entry))
                except (ValueError, UnicodeError):
                    invalid.add(name)
                    continue
                if not isinstance(data, dict):
                    invalid.add(name)
                elif data not in metadata[name]:
                    metadata[name].append(data)
        for row in rows:
            issues = list(row["issues"])
            versions = [data for name in row["sidecars"] for data in metadata[name]]
            consistent = (
                bool(versions)
                and not invalid.intersection(row["sidecars"])
                and all(data == versions[0] for data in versions)
            )
            if not consistent:
                issues.append("missing, invalid or conflicting source metadata")
            data = versions[0] if consistent else {}
            origin = data.get("googlePhotosOrigin", {})
            if consistent and (
                data.get("trashed")
                or (
                    not include_partner
                    and isinstance(origin, dict)
                    and "fromPartnerSharing" in origin
                )
            ):
                continue
            expected = {}
            if consistent and not issues:
                try:
                    expected = expectation(data, {})
                    expected.pop("albums")  # Membership can be added; this checks retained fields.
                    if data.get("people"):
                        issues.append("Google people tags are not checked")
                except ValueError as error:
                    issues.append(str(error))
            key = (row["path"], int(row["crc32"], 16), row["size"])
            archive, entry = media[key]
            with archive.open(entry) as stream:
                checksum = sha1(stream)
            title = data.get("title")
            owners = set().union(*(sidecar_owners[name] for name in row["sidecars"]))
            original = (
                re.sub(r'[\r\n\\/:*?"<>|]', "_", title)
                if isinstance(title, str) and title
                else PurePosixPath(row["path"]).name
            )
            result.append(
                {
                    "path": row["path"],
                    "name": PurePosixPath(row["path"]).name,
                    "original_filename": original,
                    "size": row["size"],
                    "sha1": checksum,
                    "capture_time": row["capture_times"][0]
                    if len(row["capture_times"]) == 1
                    else None,
                    "metadata": expected,
                    "issues": issues,
                    "albums": sorted(
                        {
                            titles[d] or d
                            for d, members in memberships.items()
                            if d in titles and owners & members
                        }
                    ),
                }
            )
    return result, [f"{message}: {count}" for message, count in sorted(warnings.items())]


def folder_inputs(
    folders: list[Path], selection: tuple[date, date]
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    cache: dict[Path, dict[str, list[Path]]] = {}
    for folder in sorted({p.expanduser().resolve() for p in folders}):
        files, notes = inspect_folder(folder)
        warnings.extend(str(folder) + ": " + note for note in notes)
        for path, data in files:
            state = sidecar_state(path, cache)
            issues = [] if state == "present_unverified" else [state + " native sidecar"]
            captured = timestamp(data.get("dateTaken")) if data is not None and not issues else None
            if captured is not None:
                captured = captured.astimezone(UTC)
            if captured is not None and not selection[0] <= captured.date() <= selection[1]:
                warnings.append(
                    "Outside requested dates; included in folder upload audit: " + str(path)
                )
            if captured is None:
                issues.append("capture time unavailable; filename/time matching cannot be assessed")
            with path.open("rb") as stream:
                checksum = sha1(stream)
            expected = {
                key: value
                for key, value in (data or {}).items()
                if key
                in {"dateTaken", "description", "latitude", "longitude", "favorited", "archived"}
            }
            _, unchecked, _ = compare(expected, {}, set())
            issues.extend(note for note in unchecked if note.startswith("invalid sidecar"))
            if data and data.get("tags"):
                issues.append("native tags are not checked")
            albums = (data or {}).get("albums", [])
            if not isinstance(albums, list) or any(
                not isinstance(album, dict)
                or not isinstance(album.get("title"), str)
                or not album["title"]
                for album in albums
            ):
                issues.append("invalid native albums")
                albums = []
            original = (data or {}).get("fileName", path.name)
            if not isinstance(original, str) or not original:
                original = path.name
                issues.append("native original filename unavailable")
            rows.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "original_filename": original,
                    "size": path.stat().st_size,
                    "sha1": checksum,
                    "capture_time": captured.isoformat() if captured else None,
                    "metadata": expected,
                    "issues": issues,
                    "native_sidecar": True,
                    "albums": sorted({album["title"] for album in albums}),
                }
            )
    return rows, warnings


def size_decision(incoming: int, existing: Any) -> str:
    if type(existing) is not int or existing < 0:
        return "unknown_size"
    if incoming > existing:
        return "replace_larger"
    return "skip_equal" if incoming == existing else "skip_smaller"


def match_window(incoming: Any, existing: Any) -> bool | None:
    a, b = timestamp(incoming), timestamp(existing)
    if a is None or b is None:
        return None
    return -5 <= (a - b).total_seconds() < 5


def public_input(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("path", "original_filename", "size", "sha1", "capture_time")}


def destination_album_collisions(client: Client, titles: set[str]) -> list[dict[str, Any]]:
    if not titles:
        return []
    albums = client.read("/albums")
    if not isinstance(albums, list) or any(
        not isinstance(album, dict)
        or not isinstance(album.get("id"), str)
        or not album["id"]
        or not isinstance(album.get("albumName"), str)
        for album in albums
    ):
        raise ValidationError("Unexpected album inventory; upload audit is incomplete.")
    groups: defaultdict[str, set[str]] = defaultdict(set)
    for album in albums:
        if album["albumName"] in titles:
            groups[album["albumName"]].add(album["id"])
    return [
        {"title": title, "album_ids": sorted(identities)}
        for title, identities in sorted(groups.items())
        if len(identities) > 1
    ]


def server_report(inputs: list[dict[str, Any]], client: Client) -> dict[str, Any]:
    user = client.read("/users/me")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
        raise ValidationError("Unexpected account response: no user ID.")
    owner = user["id"]
    inventory = [a for a in client.inventory(owner) if not a.get("libraryId")]
    by_checksum: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in inventory:
        if not isinstance(asset.get("originalFileName"), str) or not asset["originalFileName"]:
            raise ValidationError(
                "Server inventory lacks an original filename; audit is incomplete."
            )
        by_checksum[base64.b64decode(asset["checksum"], validate=True).hex()].append(asset)
        by_name[asset["originalFileName"]].append(asset)
    details = {}

    def detail(stub: dict[str, Any]) -> dict[str, Any]:
        identity = stub["id"]
        if identity not in details:
            value = client.read("/assets/" + quote(identity, safe=""))
            if (
                not isinstance(value, dict)
                or value.get("id") != identity
                or value.get("ownerId") != owner
                or value.get("checksum") != stub["checksum"]
                or value.get("originalFileName") != stub["originalFileName"]
                or bool(value.get("isTrashed", False)) != bool(stub.get("isTrashed", False))
                or value.get("libraryId")
            ):
                raise ValidationError(
                    "Asset changed during the audit; retry with a fresh snapshot."
                )
            details[identity] = value
        return details[identity]

    existing, heuristic, unresolved = [], [], []
    for row in inputs:
        identical = by_checksum.get(row["sha1"], [])
        if identical:
            for stub in identical:
                asset = detail(stub)
                changes, unchecked, checked = compare(
                    row["metadata"], asset, set(), native=row.get("native_sidecar", False)
                )
                unchecked.extend(row["issues"])
                if asset.get("isTrashed", False):
                    unchecked.append(
                        "matching checksum is in trash; upload may skip creating an active asset"
                    )
                if not checked:
                    unchecked.append("no comparable source metadata")
                if len(identical) > 1:
                    unchecked.append("multiple server assets share this checksum")
                existing.append(
                    {
                        **public_input(row),
                        "asset_id": asset["id"],
                        "server_trashed": bool(asset.get("isTrashed", False)),
                        "differences": changes,
                        "unchecked": unchecked,
                    }
                )
            continue  # Checksum takes precedence over name/time, as in immich-go.
        if row["issues"]:
            unresolved.append({**public_input(row), "issues": row["issues"]})
        for stub in by_name.get(row["name"], []):
            asset = detail(stub)
            exif = asset.get("exifInfo")
            exif = exif if isinstance(exif, dict) else {}
            matched = match_window(row["capture_time"], exif.get("dateTimeOriginal"))
            if matched is False:
                continue
            decision = size_decision(row["size"], exif.get("fileSizeInByte"))
            heuristic.append(
                {
                    **public_input(row),
                    "asset_id": asset["id"],
                    "server_trashed": bool(asset.get("isTrashed", False)),
                    "server_filename": asset["originalFileName"],
                    "server_capture_time": exif.get("dateTimeOriginal"),
                    "server_size": exif.get("fileSizeInByte"),
                    "server_checksum": base64.b64decode(asset["checksum"], validate=True).hex(),
                    "decision": decision if matched else "unknown_time",
                }
            )
    # Assets uploaded earlier in the same run are indexed by their stored original
    # name. This can differ from the next input's physical basename (edited pairs).
    new = [r for r in inputs if r["sha1"] not in by_checksum]
    originals: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in new:
        originals[row["original_filename"]].append(row)
    within_input = []
    for row in new:
        for other in originals.get(row["name"], []):
            if other["sha1"] == row["sha1"]:
                continue
            matched = match_window(row["capture_time"], other["capture_time"])
            if matched is False:
                continue
            within_input.append(
                {
                    **public_input(row),
                    "earlier_input": public_input(other),
                    "decision": size_decision(row["size"], other["size"])
                    if matched
                    else "unknown_time",
                }
            )
    input_checksums: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inputs:
        input_checksums[row["sha1"]].append(row)
    metadata_conflicts = []
    album_conflicts = []
    for checksum, copies in sorted(input_checksums.items()):
        album_sets = {tuple(row["albums"]) for row in copies}
        if len(album_sets) > 1:
            album_conflicts.append(
                {
                    "sha1": checksum,
                    "required_albums": sorted({title for names in album_sets for title in names}),
                    "copies": [{"path": row["path"], "albums": row["albums"]} for row in copies],
                }
            )
        fields = set().union(*(r["metadata"].keys() for r in copies))
        conflicting = []
        for field in sorted(fields):
            values = [
                timestamp(r["metadata"][field]) if field == "dateTaken" else r["metadata"][field]
                for r in copies
                if field in r["metadata"]
            ]
            if any(value != values[0] for value in values[1:]):
                conflicting.append(field)
        if conflicting:
            metadata_conflicts.append(
                {
                    "sha1": checksum,
                    "fields": conflicting,
                    "paths": sorted({r["path"] for r in copies}),
                }
            )
    counts = Counter(r["decision"] for r in heuristic)
    destination_collisions = destination_album_collisions(
        client, {title for row in inputs for title in row["albums"]}
    )
    return {
        "owner_id": owner,
        "server_assets": len(inventory),
        "input_files": len(inputs),
        "existing_checksum": existing,
        "heuristic_matches": heuristic,
        "within_input_matches": within_input,
        "unresolved_inputs": unresolved,
        "input_metadata_conflicts": metadata_conflicts,
        "input_album_conflicts": album_conflicts,
        "destination_album_title_collisions": destination_collisions,
        "summary": {
            "existing_checksum_files": len({r["path"] for r in existing}),
            "trashed_checksum_matches": sum(r["server_trashed"] for r in existing),
            "metadata_difference_matches": sum(bool(r["differences"]) for r in existing),
            "metadata_unchecked_matches": sum(bool(r["unchecked"]) for r in existing),
            **{
                name: counts[name]
                for name in (
                    "replace_larger",
                    "skip_equal",
                    "skip_smaller",
                    "unknown_time",
                    "unknown_size",
                )
            },
            "within_input_matches": len(within_input),
            "unresolved_inputs": len(unresolved),
            "input_metadata_conflicts": len(metadata_conflicts),
            "input_album_conflicts": len(album_conflicts),
            "destination_album_title_collisions": len(destination_collisions),
        },
    }


def upload_audit(
    paths: list[Path],
    client: Client,
    *,
    selection: tuple[date, date] | None = None,
    folders: list[Path] | None = None,
    include_partner: bool = False,
) -> dict[str, Any]:
    selection = selection or (date.min, date.max)
    inputs, warnings = (
        folder_inputs(folders, selection)
        if folders
        else archive_inputs(paths, selection, include_partner)
    )
    report = server_report(inputs, client)
    source_collisions = duplicate_album_titles(inspect_archives(paths)[2]) if paths else []
    relevant_titles = {title for row in inputs for title in row["albums"]}
    source_collisions = [c for c in source_collisions if c["title"] in relevant_titles]
    report["source_album_title_collisions"] = source_collisions
    report["summary"]["source_album_title_collisions"] = len(source_collisions)
    report.update(
        input_kind="extracted_folders" if folders else "takeout_candidates",
        date_filter_applied=not bool(folders),
        date_range=[d.isoformat() for d in selection],
        input_warnings=warnings,
        read_only=True,
        immich_go_version="0.32.0",
        has_warnings=bool(
            warnings
            or report["unresolved_inputs"]
            or report["heuristic_matches"]
            or report["within_input_matches"]
            or report["input_metadata_conflicts"]
            or report["input_album_conflicts"]
            or report["source_album_title_collisions"]
            or report["destination_album_title_collisions"]
            or any(r["differences"] or r["unchecked"] for r in report["existing_checksum"])
        ),
    )
    return report


def redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secret) for key, item in value.items()}
    return value


def print_server_report(report: dict[str, Any], details: bool) -> None:
    print("\nImmich upload-risk audit (read-only; immich-go 0.32.0)")
    print("Inputs: " + report["input_kind"] + "; UTC dates: " + " to ".join(report["date_range"]))
    if not report["date_filter_applied"]:
        print("Folder upload scope: all media, including files outside the requested dates.")
    print(
        f"Server assets: {report['server_assets']:,}; input files hashed: {report['input_files']:,}"
    )
    print("\n| Check | Matches |\n|---|---:|")
    for key, label in (
        ("existing_checksum_files", "Existing checksums: metadata will be retained"),
        ("trashed_checksum_matches", "Existing checksums in trash: may not create an active asset"),
        ("metadata_difference_matches", "Existing metadata differs and will not be updated"),
        ("metadata_unchecked_matches", "Existing metadata could not be fully compared"),
        ("replace_larger", "Larger incoming file: potential replacement and permanent deletion"),
        ("skip_equal", "Equal size, different bytes: potential skip and album association"),
        ("skip_smaller", "Smaller incoming file: potential skip and album association"),
        ("unknown_time", "Name matches with unknown capture times"),
        ("unknown_size", "Name/time matches with unknown server size"),
        ("within_input_matches", "Potential name/time collisions within this input"),
        ("unresolved_inputs", "Inputs with unverified metadata or matching"),
        ("input_metadata_conflicts", "Identical input bytes with conflicting metadata"),
        ("input_album_conflicts", "Identical input bytes with differing album memberships"),
        ("source_album_title_collisions", "Ambiguous source album titles used by this input"),
        (
            "destination_album_title_collisions",
            "Ambiguous destination album titles used by this input",
        ),
    ):
        print(f"| {label} | {report['summary'][key]:,} |")
    for key in (
        "existing_checksum",
        "heuristic_matches",
        "within_input_matches",
        "unresolved_inputs",
    ):
        for row in report[key]:
            if details or key != "existing_checksum" or row["differences"] or row["unchecked"]:
                print(json.dumps({"check": key, **row}, ensure_ascii=True))
    for warning in report["input_warnings"]:
        print("Input warning: " + json.dumps(warning))
    for conflict in report["input_metadata_conflicts"]:
        print(json.dumps({"check": "input_metadata_conflicts", **conflict}, ensure_ascii=True))
    for key in (
        "input_album_conflicts",
        "source_album_title_collisions",
        "destination_album_title_collisions",
    ):
        for finding in report[key]:
            print(json.dumps({"check": key, **finding}, ensure_ascii=True))
    if (
        report["input_album_conflicts"]
        or report["source_album_title_collisions"]
        or report["destination_album_title_collisions"]
    ):
        print(
            "Album warnings: choose distinct titles or accept a merge; consolidate differing memberships before upload. No albums were changed."
        )
    print("Different bytes do not prove different photographs; size does not prove quality.")
    print(
        "Server scope: owned uploaded assets including trash; locked and external-library assets excluded."
    )
    print(
        "Archive candidates do not predict filename/sidecar matching or extracted-folder names; use --extracted to audit the prepared upload folders."
    )
    print(
        "Snapshot warnings only: concurrent uploads, server jobs and ordering can change decisions. No uploads, metadata writes, job commands or deletions were made."
    )
