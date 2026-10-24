"""Safeguards for immich-go's heuristic matching and exit status."""

import hashlib
import json
import zipfile
from collections import defaultdict
from pathlib import Path

from immich_ctl.takeout import google_photos_path, media_type


def check_collisions(paths: list[Path]) -> None:
    """Hash potentially colliding members; CRC/size alone cannot establish equality.

    Check the whole export: upstream combines album memberships before applying
    date filters. Ordinary byte-identical album copies remain supported.
    """
    groups: defaultdict[tuple[str, int], list[tuple[Path, zipfile.ZipInfo]]] = defaultdict(list)
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if (
                    relative is not None
                    and not entry.is_dir()
                    and media_type(str(relative)) != "Other"
                ):
                    groups[relative.name, entry.file_size].append((path, entry))
    for (name, size), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        checksums = set()
        for path, entry in members:
            with zipfile.ZipFile(path) as archive, archive.open(entry) as stream:
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
            if not isinstance(record, dict) or record.get("level") in {"ERROR", "FATAL"}:
                return True
    return False
