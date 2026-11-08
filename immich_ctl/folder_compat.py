"""Conservative compatibility checks for immich-go 0.32.0's folder reader."""

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

JSON_LIMIT = 8 * 1024 * 1024


def album_duplicate_groups(
    media: list[Path], metadata: dict[Path, dict[str, Any]]
) -> list[list[Path]]:
    """Find local checksum duplicates that can queue empty album IDs in 0.32.0.

    Equal sizes only select candidates; file contents establish duplication.
    Even matching album lists are unsafe in the upstream AlreadyProcessed path.
    """
    sizes: defaultdict[int, list[Path]] = defaultdict(list)
    for path in media:
        sizes[path.stat().st_size].append(path)
    duplicates: list[list[Path]] = []
    for candidates in sizes.values():
        if len(candidates) < 2 or not any(metadata[p].get("albums") for p in candidates):
            continue
        checksums: defaultdict[str, list[Path]] = defaultdict(list)
        for path in candidates:
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            checksums[checksum].append(path)
        duplicates.extend(
            sorted(copies)
            for copies in checksums.values()
            if len(copies) > 1 and any(metadata[p].get("albums") for p in copies)
        )
    return sorted(duplicates)


def native_sidecar_issues(
    media: Path, metadata: dict[str, Any] | None, *, payload: bytes | None = None
) -> list[str]:
    """The reader uses an unescaped glob and only recognises its native marker.

    Check the full path, including parent directories. Parenthesised duplicate
    suffixes are literal; glob metacharacters and backslashes are rejected.
    """
    issues = []
    if any(character in str(media) for character in "*?[]\\"):
        issues.append("path contains unsafe immich-go glob characters")
    if metadata is None:
        issues.append("no usable native JSON sidecar")
    else:
        software = metadata.get("software")
        if (
            not isinstance(software, str)
            or not software.startswith("immich-go version:")
            or (payload is not None and b"immich-go version" not in payload)
        ):
            issues.append("missing immich-go version marker")
    return issues


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
    if candidates[0].is_symlink():
        return "symlink"
    with candidates[0].open("rb") as stream:
        payload = stream.read(JSON_LIMIT + 1)
    try:
        value = json.loads(payload) if len(payload) <= JSON_LIMIT else None
    except (ValueError, UnicodeError):
        return "invalid"
    if isinstance(value, dict):
        issues = native_sidecar_issues(media, value, payload=payload)
        if issues:
            return "; ".join(issues)
        if values is not None:
            values[media] = value
        return "present_unverified"
    return "invalid"
