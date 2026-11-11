"""Live Photo album checks follow explicit server links, never filenames or dates."""

import base64
import hashlib
import json
import unittest
from unittest.mock import patch

from immich_ctl import folder_validate as validation
from tests import test_folder_validate as fixtures


class LivePhotoValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FolderValidationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.video = dict(self.fixture.asset, type="VIDEO", visibility="hidden")
        self.parent = dict(
            self.fixture.asset,
            id="still/id",
            type="IMAGE",
            checksum=base64.b64encode(hashlib.sha1(b"still image").digest()).decode(),
            livePhotoVideoId=self.video["id"],
        )
        self.client = validation.Client("https://example/api", "synthetic-key", 30)

    def validate(self, parents=None, memberships=None, details=None):
        parents = [self.parent] if parents is None else parents
        assets = [self.video, *parents]
        details = {a["id"]: a for a in assets} if details is None else details
        memberships = (
            {"asset-1": set(), "still/id": {"Trip", "Family"}}
            if memberships is None
            else memberships
        )
        with (
            patch.object(validation, "validation_inventory", return_value=(assets, details)),
            patch.object(self.client, "read", return_value={"id": "me"}),
            patch.object(
                self.client, "albums", side_effect=lambda identity: memberships.get(identity, set())
            ) as albums,
        ):
            report = validation.validate(self.fixture.folder, self.client)
        return report, albums

    def test_owned_server_link_satisfies_albums_even_without_a_local_still(self):
        before = self.fixture.sidecar.read_bytes()
        report, albums = self.validate()
        row = report["files"][0]
        self.assertEqual(report["counts"]["ok"], 1)
        self.assertEqual(row["differences"], [])
        self.assertEqual(row["checks"], 8)
        self.assertEqual(row["album_membership_via"], ["still/id"])
        self.assertEqual([c.args for c in albums.call_args_list], [("asset-1",), ("still/id",)])
        self.assertEqual(self.fixture.sidecar.read_bytes(), before)

    def test_partial_parent_memberships_do_not_conceal_missing_albums_or_video_metadata(self):
        self.video["exifInfo"] = dict(self.video["exifInfo"], description="Wrong video description")
        report, _ = self.validate(memberships={"still/id": {"Trip"}})
        row = report["files"][0]
        self.assertEqual(row["status"], "different")
        self.assertEqual(
            set(row["differences"]), {"description differs", "missing album membership: Family"}
        )
        self.assertEqual(row["album_membership_via"], ["still/id"])

    def test_unlinked_wrong_type_foreign_trashed_and_locked_parents_cannot_confirm_memberships(
        self,
    ):
        for change in (
            {"livePhotoVideoId": "another-video"},
            {"livePhotoVideoId": None},
            {"livePhotoVideoId": ["asset-1"]},
            {"type": "VIDEO"},
            {"ownerId": "another-user"},
            {"isTrashed": True},
            {"visibility": "locked"},
        ):
            with self.subTest(change=change):
                parent = dict(self.parent, **change)
                if any(k in change for k in ("ownerId", "isTrashed", "visibility")):
                    with self.assertRaises(validation.ValidationError):
                        self.validate(parents=[parent])
                else:
                    report, albums = self.validate(parents=[parent])
                    self.assertEqual(
                        set(report["files"][0]["differences"]),
                        {"missing album membership: Trip", "missing album membership: Family"},
                    )
                    self.assertNotIn("album_membership_via", report["files"][0])
                    self.assertEqual(albums.call_count, 1)

    def test_only_video_album_checks_inherit_and_complete_direct_memberships_need_no_parent_reads(
        self,
    ):
        for change in ({"type": "IMAGE"}, {"type": None}):
            with self.subTest(change=change):
                self.video.update(change)
                report, albums = self.validate()
                self.assertEqual(report["counts"]["different"], 1)
                self.assertEqual(albums.call_count, 1)
        self.video["type"] = "VIDEO"
        report, albums = self.validate(memberships={"asset-1": {"Trip", "Family"}})
        self.assertEqual(report["counts"]["ok"], 1)
        self.assertEqual(albums.call_count, 1)
        self.assertNotIn("album_membership_via", report["files"][0])

    def test_direct_and_multiple_explicitly_linked_parents_can_supply_memberships(self):
        second = dict(self.parent, id="second-parent")
        report, _ = self.validate(
            parents=[self.parent, second],
            memberships={
                "asset-1": {"Trip"},
                "still/id": {"Unrelated"},
                "second-parent": {"Family"},
            },
        )
        self.assertEqual(report["counts"]["ok"], 1)
        self.assertEqual(report["files"][0]["album_membership_via"], ["second-parent"])

    def test_unfinished_parent_metadata_job_uses_fresh_detail_and_rejects_a_changed_link(self):
        assets = [self.video, self.parent]
        for change in (
            {},
            {"livePhotoVideoId": "changed"},
            {"type": "VIDEO"},
            {"isTrashed": True},
            {"id": "wrong-id"},
            {"ownerId": "another-user"},
            {"checksum": base64.b64encode(b"x" * 20).decode()},
        ):
            with (
                self.subTest(change=change),
                patch.object(
                    validation,
                    "validation_inventory",
                    return_value=(assets, {self.video["id"]: self.video}),
                ),
                patch.object(
                    self.client, "read", side_effect=[{"id": "me"}, dict(self.parent, **change)]
                ) as read,
                patch.object(
                    self.client,
                    "albums",
                    side_effect=lambda identity: (
                        set() if identity == self.video["id"] else {"Trip", "Family"}
                    ),
                ),
            ):
                if change:
                    with self.assertRaises(validation.ValidationError):
                        validation.validate(self.fixture.folder, self.client)
                else:
                    report = validation.validate(self.fixture.folder, self.client)
                    self.assertEqual(report["counts"]["ok"], 1)
                self.assertEqual(read.call_args_list[-1].args, ("/assets/still%2Fid",))

    def test_parent_album_cache_is_shared_with_local_stills_and_unchecked_metadata_is_preserved(
        self,
    ):
        image = self.fixture.folder / "still.jpg"
        image.write_bytes(b"still image")
        (self.fixture.folder / "still.jpg.JSON").write_text(json.dumps(self.fixture.metadata))
        metadata = dict(self.fixture.metadata)
        metadata.pop("favorited")
        self.fixture.sidecar.write_text(json.dumps(metadata))
        report, albums = self.validate()
        self.assertEqual(
            report["counts"],
            {"ok": 1, "missing": 0, "ambiguous": 0, "different": 0, "incomplete": 1},
        )
        self.assertEqual(albums.call_count, 2)
        self.assertIn(
            "sidecar omits favorited; source value unverified", report["files"][0]["unchecked"]
        )

    def test_link_completed_between_inventory_scans_uses_latest_metadata(self):
        earlier_parent = dict(self.parent, livePhotoVideoId=None)
        report, _ = self.validate(
            parents=[earlier_parent],
            details={self.video["id"]: self.video, self.parent["id"]: self.parent},
        )
        self.assertEqual(report["counts"]["ok"], 1)
        self.assertEqual(report["files"][0]["album_membership_via"], ["still/id"])

    def test_api_album_failure_never_becomes_a_passing_relationship(self):
        with (
            patch.object(
                validation,
                "validation_inventory",
                return_value=(
                    [self.video, self.parent],
                    {a["id"]: a for a in [self.video, self.parent]},
                ),
            ),
            patch.object(self.client, "read", return_value={"id": "me"}),
            patch.object(
                self.client,
                "albums",
                side_effect=[set(), validation.ValidationError("album read failed")],
            ),
            self.assertRaisesRegex(validation.ValidationError, "album read failed"),
        ):
            validation.validate(self.fixture.folder, self.client)
