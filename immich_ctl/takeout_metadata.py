"""Compare Takeout metadata with the native JSON produced by immich-go 0.32.0."""

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from immich_ctl.takeout import Coverage, capture_time


def duplicate_album_titles(titles: dict[str, str]) -> list[dict[str, Any]]:
    """Separate source directories cannot be identified uniquely by a shared title."""
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for directory, title in titles.items():
        groups[title or directory].append(directory)
    return [
        {"title": title, "source_directories": sorted(directories)}
        for title, directories in sorted(groups.items())
        if len(directories) > 1
    ]


def number(value: Any, limit: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
        raise ValueError("Invalid source GPS coordinate")
    return float(value)


def album_payloads(coverage: Coverage, directories: list[str]) -> dict[str, dict[str, Any]]:
    """Preserve full source payloads; never arbitrarily choose conflicting copies."""
    result: dict[str, dict[str, Any]] = {}
    for directory in sorted(directories):
        versions = coverage.album_metadata.get(directory, [])
        if len(versions) != 1 or directory in coverage.invalid_album_metadata:
            raise ValueError("Missing or conflicting source album metadata: " + directory)
        data = dict(versions[0])
        title = data.get("title")
        title = title if isinstance(title, str) and title else directory
        data["title"] = title
        if title in result and result[title] != data:
            raise ValueError("Conflicting source albums share a title: " + title)
        result[title] = data
    return result


def album_expectation(data: dict[str, Any]) -> dict[str, Any]:
    """Translate album enrichments using upstream's narrative/location rules."""
    result: dict[str, Any] = {"title": data["title"]}
    text = ""
    enrichments = data.get("enrichments", [])
    if not isinstance(enrichments, list):
        raise ValueError("Invalid source album enrichments")
    for enrichment in enrichments:
        if not isinstance(enrichment, dict):
            raise ValueError("Invalid source album enrichment")
        narrative_data = enrichment.get("narrativeEnrichment", {})
        location_data = enrichment.get("locationEnrichment", {})
        if not isinstance(narrative_data, dict) or not isinstance(location_data, dict):
            raise ValueError("Invalid source album enrichment")
        narrative = narrative_data.get("text", "")
        if not isinstance(narrative, str):
            raise ValueError("Invalid source album narrative")
        if narrative:
            text += ("\n" if text else "") + narrative
        locations = location_data.get("location", [])
        if not isinstance(locations, list):
            raise ValueError("Invalid source album locations")
        for location in locations:
            if not isinstance(location, dict):
                raise ValueError("Invalid source album location")
            for key, separator in (("name", "\n"), ("description", " - ")):
                value = location.get(key, "")
                if not isinstance(value, str):
                    raise ValueError("Invalid source album location text")
                if value:
                    text += (separator if text else "") + value
            result["latitude"] = number(location.get("latitudeE7", 0), 900000000) / 10000000
            result["longitude"] = number(location.get("longitudeE7", 0), 1800000000) / 10000000
    if text:
        result["description"] = text
    return result


def expectation(data: dict[str, Any], albums: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compare photo GPS and album locations at their respective metadata levels.

    The Google reader can infer an asset location from an album internally, but
    0.32.0 exports the original photo metadata without that inferred location.
    An album location is not evidence of where each photo was taken.
    """
    result: dict[str, Any] = {"albums": [album_expectation(a) for _, a in sorted(albums.items())]}
    captured = capture_time(data)
    if captured is None:
        raise ValueError("Source capture date cannot be verified")
    result["dateTaken"] = captured.isoformat()
    for field, kind in (("description", str), ("favorited", bool), ("archived", bool)):
        if field in data:
            if type(data[field]) is not kind:
                raise ValueError("Invalid source " + field)
            result[field] = data[field]
    coordinates = (0.0, 0.0)
    for key in ("geoDataExif", "geoData"):
        geo = data.get(key)
        if geo is None:
            continue
        if not isinstance(geo, dict):
            raise ValueError("Invalid source " + key)
        coordinates = (number(geo.get("latitude", 0), 90), number(geo.get("longitude", 0), 180))
        if coordinates != (0, 0):
            break
    if coordinates != (0, 0):
        result["latitude"], result["longitude"] = coordinates
    return result


def differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Omitted Go zero values are equivalent; nonzero source values must survive."""
    issues = []
    for field, value in expected.items():
        received = actual.get(field)
        if field == "albums":
            albums = received if isinstance(received, list) else []
            if (received is not None and not isinstance(received, list)) or any(
                not isinstance(a, dict) for a in albums
            ):
                issues.append("invalid generated albums")
                continue
            titles = {a.get("title") for a in albums if isinstance(a.get("title"), str)}
            if titles != {a["title"] for a in value} or len(titles) != len(albums):
                issues.append("album associations differ from source")
            for album in value:
                matches = [a for a in albums if a.get("title") == album["title"]]
                if len(matches) == 1:
                    issues.extend(
                        "album " + album["title"] + ": " + i
                        for i in differences(
                            {k: v for k, v in album.items() if k != "title"}, matches[0]
                        )
                    )
            continue
        if field == "dateTaken":
            try:
                equal = isinstance(received, str) and datetime.fromisoformat(
                    value
                ) == datetime.fromisoformat(received)
            except (ValueError, TypeError):
                equal = False
        elif field in {"latitude", "longitude"}:
            # Native JSON omits a zero coordinate even when its counterpart is nonzero.
            received = actual.get(field, 0)
            equal = (
                isinstance(received, (float, int))
                and not isinstance(received, bool)
                and math.isfinite(received)
                and abs(received - value) <= 0.00001
            )
        else:
            default = False if isinstance(value, bool) else ""
            received = actual.get(field, default)
            equal = type(received) is type(value) and received == value
        if not equal:
            issues.append(field + " differs from source")
    return issues
