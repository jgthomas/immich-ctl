"""Read-only inspection of Google Photos Takeout metadata and filenames."""

import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeAlias

ItemIdentity: TypeAlias = tuple[str, str] | tuple[str, str, int, int]
Items: TypeAlias = dict[ItemIdentity, tuple[str, str]]
FileKey: TypeAlias = tuple[str, int, int]


@dataclass
class Coverage:
    """Conservative filename matches, distinct from logical Google Photos items."""

    matched_items: set[ItemIdentity] = field(default_factory=set)
    ambiguous_items: set[ItemIdentity] = field(default_factory=set)
    missing_items: set[ItemIdentity] = field(default_factory=set)
    matched_files: set[FileKey] = field(default_factory=set)
    unmatched_files: set[FileKey] = field(default_factory=set)
    file_owners: dict[FileKey, set[ItemIdentity]] = field(default_factory=dict)
    file_sidecars: dict[FileKey, set[str]] = field(default_factory=dict)
    sidecars_without_local_match: int = 0
    ambiguous_sidecars: int = 0
    item_paths: dict[ItemIdentity, set[str]] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    capture_times: dict[ItemIdentity, set[datetime]] = field(default_factory=dict)


Inspection: TypeAlias = tuple[
    Items,
    dict[str, set[ItemIdentity]],
    dict[str, str],
    Counter[str],
    Counter[str],
    int,
    Coverage,
]


PHOTO_EXTENSIONS = set(
    "jpg jpeg heic heif png gif webp avif bmp tif tiff dng raw cr2 cr3 nef arw raf orf rw2 pef srw".split()
)
VIDEO_EXTENSIONS = set("mp4 mov m4v avi mpg mpeg 3gp 3g2 mts m2ts mkv webm wmv mod tod".split())


def media_type(name: str) -> str:
    extension = PurePosixPath(name).suffix.lower().lstrip(".")
    if extension in PHOTO_EXTENSIONS:
        return "Photos"
    if extension in VIDEO_EXTENSIONS:
        return "Videos"
    return "Other"


