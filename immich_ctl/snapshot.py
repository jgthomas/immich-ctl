"""Save an account's asset checksums and accessible album memberships as JSON."""

import argparse
import base64
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from immich_ctl.config import ConfigError, load_config
from immich_ctl.folder_validate import Client, ValidationError

VISIBILITIES = ("timeline", "archive", "hidden")
PAGE_SIZE = 1000


class SnapshotChanged(ValidationError):
    """The inventory changed while it was being collected."""


def identity(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("Immich response is missing an ID.")
    return value


def album_users(value: Any) -> list[dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError("Unexpected Immich album users.")
    roles: dict[str, str] = {}
    for entry in value:
        user = entry.get("user") if isinstance(entry, dict) else None
        if not isinstance(user, dict):
            raise ValidationError("Unexpected Immich album user.")
        user_id = identity(user.get("id"))
        role = entry.get("role")
        if not isinstance(role, str) or not role or user_id in roles:
            raise ValidationError("Unexpected or repeated Immich album user role.")
        roles[user_id] = role
    return [{"user_id": user_id, "role": role} for user_id, role in sorted(roles.items())]


def list_albums(client: Client) -> dict[str, dict[str, Any]]:
    results = client.read("/albums")
    if not isinstance(results, list):
        raise ValidationError("Unexpected Immich album list.")
    albums = {}
    for result in results:
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("albumName"), str)
            or type(result.get("assetCount")) is not int
            or result["assetCount"] < 0
        ):
            raise ValidationError("Unexpected Immich album record.")
        album_id = identity(result.get("id"))
        if album_id in albums:
            raise SnapshotChanged("Album list repeated an ID.")
        description = result.get("description")
        if description is not None and not isinstance(description, str):
            raise ValidationError("Unexpected Immich album description.")
        cover = result.get("albumThumbnailAssetId")
        albums[album_id] = {
            "name": result["albumName"],
            "description": description,
            "cover_asset_id": None if cover is None else identity(cover),
            "users": album_users(result.get("albumUsers")),
            "count": result["assetCount"],
            "updated_at": result.get("updatedAt"),
            "asset_updated_at": result.get("lastModifiedAssetTimestamp"),
        }
    return albums


def list_stacks(client: Client) -> list[dict[str, Any]]:
    try:
        results = client.read("/stacks")
    except ValidationError as error:
        raise ValidationError(
            f"Cannot read stacks. Check the stack.read permission. {error}"
        ) from error
    if not isinstance(results, list):
        raise ValidationError("Unexpected Immich stack list.")
    stacks: dict[str, dict[str, Any]] = {}
    assigned: set[str] = set()
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("assets"), list):
            raise ValidationError("Unexpected Immich stack record.")
        stack_id = identity(row.get("id"))
        primary = identity(row.get("primaryAssetId"))
        members = set()
        for asset in row["assets"]:
            member = identity(asset.get("id") if isinstance(asset, dict) else None)
            if member in members or member in assigned:
                raise SnapshotChanged("A stack member ID appeared more than once.")
            members.add(member)
        if stack_id in stacks or primary not in members:
            raise SnapshotChanged("A stack ID was repeated or its primary asset was missing.")
        assigned.update(members)
        stacks[stack_id] = {
            "id": stack_id,
            "primary_asset_id": primary,
            "asset_ids": sorted(members),
        }
    return [stacks[stack_id] for stack_id in sorted(stacks)]


def search_assets(client: Client, album_id: str | None = None) -> Iterator[dict[str, Any]]:
    """Read non-locked assets, explicitly including archived, hidden and trashed assets."""
    # Structured search includes both active and trashed assets without a trashedAt filter.
    filters: dict[str, Any] = {"visibility": {"in": list(VISIBILITIES)}}
    seen: set[str] = set()
    if album_id is not None:
        filters["albumIds"] = {"any": [album_id]}
    query: dict[str, Any] = {
        "filter": filters,
        "size": PAGE_SIZE,
        "withExif": False,
        "withStacked": True,
    }
    cursors: set[str] = set()
    while True:
        response = client.read("/search/metadata", query)
        page = response.get("assets") if isinstance(response, dict) else None
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("items"), list)
            or "nextCursor" not in page
        ):
            raise ValidationError("Snapshot needs Immich's cursor-based metadata search.")
        for asset in page["items"]:
            if (
                not isinstance(asset, dict)
                or asset.get("visibility") not in VISIBILITIES
                or type(asset.get("isTrashed")) is not bool
            ):
                raise SnapshotChanged("Metadata search returned an asset outside its scope.")
            asset_id = identity(asset.get("id"))
            if asset_id in seen:
                raise SnapshotChanged("Metadata search repeated an asset ID.")
            seen.add(asset_id)
            yield asset
        cursor = page["nextCursor"]
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or not page["items"] or cursor in cursors:
            raise SnapshotChanged("Metadata search returned inconsistent pagination.")
        cursors.add(cursor)
        query = dict(query, cursor=cursor)


