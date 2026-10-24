"""Conservative compatibility checks for immich-go 0.32.0's folder reader."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

JSON_LIMIT = 8 * 1024 * 1024


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
