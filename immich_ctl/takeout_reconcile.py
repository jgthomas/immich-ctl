"""Read-only reconciliation of Takeout source media against extracted folders."""

import json
import zipfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from immich_ctl.takeout import (
    Coverage,
    FileKey,
    ItemIdentity,
    edited_original_path,
    google_photos_path,
    inspect_archives,
    media_type,
)
from immich_ctl.takeout_metadata import album_payloads, differences, expectation
from immich_ctl.takeout_recover import digest, folder_checksums

JSON_LIMIT = 8 * 1024 * 1024


def source_rows(
    coverage: Coverage, selection: tuple[date, date]
) -> tuple[list[dict[str, Any]], int]:
    """Assign dates only through an unambiguous own or exact original association."""
    files = coverage.matched_files | coverage.unmatched_files
    filenames: defaultdict[str, set[FileKey]] = defaultdict(set)
    for key in files:
        filenames[key[0]].add(key)
    rows = []
    outside = 0
    for key in sorted(files):
        if media_type(key[0]) == "Other":
            continue
        owners = coverage.file_owners.get(key, set())
        failed = set(coverage.failed_sidecars.get(key, set()))
        association = "direct" if key in coverage.matched_files else "unmatched"
        if failed or (owners and association != "direct"):
            association = "ambiguous"
        if not owners and not failed:
            original = edited_original_path(PurePosixPath(key[0]))
            originals = filenames.get(str(original), set()) if original is not None else set()
            if (
                len(originals) == 1
                and originals <= coverage.matched_files
                and not originals.intersection(coverage.failed_sidecars)
            ):
                owners = coverage.file_owners[next(iter(originals))]
                association = "candidate_edit"
            elif originals:
                owners = set().union(*(coverage.file_owners.get(k, set()) for k in originals))
                failed.update(
                    set().union(*(coverage.failed_sidecars.get(k, set()) for k in originals))
                )
                association = "ambiguous"
        times = set().union(*(coverage.capture_times[owner] for owner in owners))
        issues = []
        if failed:
            issues.append("unusable source sidecar")
        if association in {"ambiguous", "unmatched"}:
            issues.append(
                "ambiguous association" if association == "ambiguous" else "no metadata association"
            )
        if len(times) != 1:
            issues.append("conflicting capture dates" if times else "unknown capture date")
        dated = association in {"direct", "candidate_edit"} and len(owners) == 1 and len(times) == 1
        if dated and not selection[0] <= next(iter(times)).date() <= selection[1]:
            outside += 1
            continue
        rows.append(
            {
                "path": key[0],
                "crc32": f"{key[1]:08x}",
                "size": key[2],
                "association": association,
                "scope": "selected" if dated else "export_exception",
                "capture_times": sorted(t.isoformat() for t in times),
                "sidecars": sorted(
                    set().union(*(coverage.item_paths[owner] for owner in owners)) | failed
                ),
                "failed_sidecars": sorted(failed),
                "issues": issues,
            }
        )
    return rows, outside


def sidecar_state(
    media: Path,
    cache: dict[Path, dict[str, list[Path]]],
    values: dict[Path, dict[str, Any]] | None = None,
) -> str:
    """Read a bounded native sidecar, retaining its data for source comparison."""
    if media.parent not in cache:
        names: defaultdict[str, list[Path]] = defaultdict(list)
        for path in media.parent.iterdir():
            if path.suffix.casefold() == ".json":
                names[path.with_suffix("").name].append(path)
        cache[media.parent] = dict(names)
    candidates = cache[media.parent].get(media.name, [])
    if not candidates:
        return "missing"
    if len(candidates) != 1:
        return "ambiguous"
    with candidates[0].open("rb") as stream:
        payload = stream.read(JSON_LIMIT + 1)
    try:
        value = json.loads(payload) if len(payload) <= JSON_LIMIT else None
    except (ValueError, UnicodeError):
        return "invalid"
    if isinstance(value, dict):
        if values is not None:
            values[media] = value
        return "present_unverified"
    return "invalid"


