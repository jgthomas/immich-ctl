"""Safeguards for immich-go's heuristic matching and exit status."""

import hashlib
import json
import zipfile
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path

from immich_ctl import collision_cache
from immich_ctl.takeout import google_photos_path, media_type


def check_collisions(
    paths: list[Path], *, cache_dir: Path | None = None, refresh: bool = False
) -> bool:
    """Hash potentially colliding members; CRC/size alone cannot establish equality.

    Check the whole export: upstream combines album memberships before applying
    date filters. Ordinary byte-identical album copies remain supported.
    """
    paths = sorted({path.expanduser().resolve() for path in paths})
    before = collision_cache.archive_state(paths)
    identity = collision_cache.fingerprint(before) if cache_dir is not None else None
    if (
        cache_dir is not None
        and identity is not None
        and not refresh
        and collision_cache.contains(cache_dir, identity)
    ):
        if collision_cache.archive_state(paths) != before:
            raise ValueError(
                "Source archives changed during collision checking; rerun the command."
            )
        return True
    _check_collisions(paths)
    if collision_cache.archive_state(paths) != before:
        raise ValueError("Source archives changed during collision checking; rerun the command.")
    if cache_dir is not None and identity is not None:
        collision_cache.store(cache_dir, identity)
    return False


def _check_collisions(paths: list[Path]) -> None:
    groups: defaultdict[tuple[str, int], list[tuple[zipfile.ZipFile, zipfile.ZipInfo]]] = (
        defaultdict(list)
    )
    with ExitStack() as stack:
        for path in paths:
            archive = stack.enter_context(zipfile.ZipFile(path))
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if (
                    relative is not None
                    and not entry.is_dir()
                    and media_type(str(relative)) != "Other"
                ):
                    groups[relative.name, entry.file_size].append((archive, entry))
        for (name, size), members in sorted(groups.items()):
            if len(members) < 2:
                continue
            checksums = set()
            for archive, entry in members:
                with archive.open(entry) as stream:
                    checksum = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        checksum.update(chunk)
                    checksums.add(checksum.hexdigest())
            if len(checksums) > 1:
                names = sorted({str(google_photos_path(e.filename)) for _, e in members})
                raise ValueError(
                    "Unsafe immich-go basename/size collision: "
                    f"{json.dumps(name)} ({size} bytes), different contents in {json.dumps(names)}. "
                    "Nothing started; extract these separately with their metadata before uploading."
                )


def log_has_errors(path: Path) -> bool:
    """Upstream may return zero after recording a processing failure."""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # An unreadable structured log cannot establish a clean run.
                return True
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("level"), str)
                or record["level"] in {"ERROR", "FATAL"}
            ):
                return True
    return False
