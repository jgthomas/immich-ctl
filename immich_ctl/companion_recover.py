"""Recover omitted same-stem motion videos without changing sources or contacting Immich."""

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, date
from pathlib import Path, PurePosixPath
from typing import Any

from immich_ctl import recovery_plan
from immich_ctl.folder_compat import native_sidecar_issues
from immich_ctl.folder_validate import inspect_folder, timestamp
from immich_ctl.takeout import FileKey, google_photos_path, inspect_archives, media_type
from immich_ctl.takeout_recover import RecoveryError, digest

UUID = re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MARKER = "immich-go version:0.32.0 (immich-ctl companion recovery)"


def stem(name: str) -> str:
    path = PurePosixPath(name)
    return re.sub(r"(?:~\d+|\(\d+\))$", "", path.stem if path.suffix else path.name)


def local_files(folders: list[Path]) -> tuple[set[str], list[dict[str, Any]]]:
    existing: set[str] = set()
    stills = []
    for folder in folders:
        media, warnings = inspect_folder(folder)
        if warnings:
            raise RecoveryError("Extracted folder needs review: " + json.dumps(warnings))
        for path, metadata in media:
            if native_sidecar_issues(path, metadata):
                raise RecoveryError("Extracted file needs a native sidecar: " + str(path))
            checksum = hashlib.sha256()
            identifiers: set[str] = set()
            tail = b""
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    checksum.update(chunk)
                    if media_type(path.name) == "Photos":
                        data = tail + chunk
                        identifiers.update(x.decode().upper() for x in UUID.findall(data))
                        tail = data[-40:]
            existing.add(checksum.hexdigest())
            if media_type(path.name) == "Photos":
                stills.append({"path": str(path), "identifiers": identifiers, "metadata": metadata})
    return existing, stills


def probe(path: Path, binary: str) -> dict[str, Any]:
    """Only local QuickTime/MP4 inputs are accepted; never follow network URLs."""
    try:
        result = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov",
                "-show_entries",
                "format=format_name,duration:format_tags:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("ffprobe timed out") from None
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("ffprobe could not inspect a supported video")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in data.get("streams", [])
    ):
        raise ValueError("No video stream")
    fmt = data.get("format")
    if not isinstance(fmt, dict) or "mov" not in fmt.get("format_name", "").split(","):
        raise ValueError("Not a QuickTime/MP4 video")
    tags = fmt.get("tags", {})
    if not isinstance(tags, dict):
        raise ValueError("Invalid video tags")
    identifier = tags.get("com.apple.quicktime.content.identifier")
    if identifier is not None and (
        not isinstance(identifier, str) or not UUID.fullmatch(identifier.encode())
    ):
        raise ValueError("Invalid Live Photo identifier")
    captured = timestamp(tags.get("com.apple.quicktime.creationdate"))
    coordinates = None
    location = tags.get("com.apple.quicktime.location.ISO6709")
    if location is not None:
        match = re.match(
            r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/$", str(location)
        )
        if not match:
            raise ValueError("Invalid embedded GPS")
        coordinates = [float(match[1]), float(match[2])]
        if abs(coordinates[0]) > 90 or abs(coordinates[1]) > 180:
            raise ValueError("Embedded GPS out of range")
    return {
        "identifier": identifier.upper() if identifier else None,
        "capture_time": captured.astimezone(UTC).isoformat() if captured else None,
        "coordinates": coordinates,
    }


def merged_metadata(references: list[dict[str, Any]]) -> dict[str, Any]:
    versions = [dict(ref["metadata"]) for ref in references]
    normalized = [
        {k: v for k, v in data.items() if k not in {"fileName", "software", "albums"}}
        for data in versions
    ]
    if any(data != normalized[0] for data in normalized):
        raise ValueError("Matched still copies have conflicting metadata")
    for field, limit in (("latitude", 90), ("longitude", 180)):
        if field in normalized[0]:
            value = normalized[0][field]
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError("Invalid still GPS")
    if "description" in normalized[0] and not isinstance(normalized[0]["description"], str):
        raise ValueError("Invalid still description")
    for field in ("favorited", "archived"):
        if field in normalized[0] and not isinstance(normalized[0][field], bool):
            raise ValueError("Invalid still status flag")
    albums: dict[str, dict[str, Any]] = {}
    for data in versions:
        value = data.get("albums", [])
        if not isinstance(value, list):
            raise ValueError("Invalid still albums")
        for album in value:
            if (
                not isinstance(album, dict)
                or not isinstance(album.get("title"), str)
                or not album["title"]
            ):
                raise ValueError("Invalid still album")
            title = album["title"]
            if title in albums and albums[title] != album:
                raise ValueError("Conflicting album metadata")
            albums[title] = album
    result = {**normalized[0], "software": MARKER}
    if albums:
        result["albums"] = [albums[name] for name in sorted(albums)]
    return result


