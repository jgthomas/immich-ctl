"""Test metadata-only snapshots at the CLI and Immich API boundaries."""

import base64
import errno
import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from immich_ctl import snapshot
from immich_ctl.folder_validate import Client, ValidationError
from tests.script_loader import load_script

script = load_script("immich-snapshot")
START = datetime(2026, 11, 28, 5, 6, 7, 123456, tzinfo=UTC)
OWNER = "owner"
ALBUMS = [
    {
        "id": "z",
        "albumName": "Same name",
        "assetCount": 0,
        "albumUsers": [{"user": {"id": OWNER}, "role": "owner"}],
    },
    {
        "id": "a",
        "albumName": "Same name",
        "assetCount": 2,
        "updatedAt": "revision-a",
        "description": "Two trips",
        "albumThumbnailAssetId": "archived",
        "albumUsers": [
            {"user": {"id": "viewer", "email": "private-viewer@example.test"}, "role": "viewer"},
            {"user": {"id": OWNER}, "role": "editor"},
            {"user": {"id": "another-owner"}, "role": "owner"},
        ],
    },
    {"id": "b", "albumName": "Shared", "assetCount": 1},
]


def asset(asset_id, *, owner=OWNER, visibility="timeline", trashed=False, byte=b"a", **extra):
    return {
        "id": asset_id,
        "ownerId": owner,
        "libraryId": None,
        "visibility": visibility,
        "isTrashed": trashed,
        "checksum": base64.b64encode(byte * 20).decode(),
        **extra,
    }


