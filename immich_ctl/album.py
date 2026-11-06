"""Create/reuse an owned album and add assets without moving or deleting them."""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import UUID

from immich_ctl.config import ConfigError, load_config
from immich_ctl.folder_validate import RESPONSE_LIMIT, Client, ValidationError

IDS_FILE_LIMIT = 1024 * 1024
BATCH_SIZE = 100


def asset_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Asset and album IDs must be hyphenated UUID v4 values.")
    try:
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value.lower():
            raise ValueError()
        return str(parsed)
    except (ValueError, AttributeError):
        raise ValueError("Asset and album IDs must be hyphenated UUID v4 values.") from None


def collect_ids(values: list[str], files: list[Path]) -> list[str]:
    values = list(values)
    for path in files:
        with path.expanduser().open("rb") as stream:
            payload = stream.read(IDS_FILE_LIMIT + 1)
        if len(payload) > IDS_FILE_LIMIT:
            raise ValueError("IDs file exceeds 1 MiB.")
        values.extend(payload.decode("utf-8").split())
    return list(dict.fromkeys(asset_id(value) for value in values))


class AlbumClient(Client):
    def request(self, method: str, path: str, body: dict[str, Any]) -> Any:
        if method not in {"POST", "PUT"}:
            raise ValueError("Unsupported album operation.")
        request = Request(
            self.server + path,
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": self.key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status not in {200, 201}:
                    raise ValidationError(
                        "Unexpected album response status; inspect the album before retrying."
                    )
                payload = response.read(RESPONSE_LIMIT + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if 300 <= status < 400:
                raise ValidationError("Redirect refused; configure the final Immich URL.") from None
            raise ValidationError(
                f"Immich returned HTTP {status}; check album.create and albumAsset.create permissions. Album changes may be partial."
            ) from None
        except (URLError, OSError, ValueError):
            raise ValidationError(
                "Connection failed; operation outcome is unknown. Inspect the album before retrying; requests are not automatically retried."
            ) from None
        if len(payload) > RESPONSE_LIMIT:
            raise ValidationError(
                "Album response exceeds size limit; operation may have succeeded."
            )
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError):
            raise ValidationError(
                "Invalid album JSON response; operation may have succeeded."
            ) from None


def owned_album(value: Any, owner: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("albumName"), str):
        raise ValidationError("Unexpected album response.")
    asset_id(value.get("id", ""))
    # Immich 3 uses albumUsers; earlier releases returned ownerId/owner.
    album_owner = value.get("ownerId")
    if album_owner is None and isinstance(value.get("owner"), dict):
        album_owner = value["owner"].get("id")
    if album_owner is None:
        users = value.get("albumUsers", [])
        owners = (
            [
                u.get("user", {}).get("id")
                for u in users
                if isinstance(u, dict)
                and u.get("role") == "owner"
                and isinstance(u.get("user"), dict)
            ]
            if isinstance(users, list)
            else []
        )
        if len(owners) == 1:
            album_owner = owners[0]
    if album_owner != owner:
        raise ValidationError(
            "Album ownership could not be verified; only owned albums are supported."
        )
    return value


def manage(
    client: AlbumClient,
    ids: list[str],
    *,
    name: str | None = None,
    identity: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if (name is None) == (identity is None):
        raise ValueError("Choose exactly one of album name or album ID.")
    if name is not None and not name.strip():
        raise ValueError("Album name must not be blank.")
    ids = collect_ids(ids, [])
    if identity is not None:
        identity = asset_id(identity)
    user = client.read("/users/me")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
        raise ValidationError("Unexpected account response.")
    owner = user["id"]
    album = None
    if identity:
        album = owned_album(client.read("/albums/" + identity), owner)
        if album["id"] != identity:
            raise ValidationError("Album identity changed; no writes attempted.")
    else:
        albums = client.read("/albums?isOwned=true")
        if not isinstance(albums, list):
            raise ValidationError("Unexpected album list.")
        # Verify all returned owners even if the server ignores the ownership filter.
        matching = []
        for candidate in albums:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("albumName"), str):
                raise ValidationError("Unexpected album list entry.")
            if candidate["albumName"] == name:
                matching.append(owned_album(candidate, owner))
        if len(matching) > 1:
            raise ValidationError(
                "Multiple owned albums share this name; select --album-id explicitly."
            )
        album = matching[0] if matching else None
    # Preflight every asset before creating the album or adding memberships.
    for selected in ids:
        asset = client.read("/assets/" + selected)
        if (
            not isinstance(asset, dict)
            or asset.get("id") != selected
            or asset.get("ownerId") != owner
            or asset.get("isTrashed") is not False
        ):
            raise ValidationError(
                "Asset identity/ownership is unverified or asset is in trash: " + selected
            )
    report: dict[str, Any] = {
        "album_id": album["id"] if album else None,
        "album_name": album["albumName"] if album else name,
        "create": album is None,
        "requested": len(ids),
        "added": [],
        "already_present": [],
        "failed": [],
        "not_attempted": [],
        "dry_run": dry_run,
    }
    if dry_run:
        report["asset_ids"] = ids
        return report
    if album is None:
        album = owned_album(client.request("POST", "/albums", {"albumName": name}), owner)
        if album["albumName"] != name:
            raise ValidationError("Created album name differs; inspect the album before retrying.")
        report["album_id"] = album["id"]
    # Publish identity before memberships so it remains available if a request fails.
    print("Album ID: " + album["id"].replace(client.key, "[REDACTED]"))
    for offset in range(0, len(ids), BATCH_SIZE):
        batch = ids[offset : offset + BATCH_SIZE]
        try:
            result = client.request("PUT", "/albums/" + album["id"] + "/assets", {"ids": batch})
            if not isinstance(result, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in result
            ):
                raise ValidationError("Unexpected album membership response.")
            if len(result) != len(batch) or {row.get("id") for row in result} != set(batch):
                raise ValidationError("Incomplete or mismatched album membership response.")
            for row in result:
                if row.get("success") is True and row.get("error") is None:
                    report["added"].append(row["id"])
                elif row.get("success") is False and row.get("error") == "duplicate":
                    report["already_present"].append(row["id"])
                elif row.get("success") is False:
                    report["failed"].append(row["id"])
                else:
                    raise ValidationError("Invalid membership result status.")
        except ValidationError as error:
            raise ValidationError(
                str(error)
                + " Album ID: "
                + album["id"]
                + "; memberships may be partial. Rerun with --album-id to resume."
            ) from None
        if report["failed"]:
            report["not_attempted"] = ids[offset + len(batch) :]
            break
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="Create or reuse exactly one owned album with this name")
    group.add_argument("--album-id", help="Use an existing owned album by UUID")
    parser.add_argument("ids", nargs="*", help="Asset UUIDs to add")
    parser.add_argument(
        "--ids-file",
        action="append",
        type=Path,
        default=[],
        help="Whitespace-separated UUIDs; repeat for multiple files",
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only preflight; creates no album or memberships",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    key = ""
    try:
        ids = collect_ids(args.ids, args.ids_file)
        server, key = load_config(args.config.expanduser())
        report = manage(
            AlbumClient(server, key, args.timeout),
            ids,
            name=args.name,
            identity=args.album_id,
            dry_run=args.dry_run,
        )
    except (ConfigError, ValidationError, OSError, ValueError) as error:
        message = str(error).replace(key, "[REDACTED]") if key else str(error)
        print("Error: " + json.dumps(message), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    # Redact before serialising: a key can contain quotes or backslashes.
    def redact(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(key, "[REDACTED]")
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        return value

    print(json.dumps(redact(report), indent=2))
    return 2 if report["failed"] else 0
