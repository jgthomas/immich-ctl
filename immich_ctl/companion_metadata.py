"""Exact local Go-default equivalence; no tolerance-based merging of still copies."""

import json
import math
from datetime import UTC
from typing import Any

from immich_ctl.folder_validate import timestamp


def normalize(data: dict[str, Any], *, album: bool = False) -> dict[str, Any]:
    result = dict(data)
    if not album:
        result.pop("fileName", None)
        result.pop("software", None)
    for field, default in (
        ("description", ""),
        ("favorited", False),
        ("archived", False),
        ("latitude", 0),
        ("longitude", 0),
    ):
        if field not in result:
            continue
        value = result[field]
        if field in {"latitude", "longitude"}:
            limit = 90 if field == "latitude" else 180
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError("Invalid local coordinate: " + field)
            result[field] = float(value)
        elif type(value) is not type(default):
            raise ValueError("Invalid local metadata: " + field)
        if value == default:
            del result[field]
    if not album:
        for default_field, default_value in (
            ("description", ""),
            ("favorited", False),
            ("archived", False),
            ("latitude", 0.0),
            ("longitude", 0.0),
        ):
            result.setdefault(default_field, default_value)
    if "dateTaken" in result:
        captured = timestamp(result["dateTaken"])
        if captured is None:
            raise ValueError("Invalid local capture date")
        result["dateTaken"] = captured.astimezone(UTC).isoformat()
    if "albums" in result:
        albums = result.pop("albums")
        if not isinstance(albums, list):
            raise ValueError("Invalid local albums")
        by_title = {}
        for payload in albums:
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("title"), str)
                or not payload["title"]
            ):
                raise ValueError("Invalid local album payload")
            if payload["title"] in by_title:
                raise ValueError("Repeated local album title")
            by_title[payload["title"]] = normalize(payload, album=True)
        if by_title:
            result["albums"] = [by_title[title] for title in sorted(by_title)]
    return result


def albums_equal(actual: Any, expected: Any) -> bool:
    try:
        a = normalize({"albums": actual})
        b = normalize({"albums": expected})
        return json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(
            b, sort_keys=True, allow_nan=False
        )
    except (ValueError, TypeError):
        return False
