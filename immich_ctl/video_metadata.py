"""Checker-only interpretation of Apple and standard QuickTime/MP4 metadata tags."""

import re
from datetime import UTC
from typing import Any

from immich_ctl.folder_validate import timestamp


def coordinates(value: Any) -> list[float] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/", value)
    if match is None:
        return None
    result = [float(match[1]), float(match[2])]
    return result if abs(result[0]) <= 90 and abs(result[1]) <= 180 else None


def checker_metadata(
    tags: dict[str, Any], streams: list[Any], identifier: str | None
) -> dict[str, Any]:
    containers = [("format", tags)]
    for number, stream in enumerate(streams):
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            containers.append((f"video_stream_{number}", stream.get("tags", {})))
    sources: dict[str, list[dict[str, Any]]] = {"capture_time": [], "coordinates": []}
    issues = []
    values: dict[str, Any] = {"capture_time": None, "coordinates": None}
    for location, data in containers:
        if not isinstance(data, dict):
            issues.append(location + ": invalid embedded tags")
            continue
        for field, keys in (
            ("capture_time", ("com.apple.quicktime.creationdate", "creation_time")),
            ("coordinates", ("com.apple.quicktime.location.ISO6709", "location")),
        ):
            for key in keys:
                if key not in data:
                    continue
                raw = data[key]
                value: Any
                if field == "capture_time":
                    captured = timestamp(raw)
                    value = captured.astimezone(UTC).isoformat() if captured else None
                else:
                    value = coordinates(raw)
                sources[field].append(
                    {"source": location + "." + key, "value": raw, "normalized": value}
                )
                if value is None:
                    issues.append(location + "." + key + ": invalid embedded " + field)
                elif values[field] is None:
                    values[field] = value
                elif value != values[field]:
                    issues.append(
                        "Different embedded "
                        + field
                        + " tags; capture and container/stream metadata require review"
                    )
    return {
        "identifier": identifier.upper() if identifier else None,
        **values,
        "metadata_sources": sources,
        "metadata_issues": sorted(set(issues)),
    }