def asset_record(asset: dict[str, Any], owner: str) -> dict[str, Any]:
    asset_owner = identity(asset.get("ownerId"))
    encoded = asset.get("checksum")
    checksum = None
    if (encoded is not None and encoded != "") or asset_owner == owner:
        try:
            if not isinstance(encoded, str):
                raise ValueError()
            decoded = base64.b64decode(encoded, validate=True)
            if len(decoded) != 20:
                raise ValueError()
            checksum = decoded.hex()
        except ValueError:
            raise ValidationError("Immich returned an invalid asset checksum.") from None
    checksum_kind = "unavailable"
    if checksum is not None:
        if "libraryId" not in asset:
            checksum_kind = "unverified"
        elif asset["libraryId"] is None:
            checksum_kind = "content"
        else:
            checksum_kind = "external_library"
    filename = asset.get("originalFileName")
    created_at = asset.get("fileCreatedAt")
    motion = asset.get("livePhotoVideoId")
    favorite = asset.get("isFavorite") if asset_owner == owner else None
    if (filename is not None and not isinstance(filename, str)) or (
        created_at is not None and not isinstance(created_at, str)
    ):
        raise ValidationError("Unexpected Immich asset filename or capture time.")
    if favorite is not None and type(favorite) is not bool:
        raise ValidationError("Unexpected Immich favorite flag.")
    return {
        "id": asset["id"],
        "owner_id": asset_owner,
        "checksum": checksum,
        "checksum_kind": checksum_kind,
        "original_filename": filename,
        "file_created_at": created_at,
        "live_photo_video_id": None if motion is None else identity(motion),
        "visibility": asset["visibility"],
        "is_trashed": asset["isTrashed"],
        "is_favorite": favorite,
    }


def collect(client: Client, owner: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    before = list_albums(client)
    if client.progress:
        client.progress("Reading account assets.")
    assets = {
        asset["id"]: asset_record(asset, owner)
        for asset in search_assets(client)
        if identity(asset.get("ownerId")) == owner
    }
    albums: list[dict[str, Any]] = []
    for album_id, album in sorted(before.items()):
        if client.progress:
            client.progress(f"Reading album {len(albums) + 1}/{len(before)}.")
        members = []
        visible_count = 0
        for asset in search_assets(client, album_id):
            record = asset_record(asset, owner)
            asset_id = record["id"]
            if asset_id in assets:
                if assets[asset_id] != record:
                    raise SnapshotChanged("An album member changed during the snapshot.")
            elif record["owner_id"] == owner:
                raise SnapshotChanged(
                    "An owned album member was absent from the account inventory."
                )
            else:
                assets[asset_id] = record
            members.append(asset_id)
            if not record["is_trashed"] and record["visibility"] != "hidden":
                visible_count += 1
        # Immich's album count covers only non-trashed timeline and archived assets.
        if visible_count != album["count"]:
            raise SnapshotChanged("Album membership count changed or could not be verified.")
        albums.append(
            {
                "id": album_id,
                "name": album["name"],
                "description": album["description"],
                "cover_asset_id": album["cover_asset_id"],
                "users": album["users"],
                "asset_ids": sorted(members),
            }
        )
    if list_albums(client) != before:
        raise SnapshotChanged("The album list changed during the snapshot.")
    return [assets[asset_id] for asset_id in sorted(assets)], albums


def snapshot(client: Client, started: datetime) -> dict[str, Any]:
    account = client.read("/users/me")
    owner = identity(account.get("id") if isinstance(account, dict) else None)
    for attempt in range(3):
        try:
            stacks = list_stacks(client)
            assets, albums = collect(client, owner)
            if stacks != list_stacks(client):
                raise SnapshotChanged("The stack list changed during the snapshot.")
            break
        except SnapshotChanged:
            if attempt == 2:
                raise
            if client.progress:
                client.progress("Inventory changed during the snapshot. Restarting the scan.")
            time.sleep(2**attempt)
    return {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "server": client.server,
        "account_id": owner,
        "checksum_algorithm": "sha1",
        "scope": {
            "owned_assets": True,
            "shared_album_members": True,
            "visibilities": list(VISIBILITIES),
            "includes_trash": True,
        },
        "assets": assets,
        "albums": albums,
        "stacks": stacks,
    }


def check_output_directory(directory: Path) -> None:
    if not directory.is_dir():
        raise ValidationError("Snapshot output directory must already exist.")
    with tempfile.TemporaryDirectory(prefix=".immich-snapshot-probe-", dir=directory) as name:
        original = Path(name) / "original"
        original.touch()
        try:
            os.link(original, original.with_name("link"))
        except OSError as error:
            raise ValidationError(
                "Cannot create a hard link in the snapshot output directory. "
                "Choose a writable directory on a filesystem that supports hard links."
            ) from error


def write_snapshot(directory: Path, started: datetime, data: dict[str, Any]) -> Path:
    timestamp = started.strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"immich-snapshot-{timestamp}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".immich-snapshot-", dir=directory, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Publish atomically without replacing a manifest from another run.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, metavar="INI")
    parser.add_argument("--output-dir", required=True, type=Path, metavar="DIRECTORY")
    parser.add_argument("--timeout", type=float, default=30, help="Request timeout in seconds")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    key = ""
    try:
        directory = args.output_dir.expanduser().resolve()
        check_output_directory(directory)
        server, key = load_config(args.config.expanduser())
        client = Client(server, key, args.timeout)
        client.progress = lambda message: print(message, file=sys.stderr, flush=True)
        started = datetime.now(UTC)
        data = snapshot(client, started)
        path = write_snapshot(directory, started, data)
    except (ConfigError, ValidationError, OSError, ValueError) as error:
        message = str(error)
        print(f"Error: {message.replace(key, '[REDACTED]') if key else message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    unavailable = sum(asset["checksum"] is None for asset in data["assets"])
    if unavailable:
        print(
            f"Warning: Immich withheld checksums for {unavailable} shared assets.", file=sys.stderr
        )
    print(path)
    return 0
