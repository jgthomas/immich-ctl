"""Explicit, source-verified dated names for colliding Takeout albums."""

import json
from pathlib import Path

from immich_ctl.folder_compat import JSON_LIMIT
from immich_ctl.folder_repair import RepairError, invalid_constant, unique_object
from immich_ctl.takeout import Coverage, capture_time
from immich_ctl.takeout_metadata import duplicate_album_titles


def dated_names(
    coverage: Coverage, titles: dict[str, str], selected: set[str] | None = None
) -> dict[str, str]:
    mapping = {}
    collisions = duplicate_album_titles(titles)
    if selected is not None and selected - {c["title"] for c in collisions}:
        raise ValueError("Requested title is not a duplicate source album title.")
    for collision in collisions:
        title = collision["title"]
        if selected is not None and title not in selected:
            continue
        for directory in collision["source_directories"]:
            versions = coverage.album_metadata.get(directory, [])
            if len(versions) != 1 or directory in coverage.invalid_album_metadata:
                raise ValueError("Missing or conflicting album metadata: " + directory)
            captured = capture_time({"photoTakenTime": versions[0].get("date")})
            if captured is None:
                raise ValueError("Missing or invalid album date: " + directory)
            mapping[directory] = title + " (" + captured.date().isoformat() + ")"
    final = [mapping.get(d, title or d) for d, title in titles.items()]
    # Other unresolved title groups are allowed, but new names must be unique.
    if any(final.count(name) != 1 for name in mapping.values()):
        raise ValueError("Date suffix does not uniquely distinguish the albums.")
    return dict(sorted(mapping.items()))


def apply_names(path: Path, coverage: Coverage, titles: dict[str, str]) -> dict[str, str]:
    """Validate a mapping against this export before altering in-memory expectations."""
    with path.expanduser().open("rb") as stream:
        payload = stream.read(JSON_LIMIT + 1)
    if len(payload) > JSON_LIMIT:
        raise ValueError("Album mapping exceeds the JSON size limit.")
    try:
        mapping = json.loads(
            payload, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
    except RepairError as error:
        raise ValueError(str(error)) from error
    if (
        not isinstance(mapping, dict)
        or not mapping
        or any(not isinstance(v, str) or d not in titles for d, v in mapping.items())
    ):
        raise ValueError("Invalid album mapping; expected source directories and dated titles.")
    selected = {titles[d] or d for d in mapping}
    if mapping != dated_names(coverage, titles, selected):
        raise ValueError("Album mapping does not match the source album dates and directories.")
    for directory, name in mapping.items():
        titles[directory] = name
        coverage.album_metadata[directory] = [
            dict(coverage.album_metadata[directory][0], title=name)
        ]
    return mapping