def capture_time(data: dict[str, Any]) -> datetime | None:
    """Read a nonzero integer Google capture timestamp, always in UTC."""
    try:
        timestamp = data["photoTakenTime"]["timestamp"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, (str, int)):
            return None
        if not re.fullmatch(r"[+-]?\d+", str(timestamp)) or int(timestamp) == 0:
            return None
        return datetime.fromtimestamp(int(timestamp), UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def capture_year(data: dict[str, Any]) -> str:
    captured = capture_time(data)
    return str(captured.year) if captured is not None else "Unknown"


def google_photos_path(name: str) -> PurePosixPath | None:
    parts = PurePosixPath(name).parts
    if "Google Photos" not in parts:
        return None
    return PurePosixPath(*parts[parts.index("Google Photos") + 1 :])


def match_media(
    relative: PurePosixPath,
    title: str,
    filenames: dict[str, set[FileKey]],
    stems: dict[tuple[str, str], set[FileKey]],
) -> set[FileKey]:
    """Match a sidecar locally, preserving duplicate suffixes and ambiguity."""
    base = relative.name[:-5]  # Remove .json.
    supplemental = re.fullmatch(r"(.*?)\.supplemental-metadata(?:\((\d+)\))?", base)
    numbered = False
    if supplemental:
        base, number = supplemental.groups()
        if number:
            name = PurePosixPath(base)
            base = f"{name.stem}({number}){name.suffix}"
            numbered = True
    direct = filenames.get(str(relative.parent / base), set())
    if direct:
        return direct
    # Older exports can put the duplicate index after the extension.
    duplicate = re.fullmatch(r"(.*)(\.[^.()]+)\((\d+)\)", base)
    if duplicate:
        stem, extension, number = duplicate.groups()
        base = f"{stem}({number}){extension}"
        numbered = True
        direct = filenames.get(str(relative.parent / base), set())
        if direct:
            return direct
    # Never let an indexed sidecar attach to the unindexed photo in its title.
    if not numbered and not re.search(r"\(\d+\)", base) and title:
        direct = filenames.get(str(relative.parent / title), set())
        if direct:
            return direct
    # Extensionless titles can describe one or several different file formats.
    if not PurePosixPath(base).suffix:
        return stems.get((str(relative.parent), base), set())
    return set()


def inspect_archives(paths: list[Path]) -> Inspection:
    """Collect metadata items, album membership and file counts across ZIP parts."""
    items: Items = {}
    memberships: defaultdict[str, set[ItemIdentity]] = defaultdict(set)
    album_titles = {}
    raw_files: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    seen_entries = set()
    sidecars = 0
    filenames: defaultdict[str, set[FileKey]] = defaultdict(set)
    stems: defaultdict[tuple[str, str], set[FileKey]] = defaultdict(set)
    files: set[FileKey] = set()
    coverage = Coverage()
    # Index every part first: the JSON and its media may be in different ZIPs.
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if entry.is_dir() or relative is None or relative.suffix.lower() == ".json":
                    continue
                file_key = (str(relative), entry.CRC, entry.file_size)
                files.add(file_key)
                filenames[str(relative)].add(file_key)
                stems[(str(relative.parent), relative.stem)].add(file_key)
                coverage.directories.add(str(relative.parent))
    years: defaultdict[ItemIdentity, set[str]] = defaultdict(set)
    kinds: defaultdict[ItemIdentity, set[str]] = defaultdict(set)
    matches: list[tuple[ItemIdentity, set[FileKey]]] = []
    owners: defaultdict[FileKey, set[ItemIdentity]] = defaultdict(set)
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                relative = google_photos_path(entry.filename)
                if entry.is_dir() or relative is None:
                    continue
                directory = str(relative.parent)
                # Split exports can repeat a path; keep differing contents visible.
                entry_key = (str(relative), entry.CRC, entry.file_size)
                if entry_key in seen_entries:
                    continue
                seen_entries.add(entry_key)
                if relative.suffix.lower() != ".json":
                    raw_files[media_type(relative.name)] += 1
                    continue
                if relative.name == "user-generated-memory-titles.json":
                    continue
                if entry.file_size > 8 * 1024 * 1024:
                    warnings["JSON files larger than 8 MiB (skipped)"] += 1
                    continue
                try:
                    data = json.loads(archive.read(entry))
                except (ValueError, UnicodeError):
                    warnings["Unreadable JSON files (skipped)"] += 1
                    continue
                if not isinstance(data, dict):
                    warnings["Non-object JSON files (skipped)"] += 1
                    continue
                # Accept both current metadata.json and older albumData wrappers.
                album = data.get("albumData")
                is_media = any(key in data for key in ("photoTakenTime", "creationTime", "url"))
                if isinstance(album, dict) or (relative.name == "metadata.json" and not is_media):
                    title = (album if isinstance(album, dict) else data).get("title")
                    album_titles[directory] = title if isinstance(title, str) else directory
                    memberships.setdefault(directory, set())  # Include empty albums.
                    continue
                if not is_media:
                    warnings["Unrecognised JSON files (skipped)"] += 1
                    continue
                sidecars += 1
                url = data.get("url")
                identity: ItemIdentity
                if isinstance(url, str) and url:
                    identity = ("url", url)
                else:
                    identity = ("path", str(relative), entry.CRC, entry.file_size)
                    warnings["Media JSON without a Google Photos ID (counted separately)"] += 1
                title = data.get("title")
                title = title if isinstance(title, str) else ""
                candidates = match_media(relative, title, filenames, stems)
                matches.append((identity, candidates))
                # Ambiguous candidates still compete with another ID's exact match.
                for candidate in candidates:
                    owners[candidate].add(identity)
                    coverage.file_sidecars.setdefault(candidate, set()).add(str(relative))
                candidate_types = {media_type(key[0]) for key in candidates} - {"Other"}
                kind = media_type(title)
                if len(candidate_types) == 1:
                    kind = next(iter(candidate_types))
                captured = capture_time(data)
                coverage.capture_times.setdefault(identity, set())
                if captured is not None:
                    coverage.capture_times[identity].add(captured)
                years[identity].add(capture_year(data))
                kinds[identity].add(kind)
                coverage.item_paths.setdefault(identity, set()).add(str(relative))
                memberships[directory].add(identity)

    for identity in years:
        known_years = years[identity] - {"Unknown"}
        known_kinds = kinds[identity] - {"Other"}
        year = next(iter(known_years)) if len(known_years) == 1 else "Unknown"
        kind = next(iter(known_kinds)) if len(known_kinds) == 1 else "Other"
        items[identity] = (year, kind)
        if len(known_years) > 1:
            warnings["IDs with conflicting capture years (reported as Unknown)"] += 1
        if len(known_kinds) > 1:
            warnings["IDs with conflicting media types (reported as Other)"] += 1
    for identity, candidates in matches:
        if len(candidates) == 1 and len(owners[next(iter(candidates))]) == 1:
            coverage.matched_items.add(identity)
            coverage.matched_files.update(candidates)
        elif candidates:
            coverage.ambiguous_items.add(identity)
            coverage.ambiguous_sidecars += 1
        else:
            coverage.sidecars_without_local_match += 1
    coverage.ambiguous_items -= coverage.matched_items
    coverage.missing_items = set(items) - coverage.matched_items - coverage.ambiguous_items
    coverage.unmatched_files = files - coverage.matched_files
    coverage.file_owners = dict(owners)
    return items, memberships, album_titles, raw_files, warnings, sidecars, coverage


def edited_original_path(path: PurePosixPath) -> PurePosixPath | None:
    """Derive an edited file's original name without removing literal parentheses."""
    match = re.fullmatch(r"(.+)-edited(\(\d+\))?", path.stem)
    if match is None:
        return None
    base, number = match.groups()
    return path.with_name(base + (number or "") + path.suffix)


def selected_media_files(
    coverage: Coverage, selected: set[ItemIdentity]
) -> tuple[set[FileKey], set[FileKey]]:
    """Count direct files and unclaimed edits of unambiguously selected originals.

    Recognise photo-edited.jpg, photo-edited(1).jpg and photo(1)-edited.jpg.
    Filename case, extension, directory and duplicate index must match exactly.
    A variant with its own metadata claim is never assigned an inherited date.
    """
    direct = {
        key
        for key in coverage.matched_files
        if coverage.file_owners[key] <= selected and media_type(key[0]) != "Other"
    }
    filenames: defaultdict[str, set[FileKey]] = defaultdict(set)
    for key in coverage.matched_files | coverage.unmatched_files:
        filenames[key[0]].add(key)
    edited: set[FileKey] = set()
    for key in coverage.unmatched_files:
        if key in coverage.file_owners or media_type(key[0]) == "Other":
            continue
        path = PurePosixPath(key[0])
        original_path = edited_original_path(path)
        if original_path is None:
            continue
        originals = filenames.get(str(original_path), set())
        if len(originals) == 1 and originals <= direct:
            edited.add(key)
    return direct, edited


def counts(identities: Iterable[ItemIdentity], items: Items) -> Counter[str]:
    return Counter(items[identity][1] for identity in identities)


def print_table(label: str, rows: Iterable[tuple[str, Counter[str]]]) -> None:
    rows = [(str(name), tally) for name, tally in rows]
    # Escape control characters so album names cannot alter the terminal output.
    rows = [
        ("".join(c if c.isprintable() else repr(c)[1:-1] for c in name), tally)
        for name, tally in rows
    ]
    width = max([len(label)] + [len(name) for name, _ in rows])
    print(f"{label:<{width}}  {'Photos':>8}  {'Videos':>8}  {'Other':>8}  {'Total':>8}")
    for name, tally in rows:
        print(
            f"{name:<{width}}  {tally['Photos']:>8,}  {tally['Videos']:>8,}  "
            f"{tally['Other']:>8,}  {sum(tally.values()):>8,}"
        )
