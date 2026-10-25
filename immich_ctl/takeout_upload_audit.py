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
from immich_ctl.takeout_metadata import expectation
from immich_ctl.takeout_reconcile import source_rows


def sha1(stream: Any) -> str:
    checksum = hashlib.sha1()
    while chunk := stream.read(1024 * 1024):
        checksum.update(chunk)
    return checksum.hexdigest()


def archive_inputs(
    paths: list[Path], selection: tuple[date, date], include_partner: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    *_, warnings, _sidecars, coverage = inspect_archives(paths)
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
                continue
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
                or value.get("isTrashed", False)
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
                changes, unchecked, checked = compare(row["metadata"], asset, set())
                unchecked.extend(row["issues"])
                if not checked:
                    unchecked.append("no comparable source metadata")
                if len(identical) > 1:
                    unchecked.append("multiple server assets share this checksum")
                existing.append(
                    {
                        **public_input(row),
                        "asset_id": asset["id"],
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
    for checksum, copies in sorted(input_checksums.items()):
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
    return {
        "owner_id": owner,
        "server_assets": len(inventory),
        "input_files": len(inputs),
        "existing_checksum": existing,
        "heuristic_matches": heuristic,
        "within_input_matches": within_input,
        "unresolved_inputs": unresolved,
        "input_metadata_conflicts": metadata_conflicts,
        "summary": {
            "existing_checksum_files": len({r["path"] for r in existing}),
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
    report.update(
        input_kind="extracted_folders" if folders else "takeout_candidates",
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
    print(
        f"Server assets: {report['server_assets']:,}; input files hashed: {report['input_files']:,}"
    )
    print("\n| Check | Matches |\n|---|---:|")
    for key, label in (
        ("existing_checksum_files", "Existing checksums: metadata will be retained"),
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
    print("Different bytes do not prove different photographs; size does not prove quality.")
    print(
        "Server scope: owned uploaded assets; locked, trashed and external-library assets excluded."
    )
    print(
        "Archive candidates do not predict filename/sidecar matching or extracted-folder names; use --extracted to audit the prepared upload folders."
    )
    print(
        "Snapshot warnings only: concurrent uploads, server jobs and ordering can change decisions. No uploads, metadata writes, job commands or deletions were made."
    )