def page(items, cursor=None):
    return {"assets": {"items": items, "nextCursor": cursor}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "snapshots"
        self.output.mkdir()
        self.config = self.root / "account.ini"
        self.config.write_text("[immich]\nserver=https://example.test\napi_key=private-key\n")
        self.client = Client("https://example.test/api", "private-key", 30)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = script.main(
                ["--config", str(self.config), "--output-dir", str(self.output), *args]
            )
        return code, out.getvalue(), err.getvalue()

    def test_cli_writes_account_inventory_and_all_album_memberships(self):
        unalbumed = asset(
            "free",
            originalFileName="IMG_001.HEIC",
            fileCreatedAt="2010-04-02T16:31:52.128Z",
            livePhotoVideoId="motion",
            isFavorite=True,
        )
        shared = asset(
            "foreign",
            owner="another-owner",
            byte=b"b",
            originalFileName="Été.jpg",
            fileCreatedAt="2018-07-01T21:34:07.000Z",
            isFavorite=False,
        )
        no_checksum = asset("restricted", owner="another-owner", checksum=None)
        archive = asset("archived", visibility="archive", isFavorite=False)
        hidden = asset("motion", visibility="hidden")
        trash = asset("trash", trashed=True)
        requests = []
        pages = {
            (None, None): page([unalbumed, shared, trash], "cursor-1"),
            (None, "cursor-1"): page([archive, hidden]),
            ("a", None): page([shared, archive], "album-cursor"),
            ("a", "album-cursor"): page([hidden, trash]),
            ("b", None): page([no_checksum]),
            ("z", None): page([]),
        }

        def read(path, body=None):
            requests.append((path, body))
            if path == "/users/me":
                return {"id": OWNER}
            if path == "/stacks":
                return [{"id": "burst", "primaryAssetId": "free", "assets": [unalbumed, archive]}]
            if path == "/albums":
                if [path for path, _ in requests].count("/albums") == 1:
                    return ALBUMS
                return [
                    dict(album, albumUsers=list(reversed(album["albumUsers"])))
                    if "albumUsers" in album
                    else album
                    for album in reversed(ALBUMS)
                ]
            self.assertEqual(path, "/search/metadata")
            filters = body["filter"]
            album_ids = filters.get("albumIds", {}).get("any", [])
            album = album_ids[0] if album_ids else None
            expected_filters = {
                "visibility": {"in": ["timeline", "archive", "hidden"]},
            }
            if album is not None:
                expected_filters["albumIds"] = {"any": [album]}
            cursor = body.get("cursor")
            self.assertEqual(
                body,
                {
                    "filter": expected_filters,
                    "size": 1000,
                    "withExif": False,
                    "withStacked": True,
                    **({"cursor": cursor} if cursor is not None else {}),
                },
            )
            return pages.pop((album, cursor))

        with (
            patch.object(Client, "read", side_effect=read),
            patch.object(snapshot, "Client", side_effect=Client) as client_factory,
        ):
            code, out, err = self.invoke("--timeout", "0.5")
        client_factory.assert_called_once_with("https://example.test/api", "private-key", 0.5)
        self.assertEqual(code, 0, err)
        self.assertFalse(pages)
        self.assertEqual([p for p, _ in requests].count("/albums"), 2)
        self.assertEqual([p for p, _ in requests].count("/stacks"), 2)
        path = Path(out.strip())
        self.assertEqual(list(self.output.iterdir()), [path])
        self.assertRegex(path.name, r"^immich-snapshot-\d{8}T\d{6}\.\d{6}Z\.json$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_bytes())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["server"], "https://example.test/api")
        self.assertEqual(data["account_id"], OWNER)
        self.assertLessEqual(
            datetime.fromisoformat(data["started_at"]), datetime.fromisoformat(data["completed_at"])
        )
        self.assertEqual(datetime.fromisoformat(data["started_at"]).tzinfo, UTC)
        self.assertEqual(data["checksum_algorithm"], "sha1")
        self.assertEqual(
            data["scope"],
            {
                "owned_assets": True,
                "shared_album_members": True,
                "visibilities": ["timeline", "archive", "hidden"],
                "includes_trash": True,
            },
        )
        self.assertEqual(
            data["albums"],
            [
                {
                    "id": "a",
                    "name": "Same name",
                    "description": "Two trips",
                    "cover_asset_id": "archived",
                    "asset_ids": ["archived", "foreign", "motion", "trash"],
                    "users": [
                        {"user_id": "another-owner", "role": "owner"},
                        {"user_id": OWNER, "role": "editor"},
                        {"user_id": "viewer", "role": "viewer"},
                    ],
                },
                {
                    "id": "b",
                    "name": "Shared",
                    "asset_ids": ["restricted"],
                    "users": None,
                    "description": None,
                    "cover_asset_id": None,
                },
                {
                    "id": "z",
                    "name": "Same name",
                    "description": None,
                    "cover_asset_id": None,
                    "asset_ids": [],
                    "users": [{"user_id": OWNER, "role": "owner"}],
                },
            ],
        )
        self.assertEqual(
            [r["id"] for r in data["assets"]],
            ["archived", "foreign", "free", "motion", "restricted", "trash"],
        )
        self.assertEqual(
            next(r for r in data["assets"] if r["id"] == "foreign"),
            {
                "id": "foreign",
                "owner_id": "another-owner",
                "visibility": "timeline",
                "is_trashed": False,
                "checksum": "62" * 20,
                "checksum_kind": "content",
                "original_filename": "Été.jpg",
                "file_created_at": "2018-07-01T21:34:07.000Z",
                "live_photo_video_id": None,
                "is_favorite": None,
            },
        )
        by_id = {r["id"]: r for r in data["assets"]}
        self.assertEqual(by_id["free"]["checksum"], "61" * 20)
        self.assertEqual(by_id["free"]["original_filename"], "IMG_001.HEIC")
        self.assertEqual(by_id["free"]["file_created_at"], "2010-04-02T16:31:52.128Z")
        self.assertEqual(by_id["free"]["live_photo_video_id"], "motion")
        self.assertEqual(by_id["motion"]["visibility"], "hidden")
        self.assertIsNone(by_id["restricted"]["checksum"])
        self.assertIsNone(by_id["restricted"]["original_filename"])
        self.assertIsNone(by_id["restricted"]["file_created_at"])
        self.assertIs(by_id["trash"]["is_trashed"], True)
        self.assertIs(by_id["free"]["is_favorite"], True)
        self.assertIs(by_id["archived"]["is_favorite"], False)
        self.assertIsNone(by_id["motion"]["is_favorite"])
        self.assertEqual(
            data["stacks"],
            [{"id": "burst", "primary_asset_id": "free", "asset_ids": ["archived", "free"]}],
        )
        self.assertIn("withheld checksums for 1 shared assets", err)
        self.assertNotIn("private-key", path.read_text() + err)
        self.assertNotIn("private-viewer@example.test", path.read_text())

    def test_empty_inventory_is_valid_and_unrelated_partner_assets_are_not_included(self):
        with patch.object(
            self.client,
            "read",
            side_effect=[
                {"id": OWNER},
                [],
                [],
                page([asset("partner-only", owner="partner")]),
                [],
                [],
            ],
        ):
            result = snapshot.snapshot(self.client, START)
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["albums"], [])
        self.assertEqual(result["stacks"], [])

    def test_pagination_rejects_repeated_ids_and_invalid_cursors(self):
        for pages in (
            [page([asset("same")], "next"), page([asset("same")])],
            [page([asset("one")], "next"), page([asset("two")], "next")],
            [page([], "next")],
            [page([asset("one")], 1)],
            [page([asset("one")], "")],
            [page([asset("one")], "next"), page([asset("one", trashed=True)])],
        ):
            with (
                self.subTest(pages=pages),
                patch.object(self.client, "read", side_effect=pages),
                self.assertRaises(snapshot.SnapshotChanged),
            ):
                list(snapshot.search_assets(self.client))

    def test_search_rejects_out_of_scope_or_malformed_results(self):
        for value, error in (
            (page([asset("locked", visibility="locked")]), snapshot.SnapshotChanged),
            (page([asset("trash", trashed=1)]), snapshot.SnapshotChanged),
            (page(["malformed"]), snapshot.SnapshotChanged),
            (page([asset("")]), ValidationError),
            (page([asset(42)]), ValidationError),
            ([], ValidationError),
            ({"assets": {"items": [], "nextPage": None}}, ValidationError),
            ({"assets": {"items": 1, "nextCursor": None}}, ValidationError),
        ):
            with (
                self.subTest(value=value),
                patch.object(self.client, "read", return_value=value),
                self.assertRaises(error),
            ):
                list(snapshot.search_assets(self.client))

    def test_invalid_visibility_or_trash_status_is_rejected_before_yielding(self):
        for record in (
            asset("locked", visibility="locked"),
            asset("trash", trashed=None),
        ):
            with (
                self.subTest(record=record),
                patch.object(self.client, "read", return_value=page([record])),
                self.assertRaises(snapshot.SnapshotChanged),
            ):
                next(snapshot.search_assets(self.client))

    def test_malformed_account_does_not_start_inventory(self):
        for response in ([], {}, {"id": 42}):
            with (
                self.subTest(response=response),
                patch.object(self.client, "read", return_value=response) as read,
                self.assertRaises(ValidationError),
            ):
                snapshot.snapshot(self.client, START)
            read.assert_called_once_with("/users/me")

    def test_changed_inventory_is_discarded_and_retried(self):
        replies = [
            {"id": OWNER},
            [],
            [],
            page([asset("stale")], "retry"),
            page([asset("stale")]),
            [],
            [],
            page([asset("fresh")]),
            [],
            [],
        ]
        with (
            patch.object(self.client, "read", side_effect=replies) as read,
            patch.object(snapshot.time, "sleep") as sleep,
        ):
            data = snapshot.snapshot(self.client, START)
        self.assertEqual([a["id"] for a in data["assets"]], ["fresh"])
        self.assertEqual(read.call_count, len(replies))
        sleep.assert_called_once_with(1)

    def test_membership_conflicts_abort_the_scan(self):
        album = [dict(ALBUMS[1], assetCount=1, lastModifiedAssetTimestamp="version-1")]
        owned = asset("owned")
        for responses, message in (
            ([page([]), page([owned])], "absent from the account inventory"),
            (
                [page([owned]), page([asset("owned", byte=b"b")])],
                "member changed",
            ),
            ([page([owned]), page([])], "membership count"),
            (
                [
                    page([owned]),
                    page([owned]),
                    [dict(album[0], updatedAt="new")],
                ],
                "album list changed",
            ),
            (
                [
                    page([owned]),
                    page([owned]),
                    [dict(album[0], lastModifiedAssetTimestamp="version-2")],
                ],
                "album list changed",
            ),
        ):
            with (
                self.subTest(message=message),
                patch.object(self.client, "read", side_effect=[album, *responses]),
                self.assertRaisesRegex(snapshot.SnapshotChanged, message),
            ):
                snapshot.collect(self.client, OWNER)

    def test_album_and_checksum_validation(self):
        for response, error in (
            ({}, ValidationError),
            ([None], ValidationError),
            ([dict(ALBUMS[0], assetCount=-1)], ValidationError),
            ([dict(ALBUMS[0], assetCount=True)], ValidationError),
            ([dict(ALBUMS[0], albumName=None)], ValidationError),
            ([dict(ALBUMS[0], id=None)], ValidationError),
            ([ALBUMS[0], ALBUMS[0]], snapshot.SnapshotChanged),
        ):
            with (
                self.subTest(response=response),
                patch.object(self.client, "read", return_value=response),
                self.assertRaises(error),
            ):
                snapshot.list_albums(self.client)
        valid = asset("asset")["checksum"]
        for checksum in (None, "", 123, valid + "!", "YQ=="):
            with (
                self.subTest(checksum=checksum),
                self.assertRaisesRegex(ValidationError, "invalid asset checksum"),
            ):
                snapshot.asset_record(asset("asset", checksum=checksum), OWNER)
        with self.assertRaisesRegex(ValidationError, "invalid asset checksum"):
            snapshot.asset_record(asset("asset", owner="shared", checksum=False), OWNER)
        self.assertIsNone(
            snapshot.asset_record(asset("asset", owner="shared", checksum=""), OWNER)["checksum"]
        )

    def test_owned_checksum_is_required_in_both_searches(self):
        for results in (
            [[], page([asset("owned", checksum=None)])],
            [
                [dict(ALBUMS[1], assetCount=1)],
                page([asset("owned")]),
                page([asset("owned", checksum=None)]),
            ],
        ):
            with (
                self.subTest(results=results),
                patch.object(self.client, "read", side_effect=results),
                self.assertRaisesRegex(ValidationError, "invalid asset checksum"),
            ):
                snapshot.collect(self.client, OWNER)

    def test_manifest_marks_checksum_kind_from_library_metadata(self):
        uploaded = asset("uploaded")
        external = asset("external", libraryId="external-library")
        unspecified = asset("unspecified")
        del unspecified["libraryId"]
        shared = asset("shared", owner="another-owner", checksum=None)
        album = [dict(ALBUMS[1], assetCount=2)]
        with patch.object(
            self.client,
            "read",
            side_effect=[
                {"id": OWNER},
                [],
                album,
                page([uploaded, external, unspecified]),
                page([external, shared]),
                album,
                [],
            ],
        ):
            result = snapshot.snapshot(self.client, START)
        self.assertEqual(result["albums"][0]["asset_ids"], ["external", "shared"])
        self.assertEqual(
            {a["id"]: (a["checksum"], a["checksum_kind"]) for a in result["assets"]},
            {
                "external": ("61" * 20, "external_library"),
                "shared": (None, "unavailable"),
                "unspecified": ("61" * 20, "unverified"),
                "uploaded": ("61" * 20, "content"),
            },
        )

    def test_changed_album_role_invalidates_collection(self):
        before = ALBUMS[1]
        after = dict(
            before,
            albumUsers=[
                dict(user, role="editor") if user["role"] == "viewer" else user
                for user in before["albumUsers"]
            ],
        )
        members = [asset("one"), asset("two")]
        with (
            patch.object(
                self.client, "read", side_effect=[[before], page(members), page(members), [after]]
            ),
            self.assertRaisesRegex(snapshot.SnapshotChanged, "album list changed"),
        ):
            snapshot.collect(self.client, OWNER)

    def test_album_users_preserve_exact_ids_and_roles(self):
        self.assertEqual(
            snapshot.album_users(ALBUMS[1]["albumUsers"]),
            [
                {"user_id": "another-owner", "role": "owner"},
                {"user_id": OWNER, "role": "editor"},
                {"user_id": "viewer", "role": "viewer"},
            ],
        )
        self.assertEqual(snapshot.album_users([]), [])
        self.assertIsNone(snapshot.album_users(None))

    def test_malformed_and_repeated_album_user_records_are_rejected(self):
        valid = {"user": {"id": OWNER}, "role": "owner"}
        for users in (
            {},
            [None],
            [{"user": "owner", "role": "owner"}],
            [{"user": {}, "role": "editor"}],
            [dict(valid, role=None)],
            [dict(valid, role="")],
            [valid, dict(valid, role="viewer")],
        ):
            with (
                self.subTest(users=users),
                patch.object(self.client, "read", return_value=[dict(ALBUMS[0], albumUsers=users)]),
                self.assertRaises(ValidationError),
            ):
                snapshot.list_albums(self.client)

    def test_filename_and_capture_time_changes_keep_exact_asset_identity(self):
        original = asset(
            "asset", originalFileName="before.jpg", fileCreatedAt="2024-01-01T10:00:00Z"
        )
        before = snapshot.asset_record(original, OWNER)
        for field, key, value in (
            ("originalFileName", "original_filename", "after.jpg"),
            ("fileCreatedAt", "file_created_at", "2024-01-01T11:00:00Z"),
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    snapshot.asset_record(dict(original, **{field: value}), OWNER),
                    dict(before, **{key: value}),
                )
                with self.assertRaisesRegex(ValidationError, "filename or capture time"):
                    snapshot.asset_record(dict(original, **{field: 123}), OWNER)

    def test_linking_an_existing_motion_asset_changes_only_the_still_record(self):
        still = asset("still", livePhotoVideoId=None)
        motion = asset("motion", visibility="hidden")
        linked = dict(still, livePhotoVideoId="motion")
        manifests = []
        for image in (still, linked):
            with patch.object(self.client, "read", side_effect=[[], page([image, motion]), []]):
                records, _ = snapshot.collect(self.client, OWNER)
            manifests.append(records)
        before, after = manifests
        self.assertEqual(before[0], after[0])
        self.assertIsNone(before[1]["live_photo_video_id"])
        self.assertEqual(after[1], dict(before[1], live_photo_video_id=after[0]["id"]))

    def test_album_cover_or_description_changes_with_same_membership(self):
        members = [asset("first"), asset("second")]
        before = dict(ALBUMS[1], description="Before", albumThumbnailAssetId="first")
        with patch.object(
            self.client, "read", side_effect=[[before], page(members), page(members), [before]]
        ):
            _, albums = snapshot.collect(self.client, OWNER)
        original = albums[0]
        self.assertEqual(original["description"], "Before")
        self.assertEqual(original["cover_asset_id"], "first")
        self.assertEqual(original["asset_ids"], ["first", "second"])
        for field, key, value in (
            ("description", "description", ""),
            ("albumThumbnailAssetId", "cover_asset_id", "second"),
        ):
            after = dict(before, **{field: value})
            unchanged_members = [page(members), page(members)]
            with self.subTest(field=field):
                with patch.object(
                    self.client, "read", side_effect=[[after], *unchanged_members, [after]]
                ):
                    _, albums = snapshot.collect(self.client, OWNER)
                self.assertEqual(albums, [dict(original, **{key: value})])
                with (
                    patch.object(
                        self.client, "read", side_effect=[[before], *unchanged_members, [after]]
                    ),
                    self.assertRaises(snapshot.SnapshotChanged),
                ):
                    snapshot.collect(self.client, OWNER)

    def test_references_outside_inventory_are_preserved(self):
        still = asset("still", livePhotoVideoId="missing-motion")
        album = dict(ALBUMS[1], assetCount=1, albumThumbnailAssetId="missing-cover")
        with patch.object(
            self.client, "read", side_effect=[[album], page([still]), page([still]), [album]]
        ) as read:
            assets, albums = snapshot.collect(self.client, OWNER)
        self.assertEqual(assets[0]["live_photo_video_id"], "missing-motion")
        self.assertEqual(albums[0]["cover_asset_id"], "missing-cover")
        self.assertEqual(read.call_count, 4)

    def test_malformed_link_cover_and_description_are_rejected(self):
        for value in ("", 42):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                snapshot.asset_record(asset("still", livePhotoVideoId=value), OWNER)
        for fields in (
            {"description": False},
            {"albumThumbnailAssetId": 42},
            {"albumThumbnailAssetId": ""},
        ):
            with (
                self.subTest(fields=fields),
                patch.object(self.client, "read", return_value=[dict(ALBUMS[0], **fields)]),
                self.assertRaises(ValidationError),
            ):
                snapshot.list_albums(self.client)

    def test_favorite_changes_keep_asset_identity_and_distinguish_missing_flags(self):
        before = snapshot.asset_record(asset("owned", isFavorite=False), OWNER)
        self.assertIs(before["is_favorite"], False)
        self.assertEqual(
            snapshot.asset_record(asset("owned", isFavorite=True), OWNER),
            dict(before, is_favorite=True),
        )
        self.assertIsNone(snapshot.asset_record(asset("owned"), OWNER)["is_favorite"])
        for flag in (False, True, None):
            with self.subTest(flag=flag):
                shared = asset("shared", owner="another-owner", isFavorite=flag)
                self.assertIsNone(snapshot.asset_record(shared, OWNER)["is_favorite"])
        with self.assertRaisesRegex(ValidationError, "favorite"):
            snapshot.asset_record(asset("owned", isFavorite=1), OWNER)

    def test_stack_members_and_primary_are_serialized_from_stack_endpoint(self):
        first, second = asset("first"), asset("second")
        stacks = [
            {"id": "burst", "primaryAssetId": "second", "assets": [second, first]},
            {
                "id": "another",
                "primaryAssetId": "outside",
                "assets": [{"id": "outside"}, {"id": "another-outside"}],
            },
        ]
        with patch.object(self.client, "read", return_value=stacks) as read:
            self.assertEqual(
                snapshot.list_stacks(self.client),
                [
                    {
                        "id": "another",
                        "primary_asset_id": "outside",
                        "asset_ids": ["another-outside", "outside"],
                    },
                    {"id": "burst", "primary_asset_id": "second", "asset_ids": ["first", "second"]},
                ],
            )
        read.assert_called_once_with("/stacks")

    def test_stack_changes_retry_even_when_asset_records_stay_the_same(self):
        members = [asset("first"), asset("second")]
        original = {"id": "burst", "primaryAssetId": "first", "assets": members}
        for final in ([dict(original, primaryAssetId="second")], []):
            with (
                self.subTest(final=final),
                patch.object(
                    self.client,
                    "read",
                    side_effect=[
                        {"id": OWNER},
                        [original],
                        [],
                        page(members),
                        [],
                        final,
                        final,
                        [],
                        page(members),
                        [],
                        final,
                    ],
                ),
                patch.object(snapshot.time, "sleep") as sleep,
            ):
                data = snapshot.snapshot(self.client, START)
            sleep.assert_called_once_with(1)
            expected = [
                {"id": "burst", "primary_asset_id": "second", "asset_ids": ["first", "second"]}
            ]
            self.assertEqual(data["stacks"], expected if final else [])
            self.assertEqual([asset["id"] for asset in data["assets"]], ["first", "second"])

    def test_invalid_stack_results_are_rejected(self):
        first = {"id": "first", "primaryAssetId": "a", "assets": [{"id": "a"}, {"id": "b"}]}
        second = {"id": "second", "primaryAssetId": "c", "assets": [{"id": "c"}, {"id": "d"}]}
        for rows in (
            {},
            [None],
            [dict(first, assets=None)],
            [dict(first, assets=[None])],
            [dict(first, primaryAssetId="elsewhere")],
            [dict(first, assets=[{"id": "a"}, {"id": "a"}])],
            [first, dict(second, id="first")],
            [first, dict(second, assets=[{"id": "b"}, {"id": "c"}])],
        ):
            with (
                self.subTest(rows=rows),
                patch.object(self.client, "read", return_value=rows),
                self.assertRaises(ValidationError),
            ):
                snapshot.list_stacks(self.client)

    def test_required_stack_permission_failure_prevents_snapshot(self):
        with patch.object(
            Client, "read", side_effect=[{"id": OWNER}, ValidationError("HTTP 403")]
        ) as read:
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("stack.read", err)
        self.assertIn("403", err)
        self.assertEqual(
            [call.args for call in read.call_args_list], [("/users/me",), ("/stacks",)]
        )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_inconsistent_snapshot_never_publishes_after_retry_exhaustion(self):
        with (
            patch.object(
                Client, "read", side_effect=[{"id": OWNER}, *[[], ALBUMS * 2] * 3]
            ) as read,
            patch.object(snapshot.time, "sleep") as sleep,
        ):
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Album list repeated an ID", err)
        self.assertEqual(read.call_count, 7)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_request_emits_no_file_and_redacts_key(self):
        with patch.object(Client, "read", side_effect=ValidationError("failed private-key")):
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("[REDACTED]", err)
        self.assertNotIn("private-key", err)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_collision_does_not_replace_snapshot_and_temporary_file_is_cleaned(self):
        path = snapshot.write_snapshot(self.output, START, {"complete": True})
        self.assertEqual(path.name, "immich-snapshot-20261128T050607.123456Z.json")
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            snapshot.write_snapshot(self.output, START, {"complete": False})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.output.iterdir()), [path])

    def test_failed_write_never_publishes_and_cleans_staging(self):
        with patch.object(snapshot.os, "fsync", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                snapshot.write_snapshot(self.output, START, {"complete": True})
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_output_dir_fails_before_connecting(self):
        self.output.rmdir()
        with patch.object(Client, "read") as read:
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("must already exist", err)
        read.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_filesystem_without_hard_links_fails_before_connecting(self):
        existing = self.output / "existing.json"
        existing.write_text("keep")

        def link(source, destination):
            self.assertEqual(source.parent, destination.parent)
            self.assertEqual(source.parent.parent, self.output)
            self.assertTrue(source.is_file())
            self.assertFalse(destination.exists())
            raise OSError(errno.EOPNOTSUPP, "links unsupported")

        with (
            patch.object(snapshot.os, "link", side_effect=link) as probe,
            patch.object(Client, "read") as read,
        ):
            code, out, err = self.invoke()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("supports hard links", err)
        probe.assert_called_once()
        read.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [existing])
        self.assertEqual(existing.read_text(), "keep")

    def test_invalid_timeout_rejected_before_network(self):
        for timeout in ("nan", "inf", "0", "-2"):
            with (
                self.subTest(timeout=timeout),
                patch.object(Client, "read") as read,
                self.assertRaises(SystemExit) as raised,
            ):
                self.invoke("--timeout", timeout)
            self.assertEqual(raised.exception.code, 2)
            read.assert_not_called()

    def test_staging_is_private_and_on_destination_filesystem(self):
        paths = []

        def link(staged, path):
            self.assertEqual(staged.parent, path.parent)
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            self.assertEqual(json.loads(staged.read_bytes()), {"album": "Été"})
            self.assertFalse(path.exists())
            paths.append(staged)
            raise OSError("destination filesystem refused publication")

        with patch.object(snapshot.os, "link", side_effect=link):
            with self.assertRaisesRegex(OSError, "refused publication"):
                snapshot.write_snapshot(self.output, START, {"album": "Été"})
        self.assertFalse(paths[0].exists())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_staging_creation_failure_preserves_original_error(self):
        with patch.object(snapshot.tempfile, "NamedTemporaryFile", side_effect=OSError("no space")):
            with self.assertRaisesRegex(OSError, "no space"):
                snapshot.write_snapshot(self.output, START, {})
        self.assertEqual(list(self.output.iterdir()), [])

    def test_required_options_are_rejected_by_cli_parser(self):
        for args in (
            ["--output-dir", str(self.output)],
            ["--config", str(self.config)],
        ):
            with (
                self.subTest(args=args),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                script.main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_interrupt_returns_shell_status_and_publishes_nothing(self):
        with patch.object(snapshot, "Client", side_effect=KeyboardInterrupt) as factory:
            code, out, _ = self.invoke()
        factory.assert_called_once_with("https://example.test/api", "private-key", 30)
        self.assertEqual((code, out), (130, ""))
        self.assertEqual(list(self.output.iterdir()), [])
