"""Read-only checksum, metadata and album validation of immich-go folder archives."""

import argparse
import base64
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from immich_ctl.config import ConfigError, load_config
from immich_ctl.image_format import corrected_suffix
from immich_ctl.takeout import media_type
from immich_ctl.validation_state import Checkpoint

JSON_LIMIT = 8 * 1024 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024


class ValidationError(Exception):
    pass


class TemporaryFailure(ValidationError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """Only account reads, metadata searches and album membership reads are exposed."""

    def __init__(self, server: str, key: str, timeout: float):
        self.server, self.key, self.timeout = server, key, timeout
        self.opener = build_opener(NoRedirects())
        self.progress: Callable[[str], None] | None = None

    def read(self, path: str, body: dict[str, Any] | None = None) -> Any:
        for attempt in range(3):
            try:
                return self._read_once(path, body)
            except TemporaryFailure:
                if attempt == 2:
                    raise
                if self.progress:
                    self.progress("Temporary server/connection failure; retrying request.")
                time.sleep(2**attempt)

    def _read_once(self, path: str, body: dict[str, Any] | None = None) -> Any:
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
            if status in {429, 500, 502, 503, 504}:
                raise TemporaryFailure(
                    f"Immich returned HTTP {status}; retry validation later."
                ) from None
            raise ValidationError(
                f"Immich returned HTTP {status}; check the URL and API key permissions "
                "(user.read, asset.read, album.read)."
            ) from None
        except (URLError, OSError):
            raise TemporaryFailure(
                "Connection failed; check the URL, network and TLS certificate."
            ) from None
        except ValueError:
            raise ValidationError("Invalid connection configuration.") from None
        if len(payload) > RESPONSE_LIMIT:
            raise ValidationError("Immich response exceeds 16 MiB; validation is incomplete.")
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError):
            raise ValidationError("Immich did not return valid JSON.") from None

    def matches(self, checksum: str, owner: str) -> list[dict[str, Any]]:
        return self._search(checksum, owner)

    def inventory(self, owner: str) -> list[dict[str, Any]]:
        """Read the uploader's owned-asset scope, including trash, without writes."""
        return self._search(None, owner, include_trashed=True)

    def _search(
        self,
        checksum: str | None,
        owner: str,
        *,
        include_trashed: bool = False,
        with_exif: bool = False,
    ) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        # Explicit visibility is necessary: Immich 2 defaults searches to the timeline.
        # Hidden includes Live Photo motion assets. The uploader also searches trash;
        # the post-upload validator must not accept trashed copies as preserved uploads.
        queries = [(visibility, False) for visibility in ("timeline", "archive", "hidden")]
        if include_trashed:
            queries += [(visibility, True) for visibility in ("timeline", "archive", "hidden")]
        for visibility, trash_only in queries:
            page = 1
            seen: set[str] = set()
            while True:
                result = self.read(
                    "/search/metadata",
                    {
                        **({"checksum": checksum} if checksum is not None else {}),
                        "visibility": visibility,
                        "withDeleted": include_trashed,
                        **({"trashedAfter": "0001-01-01T00:00:00Z"} if trash_only else {}),
                        # withExif=true uses an inner join in Immich 2 and can hide
                        # uploads whose metadata jobs have not run. Read details separately.
                        "withExif": with_exif,
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
                    if len(actual) != 40:
                        raise ValidationError("Search result lacks a valid checksum.")
                    if checksum is not None and actual != checksum:
                        raise ValidationError(
                            "Search returned a different checksum; validation stopped."
                        )
                    if asset["ownerId"] == owner and (
                        include_trashed or not asset.get("isTrashed", False)
                    ):
                        if identity in found and found[identity]["checksum"] != encoded:
                            raise ValidationError("Asset changed during search; retry the audit.")
                        found[identity] = asset
                if "nextPage" not in assets:
                    raise ValidationError("Search response lacks pagination information.")
                following = assets["nextPage"]
                if self.progress:
                    self.progress(
                        f"Server inventory: {visibility}, page {page}; {len(found)} assets."
                    )
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
    metadata: dict[str, Any], asset: dict[str, Any], albums: set[str], *, native: bool = False
) -> tuple[list[str], list[str], int]:
    """Compare only explicit sidecar values; omitted Go omitempty fields are unknown."""
    differences: list[str] = []
    unchecked: list[str] = []
    checked = 0
    exif = asset.get("exifInfo")
    exif = exif if isinstance(exif, dict) else {}
    for field in ("dateTaken", "description", "latitude", "longitude", "favorited", "archived"):
        if field not in metadata:
            if native:
                unchecked.append(f"sidecar omits {field}; source value unverified")
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
            for album in expected_albums:
                for field in sorted(album.keys() - {"title"}):
                    unchecked.append("unsupported album metadata: " + album["title"] + ": " + field)
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


def inspect_folder(
    folder: Path, *, check_filetypes: bool = True
) -> tuple[list[tuple[Path, dict[str, Any] | None]], list[str]]:
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
        if check_filetypes and (format_suffix := corrected_suffix(path)) is not None:
            warnings.append(
                f"image format mismatch: {path.relative_to(folder)}; use {format_suffix}"
            )
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


def validation_inventory(client: Client, owner: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Discard an inconsistent scan completely. Never silently deduplicate pages.
    for attempt in range(3):
        try:
            assets = client._search(None, owner)
            details = {a["id"]: a for a in client._search(None, owner, with_exif=True)}
            original = {a["id"]: a for a in assets}
            if any(
                identity not in original or detail["checksum"] != original[identity]["checksum"]
                for identity, detail in details.items()
            ):
                raise ValidationError("Asset identity changed during inventory; retry validation.")
            return assets, details
        except ValidationError as error:
            if "repeated an asset across pages" not in str(error) or attempt == 2:
                raise
            if client.progress:
                client.progress("Inventory changed during pagination; restarting scan.")
            time.sleep(2**attempt)
    raise ValidationError("Could not obtain a consistent inventory.")


def validate(
    folder: Path,
    client: Client,
    *,
    checkpoint_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    client.progress = progress
    if progress:
        progress("Inspecting local files and sidecars.")
    media, warnings = inspect_folder(folder)
    checkpoint = Checkpoint(checkpoint_path, folder)
    try:
        return validate_media(folder, client, media, warnings, checkpoint)
    finally:
        checkpoint.close()


def asset_detail(
    asset: dict[str, Any], client: Client, details: dict[str, Any], owner: str
) -> dict[str, Any]:
    identity = asset["id"]
    if identity not in details:
        details[identity] = client.read("/assets/" + quote(identity, safe=""))
    detail = details[identity]
    if (
        not isinstance(detail, dict)
        or detail.get("id") != identity
        or detail.get("ownerId") != owner
        or detail.get("checksum") != asset["checksum"]
        or detail.get("isTrashed", False)
        or detail.get("visibility") == "locked"
    ):
        raise ValidationError("Asset identity changed during validation; retry.")
    return detail


def validate_media(
    folder: Path,
    client: Client,
    media: list[tuple[Path, dict[str, Any] | None]],
    warnings: list[str],
    checkpoint: Checkpoint,
) -> dict[str, Any]:
    user = client.read("/users/me")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
        raise ValidationError("Unexpected account response: no user ID.")
    checkpoint.record(
        {
            "folder": str(folder),
            "owner_id": user["id"],
            "complete": False,
            "checked": 0,
            "total": len(media),
        }
    )
    assets, details = validation_inventory(client, user["id"])
    by_checksum: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    live_parents: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in assets:
        by_checksum[base64.b64decode(asset["checksum"]).hex()].append(asset)
        detail = details.get(asset["id"], asset)
        if not isinstance(detail, dict):
            continue
        motion = detail.get("livePhotoVideoId")
        if detail.get("type") == "IMAGE" and isinstance(motion, str) and motion:
            live_parents[motion].append(asset)
    album_cache: dict[str, set[str]] = {}
    rows = []
    for path, metadata in media:
        checksum = checkpoint.checksum(path)
        matches = by_checksum.get(checksum, [])
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
            asset = asset_detail(matches[0], client, details, user["id"])
            differences: list[str]
            if metadata is None:
                differences, unchecked, checked = [], ["no usable JSON sidecar"], 0
            else:
                if metadata.get("albums") and asset["id"] not in album_cache:
                    album_cache[asset["id"]] = client.albums(asset["id"])
                effective_albums = set(album_cache.get(asset["id"], set()))
                expected = metadata.get("albums")
                wanted = (
                    {a["title"] for a in expected}
                    if isinstance(expected, list)
                    and all(
                        isinstance(a, dict) and isinstance(a.get("title"), str) for a in expected
                    )
                    else set()
                )
                # Immich presents a linked Live Photo through its still image.
                # Only album checks inherit; the video's own metadata stays checked.
                if asset.get("type") == "VIDEO" and wanted - effective_albums:
                    via = []
                    for parent in live_parents[asset["id"]]:
                        parent = asset_detail(parent, client, details, user["id"])
                        if (
                            parent.get("type") != "IMAGE"
                            or parent.get("livePhotoVideoId") != asset["id"]
                        ):
                            raise ValidationError("Live Photo link changed; retry validation.")
                        identity = parent["id"]
                        if identity not in album_cache:
                            album_cache[identity] = client.albums(identity)
                        if album_cache[identity] & (wanted - effective_albums):
                            via.append(identity)
                        effective_albums.update(album_cache[identity])
                    if via:
                        row["album_membership_via"] = via
                differences, unchecked, checked = compare(
                    metadata, asset, effective_albums, native=True
                )
                if not checked:
                    unchecked.append("no comparable sidecar metadata")
            row.update(differences=differences, unchecked=unchecked, checks=checked)
            row["status"] = "different" if differences else "incomplete" if unchecked else "ok"
        rows.append(row)
        checkpoint.record(
            {
                "folder": str(folder),
                "owner_id": user["id"],
                "complete": False,
                "checked": len(rows),
                "total": len(media),
                "last_result": row,
            }
        )
        if client.progress and (len(rows) % 100 == 0 or len(rows) == len(media)):
            client.progress(f"Validated {len(rows)}/{len(media)} files.")
    counts = Counter(row["status"] for row in rows)
    report = {
        "files": rows,
        "counts": {
            name: counts[name] for name in ("ok", "missing", "ambiguous", "different", "incomplete")
        },
        "warnings": warnings,
    }

    checkpoint.record(
        {
            "folder": str(folder),
            "owner_id": user["id"],
            "complete": True,
            "checked": len(rows),
            "total": len(media),
            "counts": report["counts"],
        }
    )
    return report


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
    parser.add_argument("--quiet", action="store_true", help="Hide progress on stderr")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--checkpoint", type=Path, help="Private local hash/progress database")
    group.add_argument(
        "--no-checkpoint", action="store_true", help="Do not save local hashes/progress"
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    key = ""
    try:
        server, key = load_config(args.config.expanduser())
        folder = args.folder.expanduser().resolve()
        checkpoint_path = None
        if not args.no_checkpoint:
            identity = hashlib.sha256(str(folder).encode()).hexdigest()
            cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            checkpoint_path = args.checkpoint or cache_root / "immich-ctl" / "validation" / (
                identity + ".sqlite3"
            )
            if checkpoint_path.expanduser().resolve() == args.config.expanduser().resolve():
                raise ValidationError("Checkpoint cannot overwrite the credentials file.")

        def progress(message: str) -> None:
            print(message.replace(key, "[REDACTED]"), file=sys.stderr, flush=True)

        report = validate(
            folder,
            Client(server, key, args.timeout),
            checkpoint_path=checkpoint_path,
            progress=None if args.quiet else progress,
        )
    except (ValidationError, ConfigError, OSError, sqlite3.Error) as error:
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