def metadata_for(
    info: dict[str, Any], references: list[dict[str, Any]], include_unpaired: bool
) -> tuple[dict[str, Any], list[str], str]:
    matches = (
        [r for r in references if info["identifier"] in r["identifiers"]]
        if info["identifier"]
        else []
    )
    if matches:
        metadata = merged_metadata(matches)
        captured = timestamp(metadata.get("dateTaken"))
        embedded = timestamp(info["capture_time"])
        if captured is None or (embedded and abs((embedded - captured).total_seconds()) > 2):
            raise ValueError("Missing or conflicting still capture date")
        if info["coordinates"]:
            for field, value in zip(("latitude", "longitude"), info["coordinates"], strict=True):
                if field in metadata:
                    actual = metadata[field]
                    if (
                        type(actual) not in (int, float)
                        or not math.isfinite(actual)
                        or abs(actual - value) > 0.000051
                    ):
                        raise ValueError("Embedded GPS conflicts with still metadata")
                else:
                    metadata[field] = value
        return metadata, sorted(r["path"] for r in matches), "shared_identifier"
    if not include_unpaired or timestamp(info["capture_time"]) is None:
        raise ValueError(
            "No confirmed still association; standalone recovery requires --include-unpaired and an embedded capture date"
        )
    metadata = {"software": MARKER, "dateTaken": info["capture_time"]}
    if info["coordinates"]:
        metadata.update(zip(("latitude", "longitude"), info["coordinates"], strict=True))
    return metadata, [], "embedded_metadata_only"


def destinations(paths: list[Path], folders: list[Path], output: Path) -> tuple[Path, Path]:
    output = output.expanduser().absolute()
    manifest = output.with_name(output.name + ".manifest.json")
    if output.is_symlink() or manifest.is_symlink() or output.exists() or manifest.exists():
        raise RecoveryError("Companion recovery requires a new output and manifest path.")
    if not output.parent.is_dir() or output.resolve() != output:
        raise RecoveryError("Output parent must exist and must not contain symlinks.")
    if any(output == p or output in p.parents or p in output.parents for p in folders):
        raise RecoveryError("Output must be separate from extracted folders.")
    if any(output == p or manifest == p or output in p.parents for p in paths):
        raise RecoveryError("Output and manifest must be separate from source ZIPs.")
    return output, manifest


def publish(
    rows: list[dict[str, Any]],
    output: Path,
    manifest: Path,
    report: dict[str, Any],
    *,
    verify_inputs: Callable[[], None] | None = None,
) -> None:
    """Stage everything and verify bytes before publishing; never overwrite files."""
    stage = Path(tempfile.mkdtemp(prefix=".immich-companions-", dir=output.parent))
    published = False
    manifest_created = False
    try:
        for archive_path in sorted({row["archive"] for row in rows}):
            with zipfile.ZipFile(archive_path) as archive:
                for row in (r for r in rows if r["archive"] == archive_path):
                    entries = archive.infolist()
                    if row["entry_index"] >= len(entries):
                        raise RecoveryError(
                            "Source archive entry is missing from the recovery plan."
                        )
                    entry = entries[row["entry_index"]]
                    if (entry.filename, entry.CRC, entry.file_size) != (
                        row["member"],
                        row["crc32"],
                        row["size"],
                    ):
                        raise RecoveryError("Source archive changed during recovery.")
                    target = stage / row["output_name"]
                    with archive.open(entry) as source, target.open("xb") as dest:
                        shutil.copyfileobj(source, dest)
                    with target.open("rb") as stream:
                        if digest(stream) != row["sha256"]:
                            raise RecoveryError("Source media changed during recovery.")
                    target.with_name(target.name + ".JSON").write_text(
                        json.dumps(row["metadata"], indent=2, allow_nan=False) + "\n"
                    )
        if verify_inputs:
            verify_inputs()
        # Reserve the companion manifest exclusively before publishing the directory.
        with manifest.open("x", encoding="utf-8") as stream:
            manifest_created = True
            manifest.chmod(0o600)
            json.dump({**report, "state": "complete"}, stream, indent=2, allow_nan=False)
            stream.write("\n")
        try:
            if output.exists() or output.is_symlink():
                raise RecoveryError("Output appeared during recovery; nothing overwritten.")
            stage.rename(output)
        except BaseException:
            raise
        published = True
    finally:
        if not published:
            shutil.rmtree(stage)
            if manifest_created:
                manifest.unlink()


