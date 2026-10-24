"""Read-only audit of numbered media and edited variants in Takeout ZIPs."""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from immich_ctl.takeout import Coverage, FileKey, edited_original_path, inspect_archives, media_type


def numbered_media(coverage: Coverage) -> list[dict[str, Any]]:
    """Inventory numbered filename patterns without assuming they are duplicates."""
    files = coverage.matched_files | coverage.unmatched_files
    filenames: defaultdict[str, set[FileKey]] = defaultdict(set)
    for key in files:
        filenames[key[0]].add(key)
    rows = []
    for key in sorted(files):
        path = PurePosixPath(key[0])
        if media_type(path.name) == "Other" or re.search(r"\([0-9]+\)", path.stem) is None:
            continue
        original = edited_original_path(path)
        kind = "numbered_edit" if original is not None else "numbered_media"
        own_claims = coverage.file_owners.get(key, set())
        originals = filenames.get(str(original), set()) if original is not None else set()
        association = "missing"
        association_files: set[FileKey] = set()
        if own_claims:
            # Own metadata takes precedence, even when it is ambiguous or undated.
            association = "direct" if key in coverage.matched_files else "ambiguous"
            association_files = {key}
        elif originals:
            association_files = originals
            if len(originals) == 1 and originals <= coverage.matched_files:
                association = "candidate"
            elif len(originals) > 1 or any(coverage.file_owners.get(k) for k in originals):
                association = "ambiguous"
        owners = set().union(*(coverage.file_owners.get(k, set()) for k in association_files))
        sidecars = set().union(*(coverage.file_sidecars.get(k, set()) for k in association_files))
        year, date_status = "Unknown", "unresolved"
        if association in {"direct", "candidate"} and len(owners) == 1:
            times = coverage.capture_times.get(next(iter(owners)), set())
            if len(times) == 1:
                year, date_status = str(next(iter(times)).year), "known"
            else:
                date_status = "conflicting" if times else "missing"
        rows.append(
            {
                "path": key[0],
                "crc32": f"{key[1]:08x}",
                "size": key[2],
                "kind": kind,
                "association": association,
                "year": year,
                "date_status": date_status,
                "potentially_affected": kind == "numbered_edit" and association != "direct",
                "originals": [
                    {"path": k[0], "crc32": f"{k[1]:08x}", "size": k[2]} for k in sorted(originals)
                ],
                "sidecars": sorted(sidecars),
            }
        )
    return rows


def audit_report(paths: list[Path]) -> dict[str, Any]:
    """Read ZIP names and metadata only; media contents are never opened."""
    *_, warnings, _sidecars, coverage = inspect_archives(paths)
    rows = numbered_media(coverage)
    associations = Counter(row["association"] for row in rows)
    years: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        years[row["year"]][row["kind"]] += 1
        years[row["year"]]["potentially_affected"] += int(row["potentially_affected"])
    return {
        "media_files": sum(
            media_type(k[0]) != "Other" for k in coverage.matched_files | coverage.unmatched_files
        ),
        "numbered_files": len(rows),
        "numbered_edits": sum(row["kind"] == "numbered_edit" for row in rows),
        "potentially_affected": sum(row["potentially_affected"] for row in rows),
        "associations": {
            name: associations[name] for name in ("direct", "candidate", "ambiguous", "missing")
        },
        "years": {
            year: {
                name: counts[name]
                for name in ("numbered_media", "numbered_edit", "potentially_affected")
            }
            for year, counts in sorted(years.items())
        },
        "warnings": dict(sorted(warnings.items())),
        "files": rows,
    }


def audit(paths: list[Path], *, details: bool = False, json_output: bool = False) -> None:
    report = audit_report(paths)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return
    print("Numbered filename audit (read-only)")
    print(f"Media files inspected: {report['media_files']:,}")
    print(f"Numbered media files: {report['numbered_files']:,}")
    print(f"Numbered edited variants: {report['numbered_edits']:,}")
    print(f"Potentially affected numbered edits: {report['potentially_affected']:,}")
    print("\n| Metadata association | Files |\n|---|---:|")
    for status, label in (
        ("direct", "Directly matched sidecar"),
        ("candidate", "Candidate original sidecar"),
        ("ambiguous", "Ambiguous association"),
        ("missing", "No metadata association"),
    ):
        print(f"| {label} | {report['associations'][status]:,} |")
    print("\n| Year (UTC) | Originals/other | Edited variants | Potentially affected edits |")
    print("|---|---:|---:|---:|")
    for year, counts in report["years"].items():
        print(
            f"| {year} | {counts['numbered_media']:,} | {counts['numbered_edit']:,} | {counts['potentially_affected']:,} |"
        )
    if details:
        print("\nNumbered files: JSON records with candidate sidecars and association status")
        for row in report["files"]:
            print(json.dumps(row, ensure_ascii=True))
    if report["warnings"]:
        print("\nInspection warnings")
        for message, count in report["warnings"].items():
            print(json.dumps(message, ensure_ascii=True), f": {count:,}")
    print(
        "\nScope: filename patterns, not proof of duplicates or extraction failures. "
        "Only -edited naming is recognised for edits. Other numbered names remain visible."
    )
    print(
        "Candidate associations require an exact original path and unambiguous metadata. "
        "Unknown/conflicting dates are grouped under Unknown; folder names do not supply dates."
    )
    print(
        "Album-folder copies count separately; repeated ZIP entries count once. "
        "No media extraction, changes or server requests."
    )
