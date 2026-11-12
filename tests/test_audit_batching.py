"""Audit batching retains the uploader's scope and all matching safeguards."""

import copy
import json
import unittest
from unittest.mock import patch

from immich_ctl import takeout_upload_audit as audit
from immich_ctl.folder_validate import ValidationError
from tests import test_upload_audit as fixtures


class AuditBatchingTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.UploadAuditTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def detail_requests(self):
        return [p for method, p, _ in self.fx.calls if method == "GET" and p.startswith("/assets/")]

    def test_thousands_of_reused_filenames_need_pages_instead_of_individual_reads(self):
        for i in range(1500):
            asset = copy.deepcopy(self.fx.asset)
            asset["id"] = f"old-{i}"
            asset["exifInfo"]["dateTimeOriginal"] = "2022-01-01T00:00:00Z"
            self.fx.assets.append(asset)
        report = self.fx.report()
        self.assertEqual(report["server_assets"], 1501)
        self.assertEqual(report["summary"]["replace_larger"], 1)
        self.assertEqual(
            [x["asset_id"] for x in report["heuristic_matches"]], [self.fx.asset["id"]]
        )
        match = report["heuristic_matches"][0]
        self.assertEqual(match["server_filename"], self.fx.asset["originalFileName"])
        self.assertEqual(
            match["server_capture_time"], self.fx.asset["exifInfo"]["dateTimeOriginal"]
        )
        self.assertEqual(self.detail_requests(), [])
        searches = [b for _, p, b in self.fx.calls if p == "/search/metadata"]
        self.assertLessEqual(len(searches), 16)
        for exif in (False, True):
            queries = [b for b in searches if b["withExif"] == exif]
            self.assertEqual(
                {(b["visibility"], bool(b.get("trashedAfter"))) for b in queries},
                {(v, t) for v in ("timeline", "archive", "hidden") for t in (False, True)},
            )
            self.assertTrue(all(b["withDeleted"] for b in queries))
            self.assertTrue(any(b["page"] == 2 for b in queries))
        self.assertTrue(
            all(method == "GET" or path == "/search/metadata" for method, path, _ in self.fx.calls)
        )

    def test_pending_metadata_falls_back_once_per_asset_including_trash(self):
        self.fx.asset.update(checksum=fixtures.encoded(b"photo"), isTrashed=True)
        self.fx.entries.update({"Trip/photo.jpg": b"photo", "Trip/photo.jpg.json": self.fx.data})
        self.fx.write()
        self.fx.omit_batch_ids.add(self.fx.asset["id"])
        report = self.fx.report()
        self.assertEqual(report["summary"]["existing_checksum_files"], 2)
        self.assertEqual(report["summary"]["trashed_checksum_matches"], 2)
        self.assertEqual(self.detail_requests(), ["/assets/remote%2Fid"])
        self.assertTrue(report["has_warnings"])

    def test_missing_metadata_does_not_hide_name_collisions_with_unknown_time(self):
        self.fx.omit_batch_ids.add(self.fx.asset["id"])
        self.fx.asset["exifInfo"] = {}
        report = self.fx.report()
        self.assertEqual(report["server_assets"], 1)
        self.assertEqual(report["summary"]["unknown_time"], 1)
        self.assertEqual(self.detail_requests(), ["/assets/remote%2Fid"])
        self.assertTrue(report["has_warnings"])

    def test_absent_or_malformed_exif_is_reported_as_unknown_not_a_clean_match(self):
        for exif in (None, [], "pending"):
            for fallback in (False, True):
                with self.subTest(exif=exif, fallback=fallback):
                    self.fx.asset["exifInfo"] = exif
                    self.fx.omit_batch_ids = {self.fx.asset["id"]} if fallback else set()
                    report = self.fx.report()
                    self.assertEqual(report["summary"]["unknown_time"], 1)
                    self.assertTrue(report["has_warnings"])

    def test_batched_and_fallback_reports_have_identical_risk_results(self):
        for trashed in (False, True):
            for checksum in (fixtures.encoded(b"photo"), fixtures.encoded(b"different")):
                with self.subTest(trashed=trashed, checksum=checksum):
                    self.fx.asset.update(isTrashed=trashed, checksum=checksum)
                    self.fx.omit_batch_ids.clear()
                    batch = self.fx.report()
                    self.fx.omit_batch_ids.add(self.fx.asset["id"])
                    fallback = self.fx.report()
                    self.assertEqual(batch, fallback)
                    self.assertTrue(batch["has_warnings"])

    def test_changed_identity_fields_between_scans_abort_before_any_clean_result(self):
        client = self.fx.client()
        original = self.fx.asset
        changes = [
            {"id": "new-id"},
            {"ownerId": "different-owner"},
            {"checksum": fixtures.encoded(b"changed")},
            {"originalFileName": "changed.jpg"},
            {"isTrashed": True},
            {"libraryId": "external"},
        ]
        for change in changes:
            with (
                self.subTest(change=change),
                patch.object(client, "inventory", return_value=[original]),
                patch.object(client, "_search", return_value=[dict(original, **change)]) as search,
                self.assertRaisesRegex(ValidationError, "changed"),
            ):
                audit.audit_inventory(client, "me")
            search.assert_called_once_with(None, "me", include_trashed=True, with_exif=True)

    def test_external_libraries_stay_excluded_and_owned_trash_stays_included(self):
        external = dict(self.fx.asset, id="external", libraryId="library")
        trash = dict(self.fx.asset, id="trash", visibility="hidden", isTrashed=True)
        foreign = dict(self.fx.asset, id="foreign", ownerId="another-user")
        self.fx.assets.extend([external, trash, foreign])
        self.fx.page_size = 1
        report = self.fx.report()
        self.assertEqual(report["server_assets"], 2)
        self.assertEqual(
            {x["asset_id"] for x in report["heuristic_matches"]}, {"remote/id", "trash"}
        )
        self.assertEqual(self.detail_requests(), [])

    def test_fallback_rejects_changed_or_malformed_asset_details(self):
        self.fx.omit_batch_ids.add(self.fx.asset["id"])
        changes = [
            {"id": "new-id"},
            {"ownerId": "different-owner"},
            {"checksum": fixtures.encoded(b"changed")},
            {"originalFileName": "changed.jpg"},
            {"isTrashed": True},
            {"libraryId": "external"},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.fx.override_detail = dict(self.fx.asset, **change)
                with self.assertRaisesRegex(ValidationError, "changed"):
                    self.fx.report()
        self.fx.override_detail = None
        original = self.fx.respond

        def malformed(request, *, timeout):
            if request.get_method() == "GET" and "/assets/" in request.full_url:
                return fixtures.Response(b"[]")
            return original(request, timeout=timeout)

        with patch.object(self.fx, "respond", side_effect=malformed):
            with self.assertRaisesRegex(ValidationError, "changed"):
                self.fx.report()

    def test_invalid_account_identity_cannot_produce_a_clean_empty_audit(self):
        client = self.fx.client()
        for user in (None, [], "me", {}, {"id": ""}, {"id": 123}):
            with (
                self.subTest(user=user),
                patch.object(client, "read", return_value=user),
                patch.object(client, "inventory") as inventory,
                self.assertRaises(ValidationError),
            ):
                audit.server_report([], client)
            inventory.assert_not_called()

    def test_invalid_server_filenames_cannot_hide_potential_collisions(self):
        for filename in (None, "", 123, ["photo.jpg"]):
            with self.subTest(filename=filename):
                self.fx.asset["originalFileName"] = filename
                with self.assertRaises(ValidationError):
                    self.fx.report()

    def test_metadata_batch_failure_does_not_silently_omit_the_server_check(self):
        original = self.fx.respond

        def broken(request, *, timeout):
            if request.data and json.loads(request.data)["withExif"]:
                return fixtures.Response(b'{"assets":{"items":[],"nextPage":"2"}}')
            return original(request, timeout=timeout)

        with patch.object(self.fx, "respond", side_effect=broken):
            with self.assertRaisesRegex(ValidationError, "pagination"):
                self.fx.report()

    def test_server_metadata_is_fetched_afresh_on_each_audit(self):
        self.fx.asset.update(checksum=fixtures.encoded(b"photo"), isFavorite=True, isArchived=True)
        self.fx.asset["exifInfo"].update(description="desired", latitude=51.5, longitude=-0.12)
        self.assertFalse(self.fx.report()["has_warnings"])
        self.fx.calls.clear()
        self.fx.asset["exifInfo"]["description"] = "changed on server"
        report = self.fx.report()
        self.assertEqual(report["summary"]["metadata_difference_matches"], 1)
        self.assertIn("description differs", report["existing_checksum"][0]["differences"])
        self.assertEqual(self.detail_requests(), [])
        self.assertTrue(any(b["withExif"] for _, p, b in self.fx.calls if p == "/search/metadata"))

    def test_batches_use_actual_exif_capture_time_not_inventory_display_date(self):
        self.fx.asset["fileCreatedAt"] = "2026-01-01T00:00:00Z"
        original = self.fx.respond

        def respond(request, *, timeout):
            response = original(request, timeout=timeout)
            if request.data and not json.loads(request.data)["withExif"]:
                value = json.loads(response.getvalue())
                for asset in value["assets"]["items"]:
                    asset.pop("exifInfo", None)
                response = fixtures.Response(json.dumps(value).encode())
            return response

        with patch.object(self.fx, "respond", side_effect=respond):
            report = self.fx.report()
        self.assertEqual(report["summary"]["replace_larger"], 1)
        self.assertEqual(report["summary"]["unknown_time"], 0)
        self.assertEqual(self.detail_requests(), [])
