"""Recover missing numbered edits using conservative metadata associations."""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

from immich_ctl.takeout import FileKey, google_photos_path, inspect_archives, media_type
from immich_ctl.takeout_audit import numbered_media


class RecoveryError(Exception):
    pass


def digest(stream: IO[bytes]) -> str:
    checksum = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        checksum.update(chunk)
    return checksum.hexdigest()


def folder_checksums(folder: Path) -> dict[str, list[str]]:
    """Fail on unreadable paths or symlinks rather than claim complete coverage."""
    if not folder.is_dir():
        raise RecoveryError("--extracted must be an existing extracted folder.")
    checksums: defaultdict[str, list[str]] = defaultdict(list)

    def scan_error(error: OSError) -> None:
        raise error

    for directory, directories, names in os.walk(folder, followlinks=False, onerror=scan_error):
        for name in sorted(directories + names):
            path = Path(directory) / name
            if path.is_symlink():
                raise RecoveryError(f"Symlinks are not supported in extracted folders: {path!s}")
            if path.is_file() and media_type(path.name) != "Other":
                with path.open("rb") as stream:
                    checksums[digest(stream)].append(str(path.relative_to(folder)))
    return dict(checksums)


def recover(
    paths: list[Path],
    extracted: Path,
    output: Path,
    selection: tuple[date, date],
    extract: Callable[[list[Path], Path], int],
    *,
    dry_run: bool = False,
    include_partner: bool = False,
) -> int:
    """Plan read-only, generate metadata with immich-go, verify bytes, then publish."""
    extracted = extracted.expanduser().resolve()
    output = output.expanduser()
    if output.is_symlink() or output.with_name(output.name + ".manifest.json").is_symlink():
        raise RecoveryError("Recovery output and manifest cannot be symlinks.")
    output = output.resolve()
    manifest = output.with_name(output.name + ".manifest.json")
    if extracted == output or extracted in output.parents or output in extracted.parents:
        raise RecoveryError("Recovery output must be separate from the extracted folder.")
    if output.exists() or manifest.exists():
        raise RecoveryError("Recovery requires a new --output path and companion manifest path.")
    if output in paths or manifest in paths or any(output in p.parents for p in paths):
        raise RecoveryError("Recovery output must be separate from the source ZIPs.")
    existing = folder_checksums(extracted)
    _, memberships, titles, _, warnings, _, coverage = inspect_archives(paths)
    rows = [row for row in numbered_media(coverage) if row["kind"] == "numbered_edit"]
    report: dict[str, Any] = {
        "date_range": [day.isoformat() for day in selection],
        "archives": [str(p) for p in paths],
        "extracted": str(extracted),
        "output": str(output),
        "warnings": dict(warnings),
        "files": rows,
    }
    # Open members by ZipInfo, including split exports and repeated archive names.
    # Album membership comes from every copy of the Google Photos item, so all
    # those copies must agree on metadata before any one of them is selected.
    for row in rows:
        if row["association"] == "candidate":
            original = row["originals"][0]
            owner = next(
                iter(
                    coverage.file_owners[
                        (original["path"], int(original["crc32"], 16), original["size"])
                    ]
                )
            )
            row["sidecars"] = sorted(coverage.item_paths[owner])
    with ExitStack() as stack:
        media: dict[FileKey, tuple[zipfile.ZipFile, zipfile.ZipInfo]] = {}
        metadata: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        invalid_metadata: set[str] = set()
        wanted = set().union(*(set(row["sidecars"]) for row in rows))
        for path in paths:
            archive = stack.enter_context(zipfile.ZipFile(path))
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if relative is None or entry.is_dir():
                    continue
                key = (str(relative), entry.CRC, entry.file_size)
                media.setdefault(key, (archive, entry))
                if str(relative) in wanted:
                    if entry.file_size > 8 * 1024 * 1024:
                        invalid_metadata.add(str(relative))
                        continue
                    try:
                        data = json.loads(archive.read(entry))
                    except (ValueError, UnicodeError):
                        invalid_metadata.add(str(relative))
                        continue
                    if not isinstance(data, dict):
                        invalid_metadata.add(str(relative))
                    elif data not in metadata[str(relative)]:
                        metadata[str(relative)].append(data)
        payloads: dict[str, dict[str, Any]] = {}
        for row in rows:
            row["status"] = "unresolved"
            if row["association"] != "candidate" or row["date_status"] != "known":
                row["reason"] = "No unambiguous original metadata and capture date"
                continue
            original = row["originals"][0]
            owner = next(
                iter(
                    coverage.file_owners[
                        (original["path"], int(original["crc32"], 16), original["size"])
                    ]
                )
            )
            captured = next(iter(coverage.capture_times[owner]))
            row["capture_time"] = captured.isoformat()
            if not selection[0] <= captured.date() <= selection[1]:
                row["status"] = "outside_dates"
                continue
            versions = [data for sidecar in row["sidecars"] for data in metadata[sidecar]]
            if (
                invalid_metadata.intersection(row["sidecars"])
                or not versions
                or any(data != versions[0] for data in versions)
            ):
                row["reason"] = "Missing or conflicting source sidecar contents"
                continue
            data = versions[0]
            origin = data.get("googlePhotosOrigin", {})
            if data.get("trashed") or (
                not include_partner and isinstance(origin, dict) and "fromPartnerSharing" in origin
            ):
                row["status"] = "excluded"
                continue
            key = (row["path"], int(row["crc32"], 16), row["size"])
            archive, entry = media[key]
            with archive.open(entry) as stream:
                checksum = digest(stream)
            row["sha256"] = checksum
            row["albums"] = sorted(
                {title for d, title in titles.items() if owner in memberships[d]}
            )
            if checksum in existing:
                row["status"] = "covered"
                row["extracted_paths"] = sorted(existing[checksum])
                continue
            row["status"] = "recover"
            # Distinct from original names; also avoids immich-go basename/size collisions.
            row["recovery_name"] = checksum[:16] + "-" + PurePosixPath(row["path"]).name
            payloads[row["path"]] = data
        by_checksum: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["status"] == "recover":
                by_checksum[row["sha256"]].append(row)
        for copies in by_checksum.values():
            if any(payloads[row["path"]] != payloads[copies[0]["path"]] for row in copies):
                for row in copies:
                    row["status"] = "unresolved"
                    row["reason"] = "Identical media bytes have differing source metadata"
            else:
                name = min(row["recovery_name"] for row in copies)
                albums = sorted({album for row in copies for album in row["albums"]})
                for row in copies:
                    row["recovery_name"] = name
                    row["albums"] = albums
        counts = Counter(row["status"] for row in rows)
        print("Numbered-edit recovery (local files only)")
        print(
            "; ".join(
                f"{name}: {counts[name]}"
                for name in ("recover", "covered", "unresolved", "excluded", "outside_dates")
            )
        )
        for row in rows:
            if row["status"] != "outside_dates":
                print(json.dumps(row, ensure_ascii=True))
        print(
            "Covered means matching bytes only; metadata and album coverage still need validation."
        )
        if warnings:
            print("Inspection warnings: " + json.dumps(dict(warnings), ensure_ascii=True))
        planned = [row for row in rows if row["status"] == "recover"]
        if dry_run:
            print("Dry run: no files written, no immich-go invocation or server requests.")
            return 0
        if not planned:
            print("No recoverable missing numbered edits; no files written.")
            return 0
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".immich-recovery-", dir=output.parent
        ) as temporary:
            root = Path(temporary)
            mini = root / "takeout.zip"
            with zipfile.ZipFile(mini, "w", allowZip64=True) as target:
                written: set[str] = set()
                for index, row in enumerate(planned):
                    if row["sha256"] in written:
                        continue
                    written.add(row["sha256"])
                    key = (row["path"], int(row["crc32"], 16), row["size"])
                    archive, entry = media[key]
                    data = dict(payloads[row["path"]], title=row["recovery_name"])
                    # Explicit album copies let immich-go combine memberships normally.
                    for album_index, title in enumerate(row["albums"] or [None]):
                        prefix = f"Takeout/Google Photos/recovery-{index}-{album_index}/"
                        if title is not None:
                            target.writestr(prefix + "metadata.json", json.dumps({"title": title}))
                        with (
                            archive.open(entry) as source,
                            target.open(
                                prefix + row["recovery_name"], "w", force_zip64=True
                            ) as destination,
                        ):
                            shutil.copyfileobj(source, destination)
                        target.writestr(prefix + row["recovery_name"] + ".json", json.dumps(data))
            staged = root / "media"
            code = extract([mini], staged)
            if code:
                return code
            actual = folder_checksums(staged)
            expected = {row["sha256"] for row in planned}
            if set(actual) != expected:
                raise RecoveryError(
                    "immich-go output does not match the planned checksums; nothing published."
                )
            if any(len(copies) != 1 for copies in actual.values()):
                raise RecoveryError(
                    "immich-go produced duplicate recovery files; nothing published."
                )
            for row in planned:
                row["recovered_paths"] = sorted(actual[row["sha256"]])
                for relative in row["recovered_paths"]:
                    sidecar = staged / (relative + ".JSON")
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or data.get("fileName") != Path(relative).name:
                        raise RecoveryError("Generated metadata has an unexpected filename.")
                    if datetime.fromisoformat(data.get("dateTaken", "")) != datetime.fromisoformat(
                        row["capture_time"]
                    ):
                        raise RecoveryError("Generated metadata has an unexpected capture date.")
                    generated_albums = {a["title"] for a in data.get("albums", [])}
                    if not set(row["albums"]) <= generated_albums:
                        raise RecoveryError(
                            "Generated metadata is missing expected album associations."
                        )
            # Reserve destinations exclusively; remove our partial output if publishing fails.
            with manifest.open("x", encoding="utf-8") as stream:
                created = False
                try:
                    output.mkdir()
                    created = True
                    shutil.copytree(staged, output, dirs_exist_ok=True)
                    json.dump(report, stream, indent=2, ensure_ascii=True)
                    stream.write("\n")
                except BaseException:
                    manifest.unlink()
                    if created:
                        shutil.rmtree(output)
                    raise
            print(f"Recovery folder: {json.dumps(str(output))}")
            print(f"Manifest: {json.dumps(str(manifest))}")
    return 0
