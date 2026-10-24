"""Regression tests for upstream false success and source-to-sidecar fidelity."""

import copy
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout import Coverage, media_type
from immich_ctl.takeout_metadata import album_expectation, album_payloads, differences, expectation
from immich_ctl.takeout_reconcile import reconcile_report
from immich_ctl.takeout_safety import check_collisions, log_has_errors
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

SELECTION = (date(2023, 1, 1), date(2023, 12, 31))
takeout = load_script("immich-takeout")


class FidelityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"
        self.folder = self.root / "extracted"
        self.folder.mkdir()
        self.data = sidecar("photo.jpg") | {
            "description": "Keep this description",
            "favorited": True,
            "archived": True,
            "geoDataExif": {"latitude": 51.5, "longitude": -0.12},
        }
        self.album = {
            "title": "Trip",
            "enrichments": [
                {"narrativeEnrichment": {"text": "Our trip"}},
                {
                    "locationEnrichment": {
                        "location": [
                            {
                                "name": "London",
                                "description": "UK",
                                "latitudeE7": 515000000,
                                "longitudeE7": -1200000,
                            }
                        ]
                    }
                },
            ],
        }
        write_zip(
            self.archive,
            {
                "Trip/metadata.json": self.album,
                "Trip/photo.jpg": b"photo",
                "Trip/photo.jpg.json": self.data,
            },
        )
        self.native = {
            "software": "immich-go version:0.32.0",
            "dateTaken": "2023-01-01T00:00:00Z",
            "description": "Keep this description",
            "latitude": 51.5,
            "longitude": -0.12,
            "favorited": True,
            "archived": True,
            "albums": [
                {
                    "title": "Trip",
                    "description": "Our trip\nLondon - UK",
                    "latitude": 51.5,
                    "longitude": -0.12,
                }
            ],
        }
        (self.folder / "renamed.jpg").write_bytes(b"photo")
        self.json_path = self.folder / "renamed.jpg.JSON"
        self.json_path.write_text(json.dumps(self.native))

    def report(self):
        return reconcile_report([self.archive], [self.folder], SELECTION)

    def test_roundtrip_preserves_all_supported_source_fields(self):
        report = self.report()
        self.assertEqual(report["selected"]["present"], 1)
        self.assertTrue(report["metadata_and_albums_validated"])
        self.assertTrue(report["files"][0]["metadata_verified"])
        self.assertEqual(report["files"][0]["metadata_differences"], [])

    def test_removed_or_altered_source_fields_are_reported_even_if_server_would_agree(self):
        for field in self.native.keys() - {"software"}:
            for remove in (True, False):
                with self.subTest(field=field, remove=remove):
                    native = copy.deepcopy(self.native)
                    if remove:
                        del native[field]
                    else:
                        native[field] = {
                            "dateTaken": "2024-01-01T00:00:00Z",
                            "description": "Changed",
                            "latitude": -20,
                            "longitude": 30,
                            "favorited": False,
                            "archived": False,
                            "albums": [{"title": "Wrong album"}],
                        }[field]
                    self.json_path.write_text(json.dumps(native))
                    report = self.report()
                    self.assertFalse(report["metadata_and_albums_validated"])
                    self.assertEqual(report["selected"]["needs_review"], 1)
                    self.assertFalse(report["files"][0]["metadata_verified"])
                    self.assertTrue(report["files"][0]["metadata_differences"])

    def test_unexpected_album_and_lost_album_enrichments_are_reported(self):
        for change in ("extra", "description", "latitude", "longitude"):
            with self.subTest(change=change):
                native = copy.deepcopy(self.native)
                if change == "extra":
                    native["albums"].append({"title": "Unrelated"})
                else:
                    del native["albums"][0][change]
                self.json_path.write_text(json.dumps(native))
                row = self.report()["files"][0]
                self.assertEqual(row["status"], "needs_review")
                self.assertTrue(any("album" in i for i in row["issues"]))

    def test_identical_bytes_combine_album_memberships_but_reject_metadata_conflicts(self):
        other = self.root / "other.zip"
        write_zip(
            other,
            {
                "Family/metadata.json": {"title": "Family"},
                "Family/photo.jpg": b"photo",
                "Family/photo.jpg.json": self.data,
            },
        )
        native = self.native | {"albums": self.native["albums"] + [{"title": "Family"}]}
        self.json_path.write_text(json.dumps(native))
        report = reconcile_report([self.archive, other], [self.folder], SELECTION)
        self.assertEqual(report["selected"]["present"], 2)
        # Same content under a separate Google ID with a different description.
        write_zip(
            other,
            {
                "Family/metadata.json": {"title": "Family"},
                "Family/photo.jpg": b"photo",
                "Family/photo.jpg.json": self.data | {"url": "another", "description": "Conflict"},
            },
        )
        report = reconcile_report([self.archive, other], [self.folder], SELECTION)
        self.assertEqual(report["selected"]["needs_review"], 2)
        self.assertTrue(all(not r["metadata_verified"] for r in report["files"]))

    def test_supported_formats_cannot_disappear_from_missing_media_report(self):
        for extension in ("jxl", "psd", "jpe", "insp", "flv", "insv", "3fr", "hif", "x3f"):
            with self.subTest(extension=extension):
                name = "photo." + extension
                write_zip(self.archive, {name: b"not extracted", name + ".json": sidecar(name)})
                report = self.report()
                self.assertEqual(report["source_media_files"], 1)
                self.assertEqual(report["selected"]["missing"], 1)
                self.assertEqual(report["files"][0]["path"], name)
                self.assertNotEqual(media_type(name.upper()), "Other")

    def test_unclassified_source_files_are_explicit_and_preclude_complete_verification(self):
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("Takeout/Google Photos/Trip/photo.futureformat", b"unknown")
        report = self.report()
        self.assertEqual(report["unclassified_source_files"][0]["path"], "Trip/photo.futureformat")
        self.assertFalse(report["metadata_and_albums_validated"])
        self.assertIn("Unclassified source files (not checked as media)", report["warnings"])

    def test_conflicting_or_invalid_album_sources_require_review(self):
        other = self.root / "other.zip"
        for payload in (self.album | {"title": "Changed"}, b"broken JSON"):
            with self.subTest(payload=payload):
                write_zip(other, {"Trip/metadata.json": payload})
                report = reconcile_report([self.archive, other], [self.folder], SELECTION)
                self.assertEqual(report["selected"]["needs_review"], 1)
                self.assertFalse(report["metadata_and_albums_validated"])


