"""Read-only checksum, metadata and album validation of immich-go folder archives."""

import argparse
import base64
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from immich_ctl.config import ConfigError, load_config
from immich_ctl.takeout import media_type

JSON_LIMIT = 8 * 1024 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024


class ValidationError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """Only account reads, metadata searches and album membership reads are exposed."""

    def __init__(self, server: str, key: str, timeout: float):
        self.server, self.key, self.timeout = server, key, timeout
        self.opener = build_opener(NoRedirects())

    def read(self, path: str, body: dict[str, Any] | None = None) -> Any:
        request = Request(
            self.server + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "x-api-key": self.key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="GET" if body is None else "POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ValidationError(f"Unexpected HTTP status {response.status}.")
                payload = response.read(RESPONSE_LIMIT + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if 300 <= status < 400:
                raise ValidationError("Redirect refused; configure the final Immich URL.") from None
            raise ValidationError(
                f"Immich returned HTTP {status}; check the URL and API key permissions "
                "(user.read, asset.read, album.read)."
            ) from None
        except (URLError, OSError, ValueError):
            raise ValidationError(
                "Connection failed; check the URL, network and TLS certificate."
            ) from None
        if len(payload) > RESPONSE_LIMIT:
            raise ValidationError("Immich response exceeds 16 MiB; validation is incomplete.")
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError):
            raise ValidationError("Immich did not return valid JSON.") from None

    def matches(self, checksum: str, owner: str) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        # Explicit visibility is necessary: Immich 2 defaults searches to the timeline.
        # Hidden includes Live Photo motion assets; locked and trashed assets are excluded.
        for visibility in ("timeline", "archive", "hidden"):
            page = 1
            seen: set[str] = set()
            while True:
                result = self.read(
                    "/search/metadata",
                    {
                        "checksum": checksum,
                        "visibility": visibility,
                        "withDeleted": False,
                        # withExif=true uses an inner join in Immich 2 and can hide
                        # uploads whose metadata jobs have not run. Read details separately.
                        "withExif": False,
                        "withStacked": True,
                        "page": page,
                        "size": 1000,
                    },
                )
                assets = result.get("assets") if isinstance(result, dict) else None
                if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
                    raise ValidationError("Unexpected search response; validation is incomplete.")
                for asset in assets["items"]:
                    if (
                        not isinstance(asset, dict)
                        or not isinstance(asset.get("id"), str)
                        or not asset["id"]
                        or not isinstance(asset.get("ownerId"), str)
                    ):
                        raise ValidationError("Search result lacks asset identity or owner.")
                    identity = asset["id"]
                    if identity in seen:
                        raise ValidationError(
                            "Search repeated an asset across pages; retry validation."
                        )
                    seen.add(identity)
                    encoded = asset.get("checksum")
                    if not isinstance(encoded, str):
                        raise ValidationError("Search result lacks a valid checksum.")
                    try:
                        actual = base64.b64decode(encoded, validate=True).hex()
                    except (ValueError, TypeError):
                        raise ValidationError("Search result lacks a valid checksum.") from None
                    if actual != checksum:
                        raise ValidationError(
                            "Search returned a different checksum; validation stopped."
                        )
                    if asset["ownerId"] == owner and not asset.get("isTrashed", False):
                        found[identity] = asset
                if "nextPage" not in assets:
                    raise ValidationError("Search response lacks pagination information.")
                following = assets["nextPage"]
                if following is None:
                    break
                if str(following) != str(page + 1) or not assets["items"]:
                    raise ValidationError("Unexpected search pagination; validation is incomplete.")
                page += 1
        return list(found.values())

    def albums(self, identity: str) -> set[str]:
        result = self.read("/albums?" + urlencode({"assetId": identity}))
        if not isinstance(result, list) or any(
            not isinstance(album, dict) or not isinstance(album.get("albumName"), str)
            for album in result
        ):
            raise ValidationError("Unexpected album response; validation is incomplete.")
        return {album["albumName"] for album in result}


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def compare(
    metadata: dict[str, Any], asset: dict[str, Any], albums: set[str]
) -> tuple[list[str], list[str], int]:
    """Compare only explicit sidecar values; omitted Go omitempty fields are unknown."""
    differences: list[str] = []
    unchecked: list[str] = []
    checked = 0
    exif = asset.get("exifInfo")
    exif = exif if isinstance(exif, dict) else {}
    for field in ("dateTaken", "description", "latitude", "longitude", "favorited", "archived"):
        if field not in metadata:
            continue
        expected = metadata[field]
        actual: Any
        if field == "dateTaken":
            expected = timestamp(expected)
            actual = timestamp(asset.get("fileCreatedAt"))
            valid = expected is not None
            equal = expected == actual
        elif field in {"latitude", "longitude"}:
            limit = 90 if field == "latitude" else 180
            valid = (
                type(expected) in (float, int)
                and abs(expected) <= limit
                and math.isfinite(expected)
            )
            actual = exif.get(field)
            equal = (
                isinstance(actual, (float, int))
                and not isinstance(actual, bool)
                and abs(actual) <= limit
                and math.isfinite(actual)
                and valid
                and abs(expected - actual) <= 0.00001
            )
        elif field == "description":
            valid = isinstance(expected, str)
            actual = exif.get(field)
            equal = expected == actual
        else:
            valid = isinstance(expected, bool)
            actual = asset.get("isFavorite") if field == "favorited" else asset.get("isArchived")
            if field == "archived" and not isinstance(actual, bool):
                visibility = asset.get("visibility")
                actual = (
                    visibility == "archive"
                    if isinstance(visibility, str)
                    and visibility in {"timeline", "archive", "hidden"}
                    else None
                )
            equal = isinstance(actual, bool) and actual == expected
        if not valid:
            unchecked.append(f"invalid sidecar {field}")
        elif actual is None:
            unchecked.append(f"server did not supply {field}")
        else:
            checked += 1
            if not equal:
                differences.append(f"{field} differs")
    if "albums" in metadata:
        expected_albums = metadata["albums"]
        if not isinstance(expected_albums, list) or any(
            not isinstance(album, dict)
            or not isinstance(album.get("title"), str)
            or not album["title"]
            for album in expected_albums
        ):
            unchecked.append("invalid sidecar albums")
        else:
            for title in sorted({album["title"] for album in expected_albums}):
                checked += 1
                if title not in albums:
                    differences.append("missing album membership: " + title)
    for field in sorted(
        metadata.keys()
        - {
            "software",
            "fileName",
            "fileDate",
            "dateTaken",
            "description",
            "latitude",
            "longitude",
            "favorited",
            "archived",
            "albums",
        }
    ):
        unchecked.append("unsupported sidecar field: " + field)
    return differences, unchecked, checked


def inspect_folder(folder: Path) -> tuple[list[tuple[Path, dict[str, Any] | None]], list[str]]:
    if not folder.is_dir():
        raise ValidationError("Validation requires an existing extracted folder.")
    media: list[tuple[Path, dict[str, Any] | None]] = []
    warnings: list[str] = []

    # Do not follow symlinks, including files pointing outside the extracted archive.
    # Unlike rglob on recent Python versions, fail on unreadable directories.
    def scan_error(error: OSError) -> None:
        raise error

    files = sorted(
        Path(directory) / name
        for directory, directories, names in os.walk(folder, onerror=scan_error, followlinks=False)
        for name in directories + names
    )
    by_name: dict[tuple[Path, str], list[Path]] = defaultdict(list)
    for path in files:
        if path.suffix.casefold() == ".json":
            # Preserve the media filename's case; only the sidecar extension varies.
            by_name[path.parent, path.with_suffix("").name].append(path)
    for path in files:
        if path.is_symlink():
            warnings.append(f"symlink skipped: {path.relative_to(folder)}")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        if suffix == ".json":
            if not path.with_suffix("").is_file():
                warnings.append(f"orphan sidecar: {path.relative_to(folder)}")
            continue
        if suffix == ".xmp":
            warnings.append(f"XMP metadata not checked: {path.relative_to(folder)}")
            continue
        if media_type(path.name) == "Other":
            warnings.append(f"unsupported file: {path.relative_to(folder)}")
            continue
        sidecars = by_name[path.parent, path.name]
        metadata = None
        if len(sidecars) != 1 or sidecars[0].is_symlink():
            warnings.append(f"missing or ambiguous JSON sidecar: {path.relative_to(folder)}")
        else:
            with sidecars[0].open("rb") as stream:
                payload = stream.read(JSON_LIMIT + 1)
            try:
                parsed = json.loads(payload) if len(payload) <= JSON_LIMIT else None
                if not isinstance(parsed, dict):
                    raise ValueError()
                metadata = parsed
            except (ValueError, UnicodeError):
                warnings.append(f"invalid or oversized JSON sidecar: {path.relative_to(folder)}")
        media.append((path, metadata))
    if not media:
        raise ValidationError("No supported photos or videos found in the extracted folder.")
    return media, warnings


def validate(folder: Path, client: Client) -> dict[str, Any]:
    media, warnings = inspect_folder(folder)
    user = client.read("/users/me")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
        raise ValidationError("Unexpected account response: no user ID.")
    cache: dict[str, list[dict[str, Any]]] = {}
    album_cache: dict[str, set[str]] = {}
    rows = []
    for path, metadata in media:
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha1").hexdigest()
        if checksum not in cache:
            cache[checksum] = client.matches(checksum, user["id"])
            if len(cache[checksum]) == 1:
                identity = cache[checksum][0]["id"]
                detail = client.read("/assets/" + quote(identity, safe=""))
                if (
                    not isinstance(detail, dict)
                    or detail.get("id") != identity
                    or detail.get("ownerId") != user["id"]
                    or detail.get("checksum") != base64.b64encode(bytes.fromhex(checksum)).decode()
                    or detail.get("isTrashed", False)
                ):
                    raise ValidationError("Asset identity changed during validation; retry.")
                cache[checksum] = [detail]
        matches = cache[checksum]
        row: dict[str, Any] = {
            "path": str(path.relative_to(folder)),
            "checksum": checksum,
            "asset_ids": [asset["id"] for asset in matches],
            "differences": [],
            "unchecked": [],
            "checks": 0,
        }
        if not matches:
            row["status"] = "missing"
        elif len(matches) > 1:
            row["status"] = "ambiguous"
        else:
            asset = matches[0]
            differences: list[str]
            if metadata is None:
                differences, unchecked, checked = [], ["no usable JSON sidecar"], 0
            else:
                if metadata.get("albums") and asset["id"] not in album_cache:
                    album_cache[asset["id"]] = client.albums(asset["id"])
                differences, unchecked, checked = compare(
                    metadata, asset, album_cache.get(asset["id"], set())
                )
                if not checked:
                    unchecked.append("no comparable sidecar metadata")
            row.update(differences=differences, unchecked=unchecked, checks=checked)
            row["status"] = "different" if differences else "incomplete" if unchecked else "ok"
        rows.append(row)
    counts = Counter(row["status"] for row in rows)
    return {
        "files": rows,
        "counts": {
            name: counts[name] for name in ("ok", "missing", "ambiguous", "different", "incomplete")
        },
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Extracted immich-go archive with .JSON sidecars")
    parser.add_argument("--config", required=True, type=Path, metavar="INI")
    parser.add_argument(
        "--timeout", type=float, default=30, help="Request timeout (default: 30 seconds)"
    )
    parser.add_argument(
        "--details", action="store_true", help="Also list files whose checks passed"
    )
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    key = ""
    try:
        server, key = load_config(args.config.expanduser())
        report = validate(args.folder.expanduser().resolve(), Client(server, key, args.timeout))
    except (ValidationError, ConfigError, OSError) as error:
        print(
            "Error: " + str(error).replace(key, "[REDACTED]") if key else f"Error: {error}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        return 130

    # Redact before serialising, including keys containing quotes or backslashes.
    def redact(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(key, "[REDACTED]")
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {name: redact(item) for name, item in value.items()}
        return value

    exit_code = (
        2 if report["warnings"] or any(row["status"] != "ok" for row in report["files"]) else 0
    )
    report = redact(report)
    # Quote filenames and remote album titles so control characters cannot reach the terminal.
    if args.json:
        output = json.dumps(report, indent=2, ensure_ascii=True)
    else:
        lines = [
            f"Files: {len(report['files'])}; "
            + "; ".join(f"{k}: {v}" for k, v in report["counts"].items())
        ]
        for row in report["files"]:
            if args.details or row["status"] != "ok":
                lines.append(
                    f"{row['status']}: "
                    + json.dumps(row["path"])
                    + " "
                    + json.dumps(row["differences"] + row["unchecked"])
                )
        lines.extend("Warning: " + json.dumps(warning) for warning in report["warnings"])
        lines.append(
            "Scope: checksum presence and explicit JSON fields; excludes locked/trashed assets. "
            "Album titles checked; extra memberships allowed. Not a backup/restore test."
        )
        output = "\n".join(lines)
    print(output)
    return exit_code