def recover_companions(
    paths: list[Path],
    folders: list[Path],
    output: Path,
    selection: tuple[date, date],
    *,
    dry_run: bool = False,
    include_unpaired: bool = False,
    save_plan: Path | None = None,
    apply_plan: Path | None = None,
) -> dict[str, Any]:
    if (save_plan is not None and not dry_run) or (
        apply_plan is not None and (dry_run or save_plan is not None)
    ):
        raise RecoveryError("Use --save-plan with --dry-run, or --apply-plan without --dry-run.")
    paths = sorted({p.expanduser().resolve() for p in paths})
    folders = sorted({p.expanduser().resolve() for p in folders})
    output, manifest = destinations(paths, folders, output)
    if apply_plan is not None:
        source_plan = recovery_plan.destination(apply_plan, paths, folders, output)
        saved_inputs, report = recovery_plan.load(source_plan)
        validate_plan(report, paths, folders, output, selection, include_unpaired)
        recovery_plan.unchanged(saved_inputs, paths, folders)
        report = {**report, "dry_run": False, "applied_plan": str(source_plan)}
        recoveries = [r for r in report["files"] if r["status"] == "recover"]
        if recoveries:
            publish(
                recoveries,
                output,
                manifest,
                report,
                verify_inputs=lambda: recovery_plan.unchanged(saved_inputs, paths, folders),
            )
        return report
    plan = (
        recovery_plan.destination(save_plan, paths, folders, output)
        if save_plan is not None
        else None
    )
    if plan is not None and (plan.exists() or plan.is_symlink()):
        raise RecoveryError("Plan already exists; choose a new path.")
    inputs = recovery_plan.snapshot(paths, folders) if plan is not None else None
    binary = shutil.which("ffprobe")
    if binary is None:
        raise RecoveryError("recover-companions requires ffprobe on PATH (from FFmpeg).")
    existing, references = local_files(folders)
    stems = {stem(Path(r["path"]).name) for r in references}
    coverage = inspect_archives(paths)[-1]
    rows = []
    seen: set[tuple[FileKey, str]] = set()
    with tempfile.TemporaryDirectory(prefix="immich-companion-probe-") as temporary:
        for path in paths:
            with zipfile.ZipFile(path) as archive:
                for index, entry in enumerate(archive.infolist()):
                    relative = google_photos_path(entry.filename)
                    if relative is None or entry.is_dir():
                        continue
                    key = (str(relative), entry.CRC, entry.file_size)
                    if media_type(relative.name) != "Videos" and relative.suffix:
                        continue
                    row: dict[str, Any] = {
                        "path": str(relative),
                        "archive": str(path),
                        "member": entry.filename,
                        "entry_index": index,
                        "crc32": entry.CRC,
                        "size": entry.file_size,
                        "status": "unresolved",
                    }
                    if coverage.file_sidecars.get(key) or coverage.failed_sidecars.get(key):
                        row.update(
                            status="own_sidecar",
                            reason="Use normal extraction/numbered recovery or review own metadata",
                        )
                        rows.append(row)
                        continue
                    if stem(relative.name) not in stems and not include_unpaired:
                        row.update(
                            status="unrelated", reason="No same-stem still in supplied folders"
                        )
                        rows.append(row)
                        continue
                    candidate = Path(temporary) / "video.bin"
                    with archive.open(entry) as source, candidate.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                    with candidate.open("rb") as stream:
                        row["sha256"] = digest(stream)
                    # CRC32/size/path are not proof of identical contents across ZIP parts.
                    verified_key = (key, row["sha256"])
                    if verified_key in seen:
                        continue
                    seen.add(verified_key)
                    if row["sha256"] in existing:
                        row["status"] = "covered"
                        rows.append(row)
                        continue
                    try:
                        info = probe(candidate, binary)
                        row["embedded"] = info
                        metadata, matched, association = metadata_for(
                            info, references, include_unpaired
                        )
                        captured = timestamp(metadata["dateTaken"])
                        if (
                            captured is None
                            or not selection[0] <= captured.astimezone(UTC).date() <= selection[1]
                        ):
                            row["status"] = "outside_dates"
                        else:
                            name = row["sha256"][:16] + "-" + relative.name
                            if not relative.suffix:
                                name += ".MOV"
                            metadata["fileName"] = name
                            json.dumps(metadata, allow_nan=False)
                            row.update(
                                status="recover",
                                output_name=name,
                                metadata=metadata,
                                matched_stills=matched,
                                association=association,
                            )
                    except (ValueError, TypeError, KeyError) as error:
                        row["reason"] = str(error)
                    rows.append(row)
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["status"] == "recover":
            groups[row["sha256"]].append(row)
    recoveries = []
    for copies in groups.values():
        try:
            metadata = merged_metadata([{"metadata": r["metadata"]} for r in copies])
        except ValueError as error:
            for row in copies:
                row.update(status="unresolved", reason=str(error))
            continue
        first = copies[0]
        first["metadata"] = {**metadata, "fileName": first["output_name"]}
        recoveries.append(first)
        for row in copies[1:]:
            row.update(status="duplicate_source", output_name=first["output_name"])
    if len({r["output_name"].casefold() for r in recoveries}) != len(recoveries):
        raise RecoveryError("Recovery filename collision; no output written.")
    report = {
        "date_range": [d.isoformat() for d in selection],
        "extracted_folders": [str(p) for p in folders],
        "scope": "Videos without own Takeout sidecars and extensionless same-stem candidates; --include-unpaired also examines unmatched names. Prepared still metadata is the reference; source sharing/trash filters must already have been applied to those folders.",
        "dry_run": dry_run,
        "include_unpaired": include_unpaired,
        "output": str(output),
        "files": rows,
        "counts": dict(Counter(r["status"] for r in rows)),
    }
    if not dry_run and recoveries:
        publish(recoveries, output, manifest, report)
    if plan is not None and inputs is not None:
        recovery_plan.unchanged(inputs, paths, folders)
        recovery_plan.save(plan, inputs, report)
    return report