class MetadataTests(unittest.TestCase):
    def test_zero_values_and_timezone_transformations_are_accepted(self):
        data = sidecar("p.jpg") | {
            "description": "",
            "favorited": False,
            "archived": False,
            "geoDataExif": {"latitude": 0, "longitude": 0},
        }
        expected = expectation(data, {})
        self.assertEqual(differences(expected, {"dateTaken": "2023-01-01T01:00:00+01:00"}), [])
        self.assertNotIn("latitude", expected)
        self.assertTrue(differences(expected, {"dateTaken": "2023-01-01T00:00:00"}))

    def test_gps_precedence_zero_coordinate_and_album_fallback(self):
        data = sidecar("p.jpg") | {
            "geoDataExif": {"latitude": 0, "longitude": 12},
            "geoData": {"latitude": 30, "longitude": 40},
        }
        self.assertEqual(expectation(data, {})["latitude"], 0)
        data["geoDataExif"]["longitude"] = 0
        self.assertEqual(expectation(data, {})["latitude"], 30)
        del data["geoData"]
        album = {
            "title": "Trip",
            "enrichments": [
                {
                    "locationEnrichment": {
                        "location": [
                            {"latitudeE7": 510000000, "longitudeE7": -1000000},
                        ]
                    }
                }
            ],
        }
        self.assertEqual(expectation(data, {"Trip": album})["latitude"], 51)
        other = copy.deepcopy(album)
        other["enrichments"][0]["locationEnrichment"]["location"][0]["latitudeE7"] = 100000000
        with self.assertRaisesRegex(ValueError, "Ambiguous album-derived"):
            expectation(data, {"Trip": album, "Other": other})

    def test_invalid_source_values_and_generated_values_do_not_pass(self):
        for values in (
            {"description": []},
            {"archived": 1},
            {"favorited": "true"},
            {"photoTakenTime": {"timestamp": "0"}},
            {"geoData": []},
            {"geoData": {"latitude": True}},
            {"geoData": {"latitude": float("nan")}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                expectation(sidecar("p.jpg") | values, {})
        for actual in (
            {"albums": {}},
            {"albums": [None]},
            {"albums": [{"title": []}]},
            {"albums": [{"title": "Trip"}, {"title": "Trip"}]},
        ):
            self.assertTrue(differences({"albums": [{"title": "Trip"}]}, actual))
        for value in (None, True, "51", float("nan"), 52):
            self.assertTrue(differences({"latitude": 51}, {"latitude": value}))

    def test_gps_bounds_defaults_tolerance_and_bad_dates(self):
        for latitude, longitude in ((90, 180), (-90, -180), (0, 12), (12, 0)):
            data = sidecar("p.jpg") | {"geoData": {"latitude": latitude, "longitude": longitude}}
            expected = expectation(data, {})
            self.assertEqual((expected["latitude"], expected["longitude"]), (latitude, longitude))
            native = {k: v for k, v in expected.items() if v != 0}
            self.assertEqual(differences(expected, native), [])
        for latitude, longitude in ((90.5, 0), (0, 180.5), (-90.5, 0), (0, -180.5)):
            with self.assertRaises(ValueError):
                expectation(
                    sidecar("p.jpg") | {"geoData": {"latitude": latitude, "longitude": longitude}},
                    {},
                )
        self.assertEqual(
            expectation(sidecar("p.jpg"), {}),
            {
                "dateTaken": "2023-01-01T00:00:00+00:00",
                "albums": [],
            },
        )
        for geo in ({}, {"latitude": 12}, {"longitude": 12}):
            expected = expectation(sidecar("p.jpg") | {"geoData": geo}, {})
            self.assertEqual(expected.get("latitude", 0), geo.get("latitude", 0))
            self.assertEqual(expected.get("longitude", 0), geo.get("longitude", 0))
        for field in ("latitude", "longitude"):
            self.assertEqual(differences({field: 0.0}, {field: 0.00001}), [])
            self.assertTrue(differences({field: 0.0}, {field: 0.000011}))
        self.assertEqual(
            differences({"dateTaken": "2023-01-01T00:00:00Z"}, {"dateTaken": "broken"}),
            ["dateTaken differs from source"],
        )
        self.assertEqual(
            differences({"albums": [], "dateTaken": "2023-01-01T00:00:00Z"}, {"albums": [None]}),
            ["invalid generated albums", "dateTaken differs from source"],
        )

    def test_album_narratives_accumulate_locations_and_titles_have_safe_fallbacks(self):
        data = {
            "title": "Trip",
            "enrichments": [
                {"narrativeEnrichment": {"text": "First"}},
                {"narrativeEnrichment": {"text": "Second"}},
                {
                    "locationEnrichment": {
                        "location": [{"name": "Place"}, {"description": "Region"}]
                    }
                },
            ],
        }
        self.assertEqual(
            album_expectation(data),
            {
                "title": "Trip",
                "description": "First\nSecond\nPlace - Region",
                "latitude": 0,
                "longitude": 0,
            },
        )
        self.assertEqual(
            album_expectation(
                {
                    "title": "Trip",
                    "enrichments": [{"locationEnrichment": {"location": [{"name": "Place"}]}}],
                }
            )["description"],
            "Place",
        )
        self.assertEqual(
            album_expectation(
                {
                    "title": "Trip",
                    "enrichments": [
                        {"locationEnrichment": {"location": [{"description": "Region"}]}}
                    ],
                }
            )["description"],
            "Region",
        )
        for title in (None, "", 42):
            coverage = Coverage(album_metadata={"Folder": [{"title": title, "enrichments": []}]})
            self.assertEqual(
                album_payloads(coverage, ["Folder"]),
                {
                    "Folder": {"title": "Folder", "enrichments": []},
                },
            )
        with self.assertRaises(ValueError):
            album_payloads(Coverage(), ["Missing"])
        for latitude, longitude in ((900000001, 0), (0, 1800000001)):
            with self.assertRaises(ValueError):
                album_expectation(
                    {
                        "title": "Trip",
                        "enrichments": [
                            {
                                "locationEnrichment": {
                                    "location": [
                                        {"latitudeE7": latitude, "longitudeE7": longitude},
                                    ]
                                }
                            }
                        ],
                    }
                )

    def test_album_payloads_preserve_wrapped_source_and_reject_conflicting_titles(self):
        coverage = Coverage(
            album_metadata={
                "A": [{"title": "Trip", "description": "keep"}],
                "B": [{"title": "Trip", "description": "different"}],
            }
        )
        self.assertEqual(album_payloads(coverage, ["A"])["Trip"]["description"], "keep")
        with self.assertRaisesRegex(ValueError, "share a title"):
            album_payloads(coverage, ["A", "B"])
        for enrichments in (
            {},
            [None],
            [{"narrativeEnrichment": None}],
            [{"locationEnrichment": {"location": [None]}}],
        ):
            with self.subTest(enrichments=enrichments), self.assertRaises(ValueError):
                expectation(
                    sidecar("p.jpg"), {"Trip": {"title": "Trip", "enrichments": enrichments}}
                )

    @staticmethod
    @settings(max_examples=30, deadline=None, derandomize=True)
    @given(
        st.floats(min_value=-90, max_value=90, allow_nan=False),
        st.floats(min_value=-180, max_value=180, allow_nan=False),
        st.booleans(),
    )
    def test_comparable_metadata_roundtrips_and_lost_true_flags_fail(latitude, longitude, favorite):
        data = sidecar("p.jpg") | {
            "geoDataExif": {"latitude": latitude, "longitude": longitude},
            "favorited": favorite,
        }
        expected = expectation(data, {})
        assert differences(expected, expected) == []
        if favorite:
            reduced = expected.copy()
            del reduced["favorited"]
            assert differences(expected, reduced) == ["favorited differs from source"]


class SafetyTests(unittest.TestCase):
    def test_collision_detection_hashes_late_bytes_and_ignores_nonmedia_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, b = root / "a.zip", root / "b.zip"
            # An earlier singleton must not prevent checking a later collision.
            prefix = b"x" * (1024 * 1024 + 10)
            write_zip(a, {"A/aaa.jpg": b"only", "A/z.jpg": prefix + b"a", "A/info.txt": b"text1"})
            write_zip(b, {"B/z.jpg": prefix + b"b", "B/info.txt": b"text2"})
            with self.assertRaisesRegex(ValueError, "basename/size collision") as error:
                check_collisions([a, b])
            self.assertIn('"A/z.jpg"', str(error.exception))
            self.assertIn('"B/z.jpg"', str(error.exception))
            write_zip(b, {"B/z.jpg": prefix + b"a", "B/info.txt": b"text2"})
            with zipfile.ZipFile(b, "a") as archive:
                archive.writestr("Takeout/Google Photos/Directory/", b"")
                archive.writestr("Takeout/Other/z.jpg", prefix + b"c")
            check_collisions([a, b])

    def test_cross_part_collisions_fail_before_tool_logs_or_upload_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, b = root / "a.zip", root / "b.zip"
            write_zip(a, {"A/same.jpg": b"aaaa", "A/same.jpg.json": sidecar("same.jpg", "a")})
            write_zip(b, {"B/same.jpg": b"bbbb", "B/same.jpg.json": sidecar("same.jpg", "b")})
            config = root / "account.ini"
            config.write_text("[immich]\nserver=https://photos.example\napi_key=synthetic\n")
            before = {p: p.read_bytes() for p in (a, b, config)}
            for action, flags in (
                ("extract", ["--output", str(root / "output")]),
                ("upload", ["--config", str(config), "--dry-run"]),
            ):
                with (
                    self.subTest(action=action),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()) as error,
                    patch.object(takeout.shutil, "which", return_value="immich-go"),
                    patch.object(takeout.subprocess, "run") as tool,
                    patch.object(takeout, "run_upload") as upload,
                ):
                    code = takeout.main(
                        [
                            action,
                            "--date-range",
                            "2023",
                            "--log-file",
                            str(root / "log"),
                            *flags,
                            str(a),
                            str(b),
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn("basename/size collision", error.getvalue())
                tool.assert_not_called()
                upload.assert_not_called()
                self.assertFalse((root / "log").exists())
                self.assertFalse((root / "output").exists())
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            write_zip(b, {"B/same.jpg": b"aaaa"})
            check_collisions([b, a, b])  # Legitimate album copies and repeated parts.

    def test_structured_logs_distinguish_errors_and_invalid_logs_from_clean_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "log"
            for text, failed in (
                ("", False),
                ('\n{"level":"INFO"}\n', False),
                ('{"level":"WARN"}', False),
                ('{"level":"ERROR"}', True),
                ('\n{"level":"ERROR"}\n', True),
                ('{"level":"FATAL"}', True),
                ("[]", True),
                ("broken", True),
            ):
                with self.subTest(text=text):
                    log.write_text(text)
                    self.assertEqual(log_has_errors(log), failed)

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_collision_is_rejected_and_separate_passes_preserve_both_assets_and_albums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parts = []
            for album, content, timestamp in (
                ("A", b"aaaa", "1672531200"),
                ("B", b"bbbb", "1672617600"),
            ):
                part = root / (album + ".zip")
                write_zip(
                    part,
                    {
                        album + "/metadata.json": {"title": album},
                        album + "/same.jpg": content,
                        album + "/same.jpg.json": sidecar("same.jpg", album, timestamp),
                    },
                )
                parts.append(part)
            with (
                patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(root / "cache")}),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    takeout.main(
                        [
                            "extract",
                            "--date-range",
                            "2023",
                            "--output",
                            str(root / "unsafe"),
                            "--log-file",
                            str(root / "unsafe.log"),
                            *map(str, parts),
                        ]
                    ),
                    1,
                )
                self.assertFalse((root / "unsafe").exists())
                for album, part in zip(("A", "B"), parts, strict=True):
                    output = root / album
                    self.assertEqual(
                        takeout.main(
                            [
                                "extract",
                                "--date-range",
                                "2023",
                                "--output",
                                str(output),
                                "--log-file",
                                str(root / (album + ".log")),
                                str(part),
                            ]
                        ),
                        0,
                    )
                    media = list(output.rglob("*.jpg"))
                    self.assertEqual(
                        [p.read_bytes() for p in media], [b"aaaa" if album == "A" else b"bbbb"]
                    )
                    native = json.loads(Path(str(media[0]) + ".JSON").read_text())
                    self.assertEqual(native["albums"], [{"title": album}])
                    self.assertEqual(
                        native["dateTaken"],
                        "2023-01-01T00:00:00Z" if album == "A" else "2023-01-02T00:00:00Z",
                    )

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_real_corrupt_media_cannot_return_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            write_zip(
                archive, {"Year/p.jpg": b"unique-media-bytes", "Year/p.jpg.json": sidecar("p.jpg")}
            )
            archive.write_bytes(
                archive.read_bytes().replace(b"unique-media-bytes", b"broken-media-bytes")
            )
            before = archive.read_bytes()
            with (
                patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(root / "cache")}),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()) as error,
            ):
                code = takeout.main(
                    [
                        "extract",
                        "--date-range",
                        "2023",
                        "--output",
                        str(root / "out"),
                        "--log-file",
                        str(root / "error.log"),
                        str(archive),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("recorded errors", error.getvalue())
            self.assertIn("checksum error", (root / "error.log").read_text())
            self.assertEqual(list((root / "out").rglob("*.jpg")), [])
            self.assertEqual(archive.read_bytes(), before)
