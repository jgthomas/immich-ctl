"""Read-only HTTP-boundary tests for retained metadata and upload selection risks."""

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.folder_validate import Client, ValidationError
from immich_ctl.takeout_upload_audit import (
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
            self.assertIs(body["withDeleted"], False)
            self.assertIs(body["withStacked"], True)
            start = (body["page"] - 1) * self.page_size
            matches = [a for a in self.assets if a.get("visibility") == body["visibility"]]
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
        self.assertEqual(report["server_assets"], 4)
        self.assertEqual(len(report["heuristic_matches"]), 4)
        identities = {r["asset_id"] for r in report["heuristic_matches"]}
        self.assertFalse(identities & {"partner", "external", "trash"})
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
        self.assertEqual(self.report(folders=[self.folder], selection=SELECTION)["input_files"], 0)

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
