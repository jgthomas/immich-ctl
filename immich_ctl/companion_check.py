"""Read-only local companion checks; filenames and nearby dates never establish a link."""

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from immich_ctl.companion_metadata import albums_equal, normalize
from immich_ctl.companion_recover import UUID, merged_metadata, metadata_for
from immich_ctl.companion_recover import probe as recovery_probe
from immich_ctl.folder_compat import JSON_LIMIT, native_sidecar_issues
from immich_ctl.folder_validate import ValidationError, inspect_folder, timestamp
from immich_ctl.local_hashes import LocalHashes, fingerprint
from immich_ctl.takeout import media_type

FIELDS = ("dateTaken", "latitude", "longitude", "description", "favorited", "archived", "albums")


class CheckError(Exception):
    pass


def probe(path: Path, binary: str) -> dict[str, Any]:
    return recovery_probe(path, binary, include_standard=True)


def load_entry(path: Path, folder: Path, directories: dict[Path, list[Path]]) -> dict[str, Any]:
    if path.parent not in directories:
        directories[path.parent] = list(path.parent.iterdir())
    sidecars = [
        p
        for p in directories[path.parent]
        if p.suffix.lower() == ".json" and p.with_suffix("").name == path.name
    ]
    if len(sidecars) != 1:
        raise CheckError("Missing or ambiguous sidecar: " + str(path))
    sidecar = sidecars[0]
    before = fingerprint(sidecar)
    with sidecar.open("rb") as stream:
        raw = stream.read(JSON_LIMIT + 1)
    if fingerprint(sidecar) != before:
        raise CheckError("Sidecar changed during inspection: " + str(sidecar))
    data = json.loads(raw) if len(raw) <= JSON_LIMIT else None
    if not isinstance(data, dict) or native_sidecar_issues(path, data, payload=raw):
        raise CheckError("Unusable native sidecar: " + str(sidecar))
    json.dumps(data, allow_nan=False)
    if data.get("fileName") != path.name:
        raise CheckError("Run filename repair before companion checking: " + str(sidecar))
    return {
        "path": str(path),
        "folder": str(folder),
        "relative_path": str(path.relative_to(folder)),
        "fingerprint": fingerprint(path),
        "sidecar": str(sidecar),
        "sidecar_fingerprint": before,
        "sidecar_sha256": hashlib.sha256(raw).hexdigest(),
        "metadata": data,
    }


def scan_still(entry: dict[str, Any]) -> set[str]:
    path = Path(entry["path"])
    checksum = hashlib.sha256()
    identifiers: set[str] = set()
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
            identifiers.update(value.decode().upper() for value in UUID.findall(tail + chunk))
            tail = (tail + chunk)[-40:]
    if fingerprint(path) != entry["fingerprint"]:
        raise CheckError("Still changed while reading identifiers: " + str(path))
    entry["sha256"] = checksum.hexdigest()
    return identifiers


