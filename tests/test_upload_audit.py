"""Read-only HTTP-boundary tests for retained metadata and upload selection risks."""

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import folder_upload
from immich_ctl.folder_repair import repair
from immich_ctl.folder_validate import Client, ValidationError, inspect_folder, validate
from immich_ctl.takeout_upload_audit import (
    folder_inputs,
    match_window,
    size_decision,
    upload_audit,
)
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

script = load_script("immich-takeout")
SELECTION = (date(2023, 1, 1), date(2023, 12, 31))


def encoded(data):
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


class Response(io.BytesIO):
    status = 200


class UploadAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"
        self.config = self.root / "account.ini"
        self.key = 'synthetic-%-secret-"-key'
        self.config.write_text(
            f"[immich]\nserver=https://photos.example/immich\napi_key={self.key}\n"
        )
        self.data = sidecar("photo.jpg") | {
            "description": "desired",
            "geoDataExif": {"latitude": 51.5, "longitude": -0.12},
            "favorited": True,
            "archived": True,
        }
        self.entries = {
            "Photos from 2023/photo.jpg": b"photo",
            "Photos from 2023/photo.jpg.json": self.data,
        }
        self.write()
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        (self.folder / "renamed.jpg").write_bytes(b"photo")
        self.native = {
            "software": "immich-go version:0.32.0",
            "fileName": "original.jpg",
            "dateTaken": "2023-01-01T00:00:00Z",
            "description": "desired",
        }
        (self.folder / "renamed.jpg.JSON").write_text(json.dumps(self.native))
        self.asset = {
            "id": "remote/id",
            "ownerId": "me",
            "checksum": encoded(b"other"),
            "originalFileName": "photo.jpg",
            "fileCreatedAt": "2023-01-01T00:00:00Z",
            "visibility": "timeline",
            "isFavorite": False,
            "isArchived": False,
            "exifInfo": {
                "dateTimeOriginal": "2023-01-01T00:00:00.000+00:00",
                "fileSizeInByte": 2,
                "description": "old",
                "latitude": 0,
                "longitude": 0,
            },
        }
        self.assets = [self.asset]
        self.calls = []
        self.page_size = 1000
        self.override_detail = None
        self.failure = None
        self.destination_albums = []

    def write(self):
        write_zip(self.archive, self.entries)

    def respond(self, request, *, timeout):
        self.assertEqual(request.get_header("X-api-key"), self.key)
        self.assertGreater(timeout, 0)
        path = urlsplit(request.full_url).path.removeprefix("/immich/api")
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))
        if self.failure:
            raise self.failure
        if (method, path) == ("GET", "/users/me"):
            result = {"id": "me"}
        elif (method, path) == ("POST", "/search/metadata"):
            self.assertNotIn("checksum", body)
            self.assertIs(body["withExif"], False)
            self.assertIs(body["withDeleted"], True)
            self.assertIs(body["withStacked"], True)
            start = (body["page"] - 1) * self.page_size
            matches = [
                a
                for a in self.assets
                if a.get("visibility") == body["visibility"]
                and bool(a.get("isTrashed", False)) == bool(body.get("trashedAfter"))
            ]
            result = {
                "assets": {
                    "items": matches[start : start + self.page_size],
                    "nextPage": str(body["page"] + 1)
                    if start + self.page_size < len(matches)
                    else None,
                }
            }
        elif method == "GET" and path.startswith("/assets/"):
            identity = unquote(path.removeprefix("/assets/"))
            result = (
                self.override_detail
                if self.override_detail is not None
                else next(a for a in self.assets if a["id"] == identity)
            )
        elif (method, path) == ("GET", "/albums"):
            result = self.destination_albums
        else:
            self.fail("Non-read-only or unexpected request: " + method + " " + path)
        return Response(json.dumps(result).encode())

    def client(self):
        client = Client("https://photos.example/immich/api", self.key, 30)
        client.opener.open = self.respond
        return client

    def invoke(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("immich_ctl.folder_validate.build_opener") as build,
            patch.object(script.subprocess, "run", side_effect=AssertionError("no uploader")),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            build.return_value.open.side_effect = self.respond
            code = script.main(
                ["audit", "--config", str(self.config), *map(str, extra), str(self.archive)]
            )
        self.assertNotIn(self.key, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def report(self, **kwargs):
        return upload_audit([self.archive], self.client(), **kwargs)

    def native_media(self, name, content, **metadata):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.with_name(path.name + ".JSON").write_text(json.dumps(self.native | metadata))
        return path

    def test_folder_existing_checksum_compares_every_explicit_metadata_field(self):
        self.native_media(
            "renamed.jpg", b"photo", latitude=51.5, longitude=-0.12, favorited=True, archived=True
        )
        self.asset.update(checksum=encoded(b"photo"), fileCreatedAt="2022-01-01T00:00:00Z")
        report = self.report(folders=[self.folder])
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["summary"]["existing_checksum_files"], 1)
        self.assertEqual(report["heuristic_matches"], [])
        match = report["existing_checksum"][0]
        self.assertEqual(match["sha1"], hashlib.sha1(b"photo").hexdigest())
        self.assertEqual(match["path"], str(self.folder / "renamed.jpg"))
        self.assertEqual(match["asset_id"], self.asset["id"])
        self.assertEqual(match["original_filename"], "original.jpg")
        self.assertEqual(match["unchecked"], [])
        self.assertEqual(
            set(match["differences"]),
            {
                field + " differs"
                for field in (
                    "dateTaken",
                    "description",
                    "latitude",
                    "longitude",
                    "favorited",
                    "archived",
                )
            },
        )

    def test_folder_unusable_sidecars_do_not_supply_dates_or_hide_inputs(self):
        self.assets = []
        sidecar_path = self.folder / "renamed.jpg.JSON"
        outside = self.native | {"dateTaken": "2022-01-01T00:00:00Z"}
        for payload, issue in (
            (None, "missing native sidecar"),
            (b"not JSON", "invalid native sidecar"),
            (
                json.dumps(outside | {"software": "other"}).encode(),
                "missing immich-go version marker",
            ),
        ):
            with self.subTest(issue=issue):
                sidecar_path.unlink(missing_ok=True)
                if payload is not None:
                    sidecar_path.write_bytes(payload)
                report = self.report(folders=[self.folder], selection=SELECTION)
                self.assertTrue(report["has_warnings"])
                self.assertEqual(report["input_files"], 1)
                row = report["unresolved_inputs"][0]
                self.assertIsNone(row["capture_time"])
                self.assertTrue(any(issue in message for message in row["issues"]))

    def test_folder_invalid_fields_and_glob_paths_are_unverified(self):
        self.assets = []
        self.native_media("renamed.jpg", b"photo", latitude="north", favorited="yes")
        report = self.report(folders=[self.folder])
        issues = report["unresolved_inputs"][0]["issues"]
        self.assertIn("invalid sidecar latitude", issues)
        self.assertIn("invalid sidecar favorited", issues)
        path = self.native_media("holiday[1].jpg", b"holiday", dateTaken="2022-01-01T00:00:00Z")
        report = self.report(folders=[self.folder], selection=SELECTION)
        row = next(row for row in report["unresolved_inputs"] if row["path"] == str(path))
        self.assertIsNone(row["capture_time"])
        self.assertTrue(any("glob characters" in issue for issue in row["issues"]))
        self.assertTrue(report["has_warnings"])

    def test_folder_selection_audits_every_file_and_warns_for_outside_dates(self):
        self.assets = []
        for name, captured in (
            ("a-before.jpg", "2022-12-31T23:59:59.999999Z"),
            ("b-start.jpg", "2023-01-01T00:00:00Z"),
            ("c-end.jpg", "2023-12-31T23:59:59.999999Z"),
            ("d-after.jpg", "2024-01-01T00:00:00Z"),
        ):
            self.native_media(name, name.encode(), dateTaken=captured)
        # Each selected file has a different explicit description on the server.
        for name in ("b-start.jpg", "c-end.jpg", "renamed.jpg"):
            item = copy.deepcopy(self.asset)
            item.update(id=name, checksum=encoded((self.folder / name).read_bytes()))
            self.assets.append(item)
        report = self.report(folders=[self.folder], selection=SELECTION)
        self.assertEqual(report["input_files"], 5)
        self.assertFalse(report["date_filter_applied"])
        self.assertEqual(len(report["input_warnings"]), 2)
        self.assertTrue(report["has_warnings"])
        self.assertEqual(
            {Path(row["path"]).name for row in report["existing_checksum"]},
            {"b-start.jpg", "c-end.jpg", "renamed.jpg"},
        )

    def test_archive_filtering_and_bad_sidecars_do_not_hide_later_media(self):
        self.entries = {
            "notes.txt": b"ignored",
            "Photos from 2023/a.jpg": b"trashed",
            "Photos from 2023/a.jpg.json": sidecar("a.jpg", "trash") | {"trashed": True},
            "Photos from 2023/b.jpg": b"broken",
            "Photos from 2023/b.jpg.json": b"not JSON",
            **self.entries,
        }
        self.write()
        report = self.report()
        self.assertEqual(report["input_files"], 2)
        self.assertEqual(
            [row["path"] for row in report["heuristic_matches"]], ["Photos from 2023/photo.jpg"]
        )
        self.assertEqual(report["summary"]["unresolved_inputs"], 1)
        self.assertTrue(report["has_warnings"])

    def test_archive_directories_and_non_photo_entries_do_not_end_the_scan(self):
        with zipfile.ZipFile(self.archive) as archive:
            entries = [(entry, archive.read(entry)) for entry in archive.infolist()]
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("Takeout/Google Photos/", b"")
            archive.writestr("Takeout/account.html", b"not photo metadata")
            for entry, content in entries:
                archive.writestr(entry, content)
        self.asset["checksum"] = encoded(b"photo")
        report = self.report()
        self.assertEqual(report["input_files"], 1)
        self.assertEqual(report["summary"]["existing_checksum_files"], 1)
        self.assertIn("description differs", report["existing_checksum"][0]["differences"])

    def test_each_independent_warning_sets_the_report_flag(self):
        self.asset["checksum"] = encoded(b"photo")
        self.asset.update(isFavorite=True, isArchived=True)
        self.asset["exifInfo"].update(description="desired", latitude=51.5, longitude=-0.12)
        self.assertFalse(self.report()["has_warnings"])
        self.asset["exifInfo"]["description"] = "different"
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertTrue(report["existing_checksum"][0]["differences"])
        self.assertEqual(report["existing_checksum"][0]["unchecked"], [])
        self.asset["exifInfo"]["description"] = "desired"
        del self.asset["exifInfo"]["latitude"]
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["existing_checksum"][0]["differences"], [])
        self.assertTrue(report["existing_checksum"][0]["unchecked"])

        self.assets = []
        (self.folder / "notes.txt").write_text("unrecognised file")
        report = self.report(folders=[self.folder])
        self.assertTrue(report["input_warnings"])
        self.assertEqual(report["unresolved_inputs"], [])
        self.assertEqual(report["within_input_matches"], [])
        self.assertTrue(report["has_warnings"])

    def test_native_filename_alias_collisions_and_unknown_times_warn(self):
        self.assets = []
        self.native_media("original.jpg", b"larger original photo", fileName="distinct.jpg")
        for captured, decision in (
            ("2023-01-01T00:00:00Z", "replace_larger"),
            ("not a date", "unknown_time"),
        ):
            with self.subTest(captured=captured):
                self.native_media("renamed.jpg", b"photo", dateTaken=captured)
                report = self.report(folders=[self.folder])
                self.assertTrue(report["has_warnings"])
                matches = report["within_input_matches"]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["path"], str(self.folder / "original.jpg"))
                self.assertEqual(
                    matches[0]["earlier_input"]["path"], str(self.folder / "renamed.jpg")
                )
                self.assertEqual(matches[0]["earlier_input"]["original_filename"], "original.jpg")
                self.assertEqual(matches[0]["decision"], decision)
                self.assertEqual(report["heuristic_matches"], [])

    def test_two_server_assets_with_identical_bytes_are_explicitly_ambiguous(self):
        self.asset.update(checksum=encoded(b"photo"), isFavorite=True, isArchived=True)
        self.asset["exifInfo"].update(description="desired", latitude=51.5, longitude=-0.12)
        self.assets.append(self.asset | {"id": "second"})
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["summary"]["existing_checksum_files"], 1)
        self.assertEqual(report["summary"]["metadata_unchecked_matches"], 2)
        self.assertEqual(
            {row["asset_id"] for row in report["existing_checksum"]}, {"remote/id", "second"}
        )
        for row in report["existing_checksum"]:
            self.assertEqual(row["differences"], [])
            self.assertIn("multiple server assets share this checksum", row["unchecked"])

    def test_input_collision_scan_continues_past_identical_and_outside_time_candidates(self):
        self.assets = []
        self.native_media("a/copy.jpg", b"photo", fileName="renamed.jpg")
        self.native_media(
            "b/copy.jpg", b"old photo", fileName="renamed.jpg", dateTaken="2010-01-01T00:00:00Z"
        )
        candidate = self.native_media("c/copy.jpg", b"different photo", fileName="renamed.jpg")
        report = self.report(folders=[self.folder])
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["input_files"], 4)
        self.assertEqual(len(report["within_input_matches"]), 1)
        match = report["within_input_matches"][0]
        self.assertEqual(match["path"], str(self.folder / "renamed.jpg"))
        self.assertEqual(match["earlier_input"]["path"], str(candidate))
        self.assertEqual(match["decision"], "skip_smaller")

    def test_identical_bytes_normalise_offset_dates_and_check_other_metadata_conflicts(self):
        self.assets = []
        self.native_media("copy.jpg", b"photo", dateTaken="2023-01-01T01:00:00+01:00")
        report = self.report(folders=[self.folder])
        self.assertEqual(report["input_metadata_conflicts"], [])
        self.assertFalse(report["has_warnings"])
        self.native_media(
            "copy.jpg", b"photo", dateTaken="2023-01-01T01:00:00+01:00", description="different"
        )
        report = self.report(folders=[self.folder])
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["heuristic_matches"], [])
        self.assertEqual(report["within_input_matches"], [])
        self.assertEqual(report["unresolved_inputs"], [])
        self.assertEqual(
            report["input_metadata_conflicts"],
            [
                {
                    "sha1": hashlib.sha1(b"photo").hexdigest(),
                    "fields": ["description"],
                    "paths": sorted(
                        str(self.folder / name) for name in ("copy.jpg", "renamed.jpg")
                    ),
                }
            ],
        )

    def test_matching_window_exact_and_just_outside_boundaries(self):
        base = datetime(2023, 1, 1, tzinfo=UTC)
        for seconds, expected in (
            (-6, False),
            (-5.000001, False),
            (-5, True),
            (4.999999, True),
            (5, False),
        ):
            with self.subTest(seconds=seconds):
                incoming = (base + timedelta(seconds=seconds)).isoformat()
                self.assertIs(match_window(incoming, base.isoformat()), expected)

    def test_larger_equal_and_smaller_different_content_are_distinguished(self):
        before = self.archive.read_bytes()
        for size, expected in ((2, "replace_larger"), (5, "skip_equal"), (10, "skip_smaller")):
            with self.subTest(size=size):
                self.asset["exifInfo"]["fileSizeInByte"] = size
                report = self.report()
                self.assertTrue(report["has_warnings"])
                self.assertEqual(report["summary"][expected], 1)
                row = report["heuristic_matches"][0]
                self.assertEqual(row["decision"], expected)
                self.assertEqual(row["server_size"], size)
                self.assertNotEqual(row["sha1"], row["server_checksum"])
                self.assertEqual(row["asset_id"], "remote/id")
        self.assertEqual(before, self.archive.read_bytes())
        self.assertTrue(
            all(method == "GET" or path == "/search/metadata" for method, path, _ in self.calls)
        )

    def test_existing_checksum_warns_for_each_retained_metadata_field_and_overrides_names(self):
        self.asset["checksum"] = encoded(b"photo")
        self.asset["originalFileName"] = "different-name.jpg"
        self.asset["fileCreatedAt"] = "2022-01-01T00:00:00Z"
        report = self.report()
        self.assertEqual(report["heuristic_matches"], [])
        self.assertEqual(report["summary"]["existing_checksum_files"], 1)
        self.assertEqual(
            set(report["existing_checksum"][0]["differences"]),
            {
                "dateTaken differs",
                "description differs",
                "latitude differs",
                "longitude differs",
                "favorited differs",
                "archived differs",
            },
        )
        self.assertEqual(report["summary"]["metadata_difference_matches"], 1)

    def test_matching_existing_metadata_has_no_differences_and_details_are_cached(self):
        self.asset.update(checksum=encoded(b"photo"), isFavorite=True, isArchived=True)
        self.asset["exifInfo"].update(description="desired", latitude=51.5, longitude=-0.12)
        self.entries.update({"Trip/photo.jpg": b"photo", "Trip/photo.jpg.json": self.data})
        self.write()
        report = self.report()
        self.assertFalse(report["has_warnings"])
        self.assertEqual(report["summary"]["existing_checksum_files"], 2)
        self.assertEqual(report["summary"]["metadata_difference_matches"], 0)
        self.assertEqual(sum(path.startswith("/assets/") for _, path, _ in self.calls), 1)

    def test_unknown_metadata_and_server_time_or_size_are_not_certified(self):
        self.asset["exifInfo"].pop("dateTimeOriginal")
        report = self.report()
        self.assertEqual(report["summary"]["unknown_time"], 1)
        self.asset["exifInfo"]["dateTimeOriginal"] = "2023-01-01T00:00:00Z"
        for size in (None, True, -1, "2"):
            self.asset["exifInfo"]["fileSizeInByte"] = size
            self.assertEqual(self.report()["summary"]["unknown_size"], 1)
        self.asset["checksum"] = encoded(b"photo")
        self.asset["exifInfo"] = {}
        report = self.report()
        self.assertEqual(report["summary"]["metadata_unchecked_matches"], 1)
        self.assertTrue(report["existing_checksum"][0]["unchecked"])

    def test_unrelated_names_and_times_are_not_warnings(self):
        self.asset["originalFileName"] = "photo.heic"
        self.assertFalse(self.report()["has_warnings"])
        self.asset["originalFileName"] = "photo.jpg"
        self.asset["exifInfo"]["dateTimeOriginal"] = "2023-01-01T00:00:05Z"
        # Incoming minus server is -5: included.
        self.assertEqual(self.report()["summary"]["replace_larger"], 1)
        self.asset["exifInfo"]["dateTimeOriginal"] = "2022-12-31T23:59:55Z"
        self.assertFalse(self.report()["has_warnings"])

    def test_all_visibility_pages_and_owner_external_filters(self):
        self.page_size = 1
        for index, visibility in enumerate(("timeline", "archive", "hidden")):
            item = copy.deepcopy(self.asset)
            item.update(id=f"extra-{index}", visibility=visibility)
            self.assets.append(item)
        partner = copy.deepcopy(self.asset)
        partner.update(id="partner", ownerId="partner")
        external = copy.deepcopy(self.asset)
        external.update(id="external", libraryId="library")
        trashed = copy.deepcopy(self.asset)
        trashed.update(id="trash", isTrashed=True)
        self.assets.extend([partner, external, trashed])
        report = self.report()
        self.assertEqual(report["server_assets"], 5)
        self.assertEqual(len(report["heuristic_matches"]), 5)
        identities = {r["asset_id"] for r in report["heuristic_matches"]}
        self.assertFalse(identities & {"partner", "external"})
        self.assertIn("trash", identities)
        self.assertEqual(
            {body["visibility"] for _, path, body in self.calls if path == "/search/metadata"},
            {"timeline", "archive", "hidden"},
        )

    def test_stale_asset_or_bad_inventory_fails_instead_of_printing_a_partial_report(self):
        for change in (
            {"checksum": encoded(b"changed")},
            {"ownerId": "other"},
            {"originalFileName": "changed.jpg"},
            {"isTrashed": True},
            {"libraryId": "external"},
        ):
            with self.subTest(change=change):
                self.override_detail = self.asset | change
                with self.assertRaisesRegex(ValidationError, "changed"):
                    self.report()
        self.override_detail = None
        self.asset["checksum"] = "invalid"
        with self.assertRaises(ValidationError):
            self.report()
        self.asset["checksum"] = base64.b64encode(b"short").decode()
        with self.assertRaises(ValidationError):
            self.report()

    def test_folder_mode_uses_actual_local_basename_and_utc_dates(self):
        self.asset["originalFileName"] = "photo.jpg"  # Archive would match; prepared file will not.
        self.assertFalse(self.report(folders=[self.folder])["has_warnings"])
        self.asset["originalFileName"] = "renamed.jpg"
        report = self.report(folders=[self.folder])
        self.assertEqual(report["input_kind"], "extracted_folders")
        self.assertEqual(report["summary"]["replace_larger"], 1)
        self.native["dateTaken"] = "2023-01-01T01:00:00+02:00"  # Still 2022 in UTC.
        (self.folder / "renamed.jpg.JSON").write_text(json.dumps(self.native))
        report = self.report(folders=[self.folder], selection=SELECTION)
        self.assertEqual(report["input_files"], 1)
        self.assertTrue(report["input_warnings"])
        self.assertTrue(report["has_warnings"])

    def test_input_aliases_can_collide_after_an_earlier_upload(self):
        self.assets = []
        self.entries["Photos from 2023/photo-edited.jpg"] = b"edited larger"
        self.write()
        report = self.report()
        self.assertEqual(report["heuristic_matches"], [])
        self.assertTrue(report["within_input_matches"])
        self.assertEqual(report["within_input_matches"][0]["decision"], "skip_smaller")
        self.assertEqual(
            report["within_input_matches"][0]["earlier_input"]["path"],
            "Photos from 2023/photo-edited.jpg",
        )

    def test_identical_input_bytes_with_conflicting_dates_are_reported(self):
        self.assets = []
        self.entries.update(
            {
                "Photos from 2022/other.jpg": b"photo",
                "Photos from 2022/other.jpg.json": sidecar("other.jpg", "other", "1640995200"),
            }
        )
        self.write()
        report = self.report()
        self.assertEqual(report["summary"]["input_metadata_conflicts"], 1)
        self.assertTrue(report["has_warnings"])
        self.assertIn("dateTaken", report["input_metadata_conflicts"][0]["fields"])
        code, output, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertIn('"check": "input_metadata_conflicts"', output)
        self.assertIn('"fields": ["dateTaken"]', output)
        self.assertEqual(self.report(selection=SELECTION)["summary"]["input_metadata_conflicts"], 0)

    def test_unmatched_invalid_and_filtered_source_items_remain_visible(self):
        for flags in ({"trashed": True}, {"googlePhotosOrigin": {"fromPartnerSharing": {}}}):
            self.entries["Photos from 2023/photo.jpg.json"] = self.data | flags
            self.write()
            self.assertEqual(self.report()["input_files"], 0)
        self.assertEqual(self.report(include_partner=True)["input_files"], 1)
        self.entries["Photos from 2023/photo.jpg.json"] = b"not JSON"
        self.write()
        report = self.report(selection=SELECTION)
        self.assertEqual(report["summary"]["unresolved_inputs"], 1)
        self.assertTrue(report["input_warnings"])
        self.assertTrue(report["has_warnings"])
        self.asset["checksum"] = encoded(b"photo")
        self.assertEqual(self.report()["summary"]["metadata_unchecked_matches"], 1)

    def test_cli_reports_are_read_only_and_redact_secrets(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.asset["id"] = self.key
        for extra in (("--json",), ("--details",), ("--extracted", self.folder, "--json")):
            with self.subTest(extra=extra):
                code, output, error = self.invoke(*extra)
                self.assertEqual(code, 0, error)
                if "--json" in extra:
                    report = json.loads(output)["server_audit"]
                    self.assertIs(report["read_only"], True)
                    if report["heuristic_matches"]:
                        self.assertEqual(report["heuristic_matches"][0]["asset_id"], "[REDACTED]")
                else:
                    self.assertIn("permanent deletion", output)
                    self.assertIn("No uploads", output)
                    self.assertIn("Snapshot warnings", output)
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )

    def test_config_requirements_timeout_and_safe_network_errors(self):
        for flag in ("--extracted", "--date-range"):
            value = str(self.folder) if flag == "--extracted" else "2023"
            out = io.StringIO()
            with redirect_stderr(out):
                self.assertEqual(script.main(["audit", flag, value, str(self.archive)]), 1)
            self.assertIn("require --config", out.getvalue())
        for value in ("0", "nan", "inf", "-1"):
            code, _, error = self.invoke("--timeout", value)
            self.assertEqual(code, 1)
            self.assertIn("positive finite", error)
        self.failure = HTTPError(
            "https://photos.example", 403, "secret", {}, io.BytesIO(self.key.encode())
        )
        code, output, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("HTTP 403", error)

    def test_inventory_pagination_repeats_are_rejected(self):
        self.page_size = 1
        self.assets.append(copy.deepcopy(self.asset))
        with self.assertRaisesRegex(ValidationError, "repeated"):
            self.report()

    def test_source_title_collisions_are_checked_across_dates_and_reported(self):
        self.assets = []
        self.entries = {
            "First/metadata.json": {"title": "Trip"},
            "First/one.jpg": b"one photo",
            "First/one.jpg.json": sidecar("one.jpg", "one"),
            "Second/metadata.json": {"title": "Trip"},
            "Second/two.jpg": b"two photo",
            "Second/two.jpg.json": sidecar("two.jpg", "two", "1704067200"),
        }
        self.write()
        report = self.report(selection=SELECTION)
        self.assertEqual(report["input_files"], 1)
        self.assertTrue(report["has_warnings"])
        self.assertEqual(
            report["source_album_title_collisions"],
            [{"title": "Trip", "source_directories": ["First", "Second"]}],
        )
        self.assertEqual(report["summary"]["source_album_title_collisions"], 1)
        code, output, error = self.invoke("--date-range", "2023")
        self.assertEqual(code, 0, error)
        self.assertIn("Export-wide duplicate album title", output)
        self.assertIn('"check": "source_album_title_collisions"', output)
        self.assertEqual(self.entries["First/metadata.json"]["title"], "Trip")
        # Other years' album ambiguities remain in the offline report, not this input's risk count.
        selected = self.report(selection=(date(2025, 1, 1), date(2025, 12, 31)))
        self.assertEqual(selected["source_album_title_collisions"], [])
        self.assertFalse(selected["has_warnings"])

    def test_duplicate_destination_titles_warn_but_single_existing_title_is_normal(self):
        self.assets = []
        self.native_media("renamed.jpg", b"photo", albums=[{"title": "Trip"}])
        self.destination_albums = [
            {"id": "trip-1", "albumName": "Trip"},
            {"id": "trip-2", "albumName": "Trip"},
            {"id": "other-1", "albumName": "Unrelated"},
            {"id": "other-2", "albumName": "Unrelated"},
        ]
        report = self.report(folders=[self.folder])
        self.assertTrue(report["has_warnings"])
        self.assertEqual(
            report["destination_album_title_collisions"],
            [{"title": "Trip", "album_ids": ["trip-1", "trip-2"]}],
        )
        self.assertEqual(report["summary"]["destination_album_title_collisions"], 1)
        self.destination_albums.pop(1)
        self.destination_albums.append(
            self.destination_albums[0]
        )  # Same ID isn't a separate album.
        report = self.report(folders=[self.folder])
        self.assertEqual(report["destination_album_title_collisions"], [])
        self.assertFalse(report["has_warnings"])

    def test_album_inventory_is_not_requested_without_album_memberships(self):
        self.report(folders=[self.folder])
        self.assertFalse(any(path == "/albums" for _, path, _ in self.calls))

    def test_invalid_album_inventory_does_not_produce_a_clean_report(self):
        self.native_media("renamed.jpg", b"photo", albums=[{"title": "Trip"}])
        for response in ({}, [None], [{"albumName": "Trip"}], [{"id": "album", "albumName": None}]):
            with self.subTest(response=response):
                self.destination_albums = response
                with self.assertRaisesRegex(ValidationError, "album inventory"):
                    self.report(folders=[self.folder])

    def test_repaired_identical_files_with_differing_albums_warn_until_consolidated(self):
        self.assets = []
        for first, second in (
            (["Trip"], ["Family"]),
            (["Trip"], ["Family", "Trip"]),
            ([], ["Trip"]),
        ):
            with self.subTest(first=first, second=second):
                self.native_media("renamed.jpg", b"photo", albums=[{"title": t} for t in first])
                self.native_media("other.jpg", b"photo", albums=[{"title": t} for t in second])
                with redirect_stdout(io.StringIO()):
                    repair(self.folder)
                before = {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
                report = self.report(folders=[self.folder])
                self.assertTrue(report["has_warnings"])
                self.assertEqual(report["within_input_matches"], [])
                self.assertEqual(report["input_metadata_conflicts"], [])
                self.assertEqual(report["source_album_title_collisions"], [])
                conflict = report["input_album_conflicts"][0]
                self.assertEqual(conflict["sha1"], hashlib.sha1(b"photo").hexdigest())
                self.assertEqual(conflict["required_albums"], sorted(set(first + second)))
                self.assertEqual(
                    {Path(copy["path"]).name: copy["albums"] for copy in conflict["copies"]},
                    {"renamed.jpg": sorted(set(first)), "other.jpg": sorted(set(second))},
                )
                self.assertEqual(report["summary"]["input_album_conflicts"], 1)
                code, output, error = self.invoke("--extracted", self.folder)
                self.assertEqual(code, 0, error)
                self.assertIn('"check": "input_album_conflicts"', output)
                self.assertIn('"required_albums"', output)
                self.assertIn("No albums were changed", output)
                self.assertEqual(
                    before, {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
                )
        for name in ("renamed.jpg", "other.jpg"):
            self.native_media(
                name, b"photo", fileName=name, albums=[{"title": "Trip"}, {"title": "Family"}]
            )
        self.assertFalse(self.report(folders=[self.folder])["has_warnings"])
        self.native_media(
            "other.jpg",
            b"photo",
            fileName="other.jpg",
            albums=[{"title": "Family"}, {"title": "Trip"}, {"title": "Trip"}],
        )
        self.assertFalse(self.report(folders=[self.folder])["has_warnings"])

    def test_invalid_native_album_memberships_are_not_silently_treated_as_empty(self):
        self.assets = []
        for albums in (None, "Trip", ["Trip"], [{"title": ""}], [{"title": 1}]):
            with self.subTest(albums=albums):
                self.native_media("renamed.jpg", b"photo", albums=albums)
                report = self.report(folders=[self.folder])
                self.assertTrue(report["has_warnings"])
                self.assertIn("invalid native albums", report["unresolved_inputs"][0]["issues"])

    def test_archive_duplicate_content_album_memberships_use_all_copies_of_each_google_id(self):
        self.assets = []
        self.entries = {
            "Trip/metadata.json": {"title": "Trip"},
            "Trip/one.jpg": b"photo",
            "Trip/one.jpg.json": sidecar("one.jpg", "one"),
            "Family/metadata.json": {"title": "Family"},
            "Family/two.jpg": b"photo",
            "Family/two.jpg.json": sidecar("two.jpg", "two"),
        }
        self.write()
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["input_album_conflicts"][0]["required_albums"], ["Family", "Trip"])
        # Album-folder copies of one Google item already have combined source memberships.
        self.entries["Family/two.jpg.json"] = sidecar("one.jpg", "one")
        self.write()
        report = self.report()
        self.assertEqual(report["input_album_conflicts"], [])
        self.assertFalse(report["has_warnings"])

    def test_trashed_checksum_match_warns_even_when_metadata_matches(self):
        self.asset.update(
            checksum=encoded(b"photo"), isTrashed=True, isFavorite=True, isArchived=True
        )
        self.asset["exifInfo"].update(description="desired", latitude=51.5, longitude=-0.12)
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["summary"]["trashed_checksum_matches"], 1)
        self.assertEqual(report["heuristic_matches"], [])
        match = report["existing_checksum"][0]
        self.assertTrue(match["server_trashed"])
        self.assertEqual(match["differences"], [])
        self.assertTrue(any("in trash" in note for note in match["unchecked"]))
        searches = [body for _, path, body in self.calls if path == "/search/metadata"]
        self.assertEqual(len(searches), 6)
        self.assertEqual(
            {(q["visibility"], bool(q.get("trashedAfter"))) for q in searches},
            {(v, trash) for v in ("timeline", "archive", "hidden") for trash in (False, True)},
        )
        self.override_detail = self.asset | {"isTrashed": False}
        with self.assertRaisesRegex(ValidationError, "changed"):
            self.report()

    def test_trashed_name_time_matches_warn_about_replacement_and_skips(self):
        self.asset["isTrashed"] = True
        for visibility in ("timeline", "archive", "hidden"):
            for size, decision in ((2, "replace_larger"), (5, "skip_equal"), (10, "skip_smaller")):
                with self.subTest(visibility=visibility, size=size):
                    self.asset["visibility"] = visibility
                    self.asset["exifInfo"]["fileSizeInByte"] = size
                    report = self.report()
                    self.assertTrue(report["has_warnings"])
                    self.assertEqual(report["summary"][decision], 1)
                    match = report["heuristic_matches"][0]
                    self.assertTrue(match["server_trashed"])
                    self.assertEqual(match["asset_id"], self.asset["id"])

    def test_outside_date_media_is_audited_and_matches_the_folder_upload_input(self):
        outside = self.native_media(
            "2022/old.jpg", b"larger old photo", dateTaken="2022-01-01T00:00:00Z"
        )
        self.asset["originalFileName"] = "old.jpg"
        self.asset["exifInfo"]["dateTimeOriginal"] = "2022-01-01T00:00:00Z"
        report = self.report(folders=[self.folder], selection=SELECTION)
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["input_files"], 2)
        self.assertEqual(report["summary"]["replace_larger"], 1)
        self.assertEqual(report["heuristic_matches"][0]["path"], str(outside))
        self.assertTrue(any(str(outside) in note for note in report["input_warnings"]))
        audited, _ = folder_inputs([self.folder], SELECTION)
        audited_paths = {Path(row["path"]) for row in audited}
        observed = []

        def uploader(command, env, *, dry_run):
            self.assertTrue(dry_run)
            self.assertIn("--recursive=true", command)
            self.assertFalse(any("date-range" in argument for argument in command))
            media, warnings = inspect_folder(Path(command[-1]))
            self.assertEqual(warnings, [])
            observed.extend(path for path, _ in media)
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(folder_upload.shutil, "which", return_value="immich-go"),
            patch.object(folder_upload, "run_upload", side_effect=uploader),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                folder_upload.upload_folder(
                    self.folder, self.config, dry_run=True, log_file=self.root / "preview.log"
                ),
                0,
            )
        self.assertEqual(audited_paths, set(observed))
        self.assertEqual(audited_paths, {outside, self.folder / "renamed.jpg"})

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_omitted_zero_and_false_values_are_unverified_on_server(self):
        self.entries["Photos from 2023/photo.jpg.json"] = self.data | {
            "geoDataExif": {"latitude": 51.5, "longitude": 0},
            "favorited": False,
            "archived": False,
        }
        self.write()
        extracted = self.root / "zero-extracted"
        with (
            patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}),
            redirect_stdout(io.StringIO()),
        ):
            code = script.main(
                [
                    "extract",
                    "--date-range",
                    "2023",
                    "--output",
                    str(extracted),
                    "--log-file",
                    str(self.root / "extract.log"),
                    str(self.archive),
                ]
            )
        self.assertEqual(code, 0)
        media, warnings = inspect_folder(extracted)
        self.assertEqual(warnings, [])
        self.assertEqual(len(media), 1)
        for field in ("longitude", "favorited", "archived"):
            self.assertNotIn(field, media[0][1])
        self.asset.update(checksum=encoded(b"photo"), isFavorite=True, isArchived=True)
        self.asset["exifInfo"].update(latitude=51.5, longitude=100, description="desired")
        # The ZIP has explicit expectations; the native folder cannot supply the omitted ones.
        source_report = self.report()
        self.assertEqual(
            set(source_report["existing_checksum"][0]["differences"]),
            {"longitude differs", "favorited differs", "archived differs"},
        )
        report = self.report(folders=[extracted])
        self.assertTrue(report["has_warnings"])
        for field in ("longitude", "favorited", "archived"):
            self.assertIn(
                f"sidecar omits {field}; source value unverified",
                report["existing_checksum"][0]["unchecked"],
            )
        client = self.client()
        with patch.object(client, "matches", return_value=[self.asset]):
            checked = validate(extracted, client)
        self.assertEqual(checked["counts"]["incomplete"], 1)
        for field in ("longitude", "favorited", "archived"):
            self.assertIn(
                f"sidecar omits {field}; source value unverified", checked["files"][0]["unchecked"]
            )

    def test_server_name_search_continues_after_an_outside_time_candidate(self):
        first = copy.deepcopy(self.asset)
        first.update(id="earlier-nonmatch", checksum=encoded(b"different older photo"))
        first["exifInfo"]["dateTimeOriginal"] = "2022-01-01T00:00:00Z"
        self.assets.insert(0, first)
        self.page_size = 1
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["summary"]["replace_larger"], 1)
        self.assertEqual(
            [row["asset_id"] for row in report["heuristic_matches"]], [self.asset["id"]]
        )
        detail_reads = [
            path
            for method, path, _ in self.calls
            if method == "GET" and path.startswith("/assets/")
        ]
        self.assertIn("/assets/earlier-nonmatch", detail_reads)
        self.assertIn("/assets/remote%2Fid", detail_reads)

    def test_valid_metadata_copy_cannot_hide_conflicting_or_unreadable_copies(self):
        self.assets = []
        part = self.root / "part-2.zip"
        limit = len(json.dumps(self.data).encode()) + 64
        later = self.data | {"title": "later.jpg", "url": "later"}
        for metadata_copy in (
            self.data | {"description": "conflicting description"},
            b"not JSON",
            b"[]",
            b'{"unrecognised": true}',
            json.dumps(self.data | {"description": "x" * limit}).encode(),
        ):
            with self.subTest(copy_type=type(metadata_copy).__name__, size=len(metadata_copy)):
                write_zip(
                    part,
                    {
                        "Photos from 2023/photo.jpg.json": metadata_copy,
                        "Photos from 2023/later.jpg": b"later photo",
                        "Photos from 2023/later.jpg.json": later,
                    },
                )
                with patch("immich_ctl.takeout_upload_audit.JSON_LIMIT", limit):
                    report = upload_audit([self.archive, part], self.client())
                self.assertTrue(report["has_warnings"])
                self.assertEqual(report["input_files"], 2)
                self.assertEqual(len(report["unresolved_inputs"]), 1)
                row = report["unresolved_inputs"][0]
                self.assertEqual(row["path"], "Photos from 2023/photo.jpg")
                self.assertIn("missing, invalid or conflicting source metadata", row["issues"])
                self.assertEqual(report["input_metadata_conflicts"], [])

    def test_trash_search_bound_is_valid_utc_and_includes_every_possible_date(self):
        self.asset.update(isTrashed=True, visibility="hidden")
        report = self.report()
        self.assertTrue(report["has_warnings"])
        self.assertEqual(report["server_assets"], 1)
        self.assertTrue(report["heuristic_matches"][0]["server_trashed"])
        trash_queries = [
            body
            for _, path, body in self.calls
            if path == "/search/metadata" and "trashedAfter" in body
        ]
        self.assertEqual(len(trash_queries), 3)
        self.assertEqual(
            {body["visibility"] for body in trash_queries}, {"timeline", "archive", "hidden"}
        )
        for query in trash_queries:
            bound = datetime.fromisoformat(query["trashedAfter"].replace("Z", "+00:00"))
            self.assertEqual(bound, datetime.min.replace(tzinfo=UTC))
            self.assertIs(query["withDeleted"], True)

    def test_missing_native_filename_falls_back_and_invalid_values_warn(self):
        self.assets = []
        native = self.native.copy()
        del native["fileName"]
        sidecar_path = self.folder / "renamed.jpg.JSON"
        sidecar_path.write_text(json.dumps(native))
        rows, warnings = folder_inputs([self.folder], SELECTION)
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["original_filename"], "renamed.jpg")
        self.assertEqual(rows[0]["issues"], [])
        self.assertFalse(self.report(folders=[self.folder])["has_warnings"])
        for name in (None, "", 123, False, {"invalid": "name"}, ["name"]):
            with self.subTest(fileName=name):
                sidecar_path.write_text(json.dumps(native | {"fileName": name}))
                rows, _ = folder_inputs([self.folder], SELECTION)
                self.assertEqual(rows[0]["original_filename"], "renamed.jpg")
                self.assertIn("native original filename unavailable", rows[0]["issues"])
                report = self.report(folders=[self.folder])
                self.assertTrue(report["has_warnings"])
                self.assertEqual(report["summary"]["unresolved_inputs"], 1)


class UploadAuditProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=30, deadline=None, derandomize=True)
    @given(
        st.integers(min_value=-7000, max_value=7000),
        st.integers(min_value=0, max_value=1000),
        st.integers(min_value=0, max_value=1000),
    )
    def test_timestamp_window_and_size_decisions(delta_ms, incoming, existing):
        base = datetime(2023, 1, 1, tzinfo=UTC)
        a = (base + timedelta(milliseconds=delta_ms)).isoformat()
        assert match_window(a, base.isoformat()) is (-5000 <= delta_ms < 5000)
        decision = size_decision(incoming, existing)
        assert decision == (
            "replace_larger"
            if incoming > existing
            else "skip_equal"
            if incoming == existing
            else "skip_smaller"
        )
        assert size_decision(incoming, None) == "unknown_size"