def reconcile_report(
    paths: list[Path],
    folders: list[Path],
    selection: tuple[date, date],
    *,
    include_partner: bool = False,
) -> dict[str, Any]:
    paths = sorted({p.expanduser().resolve() for p in paths})
    folders = sorted({p.expanduser().resolve() for p in folders})
    if not folders:
        raise ValueError("Reconciliation requires at least one --extracted folder.")
    local: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    sidecars: dict[Path, dict[str, list[Path]]] = {}
    local_metadata: dict[Path, dict[str, Any]] = {}
    for folder in folders:
        for checksum, relatives in folder_checksums(folder).items():
            for local_relative in relatives:
                local[checksum].append(
                    {
                        "folder": str(folder),
                        "path": local_relative,
                        "sidecar": sidecar_state(folder / local_relative, sidecars, local_metadata),
                    }
                )
    _, memberships, titles, _, warnings, _, coverage = inspect_archives(paths)
    sidecar_owners: defaultdict[str, set[ItemIdentity]] = defaultdict(set)
    for owner, item_paths in coverage.item_paths.items():
        for name in item_paths:
            sidecar_owners[name].add(owner)
    rows, outside = source_rows(coverage, selection)
    unclassified = [
        {"path": key[0], "crc32": f"{key[1]:08x}", "size": key[2]}
        for key in sorted(coverage.matched_files | coverage.unmatched_files)
        if media_type(key[0]) == "Other"
    ]
    if unclassified:
        warnings["Unclassified source files (not checked as media)"] += len(unclassified)
    missing_items = []
    for owner in sorted(coverage.missing_items):
        times = coverage.capture_times[owner]
        if len(times) == 1 and not selection[0] <= next(iter(times)).date() <= selection[1]:
            continue
        missing_items.append(
            {
                "scope": "selected" if len(times) == 1 else "export_exception",
                "sidecars": sorted(coverage.item_paths[owner]),
                "capture_times": sorted(t.isoformat() for t in times),
            }
        )
    wanted = {sidecar for row in rows for sidecar in row["sidecars"]}
    metadata: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid: set[str] = set()
    expectations: dict[str, dict[str, Any]] = {}
    album_sources: dict[str, dict[str, dict[str, Any]]] = {}
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
                    value = json.loads(archive.read(entry))
                except (ValueError, UnicodeError):
                    invalid.add(name)
                    continue
                if not isinstance(value, dict):
                    invalid.add(name)
                elif value not in metadata[name]:
                    metadata[name].append(value)
        for row in rows:
            versions = [data for name in row["sidecars"] for data in metadata[name]]
            if invalid.intersection(row["sidecars"]):
                row["issues"].append("invalid source metadata")
            if versions and any(data != versions[0] for data in versions):
                row["issues"].append("conflicting source metadata")
            if row["sidecars"] and not versions:
                row["issues"].append("missing source metadata")
            key = (row["path"], int(row["crc32"], 16), row["size"])
            archive, entry = media[key]
            with archive.open(entry) as stream:
                checksum = digest(stream)
            row["sha256"] = checksum
            row["local_matches"] = sorted(
                local.get(checksum, []), key=lambda p: (p["folder"], p["path"])
            )
            row["content"] = "present" if row["local_matches"] else "missing"
            owners = set().union(*(sidecar_owners[name] for name in row["sidecars"]))
            directories = [
                directory
                for directory in set(titles) | coverage.invalid_album_metadata
                if owners & memberships.get(directory, set())
            ]
            row["albums"] = sorted({titles.get(d, d) for d in directories})
            try:
                album_sources[row["path"]] = album_payloads(coverage, directories)
                if len(owners) == 1 and versions and not row["issues"]:
                    expectations[row["path"]] = expectation(versions[0], album_sources[row["path"]])
            except ValueError as error:
                row["issues"].append(str(error))
            data = versions[0] if versions else {}
            origin = data.get("googlePhotosOrigin", {})
            excluded = bool(data.get("trashed")) or (
                not include_partner and isinstance(origin, dict) and "fromPartnerSharing" in origin
            )
            for state in sorted(
                {p["sidecar"] for p in row["local_matches"]} - {"present_unverified"}
            ):
                row["issues"].append(f"{state} local JSON sidecar")
            row["status"] = (
                "needs_review" if row["issues"] else "excluded" if excluded else row["content"]
            )
    # Identical media may legitimately occur in several albums. Require compatible
    # source fields and combine memberships before comparing every local copy.
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["sha256"]].append(row)
    for copies in groups.values():
        if any(row["issues"] for row in copies) and len(copies) > 1:
            for row in copies:
                if not row["issues"]:
                    row["issues"].append("identical media has unresolved source metadata")
        candidates = [expectations[r["path"]] for r in copies if r["path"] in expectations]
        merged: dict[str, Any] = {}
        for candidate in candidates:
            for field, value in candidate.items():
                if field == "albums":
                    continue
                if field in merged and merged[field] != value:
                    for row in copies:
                        row["issues"].append("identical media has conflicting source " + field)
                merged[field] = value
        album_versions: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            for album in candidate["albums"]:
                title = album["title"]
                if title in album_versions and album_versions[title] != album:
                    for row in copies:
                        row["issues"].append("conflicting source album metadata: " + title)
                album_versions[title] = album
        merged["albums"] = list(album_versions.values())
        for row in copies:
            row["metadata_differences"] = []
            row["metadata_verified"] = False
            if row["path"] in expectations and not row["issues"] and row["status"] != "excluded":
                for match in row["local_matches"]:
                    native = local_metadata.get(Path(match["folder"]) / match["path"])
                    if native is not None:
                        for issue in differences(merged, native):
                            row["metadata_differences"].append(
                                {
                                    "folder": match["folder"],
                                    "path": match["path"],
                                    "issue": issue,
                                }
                            )
                row["metadata_verified"] = (
                    bool(row["local_matches"]) and not row["metadata_differences"]
                )
                row["issues"].extend(sorted({d["issue"] for d in row["metadata_differences"]}))
            if row["issues"]:
                row["status"] = "needs_review"
    summaries = {}
    for scope in ("selected", "export_exception"):
        scoped = [row for row in rows if row["scope"] == scope]
        counts = Counter(row["status"] for row in scoped)
        summaries[scope] = {
            "files": len(scoped),
            "unique_checksums": len({r["sha256"] for r in scoped}),
            **{name: counts[name] for name in ("present", "missing", "needs_review", "excluded")},
            "content_present": sum(r["content"] == "present" for r in scoped),
            "content_missing": sum(r["content"] == "missing" for r in scoped),
        }
    return {
        "date_range": [d.isoformat() for d in selection],
        "archives": [str(p) for p in paths],
        "extracted_folders": [str(p) for p in folders],
        "include_partner": include_partner,
        "source_media_files": sum(
            media_type(k[0]) != "Other" for k in coverage.matched_files | coverage.unmatched_files
        ),
        "outside_date_files": outside,
        **summaries,
        "files": rows,
        "metadata_without_media": missing_items,
        "warnings": dict(sorted(warnings.items())),
        "unclassified_source_files": unclassified,
        "metadata_and_albums_validated": bool(rows)
        and all(row["metadata_verified"] or row["status"] == "excluded" for row in rows)
        and not missing_items
        and not unclassified
        and not warnings,
        "metadata_scope": [
            "dateTaken",
            "description",
            "latitude",
            "longitude",
            "favorited",
            "archived",
            "albums",
        ],
    }


