"""Regression coverage for symmetric albums, equivalent copies and standard video tags."""

import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from immich_ctl import companion_check as cc
from immich_ctl import companion_metadata as cm
from immich_ctl import companion_recover as cr
from immich_ctl.video_metadata import checker_metadata, coordinates
from tests import test_companion_check as fixtures
from tests.test_companion_recover import DATE, IDENTIFIER, INFO, native


class CompanionReviewTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.CompanionCheckTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_album_payloads_compare_symmetrically_without_mutating_inputs(self):
        for extra in (
            {"description": "Wrong"},
            {"latitude": 51.5},
            {"longitude": 1},
            {"location": {"name": "Wrong"}},
        ):
            for actual, expected in (
                ([{"title": "Trip", **extra}], [{"title": "Trip"}]),
                ([{"title": "Trip"}], [{"title": "Trip", **extra}]),
            ):
                with self.subTest(actual=actual, expected=expected):
                    before = copy.deepcopy((actual, expected))
                    self.assertFalse(cm.albums_equal(actual, expected))
                    self.assertEqual((actual, expected), before)
        self.assertTrue(
            cm.albums_equal(
                [{"title": "Trip", "description": "", "latitude": 0}], [{"title": "Trip"}]
            )
        )
        self.assertFalse(
            cm.albums_equal([{"title": "Trip"}, {"title": "Trip"}], [{"title": "Trip"}])
        )
        self.fx.metadata(
            self.fx.still, native(fileName=self.fx.still.name, albums=[{"title": "Trip"}])
        )
        self.assertEqual(self.fx.row()["status"], "metadata_difference")

    def test_equivalent_still_copies_merge_defaults_timezones_and_albums(self):
        other = self.fx.add("copy.HEIC", b"copy" + IDENTIFIER.encode())
        first = native(fileName=self.fx.still.name, albums=[{"title": "Trip"}])
        second = native(
            fileName=other.name,
            dateTaken="2025-06-15T14:00:00+02:00",
            albums=[{"title": "Family"}],
            favorited=False,
            archived=False,
            description="",
        )
        first.pop("longitude")
        self.fx.metadata(self.fx.still, first)
        self.fx.metadata(other, second)
        row = self.fx.row()
        self.assertEqual(row["status"], "metadata_difference")
        self.assertEqual(row["expected_metadata"]["dateTaken"], DATE)
        self.assertEqual(
            {a["title"] for a in row["expected_metadata"]["albums"]}, {"Trip", "Family"}
        )
        for change in (
            {"description": "different"},
            {"favorited": True},
            {"archived": True},
            {"latitude": 51.500001},
            {"dateTaken": "2025-06-15T12:00:01Z"},
        ):
            with self.subTest(change=change):
                self.fx.metadata(other, second | change)
                row = self.fx.row()
                self.assertEqual(row["status"], "reference_conflict")
                self.assertNotIn("expected_metadata", row)

    def test_zero_still_gps_does_not_accept_nonzero_embedded_gps(self):
        self.fx.metadata(self.fx.still, native(fileName=self.fx.still.name, latitude=0))
        self.assertEqual(self.fx.row()["status"], "reference_conflict")

    def test_invalid_normalization_is_never_equivalent(self):
        for data in (
            {"latitude": True},
            {"latitude": 91},
            {"longitude": 181},
            {"longitude": float("nan")},
            {"favorited": 0},
            {"description": None},
            {"dateTaken": "2025-06-15"},
            {"albums": {}},
            {"albums": [{}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                cm.normalize(data)
        self.assertEqual(cm.normalize({"custom": {"important": 1}})["custom"], {"important": 1})

    def probe(self, tags, streams=None, checker=True):
        data = {
            "format": {"format_name": "mov,mp4", "tags": tags},
            "streams": streams or [{"codec_type": "video"}],
        }
        result = subprocess.CompletedProcess([], 0, json.dumps(data).encode(), b"")
        with patch.object(cr.subprocess, "run", return_value=result) as run:
            value = (
                cc.probe(self.fx.video, "ffprobe")
                if checker
                else cr.probe(self.fx.video, "ffprobe")
            )
        return value, run.call_args.args[0]

    def test_standard_tags_are_compared_through_actual_probe_path(self):
        info, args = self.probe(
            {
                "com.apple.quicktime.content.identifier": IDENTIFIER.lower(),
                "creation_time": DATE,
                "location": "+51.5000+000.0000/",
            }
        )
        self.assertEqual({k: info[k] for k in INFO}, INFO)
        self.assertEqual(info["metadata_issues"], [])
        self.assertIn(":stream_tags", args[args.index("-show_entries") + 1])
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")
        self.fx.metadata(
            self.fx.video,
            native(fileName=self.fx.video.name, dateTaken="2020-01-01T00:00:00Z", latitude=40),
        )
        row = self.fx.row(info=info)
        self.assertEqual(row["status"], "metadata_difference")
        self.assertEqual(
            {d["field"] for d in row["comparisons"]["video_to_embedded"]["differences"]},
            {"dateTaken", "latitude"},
        )

    def test_stream_standard_metadata_and_ignored_nonvideo_streams(self):
        info, _ = self.probe(
            {},
            [
                {
                    "codec_type": "video",
                    "tags": {"creation_time": DATE, "location": "+51.5+000.0/"},
                },
                {"codec_type": "audio", "tags": {"creation_time": "2000-01-01T00:00:00Z"}},
                {"codec_type": "data", "tags": {"location": "+00+000/"}},
            ],
        )
        self.assertEqual(info["capture_time"], DATE)
        self.assertEqual(info["coordinates"], [51.5, 0])
        self.assertEqual(info["metadata_issues"], [])
        self.assertEqual(len(info["metadata_sources"]["capture_time"]), 1)

    def test_conflicting_or_invalid_tags_remain_unverified(self):
        apple = {
            "com.apple.quicktime.content.identifier": IDENTIFIER,
            "com.apple.quicktime.creationdate": DATE,
            "com.apple.quicktime.location.ISO6709": "+51.5+000/",
        }
        for extra in (
            {"creation_time": "2025-06-15T12:00:06Z"},
            {"creation_time": "bad"},
            {"creation_time": "2025-06-15T12:00:00"},
            {"location": "+40+010/"},
            {"location": "+91+000/"},
            {"location": "broken"},
        ):
            with self.subTest(extra=extra):
                info, _ = self.probe(apple | extra)
                self.assertTrue(info["metadata_issues"])
                self.assertEqual(info["capture_time"], DATE)
                report = self.fx.report(info=info)
                self.assertEqual(report["files"][0]["status"], "embedded_metadata_unverified")
                self.assertTrue(report["has_findings"])
                self.assertTrue(info["metadata_sources"]["capture_time"])
        info, _ = self.probe(
            apple | {"creation_time": "2025-06-15T14:00:00+02:00", "location": "+51.500+000.000/"}
        )
        self.assertEqual(info["metadata_issues"], [])

    def test_empty_and_invalid_stream_tags_are_not_fabricated(self):
        info = checker_metadata({}, [{"codec_type": "video", "tags": None}], None)
        self.assertIsNone(info["capture_time"])
        self.assertIsNone(info["coordinates"])
        self.assertTrue(info["metadata_issues"])
        self.assertEqual(checker_metadata({}, [], None)["metadata_issues"], [])
        for location in (None, 12, "+90.1+000/", "+00+180.1/", "+00+000/trailing"):
            self.assertIsNone(coordinates(location))
        self.assertEqual(coordinates("-90-180+001/"), [-90, -180])

    def test_recovery_probe_keeps_original_apple_only_policy(self):
        info, args = self.probe(
            {
                "com.apple.quicktime.content.identifier": IDENTIFIER,
                "com.apple.quicktime.creationdate": DATE,
                "com.apple.quicktime.location.ISO6709": "+51.5+000/",
                "creation_time": "2000-01-01T00:00:00Z",
                "location": "+40+010/",
            },
            checker=False,
        )
        self.assertEqual(info, INFO)
        self.assertNotIn(":stream_tags", args[args.index("-show_entries") + 1])

    def test_normalized_defaults_transport_fields_and_album_payloads(self):
        normalized = cm.normalize({"fileName": "copy.jpg", "software": "marker"})
        self.assertEqual(
            normalized,
            {
                "description": "",
                "favorited": False,
                "archived": False,
                "latitude": 0.0,
                "longitude": 0.0,
            },
        )
        self.assertEqual(cm.normalize({"archived": False}), normalized)
        self.assertEqual(
            cm.normalize(
                {"albums": [{"title": "Trip", "fileName": "evidence", "software": "source"}]}
            )["albums"],
            [{"title": "Trip", "fileName": "evidence", "software": "source"}],
        )
        self.assertFalse(
            cm.albums_equal(
                [{"title": "Trip", "custom": float("nan")}],
                [{"title": "Trip", "custom": float("nan")}],
            )
        )

    def test_coordinate_boundaries_and_negative_values(self):
        for latitude in (-90, 90):
            for longitude in (-180, 180):
                self.assertEqual(
                    cm.normalize({"latitude": latitude, "longitude": longitude})["longitude"],
                    longitude,
                )
                self.assertEqual(
                    coordinates(f"{latitude:+d}{longitude:+d}/"), [latitude, longitude]
                )
        for data in (
            {"latitude": -90.1},
            {"longitude": -180.1},
            {"longitude": float("inf")},
            {"archived": 0},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                cm.normalize(data)

    def test_embedded_provenance_keeps_original_and_normalized_values(self):
        info = checker_metadata(
            {"creation_time": "2025-06-15T14:00:00+02:00", "location": "+51.50-001.00/"},
            [],
            IDENTIFIER.lower(),
        )
        self.assertEqual(info["identifier"], IDENTIFIER)
        self.assertEqual(
            info["metadata_sources"],
            {
                "capture_time": [
                    {
                        "source": "format.creation_time",
                        "value": "2025-06-15T14:00:00+02:00",
                        "normalized": DATE,
                    }
                ],
                "coordinates": [
                    {
                        "source": "format.location",
                        "value": "+51.50-001.00/",
                        "normalized": [51.5, -1.0],
                    }
                ],
            },
        )
        info = checker_metadata(
            {},
            [
                {"codec_type": "video", "tags": None},
                {"codec_type": "video", "tags": {"creation_time": DATE}},
            ],
            None,
        )
        self.assertEqual(info["capture_time"], DATE)
        self.assertTrue(info["metadata_issues"])
        self.assertTrue(all(isinstance(issue, str) for issue in info["metadata_issues"]))

    def test_checker_uses_normalized_album_equivalence(self):
        self.fx.metadata(
            self.fx.still,
            native(
                fileName=self.fx.still.name,
                albums=[{"title": "Trip", "description": "", "latitude": 0}],
            ),
        )
        self.fx.metadata(
            self.fx.video, native(fileName=self.fx.video.name, albums=[{"title": "Trip"}])
        )
        self.assertEqual(self.fx.row()["status"], "matched")

    def test_comparison_tolerance_boundaries(self):
        actual = {"dateTaken": DATE, "latitude": 0}
        for seconds, significant in ((2, False), (3, True)):
            changed, small = cc.compare_metadata(
                actual, {"dateTaken": f"2025-06-15T12:00:0{seconds}Z"}, ("dateTaken",)
            )
            self.assertEqual(bool(changed), significant)
            self.assertEqual(bool(small), not significant)
        for delta, significant in ((0.000051, False), (0.0000511, True)):
            changed, small = cc.compare_metadata(actual, {"latitude": delta}, ("latitude",))
            self.assertEqual(bool(changed), significant)
            self.assertEqual(bool(small), not significant)
