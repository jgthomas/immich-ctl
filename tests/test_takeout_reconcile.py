"""Read-only source coverage, conservative date scoping and extraction reconciliation."""

import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout_audit import audit_report
from immich_ctl.takeout_reconcile import JSON_LIMIT, reconcile_report
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

takeout = load_script("immich-takeout")
SELECTION = (date(2023, 1, 1), date(2023, 12, 31))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"
        self.extracted = self.root / "extracted"
        self.extracted.mkdir()
        self.recovery = self.root / "recovered"
        self.entries = {
            "Photos from 2023/photo(1).jpg": b"original",
            "Photos from 2023/photo.jpg.supplemental-metadata(1).json": sidecar("photo.jpg"),
            "Photos from 2023/photo-edited(1).jpg": b"edited",
        }
        self.write()

    def write(self):
        write_zip(self.archive, self.entries)

    def local_file(self, folder, name, contents):
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        Path(str(target) + ".JSON").write_text(
            json.dumps(
                {"software": "immich-go version:0.32.0", "dateTaken": "2023-01-01T00:00:00Z"}
            )
        )
        return target

    def report(self, *folders):
        return reconcile_report([self.archive], list(folders or [self.extracted]), SELECTION)

    def invoke(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = takeout.main(
                [
                    "reconcile",
                    "--date-range",
                    "2023",
                    "--extracted",
                    str(self.extracted),
                    *map(str, extra),
                    str(self.archive),
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def test_normal_and_recovery_folders_combine_by_content_not_filename(self):
        self.local_file(self.extracted, "renamed.jpg", b"original")
        first = self.report()
        self.assertEqual(first["selected"]["present"], 1)
        self.assertEqual(first["selected"]["missing"], 1)
        edit = next(row for row in first["files"] if row["association"] == "candidate_edit")
        self.assertEqual(edit["path"], "Photos from 2023/photo-edited(1).jpg")
        self.assertEqual(edit["status"], "missing")
        self.local_file(self.recovery, "arbitrary-prefix.jpg", b"edited")
        combined = self.report(self.extracted, self.recovery)
        self.assertEqual(combined["selected"]["files"], 2)
        self.assertEqual(combined["selected"]["present"], 2)
        self.assertEqual(combined["selected"]["missing"], 0)
        self.assertEqual(combined["selected"]["unique_checksums"], 2)
        self.assertTrue(combined["metadata_and_albums_validated"])

    def test_album_copies_count_as_source_files_and_conflicts_require_review(self):
        source = self.entries["Photos from 2023/photo.jpg.supplemental-metadata(1).json"]
        self.entries.update(
            {
                "Trip/metadata.json": {"title": "Trip"},
                "Trip/photo.jpg": b"original",
                "Trip/photo.jpg.json": source,
            }
        )
        self.write()
        original = self.local_file(self.extracted, "original.jpg", b"original")
        Path(str(original) + ".JSON").write_text(
            json.dumps(
                {
                    "software": "immich-go version:0.32.0",
                    "dateTaken": "2023-01-01T00:00:00Z",
                    "albums": [{"title": "Trip"}],
                }
            )
        )
        report = self.report()
        self.assertEqual(report["selected"]["files"], 3)
        self.assertEqual(report["selected"]["unique_checksums"], 2)
        self.assertEqual(report["selected"]["present"], 2)
        self.assertTrue(all(row["albums"] == ["Trip"] for row in report["files"]))
        self.entries["Trip/photo.jpg.json"] = source | {"trashed": True, "description": "conflict"}
        self.write()
        report = self.report()
        self.assertEqual(report["selected"]["needs_review"], 3)
        self.assertTrue(
            all("conflicting source metadata" in row["issues"] for row in report["files"])
        )
        self.assertEqual(report["selected"]["content_present"], 2)

    def test_album_memberships_never_include_an_unrelated_items_albums(self):
        self.entries.update(
            {
                "Other Trip/metadata.json": {"title": "Unrelated album"},
                "Other Trip/other.jpg": b"another photo",
                "Other Trip/other.jpg.json": sidecar("other.jpg", "other"),
            }
        )
        self.write()
        rows = {row["path"]: row for row in self.report()["files"]}
        self.assertEqual(rows["Other Trip/other.jpg"]["albums"], ["Unrelated album"])
        self.assertEqual(rows["Photos from 2023/photo(1).jpg"]["albums"], [])
        self.assertEqual(rows["Photos from 2023/photo-edited(1).jpg"]["albums"], [])

    def test_unclassified_files_are_reported_without_hiding_later_media(self):
        self.entries = {"000-first.bin": b"unsupported"} | self.entries
        self.write()
        report = self.report()
        self.assertEqual(report["source_media_files"], 2)
        self.assertEqual(report["selected"]["files"], 2)
        self.assertEqual(report["selected"]["missing"], 2)
        self.assertEqual(report["export_exception"]["files"], 0)
        self.assertEqual(
            [row["path"] for row in report["unclassified_source_files"]], ["000-first.bin"]
        )
        self.assertEqual(
            report["warnings"], {"Unclassified source files (not checked as media)": 1}
        )
        self.assertTrue(all(not row["path"].endswith(".bin") for row in report["files"]))

    def test_report_provenance_and_multiple_local_matches_are_complete_and_ordered(self):
        for folder in (self.recovery, self.extracted):
            self.local_file(folder, "renamed.jpg", b"original")
        report = self.report(self.recovery, self.extracted)
        self.assertEqual(report["archives"], [str(self.archive)])
        self.assertEqual(report["date_range"], ["2023-01-01", "2023-12-31"])
        self.assertEqual(
            report["extracted_folders"], sorted([str(self.recovery), str(self.extracted)])
        )
        self.assertIs(report["include_partner"], False)
        self.assertEqual(report["warnings"], {})
        direct = next(row for row in report["files"] if row["association"] == "direct")
        self.assertEqual(
            direct["local_matches"],
            [
                {"folder": str(folder), "path": "renamed.jpg", "sidecar": "present_unverified"}
                for folder in sorted([self.extracted, self.recovery])
            ],
        )

    def test_local_json_limit_accepts_exact_boundary_and_rejects_larger_valid_json(self):
        media = self.local_file(self.extracted, "renamed.jpg", b"original")
        real_open = Path.open

        @contextmanager
        def bounded_open(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:

                class Reader:
                    def read(reader, size=-1):
                        self.assertIsInstance(size, int)
                        self.assertGreater(size, 0)
                        self.assertLessEqual(size, JSON_LIMIT + 1)
                        return stream.read(size)

                yield Reader()

        for size in (JSON_LIMIT, JSON_LIMIT + 1):
            with self.subTest(size=size):
                base = b'{"software":"immich-go version:0.32.0","dateTaken":"2023-01-01T00:00:00Z","padding":""}'
                payload = base[:-2] + b"x" * (size - len(base)) + b'"}'
                Path(str(media) + ".JSON").write_bytes(payload)
                with patch.object(Path, "open", autospec=True, side_effect=bounded_open):
                    report = self.report()
                row = next(row for row in report["files"] if row["association"] == "direct")
                self.assertEqual(
                    row["local_matches"][0]["sidecar"],
                    "present_unverified" if size == JSON_LIMIT else "invalid",
                )
                self.assertEqual(
                    row["issues"], [] if size == JSON_LIMIT else ["invalid local JSON sidecar"]
                )

    def test_ignored_zip_entries_and_rejected_sidecars_do_not_hide_later_media(self):
        prefix = "Takeout/Google Photos/"
        rejected = "Photos from 2023/photo-edited(1).jpg.json"
        for name, payload, invalid in (
            ("Takeout/Other/ignored.txt", b"outside", False),
            (prefix + "Directory/", b"", False),
            (prefix + rejected, b"{", True),
            (prefix + rejected, b"[]", True),
            (prefix + rejected, b" " * (JSON_LIMIT + 1), True),
        ):
            with self.subTest(name=name, prefix=payload[:8]):
                with zipfile.ZipFile(self.archive, "w") as archive:
                    archive.writestr(name, payload)
                    for path, value in self.entries.items():
                        archive.writestr(
                            prefix + path, value if isinstance(value, bytes) else json.dumps(value)
                        )
                report = self.report()
                self.assertEqual(len(report["files"]), 2)
                row = next(
                    row for row in report["files"] if row["path"].endswith("photo-edited(1).jpg")
                )
                if invalid:
                    self.assertIn("invalid source metadata", row["issues"])
                    self.assertIn("missing source metadata", row["issues"])
                    self.assertEqual(row["scope"], "export_exception")
                else:
                    self.assertEqual(row["status"], "missing")

    def test_unknown_ambiguous_and_unpaired_files_remain_export_wide(self):
        self.entries.update(
            {
                "Photos from 2021/motion(1).MP4": b"motion",
                "Photos from 2014/choice.jpg": b"jpg",
                "Photos from 2014/choice.gif": b"gif",
                "Photos from 2014/choice.json": sidecar("choice", "ambiguous"),
                "Photos from 2007/undated.jpg": b"unknown",
                "Photos from 2007/undated.jpg.json": sidecar("undated.jpg", "undated", "0"),
                # Own undated metadata must not be replaced by the original's date.
                "Photos from 2023/photo-edited(1).jpg.json": sidecar(
                    "photo-edited(1).jpg", "own", "0"
                ),
            }
        )
        self.write()
        report = self.report()
        self.assertEqual(report["selected"]["files"], 1)
        self.assertEqual(report["export_exception"]["files"], 5)
        self.assertEqual(report["export_exception"]["needs_review"], 5)
        rows = {row["path"]: row for row in report["files"]}
        self.assertEqual(rows["Photos from 2021/motion(1).MP4"]["capture_times"], [])
        self.assertIn("no metadata association", rows["Photos from 2021/motion(1).MP4"]["issues"])
        self.assertIn("unknown capture date", rows["Photos from 2021/motion(1).MP4"]["issues"])
        self.assertEqual(rows["Photos from 2014/choice.jpg"]["association"], "ambiguous")
        self.assertIn("ambiguous association", rows["Photos from 2014/choice.jpg"]["issues"])
        self.assertEqual(rows["Photos from 2023/photo-edited(1).jpg"]["scope"], "export_exception")

    def test_conflicting_dates_and_differing_same_path_contents_are_never_assigned_to_year(self):
        other = self.root / "part2.zip"
        data = self.entries["Photos from 2023/photo.jpg.supplemental-metadata(1).json"]
        write_zip(
            other,
            {
                "Album/photo.jpg": b"other",
                "Album/photo.jpg.json": data | {"photoTakenTime": {"timestamp": "1704067200"}},
            },
        )
        report = reconcile_report([self.archive, other], [self.extracted], SELECTION)
        self.assertEqual(report["selected"]["files"], 0)
        self.assertEqual(report["export_exception"]["files"], 3)
        self.assertTrue(
            all("conflicting capture dates" in row["issues"] for row in report["files"])
        )
        write_zip(other, {"Photos from 2023/photo(1).jpg": b"different bytes"})
        report = reconcile_report([self.archive, other], [self.extracted], SELECTION)
        originals = [row for row in report["files"] if row["path"].endswith("/photo(1).jpg")]
        self.assertEqual(len(originals), 2)
        self.assertEqual(len({row["sha256"] for row in originals}), 2)
        self.assertTrue(all(row["scope"] == "export_exception" for row in originals))

    def test_ambiguous_original_owners_preserve_candidate_dates_and_sidecars_for_review(self):
        self.entries = {
            "Year/photo(1).jpg": b"original",
            "Year/photo-edited(1).jpg": b"edit",
            "Year/photo(1).jpg.json": sidecar("photo(1).jpg", "one", "1672531200"),
            "Year/photo(1).jpg.supplemental-metadata.json": sidecar(
                "photo(1).jpg", "two", "1704067200"
            ),
        }
        self.write()
        report = self.report()
        self.assertEqual(report["selected"]["files"], 0)
        self.assertEqual(report["export_exception"]["files"], 2)
        for row in report["files"]:
            self.assertEqual(row["association"], "ambiguous")
            self.assertEqual(
                row["capture_times"], ["2023-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"]
            )
            self.assertEqual(
                row["sidecars"],
                ["Year/photo(1).jpg.json", "Year/photo(1).jpg.supplemental-metadata.json"],
            )
            self.assertIn("ambiguous association", row["issues"])
            self.assertIn("conflicting capture dates", row["issues"])
        audit = audit_report([self.archive])
        self.assertEqual(audit["associations"]["ambiguous"], 2)
        self.assertTrue(all(row["date_status"] == "unresolved" for row in audit["files"]))

    def test_inclusive_boundaries_and_metadata_without_media_are_separate(self):
        self.entries = {}
        for title, timestamp in (
            ("first", "1672531200"),
            ("last", "1704067199"),
            ("next", "1704067200"),
        ):
            self.entries[title + ".jpg"] = title.encode()
            self.entries[title + ".jpg.json"] = sidecar(title + ".jpg", title, timestamp)
        self.entries["ghost.jpg.json"] = sidecar("ghost.jpg", "ghost")
        self.entries["last-ghost.jpg.json"] = sidecar("last-ghost.jpg", "last-ghost", "1704067199")
        self.entries["unknown.jpg.json"] = sidecar("unknown.jpg", "unknown", "0")
        self.entries["outside.jpg.json"] = sidecar("outside.jpg", "outside", "1704067200")
        self.write()
        report = self.report()
        self.assertEqual(report["selected"]["files"], 2)
        self.assertEqual(report["outside_date_files"], 1)
        self.assertEqual(report["source_media_files"], 3)
        self.assertEqual(report["selected"]["missing"], 2)
        missing = {tuple(row["sidecars"]): row["scope"] for row in report["metadata_without_media"]}
        self.assertEqual(
            missing,
            {
                ("ghost.jpg.json",): "selected",
                ("last-ghost.jpg.json",): "selected",
                ("unknown.jpg.json",): "export_exception",
            },
        )
        missing_dates = {
            tuple(row["sidecars"]): row["capture_times"] for row in report["metadata_without_media"]
        }
        self.assertEqual(missing_dates[("ghost.jpg.json",)], ["2023-01-01T00:00:00+00:00"])
        self.assertEqual(missing_dates[("last-ghost.jpg.json",)], ["2023-12-31T23:59:59+00:00"])
        self.assertEqual(missing_dates[("unknown.jpg.json",)], [])

    def test_partner_and_trashed_filters_do_not_hide_source_metadata_conflicts(self):
        key = "Photos from 2023/photo.jpg.supplemental-metadata(1).json"
        for flags in ({"trashed": True}, {"googlePhotosOrigin": {"fromPartnerSharing": {}}}):
            with self.subTest(flags=flags):
                write_zip(self.archive, self.entries | {key: self.entries[key] | flags})
                self.assertEqual(self.report()["selected"]["excluded"], 2)
        self.assertEqual(
            reconcile_report([self.archive], [self.extracted], SELECTION, include_partner=True)[
                "selected"
            ]["missing"],
            2,
        )

    def test_json_sidecar_case_matching_and_invalid_content_do_not_confirm_metadata(self):
        lower = self.local_file(self.extracted, "photo.jpg", b"original")
        upper = self.extracted / "PHOTO.JPG"
        upper.write_bytes(b"edited")
        report = self.report()
        rows = {row["association"]: row for row in report["files"]}
        self.assertEqual(rows["direct"]["local_matches"][0]["sidecar"], "present_unverified")
        self.assertEqual(rows["candidate_edit"]["local_matches"][0]["sidecar"], "missing")
        for payload in ("not JSON", "[]", " " * JSON_LIMIT + "{}"):
            with self.subTest(payload=payload[:10]):
                Path(str(lower) + ".JSON").write_text(payload)
                self.assertEqual(self.report()["selected"]["needs_review"], 2)
        Path(str(lower) + ".json").write_text("{}")
        report = self.report()
        direct = next(row for row in report["files"] if row["association"] == "direct")
        self.assertEqual(direct["local_matches"][0]["sidecar"], "ambiguous")

    def test_cli_json_details_and_reports_make_no_writes_or_tool_or_server_calls(self):
        self.local_file(self.extracted, "original.jpg", b"original")
        before = {str(p): p.read_bytes() if p.is_file() else None for p in self.root.rglob("*")}
        for arguments in ([], ["--details"], ["--json"]):
            with (
                patch("subprocess.run", side_effect=AssertionError("no tool")),
                patch("urllib.request.urlopen", side_effect=AssertionError("no server")),
                patch.object(takeout, "load_config", side_effect=AssertionError("no config")),
                patch.object(Path, "mkdir", side_effect=AssertionError("no directories")),
                patch.object(Path, "write_text", side_effect=AssertionError("no writes")),
            ):
                code, out, err = self.invoke(*arguments)
            self.assertEqual(code, 0, err)
            if arguments == ["--json"]:
                report = json.loads(out)
                self.assertEqual(report["selected"]["files"], 2)
                self.assertFalse(report["metadata_and_albums_validated"])
            else:
                self.assertIn("Bytes and supported metadata verified", out)
                self.assertIn("Export-wide exceptions", out)
                self.assertIn('"path": "Photos from 2023/photo-edited(1).jpg"', out)
                self.assertEqual(
                    '"path": "Photos from 2023/photo(1).jpg"' in out, arguments == ["--details"]
                )
        self.assertEqual(
            before, {str(p): p.read_bytes() if p.is_file() else None for p in self.root.rglob("*")}
        )

    def test_invalid_folders_symlinks_and_zip_errors_fail_without_partial_report(self):
        code, out, err = self.invoke("--extracted", self.root / "missing")
        self.assertEqual(code, 1)
        self.assertIn("existing extracted folder", err)
        self.assertEqual(out, "")
        (self.extracted / "alias").symlink_to(self.archive)
        self.assertEqual(self.invoke()[0], 1)
        (self.extracted / "alias").unlink()
        self.archive.write_bytes(b"invalid zip")
        self.assertEqual(self.invoke()[0], 1)

    def test_names_with_control_characters_are_json_escaped_in_reports(self):
        self.entries["Year/weird\nname.jpg"] = b"unpaired"
        self.write()
        code, out, err = self.invoke("--details")
        self.assertEqual(code, 0, err)
        self.assertIn(r"weird\nname.jpg", out)
        self.assertNotIn("weird\nname.jpg", out)

    def test_default_report_summarises_export_exceptions_without_listing_unrelated_files(self):
        self.entries["Photos from 2021/unpaired.mp4"] = b"unpaired video"
        self.entries["ghost.jpg.json"] = sidecar("ghost.jpg", "ghost")
        self.write()
        code, out, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertIn("Export-wide exceptions: 1 files; not assigned to the selected dates.", out)
        self.assertIn("| Unpaired media | 1 |", out)
        self.assertIn("Metadata items without a media filename match: 1", out)
        self.assertNotIn("unpaired.mp4", out)
        self.assertNotIn("ghost.jpg.json", out)
        code, out, err = self.invoke("--details")
        self.assertEqual(code, 0, err)
        self.assertIn("unpaired.mp4", out)
        self.assertIn("ghost.jpg.json", out)

    @unittest.skipUnless(shutil.which("immich-go"), "optional installed immich-go integration")
    def test_native_extraction_then_recovery_resolves_missing_edited_bytes(self):
        with patch.dict(os.environ, {"IMMICHGO_TEMPDIR": str(self.root / "cache")}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    takeout.main(
                        [
                            "extract",
                            "--date-range",
                            "2023",
                            "--output",
                            str(self.extracted),
                            "--log-file",
                            str(self.root / "extract.log"),
                            str(self.archive),
                        ]
                    ),
                    0,
                )
                self.assertEqual(self.report()["selected"]["missing"], 1)
                self.assertEqual(
                    takeout.main(
                        [
                            "recover",
                            "--date-range",
                            "2023",
                            "--extracted",
                            str(self.extracted),
                            "--output",
                            str(self.recovery),
                            "--log-file",
                            str(self.root / "recover.log"),
                            str(self.archive),
                        ]
                    ),
                    0,
                )
        report = self.report(self.extracted, self.recovery)
        self.assertEqual(report["selected"]["present"], 2)
        self.assertEqual(report["selected"]["missing"], 0)
        self.assertTrue(report["metadata_and_albums_validated"])


class ReconcileProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=15, deadline=None, derandomize=True)
    @given(
        st.binary(min_size=1, max_size=40),
        st.sampled_from(["0", "1672531200", "1704067199", "1704067200"]),
        st.booleans(),
    )
    def test_archive_order_repetition_and_partition_invariants(contents, timestamp, present):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "extracted"
            folder.mkdir()
            a, b = root / "a.zip", root / "b.zip"
            write_zip(a, {"Year/photo.jpg": contents, "Year/photo-edited.jpg": contents + b"edit"})
            write_zip(b, {"Year/photo.jpg.json": sidecar("photo.jpg", timestamp=timestamp)})
            if present:
                (folder / "renamed.jpg").write_bytes(contents)
                (folder / "renamed.jpg.JSON").write_text("{}")
            expected = reconcile_report([a, b], [folder], SELECTION)
            assert expected == reconcile_report([b, a, b], [folder, folder], SELECTION)
            assert (
                expected["source_media_files"]
                == expected["selected"]["files"]
                + expected["export_exception"]["files"]
                + expected["outside_date_files"]
            )
            for scope in ("selected", "export_exception"):
                summary = expected[scope]
                assert summary["files"] == sum(
                    summary[s] for s in ("present", "missing", "needs_review", "excluded")
                )
                assert summary["files"] == summary["content_present"] + summary["content_missing"]
            assert not expected["metadata_and_albums_validated"]