def reconcile(
    paths: list[Path],
    folders: list[Path],
    selection: tuple[date, date],
    *,
    include_partner: bool = False,
    details: bool = False,
    json_output: bool = False,
) -> None:
    report = reconcile_report(paths, folders, selection, include_partner=include_partner)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return
    print("Takeout extraction reconciliation (read-only)")
    print("Capture dates (UTC, inclusive): " + " to ".join(report["date_range"]))
    print("\n| Selected media | Files |\n|---|---:|")
    for key, label in (
        ("present", "Bytes and supported metadata verified"),
        ("missing", "Missing media with consistent source metadata"),
        ("needs_review", "Metadata or sidecars need review"),
        ("excluded", "Excluded by partner/trashed filters"),
        ("files", "Total selected source files"),
    ):
        print(f"| {label} | {report['selected'][key]:,} |")
    print(f"Distinct selected media checksums: {report['selected']['unique_checksums']:,}")
    print(
        f"Export-wide exceptions: {report['export_exception']['files']:,} files; not assigned to the selected dates."
    )
    print("\nSelected files" + ("" if details else " needing attention"))
    for row in report["files"]:
        if row["scope"] == "selected" and (details or row["status"] in {"missing", "needs_review"}):
            print(json.dumps(row, ensure_ascii=True))
    exceptions = [row for row in report["files"] if row["scope"] == "export_exception"]
    if exceptions:
        print("\nExport-wide exceptions (not assigned to the selected dates)")
        counts = Counter(row["association"] for row in exceptions)
        print("| Association | Files |\n|---|---:|")
        for key, label in (
            ("unmatched", "Unpaired media"),
            ("ambiguous", "Ambiguous associations"),
            ("direct", "Direct metadata with unknown/conflicting dates"),
            ("candidate_edit", "Candidate edits with unknown/conflicting dates"),
        ):
            print(f"| {label} | {counts[key]:,} |")
        if details:
            for row in exceptions:
                print(json.dumps(row, ensure_ascii=True))
        else:
            print("Use --details or --json for the full export-wide exceptions list.")
    print(
        f"\nMetadata items without a media filename match: {len(report['metadata_without_media']):,}"
    )
    if details:
        for row in report["metadata_without_media"]:
            print(json.dumps(row, ensure_ascii=True))
    if details:
        for row in report["unclassified_source_files"]:
            print("Unclassified source file: " + json.dumps(row, ensure_ascii=True))
    if report["warnings"]:
        print("Inspection warnings: " + json.dumps(report["warnings"], ensure_ascii=True))
    print(
        "\nScope: local SHA-256 byte coverage and supported source metadata checks. "
        "Album-folder copies count separately; repeated ZIP entries count once. "
        "Supported source fields and exact album associations are compared with generated JSON. Other metadata is outside this check. "
        "This does not check Immich uploads or backups. No files written or server requests."
    )
