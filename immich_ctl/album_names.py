"""Source-verified names for colliding Takeout albums."""

import json
import re
from pathlib import Path

from immich_ctl.folder_compat import JSON_LIMIT
from immich_ctl.folder_repair import RepairError, invalid_constant, unique_object
from immich_ctl.takeout import Coverage, capture_time
from immich_ctl.takeout_metadata import duplicate_album_titles


def numbered_names(
    coverage: Coverage, titles: dict[str, str], selected: set[str] | None = None
) -> dict[str, str]:
    """Keep the unnumbered title and turn Takeout's directory suffix into _N."""
    mapping = {}
    collisions = duplicate_album_titles(titles)
    if selected is not None and selected - {c["title"] for c in collisions}:
        raise ValueError("Requested title is not a duplicate source album title.")
    for collision in collisions:
        title = collision["title"]
        if selected is not None and title not in selected:
            continue
        directories = collision["source_directories"]
        if title not in directories:
            raise ValueError("Numbered names require an unnumbered source directory: " + title)
        for directory in directories:
            if len(coverage.album_metadata.get(directory, [])) != 1 or (
                directory in coverage.invalid_album_metadata
            ):
                raise ValueError("Missing or conflicting album metadata: " + directory)
            if directory == title:
                mapping[directory] = title
                continue
            match = re.fullmatch(re.escape(title) + r"\(([1-9][0-9]*)\)", directory)
            if match is None:
                raise ValueError(
                    "Source directory has no unambiguous numbered suffix: " + directory
                )
            mapping[directory] = f"{title}_{match[1]}"
    final = [mapping.get(d, title or d) for d, title in titles.items()]
    if any(final.count(name) != 1 for name in mapping.values()):
        raise ValueError("Numbered suffix does not uniquely distinguish the albums.")
    return dict(sorted(mapping.items()))


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


def read_names(path: Path, coverage: Coverage, titles: dict[str, str]) -> dict[str, str]:
    """Read generated names or explicitly approved names for complete title groups."""
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
    explicit = (
        isinstance(mapping, dict)
        and set(mapping) == {"names"}
        and isinstance(mapping["names"], dict)
    )
    if explicit:
        mapping = mapping["names"]
    if (
        not isinstance(mapping, dict)
        or not mapping
        or any(not isinstance(v, str) or d not in titles for d, v in mapping.items())
    ):
        raise ValueError("Invalid album mapping; expected source directories and titles.")
    selected = {titles[d] or d for d in mapping}
    validated: dict[str, str] = {}
    for collision in duplicate_album_titles(titles):
        if collision["title"] not in selected:
            continue
        group = {d: titles[d] for d in collision["source_directories"]}
        proposed = {d: mapping[d] for d in group if d in mapping}
        if explicit:
            if proposed.keys() != group.keys() or any(
                len(coverage.album_metadata.get(d, [])) != 1
                or d in coverage.invalid_album_metadata
                or not proposed[d].strip()
                for d in group
            ):
                raise ValueError(
                    "Explicit album names require complete groups with valid metadata."
                )
            validated.update(proposed)
            continue
        for build in (dated_names, numbered_names):
            try:
                expected = build(coverage, group)
            except ValueError:
                continue
            if proposed == expected:
                validated.update(expected)
                break
    if mapping != validated:
        raise ValueError("Album mapping does not match the source album dates or directories.")
    final = [mapping.get(d, title or d) for d, title in titles.items()]
    if explicit:
        final = [name.strip().casefold() for name in final]
    if any(
        final.count(name.strip().casefold() if explicit else name) != 1 for name in mapping.values()
    ):
        raise ValueError("Album mapping would collide with another album title.")
    return validated


def apply_names(path: Path, coverage: Coverage, titles: dict[str, str]) -> dict[str, str]:
    """Validate a mapping against this export before altering in-memory expectations."""
    mapping = read_names(path, coverage, titles)
    for directory, name in mapping.items():
        titles[directory] = name
        coverage.album_metadata[directory] = [
            dict(coverage.album_metadata[directory][0], title=name)
        ]
    return mapping
