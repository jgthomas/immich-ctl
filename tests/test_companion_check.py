"""Reusable extracted-companion checks use identifiers, preserve inputs and report uncertainty."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from immich_ctl import companion_check as cc
from tests.test_companion_recover import DATE, IDENTIFIER, INFO, OTHER, native


class CompanionCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "prepared"
        self.folder.mkdir()
        self.still = self.add(
            "photo.HEIC",
            b"still " + IDENTIFIER.encode(),
            albums=[{"title": "Trip", "description": "Narrative"}],
        )
        self.video = self.add(
            "completely-different-name.MP4",
            b"video",
            albums=[{"title": "Trip", "description": "Narrative"}],
        )
        env = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        env.start()
        self.addCleanup(env.stop)

    def add(self, name, contents, **fields):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        self.metadata(path, native(fileName=path.name, **fields))
        return path

    def metadata(self, path, data):
        path.with_name(path.name + ".JSON").write_text(json.dumps(data))

    def report(self, info=INFO, **options):
        with (
            patch.object(cc.shutil, "which", return_value="ffprobe"),
            patch.object(cc, "probe", return_value=info),
        ):
            return cc.check_companions([self.folder], **options)

    def row(self, **options):
        return self.report(**options)["files"][0]

    def test_identifier_match_ignores_names_and_preserves_bytes_and_metadata(self):
        before = {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
        report = self.report()
        self.assertEqual(report["counts"], {"matched": 1})
        self.assertFalse(report["has_findings"])
        row = report["files"][0]
        self.assertEqual([r["path"] for r in row["matched_stills"]], [str(self.still)])
        self.assertEqual(
            row["expected_metadata"]["albums"], [{"title": "Trip", "description": "Narrative"}]
        )
        self.assertEqual(row["sha256"], hashlib.sha256(b"video").hexdigest())
        self.assertEqual(
            row["matched_stills"][0]["sha256"], hashlib.sha256(before[self.still]).hexdigest()
        )
        self.assertEqual(before, {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()})
        self.assertTrue(report["read_only"])

    def test_global_identifier_matching_handles_case_and_chunk_boundaries(self):
        self.still.write_bytes(b"x" * (1024 * 1024 - 10) + IDENTIFIER.lower().encode() + b"end")
        self.assertEqual(self.row()["status"], "matched")

    def test_zero_gps_and_false_flags_follow_local_native_omitempty_defaults(self):
        for p in (self.still, self.video):
            self.metadata(p, native(fileName=p.name, latitude=51.5, albums=[]))
        data = json.loads(self.video.with_name(self.video.name + ".JSON").read_text())
        data.pop("longitude")
        data.update(favorited=False, archived=False, description="")
        self.metadata(self.video, data)
        self.assertEqual(self.row()["status"], "matched")

    def test_missing_video_gps_date_description_flags_and_albums_are_reported(self):
        expected = native(
            fileName=self.still.name,
            description="important",
            favorited=True,
            archived=True,
            albums=[{"title": "Trip", "description": "Narrative"}],
        )
        self.metadata(self.still, expected)
        self.metadata(
            self.video,
            {
                "software": cc.merged_metadata([{"metadata": native()}])["software"],
                "fileName": self.video.name,
            },
        )
        row = self.row()
        self.assertEqual(row["status"], "metadata_difference")
        fields = {d["field"] for d in row["comparisons"]["video_to_stills"]["differences"]}
        self.assertEqual(
            fields, {"dateTaken", "latitude", "description", "favorited", "archived", "albums"}
        )

    def test_missing_album_description_is_a_difference_even_with_same_membership(self):
        self.metadata(self.video, native(fileName=self.video.name, albums=[{"title": "Trip"}]))
        row = self.row()
        self.assertEqual(row["status"], "metadata_difference")
        self.assertEqual(
            [d["field"] for d in row["comparisons"]["video_to_stills"]["differences"]], ["albums"]
        )

    def test_small_offsets_report_separately_from_significant_differences(self):
        self.metadata(
            self.video,
            native(
                fileName=self.video.name,
                dateTaken="2025-06-15T12:00:01Z",
                latitude=51.50000000000001,
                albums=[{"title": "Trip", "description": "Narrative"}],
            ),
        )
        row = self.row()
        self.assertEqual(row["status"], "within_tolerance")
        self.assertEqual(
            {d["field"] for d in row["comparisons"]["video_to_stills"]["within_tolerance"]},
            {"dateTaken", "latitude"},
        )
        self.assertFalse(self.report()["has_findings"])
        self.metadata(
            self.video,
            native(
                fileName=self.video.name,
                dateTaken="2025-06-16T12:00:00Z",
                latitude=40,
                albums=[{"title": "Trip", "description": "Narrative"}],
            ),
        )
        self.assertEqual(self.row()["status"], "metadata_difference")

    def test_equivalent_timezones_compare_as_same_instant(self):
        self.metadata(
            self.video,
            native(
                fileName=self.video.name,
                dateTaken="2025-06-15T14:00:00+02:00",
                albums=[{"title": "Trip", "description": "Narrative"}],
            ),
        )
        self.assertEqual(self.row()["status"], "matched")

    def test_embedded_conflicts_with_still_are_not_automatically_repairable(self):
        for changed in ({"capture_time": "2024-01-01T00:00:00Z"}, {"coordinates": [40, 10]}):
            with self.subTest(changed=changed):
                row = self.row(info=INFO | changed)
                self.assertEqual(row["status"], "reference_conflict")
                self.assertTrue(row["reference_error"])
                self.assertTrue(row["matched_stills"])

    def test_duplicate_stills_union_albums_but_conflicting_metadata_blocks_match(self):
        other = self.add("copy.HEIC", b"copy " + IDENTIFIER.encode(), albums=[{"title": "Family"}])
        row = self.row()
        self.assertEqual(
            {a["title"] for a in row["expected_metadata"]["albums"]}, {"Trip", "Family"}
        )
        self.assertEqual(row["status"], "metadata_difference")
        self.metadata(
            other,
            native(fileName=other.name, description="conflicting", albums=[{"title": "Family"}]),
        )
        row = self.row()
        self.assertEqual(row["status"], "reference_conflict")
        self.assertNotIn("expected_metadata", row)

    def test_no_identifier_and_unmatched_identifier_do_not_infer_a_link(self):
        # Identical basename and date are not evidence of a Live Photo relationship.
        renamed = self.video.with_name("photo.MP4")
        self.video.rename(renamed)
        self.video.with_name(self.video.name + ".JSON").unlink()
        self.metadata(renamed, native(fileName=renamed.name, albums=[{"title": "Trip"}]))
        for identity, status in ((None, "no_identifier"), (OTHER, "identifier_unmatched")):
            with self.subTest(identity=identity):
                row = self.row(info=INFO | {"identifier": identity})
                self.assertEqual(row["status"], status)
                self.assertEqual(row["matched_stills"], [])
                self.assertNotIn("expected_metadata", row)

    def test_unpaired_video_own_embedded_metadata_is_still_compared(self):
        row = self.row(
            info=INFO
            | {"identifier": None, "capture_time": "2020-01-01T00:00:00Z", "coordinates": [0, 0]}
        )
        self.assertEqual(row["status"], "no_identifier")
        self.assertEqual(
            {d["field"] for d in row["comparisons"]["video_to_embedded"]["differences"]},
            {"dateTaken", "latitude"},
        )

    def test_absent_embedded_fields_do_not_invent_zero_gps_or_dates(self):
        row = self.row(info=INFO | {"capture_time": None, "coordinates": None})
        self.assertEqual(
            row["comparisons"]["video_to_embedded"], {"differences": [], "within_tolerance": []}
        )
        self.assertEqual(row["status"], "matched")

    def test_unknown_native_fields_are_reported_as_unchecked(self):
        self.metadata(
            self.video,
            native(
                fileName=self.video.name,
                tags=["Keep"],
                albums=[{"title": "Trip", "description": "Narrative"}],
            ),
        )
        row = self.row()
        self.assertEqual(row["unchecked_fields"], ["tags"])
        self.assertTrue(self.report()["has_findings"])

    def test_probe_failure_is_a_finding_without_losing_other_video_results(self):
        other = self.add(
            "second.MOV", b"second", albums=[{"title": "Trip", "description": "Narrative"}]
        )

        def probe(path, binary):
            if path == self.video:
                raise ValueError("unsupported video")
            return INFO

        with (
            patch.object(cc.shutil, "which", return_value="ffprobe"),
            patch.object(cc, "probe", side_effect=probe),
        ):
            report = cc.check_companions([self.folder])
        self.assertEqual(report["counts"], {"error": 1, "matched": 1})
        self.assertTrue(report["has_findings"])
        self.assertEqual({r["path"] for r in report["files"]}, {str(self.video), str(other)})

    def test_multiple_and_overlapping_folders_deduplicate_files_and_find_global_matches(self):
        nested = self.folder / "nested"
        nested.mkdir()
        target = nested / self.still.name
        self.still.rename(target)
        self.still.with_name(self.still.name + ".JSON").rename(
            target.with_name(target.name + ".JSON")
        )
        with (
            patch.object(cc.shutil, "which", return_value="ffprobe"),
            patch.object(cc, "probe", return_value=INFO),
        ):
            r = cc.check_companions([self.folder, nested, self.folder])
        self.assertEqual((r["stills"], r["videos"]), (1, 1))
        self.assertEqual(r["counts"], {"matched": 1})

    def test_changed_files_sidecars_or_roster_abort_without_a_clean_report(self):
        for which in ("video", "still", "sidecar", "new_file"):
            with self.subTest(which=which):
                source = self.video if which == "video" else self.still
                original = source.read_bytes()
                side = self.still.with_name(self.still.name + ".JSON")
                oldside = side.read_bytes()
                extra = self.folder / "new.jpg"

                def changing(
                    path,
                    binary,
                    which=which,
                    side=side,
                    oldside=oldside,
                    extra=extra,
                    source=source,
                    original=original,
                ):
                    if which == "sidecar":
                        side.write_bytes(oldside + b" ")
                    elif which == "new_file":
                        extra.write_bytes(b"new")
                    else:
                        source.write_bytes(original + b"x")
                    return INFO

                with (
                    patch.object(cc.shutil, "which", return_value="ffprobe"),
                    patch.object(cc, "probe", side_effect=changing),
                ):
                    with self.assertRaisesRegex(cc.CheckError, "changed"):
                        cc.check_companions([self.folder])
                source.write_bytes(original)
                side.write_bytes(oldside)
                if extra.exists():
                    extra.unlink()

    def test_invalid_sidecars_and_filename_aliases_require_review(self):
        side = self.video.with_name(self.video.name + ".JSON")
        original = side.read_bytes()
        for data in ({}, native(fileName="wrong.HEIC"), {"bad": "metadata"}):
            with self.subTest(data=data):
                self.metadata(self.video, data)
                with self.assertRaises((cc.CheckError, ValueError)):
                    self.report()
        side.write_bytes(original)
        link = self.folder / "linked.jpg"
        link.symlink_to(self.still)
        with self.assertRaisesRegex(cc.CheckError, "review"):
            self.report()

    def test_worker_validation_and_missing_ffprobe(self):
        for workers in (0, 9, True):
            with self.subTest(workers=workers), self.assertRaises(cc.CheckError):
                self.report(workers=workers)
        with patch.object(cc.shutil, "which", return_value=None), self.assertRaises(cc.CheckError):
            cc.check_companions([self.folder])

    def test_cli_json_progress_and_findings_exit_codes(self):
        for info, code in ((INFO, 0), (INFO | {"identifier": None}, 2)):
            out, err = io.StringIO(), io.StringIO()
            with (
                patch.object(cc.shutil, "which", return_value="ffprobe"),
                patch.object(cc, "probe", return_value=info),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                self.assertEqual(cc.main([str(self.folder), "--json"]), code)
            report = json.loads(out.getvalue())
            self.assertEqual(report["videos"], 1)
            self.assertIn("Videos checked: 1/1", err.getvalue())
            self.assertNotIn("Videos checked", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(cc.shutil, "which", return_value="ffprobe"),
            patch.object(cc, "probe", return_value=INFO),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(cc.main([str(self.folder), "--quiet", "--details"]), 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("matched", out.getvalue())

    def test_omitted_native_zero_fields_and_empty_albums_compare_equal(self):
        self.metadata(self.still, native(fileName=self.still.name, latitude=0, longitude=0))
        self.metadata(
            self.video,
            {"software": native()["software"], "fileName": self.video.name, "dateTaken": DATE},
        )
        self.assertEqual(self.row(info=INFO | {"coordinates": [0, 0]})["status"], "matched")

    def test_album_order_does_not_hide_or_invent_full_metadata_differences(self):
        albums = [{"title": "Trip", "description": "Narrative"}, {"title": "Family"}]
        self.metadata(self.still, native(fileName=self.still.name, albums=albums))
        self.metadata(self.video, native(fileName=self.video.name, albums=list(reversed(albums))))
        self.assertEqual(self.row()["status"], "matched")

    def test_large_gps_discrepancy_and_invalid_boolean_coordinate_need_review(self):
        self.metadata(
            self.video,
            native(
                fileName=self.video.name,
                latitude=51.51,
                albums=[{"title": "Trip", "description": "Narrative"}],
            ),
        )
        report = self.report()
        row = report["files"][0]
        self.assertEqual(row["status"], "metadata_difference")
        self.assertEqual(report["differing_fields"], {"latitude": 1})
        self.assertEqual(
            row["comparisons"]["video_to_stills"]["differences"],
            [{"field": "latitude", "actual": 51.51, "expected": 51.5}],
        )
        self.metadata(self.still, native(fileName=self.still.name, latitude=1, albums=[]))
        self.metadata(self.video, native(fileName=self.video.name, latitude=True, albums=[]))
        self.assertEqual(
            self.row(info=INFO | {"coordinates": [1, 0]})["status"], "metadata_difference"
        )

    def test_still_only_unsupported_fields_are_also_unchecked(self):
        self.metadata(
            self.still,
            native(
                fileName=self.still.name,
                tags=["Keep"],
                albums=[{"title": "Trip", "description": "Narrative"}],
            ),
        )
        report = self.report()
        self.assertEqual(report["files"][0]["unchecked_fields"], ["tags"])
        self.assertTrue(report["has_findings"])

    def test_small_embedded_offset_alone_has_tolerance_status(self):
        row = self.row(info=INFO | {"capture_time": "2025-06-15T12:00:01Z"})
        self.assertEqual(row["status"], "within_tolerance")
        self.assertEqual(row["comparisons"]["video_to_stills"]["within_tolerance"], [])
        self.assertTrue(row["comparisons"]["video_to_embedded"]["within_tolerance"])

    def test_invalid_version_marker_and_nonfinite_json_abort_before_probing(self):
        for data in (
            native(fileName=self.video.name, software="unknown"),
            native(fileName=self.video.name, latitude=float("nan")),
        ):
            with self.subTest(data=data):
                self.metadata(self.video, data)
                with patch.object(cc, "probe") as probe:
                    with self.assertRaises((cc.CheckError, ValueError)):
                        self.report()
                    probe.assert_not_called()

    def test_cli_details_errors_interrupts_and_worker_option(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(cc.shutil, "which", return_value="ffprobe"),
            patch.object(cc, "probe", return_value=INFO | {"identifier": None}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(
                cc.main([str(self.folder), "--details", "--quiet", "--workers", "1"]), 2
            )
        findings = [
            json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["path"], str(self.video))
        for error, status in ((cc.CheckError("input problem"), 1), (KeyboardInterrupt(), 130)):
            out, err = io.StringIO(), io.StringIO()
            with (
                patch.object(cc, "check_companions", side_effect=error),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                self.assertEqual(cc.main([str(self.folder), "--json"]), status)
            self.assertEqual(out.getvalue(), "")
            if status == 1:
                self.assertIn("input problem", err.getvalue())

    def test_explicit_probe_concurrency_is_honoured(self):
        with patch.object(cc, "ThreadPoolExecutor", wraps=cc.ThreadPoolExecutor) as pool:
            self.report(workers=2)
        pool.assert_called_once_with(max_workers=2)

    def test_sidecar_read_limit_is_bounded_even_after_preflight(self):
        sidecar = self.video.with_name(self.video.name + ".JSON")
        payload = sidecar.read_bytes()
        real_open = Path.open

        @contextmanager
        def bounded(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:
                mode = args[0] if args else kwargs.get("mode", "r")
                if "r" not in mode:
                    yield stream
                    return

                class Reader:
                    def read(reader, size=-1):
                        self.assertIsInstance(size, int)
                        self.assertTrue(0 < size <= len(payload) + 1)
                        return stream.read(size)

                yield Reader()

        with (
            patch.object(cc, "JSON_LIMIT", len(payload)),
            patch.object(Path, "open", autospec=True, side_effect=bounded),
        ):
            self.assertEqual(
                cc.load_entry(self.video, self.folder, {})["metadata"], json.loads(payload)
            )
            sidecar.write_bytes(payload + b" ")
            with self.assertRaises(cc.CheckError):
                cc.load_entry(self.video, self.folder, {})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg integration")
    def test_real_ffprobe_identifier_and_metadata_round_trip(self):
        self.video.unlink()
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=16x16:r=5",
                "-t",
                "0.4",
                "-c:v",
                "mpeg4",
                "-metadata",
                "com.apple.quicktime.content.identifier=" + IDENTIFIER,
                "-metadata",
                "com.apple.quicktime.creationdate=" + DATE,
                "-metadata",
                "com.apple.quicktime.location.ISO6709=+51.5000+000.0000/",
                "-movflags",
                "use_metadata_tags",
                "-f",
                "mov",
                str(self.video),
            ],
            capture_output=True,
            check=True,
        )
        report = cc.check_companions([self.folder], workers=1)
        self.assertEqual(report["counts"], {"matched": 1})
        self.assertEqual({k: report["files"][0]["embedded"][k] for k in INFO}, INFO)
        self.assertEqual(report["files"][0]["embedded"]["metadata_issues"], [])