def validate_plan(
    report: dict[str, Any],
    paths: list[Path],
    folders: list[Path],
    output: Path,
    selection: tuple[date, date],
    include_unpaired: bool,
) -> None:
    """Reject mismatched options and unsafe destinations before staging any files."""
    try:
        if (
            report["dry_run"] is not True
            or report["date_range"] != [d.isoformat() for d in selection]
            or report["extracted_folders"] != [str(p) for p in folders]
            or report["output"] != str(output)
            or report["include_unpaired"] is not include_unpaired
            or not isinstance(report["files"], list)
        ):
            raise ValueError()
        names: set[str] = set()
        checksums: set[str] = set()
        for row in report["files"]:
            if row["archive"] not in {str(p) for p in paths}:
                raise ValueError()
            if row["status"] != "recover":
                continue
            name = row["output_name"]
            captured = timestamp(row["metadata"].get("dateTaken"))
            if (
                not isinstance(name, str)
                or name in {"", ".", ".."}
                or PurePosixPath(name).name != name
                or "\\" in name
                or name.casefold() in names
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or row["sha256"] in checksums
                or row["metadata"].get("fileName") != name
                or row["metadata"].get("software") != MARKER
                or captured is None
                or not selection[0] <= captured.astimezone(UTC).date() <= selection[1]
                or type(row["entry_index"]) is not int
                or row["entry_index"] < 0
                or type(row["crc32"]) is not int
                or not 0 <= row["crc32"] <= 0xFFFFFFFF
                or type(row["size"]) is not int
                or row["size"] < 0
                or not isinstance(row["member"], str)
            ):
                raise ValueError()
            names.add(name.casefold())
            checksums.add(row["sha256"])
        if report["counts"] != dict(Counter(r["status"] for r in report["files"])):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RecoveryError("Recovery plan does not match these options or is invalid.") from None
