"""Exercise validation through the HTTP boundary without using Immich credentials."""

import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl import folder_validate as validation
from tests.script_loader import load_script

script = load_script("immich-validate-folder")


class Response(io.BytesIO):
    status = 200


class FolderValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.media = self.folder / "photo.jpg"
        self.media.write_bytes(b"photo contents")
        self.checksum = hashlib.sha1(self.media.read_bytes()).hexdigest()
        self.encoded = base64.b64encode(bytes.fromhex(self.checksum)).decode()
        self.sidecar = self.folder / "photo.jpg.JSON"
        self.metadata = {
            "software": "immich-go 0.32.0",
            "fileName": "photo.jpg",
            "dateTaken": "2023-01-01T00:00:00Z",
            "latitude": 51.5,
            "longitude": -0.12,
            "description": "A description",
            "favorited": True,
            "archived": True,
            "albums": [{"title": "Trip"}, {"title": "Family"}],
        }
        self.sidecar.write_text(json.dumps(self.metadata))
        self.key = 'synthetic-secret-"-\\-%'
        self.config = self.root / "account.ini"
        self.config.write_text(
            f"[immich]\nserver=https://photos.example/immich/api/\napi_key={self.key}\n"
        )
        self.asset = {
            "id": "asset-1",
            "ownerId": "me",
            "checksum": self.encoded,
            "fileCreatedAt": "2023-01-01T01:00:00+01:00",
            "isFavorite": True,
            "isArchived": True,
            "visibility": "archive",
            "isTrashed": False,
            "exifInfo": {"latitude": 51.5, "longitude": -0.12, "description": "A description"},
        }
        self.calls = []
        self.album_names = ["Trip", "Family", "Extra"]

    def respond(self, request, *, timeout):
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_header("X-api-key"), self.key)
        self.assertTrue(request.full_url.startswith("https://photos.example/immich/api/"))
        path = urlsplit(request.full_url).path.removeprefix("/immich/api")
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), path, body))
        if path == "/users/me":
            self.assertEqual(request.get_method(), "GET")
            payload = {"id": "me"}
        elif path == "/search/metadata":
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(body["checksum"], self.checksum)
            self.assertIs(body["withDeleted"], False)
            self.assertIs(body["withExif"], False)
            self.assertIs(body["withStacked"], True)
            self.assertEqual(body["size"], 1000)
            self.assertEqual(body["page"], 1)
            payload = {
                "assets": {
                    "items": [self.asset] if body["visibility"] == self.asset["visibility"] else [],
                    "nextPage": None,
                }
            }
        elif path == "/assets/asset-1":
            self.assertEqual(request.get_method(), "GET")
            payload = self.asset
        elif path == "/albums":
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"assetId": ["asset-1"]})
            payload = [{"albumName": name} for name in self.album_names]
        else:
            self.fail(f"Unexpected request: {request.get_method()} {path}")
        return Response(json.dumps(payload).encode())

    def invoke(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(validation, "build_opener") as build,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            build.return_value.open.side_effect = self.respond
            code = script.main([str(self.folder), "--config", str(self.config), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_read_only_end_to_end_metadata_albums_and_unchanged_inputs(self):
        before = {p: p.read_bytes() for p in self.folder.iterdir()}
        code, output, error = self.invoke("--json")
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(
            report["counts"],
            {"ok": 1, "missing": 0, "ambiguous": 0, "different": 0, "incomplete": 0},
        )
        self.assertEqual(report["files"][0]["checks"], 8)
        self.assertEqual(report["files"][0]["asset_ids"], ["asset-1"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual({p: p.read_bytes() for p in self.folder.iterdir()}, before)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(
            {body["visibility"] for method, path, body in self.calls if path == "/search/metadata"},
            {"timeline", "archive", "hidden"},
        )
        self.assertEqual(error, "")

    def test_differences_for_each_field_and_missing_album(self):
        self.asset.update(fileCreatedAt="2023-01-02T00:00:00Z", isFavorite=False, isArchived=False)
        self.asset["exifInfo"] = {"latitude": -20, "longitude": 30, "description": "Changed"}
        self.album_names = ["Trip"]
        code, output, _ = self.invoke("--json")
        self.assertEqual(code, 2)
        row = json.loads(output)["files"][0]
        self.assertEqual(row["status"], "different")
        self.assertEqual(
            set(row["differences"]),
            {
                "dateTaken differs",
                "description differs",
                "latitude differs",
                "longitude differs",
                "favorited differs",
                "archived differs",
                "missing album membership: Family",
            },
        )

    def test_partner_and_trashed_copies_do_not_confirm_upload(self):
        for change in ({"ownerId": "partner"}, {"isTrashed": True}):
            with self.subTest(change=change):
                original = self.asset.copy()
                self.asset.update(change)
                code, output, _ = self.invoke("--json")
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output)["counts"]["missing"], 1)
                self.asset = original

    def test_missing_and_ambiguous_matches_never_compare_metadata(self):
        for matches, status in (
            ([], "missing"),
            ([self.asset, {**self.asset, "id": "other"}], "ambiguous"),
        ):
            with (
                self.subTest(status=status),
                patch.object(validation.Client, "matches", return_value=matches),
            ):
                code, output, _ = self.invoke("--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["files"][0]["status"], status)
            self.assertFalse(
                any(path.startswith("/assets/") or path == "/albums" for _, path, _ in self.calls)
            )
            self.calls.clear()

    def test_absent_invalid_and_unsupported_metadata_is_incomplete(self):
        for metadata in (
            None,
            b"invalid",
            b"[]",
            b"{}",
            json.dumps({"tags": [{"value": "People/A"}]}).encode(),
            json.dumps({"dateTaken": "not a date", "latitude": True, "albums": [None]}).encode(),
        ):
            with self.subTest(metadata=metadata):
                if metadata is None:
                    self.sidecar.unlink()
                else:
                    self.sidecar.write_bytes(metadata)
                code, output, _ = self.invoke("--json")
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output)["counts"]["incomplete"], 1)
        self.sidecar.write_text(json.dumps(self.metadata))
        self.asset.pop("fileCreatedAt")
        self.asset.pop("exifInfo")
        code, output, _ = self.invoke("--json")
        self.assertEqual(code, 2)
        self.assertEqual(len(json.loads(output)["files"][0]["unchecked"]), 4)

    def test_identical_local_files_cache_searches_but_check_each_sidecar(self):
        (self.folder / "second.jpg").write_bytes(self.media.read_bytes())
        (self.folder / "second.jpg.JSON").write_text(
            json.dumps({"description": "Another description"})
        )
        code, output, _ = self.invoke("--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["counts"]["ok"], 1)
        self.assertEqual(json.loads(output)["counts"]["different"], 1)
        self.assertEqual(len([p for _, p, _ in self.calls if p == "/search/metadata"]), 3)
        self.assertEqual(len([p for _, p, _ in self.calls if p == "/assets/asset-1"]), 1)

    def test_symlinks_orphans_unknown_files_xmp_and_case_ambiguity_are_reported(self):
        (self.folder / "elsewhere.jpg").symlink_to(self.media)
        (self.folder / "alias").symlink_to(self.root, target_is_directory=True)
        (self.folder / "orphan.jpg.JSON").write_text("{}")
        (self.folder / "photo.jpg.XMP").write_text("xmp")
        (self.folder / "notes.txt").write_text("note")
        (self.folder / "photo.jpg.json").write_text("{}")
        code, output, _ = self.invoke("--json")
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(len(report["files"]), 1)
        self.assertEqual(len(report["warnings"]), 6)
        self.assertEqual(report["counts"]["incomplete"], 1)

    def test_oversized_sidecars_and_empty_folders(self):
        with patch.object(validation, "JSON_LIMIT", 10):
            self.assertEqual(self.invoke()[0], 2)
        self.sidecar.unlink()
        self.media.unlink()
        code, _, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("No supported", error)
        self.assertEqual(self.calls.count(("GET", "/users/me", None)), 1)

    def test_unreadable_directories_and_interrupts_cannot_report_success(self):
        def unreadable(folder, *, onerror, followlinks):
            self.assertIs(followlinks, False)
            onerror(PermissionError("directory cannot be read"))

        with patch.object(validation.os, "walk", side_effect=unreadable):
            code, _, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("directory cannot be read", error)
        self.assertEqual(self.calls, [])
        with patch.object(validation.Client, "read", side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke()[0], 130)

    def test_text_summary_details_and_secret_redaction(self):
        code, output, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("ok: 1", output)
        self.assertNotIn('"photo.jpg"', output)
        self.assertIn('"photo.jpg"', self.invoke("--details")[1])
        self.metadata["albums"] = [{"title": self.key + "\ncontrol"}]
        self.sidecar.write_text(json.dumps(self.metadata))
        code, output, error = self.invoke("--json")
        self.assertEqual(code, 2)
        difference = json.loads(output)["files"][0]["differences"][0]
        self.assertIn("[REDACTED]", difference)
        self.assertNotIn(self.key, difference)
        self.assertNotIn(self.key, error)

    def test_invalid_account_or_asset_response_and_invalid_arguments(self):
        with patch.object(validation.Client, "read", return_value={}):
            self.assertEqual(self.invoke()[0], 1)
        with patch.object(
            validation.Client, "matches", return_value=[{**self.asset, "id": "wrong-id"}]
        ):
            # Any unexpected route in the responder would be a test failure; return a wrong detail directly.
            with patch.object(validation.Client, "read", side_effect=[{"id": "me"}, self.asset]):
                self.assertEqual(self.invoke()[0], 1)
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as error:
                self.invoke("--timeout", value)
            self.assertEqual(error.exception.code, 2)
        self.config.write_text("bad secret config")
        self.assertEqual(self.invoke()[0], 1)


class ApiTests(unittest.TestCase):
    def client(self):
        return validation.Client("https://example/api", "synthetic-key", 7)

    def test_comparison_tolerances_explicit_false_zero_and_invalid_values(self):
        metadata = {
            "dateTaken": "2023-01-01T00:00:00Z",
            "latitude": 0,
            "longitude": 0,
            "favorited": False,
            "archived": False,
            "description": "",
        }
        asset = {
            "fileCreatedAt": "2023-01-01T01:00:00+01:00",
            "isFavorite": False,
            "visibility": "timeline",
            "exifInfo": {
                "latitude": 0.000009,
                "longitude": -0.000009,
                "description": "",
            },
        }
        self.assertEqual(validation.compare(metadata, asset, set()), ([], [], 6))
        asset["exifInfo"]["latitude"] = 0.000011
        self.assertEqual(validation.compare(metadata, asset, set()), (["latitude differs"], [], 6))
        self.assertEqual(validation.compare({}, asset, set()), ([], [], 0))
        invalid = {
            "dateTaken": "2023-01-01T00:00:00",
            "description": [],
            "latitude": float("nan"),
            "longitude": 10**1000,
            "archived": "true",
            "favorited": 1,
            "albums": [None],
        }
        differences, unchecked, checked = validation.compare(invalid, asset, set())
        self.assertEqual(differences, [])
        self.assertEqual(len(unchecked), 7)
        self.assertEqual(checked, 0)
        self.assertIsNone(validation.timestamp(None))
        # Archive status remains checkable when an API omits the legacy isArchived field.
        self.assertEqual(
            validation.compare({"archived": True}, {"visibility": "archive"}, set()), ([], [], 1)
        )

    def test_http_failures_redirects_connection_errors_and_invalid_json(self):
        for outcome in (
            HTTPError("https://example", code, "secret response", {}, io.BytesIO(b"secret"))
            for code in (301, 401, 403, 404, 500)
        ):
            with self.subTest(code=outcome.code), patch.object(validation, "build_opener") as build:
                build.return_value.open.side_effect = outcome
                with self.assertRaises(validation.ValidationError) as error:
                    self.client().read("/users/me")
                self.assertNotIn("secret", str(error.exception))
        for outcome in (URLError("secret"), OSError("secret")):
            with patch.object(validation, "build_opener") as build:
                build.return_value.open.side_effect = outcome
                with self.assertRaisesRegex(validation.ValidationError, "Connection failed"):
                    self.client().read("/users/me")
        for body in (b"invalid", b"\xff", b"x" * (validation.RESPONSE_LIMIT + 1)):
            with patch.object(validation, "build_opener") as build:
                build.return_value.open.return_value = Response(body)
                with self.assertRaises(validation.ValidationError):
                    self.client().read("/users/me")
        self.assertIsNone(
            validation.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other")
        )

    def test_search_pagination_ownership_and_invalid_shapes(self):
        checksum = hashlib.sha1(b"photo").hexdigest()
        asset = {
            "id": "one",
            "ownerId": "me",
            "checksum": base64.b64encode(bytes.fromhex(checksum)).decode(),
        }
        client = self.client()
        responses = [
            {"assets": {"items": [asset], "nextPage": "2"}},
            {
                "assets": {
                    "items": [{**asset, "id": "partner", "ownerId": "other"}],
                    "nextPage": None,
                }
            },
            *[{"assets": {"items": [], "nextPage": None}}] * 2,
        ]
        with patch.object(client, "read", side_effect=responses) as read:
            self.assertEqual(client.matches(checksum, "me"), [asset])
            self.assertEqual(read.call_args_list[1].args[1]["page"], 2)
        for response in (
            None,
            {},
            {"assets": {"items": []}},
            {"assets": {"items": [], "nextPage": "2"}},
            {"assets": {"items": [asset], "nextPage": "1"}},
            {"assets": {"items": [{}], "nextPage": None}},
            {"assets": {"items": [{**asset, "checksum": "bad"}], "nextPage": None}},
            {
                "assets": {
                    "items": [{**asset, "checksum": base64.b64encode(b"wrong").decode()}],
                    "nextPage": None,
                }
            },
        ):
            with (
                self.subTest(response=response),
                patch.object(client, "read", return_value=response),
            ):
                with self.assertRaises(validation.ValidationError):
                    client.matches(checksum, "me")
        repeated = {"assets": {"items": [asset], "nextPage": "2"}}
        with patch.object(client, "read", side_effect=[repeated, repeated]):
            with self.assertRaisesRegex(validation.ValidationError, "repeated an asset"):
                client.matches(checksum, "me")
        for response in ({}, [None], [{}]):
            with (
                patch.object(client, "read", return_value=response),
                self.assertRaises(validation.ValidationError),
            ):
                client.albums("asset")


class ComparisonProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=30, deadline=None, derandomize=True)
    @given(
        st.floats(min_value=-89, max_value=89, allow_nan=False),
        st.floats(min_value=-179, max_value=179, allow_nan=False),
        st.text(max_size=40),
        st.booleans(),
    )
    def test_equal_metadata_matches_and_meaningful_changes_are_detected(
        latitude, longitude, description, favorite
    ):
        expected = {
            "latitude": latitude,
            "longitude": longitude,
            "description": description,
            "favorited": favorite,
        }
        actual = {
            "exifInfo": {"latitude": latitude, "longitude": longitude, "description": description},
            "isFavorite": favorite,
        }
        assert validation.compare(expected, actual, set()) == ([], [], 4)
        actual["exifInfo"]["latitude"] += 0.001
        actual["isFavorite"] = not favorite
        differences, unchecked, checked = validation.compare(expected, actual, set())
        assert set(differences) == {"latitude differs", "favorited differs"}
        assert not unchecked and checked == 4

    @staticmethod
    @settings(max_examples=25, deadline=None, derandomize=True)
    @given(st.lists(st.text(min_size=1, max_size=12), max_size=8))
    def test_album_order_and_repetition_do_not_change_membership_checks(titles):
        actual = set(titles)
        first = validation.compare({"albums": [{"title": title} for title in titles]}, {}, actual)
        second = validation.compare(
            {"albums": [{"title": title} for title in reversed(titles + titles)]}, {}, actual
        )
        assert first == second == ([], [], len(actual))


class ExtractedMetadataIntegration(unittest.TestCase):
    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_extraction_supplies_supported_dates_gps_and_multiple_albums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "takeout.zip"
            takeout_metadata = {
                "title": "photo.jpg",
                "url": "photo-id",
                "photoTakenTime": {"timestamp": "1672531200"},
                "description": "Description",
                "favorited": True,
                "archived": True,
                "geoDataExif": {"latitude": 51.5, "longitude": -0.12},
            }
            with zipfile.ZipFile(source, "w") as archive:
                for album in ("Trip", "Family"):
                    prefix = f"Takeout/Google Photos/{album}/"
                    archive.writestr(prefix + "metadata.json", json.dumps({"title": album}))
                    archive.writestr(prefix + "photo.jpg", b"original media")
                    archive.writestr(prefix + "photo.jpg.json", json.dumps(takeout_metadata))
            extracted = root / "extracted"
            with (
                patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(root / "cache")}),
                redirect_stdout(io.StringIO()),
            ):
                code = load_script("immich-takeout").main(
                    [
                        "extract",
                        "--date-range",
                        "2023",
                        "--output",
                        str(extracted),
                        "--log-file",
                        str(root / "extract.log"),
                        str(source),
                    ]
                )
            self.assertEqual(code, 0)
            media, warnings = validation.inspect_folder(extracted)
            self.assertEqual(warnings, [])
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0][0].read_bytes(), b"original media")
            asset = {
                "fileCreatedAt": "2023-01-01T00:00:00.000Z",
                "isFavorite": True,
                "visibility": "archive",
                "exifInfo": {"description": "Description", "latitude": 51.5, "longitude": -0.12},
            }
            self.assertEqual(
                validation.compare(media[0][1], asset, {"Trip", "Family"}), ([], [], 8)
            )


if __name__ == "__main__":
    unittest.main()