def compare_metadata(
    actual: dict[str, Any], expected: dict[str, Any], fields: tuple[str, ...] = FIELDS
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changed: list[dict[str, Any]] = []
    small: list[dict[str, Any]] = []
    for field in fields:
        fallback: Any = (
            []
            if field == "albums"
            else False
            if field in {"favorited", "archived"}
            else 0
            if field in {"latitude", "longitude"}
            else ""
        )
        received, wanted = actual.get(field, fallback), expected.get(field, fallback)
        minor = False
        if field == "dateTaken":
            a, b = timestamp(received), timestamp(wanted)
            equal = a is not None and b is not None and a == b
            minor = a is not None and b is not None and abs((a - b).total_seconds()) <= 2
        elif field in {"latitude", "longitude"}:
            valid = all(type(v) in (int, float) and math.isfinite(v) for v in (received, wanted))
            equal = valid and received == wanted
            minor = valid and abs(received - wanted) <= 0.000051
        elif field == "albums":
            equal = albums_equal(received, wanted)
        else:
            equal = type(received) is type(wanted) and received == wanted
        if not equal:
            finding = {"field": field, "actual": received, "expected": wanted}
            (small if minor else changed).append(finding)
    return changed, small


def inspect_video(entry: dict[str, Any], binary: str) -> dict[str, Any]:
    path = Path(entry["path"])
    if fingerprint(path) != entry["fingerprint"]:
        raise CheckError("Video changed before probing: " + str(path))
    result: dict[str, Any]
    try:
        result = {"embedded": probe(path, binary)}
    except (ValueError, TypeError, KeyError, OSError) as error:
        result = {"probe_error": str(error)}
    if fingerprint(path) != entry["fingerprint"]:
        raise CheckError("Video changed during probing: " + str(path))
    return result


def compare_video(
    entry: dict[str, Any], info: dict[str, Any], index: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    row = {**entry, **info, "matched_stills": [], "comparisons": {}, "unchecked_fields": []}
    if "probe_error" in info:
        return {**row, "status": "error"}
    embedded = info["embedded"]
    references = index.get(embedded["identifier"], []) if embedded["identifier"] else []
    row["matched_stills"] = references
    own: dict[str, Any] = {}
    if embedded["capture_time"]:
        own["dateTaken"] = embedded["capture_time"]
    if embedded["coordinates"] is not None:
        own.update(zip(("latitude", "longitude"), embedded["coordinates"], strict=True))
    changes, small = compare_metadata(entry["metadata"], own, tuple(own))
    row["comparisons"]["video_to_embedded"] = {"differences": changes, "within_tolerance": small}
    row["unchecked_fields"] = sorted(set(entry["metadata"]) - {*FIELDS, "fileName", "software"})
    if not references:
        return {
            **row,
            "status": "identifier_unmatched" if embedded["identifier"] else "no_identifier",
        }
    try:
        normalized = [
            {**reference, "metadata": normalize(reference["metadata"])} for reference in references
        ]
        expected, _, _ = metadata_for(
            embedded,
            [{**reference, "identifiers": {embedded["identifier"]}} for reference in normalized],
            False,
        )
    except ValueError as error:
        row.update(status="reference_conflict", reference_error=str(error))
        try:
            expected = merged_metadata(
                [
                    {**reference, "metadata": normalize(reference["metadata"])}
                    for reference in references
                ]
            )
        except ValueError:
            return row
    changed, minor = compare_metadata(entry["metadata"], expected)
    row["expected_metadata"] = {
        k: v for k, v in expected.items() if k not in {"fileName", "software"}
    }
    row["comparisons"]["video_to_stills"] = {"differences": changed, "within_tolerance": minor}
    row["unchecked_fields"] = sorted(
        set(row["unchecked_fields"]) | (set(expected) - {*FIELDS, "fileName", "software"})
    )
    if row.get("status") != "reference_conflict":
        row["status"] = (
            "embedded_metadata_unverified"
            if embedded.get("metadata_issues")
            else "metadata_difference"
            if changed or changes
            else "within_tolerance"
            if minor or small
            else "matched"
        )
    return row


def roster(folders: list[Path]) -> set[str]:
    def failed(error: OSError) -> None:
        raise error

    return {
        str(Path(directory) / name)
        for folder in folders
        for directory, directories, names in os.walk(folder, followlinks=False, onerror=failed)
        for name in directories + names
    }


def check_companions(
    folders: list[Path], *, workers: int = 4, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    if type(workers) is not int or not 1 <= workers <= 8:
        raise CheckError("--workers must be between 1 and 8.")
    binary = shutil.which("ffprobe")
    if binary is None:
        raise CheckError("Companion checking requires ffprobe on PATH (from FFmpeg).")
    folders = sorted({p.expanduser().resolve() for p in folders})
    if not folders:
        raise CheckError("Supply at least one extracted folder.")
    original_roster = roster(folders)
    entries: dict[str, dict[str, Any]] = {}
    directories: dict[Path, list[Path]] = {}
    for folder in folders:
        media, warnings = inspect_folder(folder, check_filetypes=False)
        if warnings:
            raise CheckError("Extracted folder needs review: " + json.dumps(warnings))
        for path, _ in media:
            if str(path) not in entries:
                entries[str(path)] = load_entry(path, folder, directories)
    stills = [r for r in entries.values() if media_type(r["path"]) == "Photos"]
    videos = [r for r in entries.values() if media_type(r["path"]) == "Videos"]
    index: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for number, entry in enumerate(stills, 1):
        for identity in scan_still(entry):
            index[identity].append(entry)
        if progress and (number % 100 == 0 or number == len(stills)):
            progress(f"Stills indexed: {number}/{len(stills)}")
    with LocalHashes(folders) as hashes:
        for number, entry in enumerate(videos, 1):
            entry["sha256"] = hashes.checksums(Path(entry["path"]))["sha256"]
            if progress and (number % 100 == 0 or number == len(videos)):
                progress(f"Video checksums ready: {number}/{len(videos)}")
    rows = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for number, (entry, info) in enumerate(
            zip(videos, pool.map(lambda r: inspect_video(r, binary), videos), strict=True), 1
        ):
            rows.append(compare_video(entry, info, index))
            if progress and (number % 25 == 0 or number == len(videos)):
                progress(f"Videos checked: {number}/{len(videos)}")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    for entry in entries.values():
        if (
            fingerprint(Path(entry["path"])) != entry["fingerprint"]
            or fingerprint(Path(entry["sidecar"])) != entry["sidecar_fingerprint"]
        ):
            raise CheckError("Inputs changed during the check; rerun: " + entry["path"])
    if roster(folders) != original_roster:
        raise CheckError("Folder contents changed during the check; rerun.")
    counts = dict(Counter(row["status"] for row in rows))
    differing = Counter(
        field
        for row in rows
        for field in {
            finding["field"]
            for comparison in row["comparisons"].values()
            for finding in comparison["differences"]
        }
    )
    return {
        "folders": [str(p) for p in folders],
        "stills": len(stills),
        "videos": len(videos),
        "counts": counts,
        "differing_fields": dict(differing),
        "files": rows,
        "has_findings": any(
            r["status"] not in {"matched", "within_tolerance"} or r["unchecked_fields"]
            for r in rows
        ),
        "read_only": True,
        "scope": "Local native metadata and embedded video date/GPS only; still associations require shared embedded identifiers. Filenames never establish links. Omitted native zero values use local Go defaults. Small date/GPS differences are reported separately (2 seconds / 0.000051 degrees). No Takeout source or Immich server verification. Media and sidecars unchanged; the private checksum cache may update.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="Print all comparisons and provenance")
    parser.add_argument("--details", action="store_true", help="List videos needing review")
    parser.add_argument("--quiet", action="store_true", help="Hide progress on stderr")
    parser.add_argument(
        "--workers", type=int, default=4, help="Concurrent ffprobe processes, 1–8 (default: 4)"
    )
    args = parser.parse_args(argv)
    try:
        report = check_companions(
            args.folders,
            workers=args.workers,
            progress=None if args.quiet else lambda s: print(s, file=sys.stderr, flush=True),
        )
    except (CheckError, ValidationError, OSError, ValueError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print("Extracted companion-video check (read-only)")
        print(f"Stills: {report['stills']}; videos: {report['videos']}")
        print("\n| Result | Videos |\n|---|---:|")
        for status, count in sorted(report["counts"].items()):
            print(f"| {status} | {count} |")
        if args.details:
            for row in report["files"]:
                if row["status"] not in {"matched", "within_tolerance"} or row["unchecked_fields"]:
                    print(json.dumps(row, ensure_ascii=True, allow_nan=False))
        print("Scope: " + report["scope"])
    return 2 if report["has_findings"] else 0
