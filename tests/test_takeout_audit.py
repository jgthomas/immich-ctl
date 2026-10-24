"""Numbered-file auditing across ZIP parts without extracting or opening media."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout_audit import audit_report
from tests.script_loader import load_script

takeout = load_script("immich-takeout")


def sidecar(title, identity="photo", timestamp="1672531200"):
    return {"title": title, "url": identity, "photoTakenTime": {"timestamp": timestamp}}


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(
                "Takeout/Google Photos/" + name,
                data if isinstance(data, bytes) else json.dumps(data),
            )


class TakeoutAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "takeout.zip"

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = takeout.main(["audit", *map(str, arguments)])
        return code, stdout.getvalue(), stderr.getvalue()

    def basic_export(self):
        write_zip(
            self.archive,
            {
                "Year/photo.jpg": b"unnumbered",
                "Year/photo(1).jpg": b"numbered original",
                "Year/photo.jpg.supplemental-metadata(1).json": sidecar("photo.jpg"),
                "Year/photo-edited(1).jpg": b"candidate edit",
                "Year/photo(2).jpg": b"no metadata",
                "Year/photo-edited(3).jpg": b"no original",
                "Year/photo-edited(4).jpg": b"own metadata",
                "Year/photo-edited(4).jpg.json": sidecar("photo-edited(4).jpg", "own"),
                "Year/photo-edited(5).jpg": b"ambiguous metadata",
                "Year/photo-edited(5).jpg.json": sidecar("photo-edited(5).jpg", "claim-a"),
                "Year/photo-edited(5).jpg.supplemental-metadata.json": sidecar(
                    "photo-edited(5).jpg", "claim-b"
                ),
                "Year/document(1).bin": b"not media",
            },
        )

    def test_categories_partition_numbered_files_and_years_without_guessing(self):
        self.basic_export()
        report = audit_report([self.archive])
        self.assertEqual(report["media_files"], 7)
        self.assertEqual(report["numbered_files"], 6)
        self.assertEqual(report["numbered_edits"], 4)
        self.assertEqual(report["potentially_affected"], 3)
        self.assertEqual(
            report["associations"], {"direct": 2, "candidate": 1, "ambiguous": 1, "missing": 2}
        )
        self.assertEqual(
            report["years"],
            {
                "2023": {"numbered_media": 1, "numbered_edit": 2, "potentially_affected": 1},
                "Unknown": {"numbered_media": 1, "numbered_edit": 2, "potentially_affected": 2},
            },
        )
        rows = {row["path"]: row for row in report["files"]}
        edit = rows["Year/photo-edited(1).jpg"]
        self.assertEqual(edit["association"], "candidate")
        self.assertEqual(edit["originals"][0]["path"], "Year/photo(1).jpg")
        self.assertEqual(edit["sidecars"], ["Year/photo.jpg.supplemental-metadata(1).json"])
        self.assertEqual(edit["size"], len(b"candidate edit"))
        self.assertEqual(len(edit["crc32"]), 8)

    def test_cli_reads_only_json_without_network_tools_or_filesystem_writes(self):
        self.basic_export()
        before = self.archive.read_bytes()
        original_open = zipfile.ZipFile.open

        def json_only(archive, member, *args, **kwargs):
            name = member.filename if isinstance(member, zipfile.ZipInfo) else member
            self.assertTrue(name.endswith(".json"), f"Opened media payload: {name}")
            return original_open(archive, member, *args, **kwargs)

        with (
            patch.object(zipfile.ZipFile, "open", autospec=True, side_effect=json_only),
            patch.object(takeout.subprocess, "run") as run,
            patch.object(takeout, "load_config") as config,
            patch.object(takeout.shutil, "which") as which,
            patch("socket.create_connection", side_effect=AssertionError("network request")),
        ):
            code, output, error = self.invoke("--json", self.archive)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["numbered_files"], 6)
        self.assertEqual(error, "")
        run.assert_not_called()
        config.assert_not_called()
        which.assert_not_called()
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"takeout.zip"})

    def test_text_summary_details_and_control_character_escaping(self):
        self.basic_export()
        code, output, error = self.invoke(self.archive)
        self.assertEqual(code, 0, error)
        self.assertIn("Numbered media files: 6", output)
        self.assertIn("Potentially affected numbered edits: 3", output)
        self.assertIn("| Candidate original sidecar | 1 |", output)
        self.assertIn("| 2023 | 1 | 2 | 1 |", output)
        self.assertNotIn('"path"', output)
        self.assertIn('"path"', self.invoke("--details", self.archive)[1])
        write_zip(self.archive, {"Trip\n/photo(1).jpg": b"media"})
        code, output, _ = self.invoke("--details", self.archive)
        self.assertEqual(code, 0)
        self.assertIn("Trip\\n/photo(1).jpg", output)
        self.assertNotIn("Trip\n/photo(1).jpg", output)

    def test_literal_parentheses_and_large_indexes_are_preserved(self):
        original = "Negative(35)(12).jpg"
        edit = "Negative(35)-edited(12).jpg"
        write_zip(
            self.archive,
            {
                "Album/" + original: b"original",
                "Album/" + original + ".json": sidecar(original),
                "Album/" + edit: b"edit",
            },
        )
        report = audit_report([self.archive])
        row = next(row for row in report["files"] if row["kind"] == "numbered_edit")
        self.assertEqual(row["originals"][0]["path"], "Album/" + original)
        self.assertEqual(row["association"], "candidate")
        self.assertEqual(row["year"], "2023")

    def test_own_metadata_precedes_original_date_even_when_undated(self):
        for timestamp, expected in (("1704067200", "2024"), ("0", "Unknown")):
            with self.subTest(timestamp=timestamp):
                write_zip(
                    self.archive,
                    {
                        "Album/photo(1).jpg": b"original",
                        "Album/photo(1).jpg.json": sidecar("photo(1).jpg"),
                        "Album/photo-edited(1).jpg": b"edit",
                        "Album/photo-edited(1).jpg.json": sidecar(
                            "photo-edited(1).jpg", "edit", timestamp
                        ),
                    },
                )
                report = audit_report([self.archive])
                row = next(row for row in report["files"] if row["kind"] == "numbered_edit")
                self.assertEqual(row["association"], "direct")
                self.assertEqual(row["year"], expected)
                self.assertEqual(row["date_status"], "known" if timestamp != "0" else "missing")
                self.assertEqual(row["sidecars"], ["Album/photo-edited(1).jpg.json"])
                self.assertFalse(row["potentially_affected"])

    def test_ambiguous_original_content_keeps_edit_unresolved(self):
        write_zip(
            self.archive,
            {
                "Album/photo(1).jpg": b"first original",
                "Album/photo(1).jpg.json": sidecar("photo(1).jpg"),
                "Album/photo-edited(1).jpg": b"edit",
            },
        )
        second = self.root / "second.zip"
        write_zip(second, {"Album/photo(1).jpg": b"different original"})
        report = audit_report([self.archive, second])
        self.assertEqual(
            report["associations"], {"direct": 0, "candidate": 0, "ambiguous": 3, "missing": 0}
        )
        row = next(row for row in report["files"] if row["kind"] == "numbered_edit")
        self.assertEqual(len(row["originals"]), 2)
        self.assertEqual(row["year"], "Unknown")

    def test_original_with_no_metadata_does_not_establish_edit_date(self):
        write_zip(
            self.archive,
            {
                "Photos from 2013/photo(1).jpg": b"original",
                "Photos from 2013/photo-edited(1).jpg": b"edit",
            },
        )
        report = audit_report([self.archive])
        self.assertEqual(report["associations"]["missing"], 2)
        self.assertEqual(set(report["years"]), {"Unknown"})
        self.assertTrue(all(not row["sidecars"] for row in report["files"]))

    def test_folder_case_and_extension_are_not_guessed(self):
        write_zip(
            self.archive,
            {
                "Album/photo(1).jpg": b"original",
                "Album/photo(1).jpg.json": sidecar("photo(1).jpg"),
                "Album/PHOTO-edited(1).jpg": b"case differs",
                "Album/photo-edited(1).png": b"extension differs",
                "Other/photo-edited(1).jpg": b"folder differs",
            },
        )
        report = audit_report([self.archive])
        self.assertEqual(
            report["associations"], {"direct": 1, "candidate": 0, "ambiguous": 0, "missing": 3}
        )

    def test_conflicting_times_remain_unknown_even_in_same_year(self):
        write_zip(
            self.archive,
            {
                "Album/photo(1).jpg": b"original",
                "Album/photo(1).jpg.json": sidecar("photo(1).jpg"),
                "Album/photo-edited(1).jpg": b"edit",
            },
        )
        second = self.root / "second.zip"
        write_zip(second, {"Other/photo.json": sidecar("photo.jpg", timestamp="1672531201")})
        report = audit_report([self.archive, second])
        self.assertEqual(set(report["years"]), {"Unknown"})
        self.assertTrue(all(row["date_status"] == "conflicting" for row in report["files"]))

    def test_album_copies_keep_local_sidecar_paths_and_count_as_files(self):
        entries = {}
        for folder in ("Year", "Album"):
            entries[folder + "/photo(1).jpg"] = b"same original"
            entries[folder + "/photo(1).jpg.json"] = sidecar("photo(1).jpg")
        write_zip(self.archive, entries)
        report = audit_report([self.archive, self.archive])
        self.assertEqual(report["numbered_files"], 2)
        for row in report["files"]:
            self.assertEqual(row["sidecars"], [row["path"] + ".json"])

    def test_empty_and_malformed_exports_report_without_hiding_warnings(self):
        write_zip(self.archive, {})
        self.assertEqual(audit_report([self.archive])["numbered_files"], 0)
        write_zip(
            self.archive, {"Album/photo(1).jpg": b"media", "Album/photo(1).jpg.json": b"bad JSON"}
        )
        report = audit_report([self.archive])
        self.assertEqual(report["associations"]["missing"], 1)
        self.assertEqual(report["warnings"], {"Unreadable JSON files (skipped)": 1})
        self.assertIn("Inspection warnings", self.invoke(self.archive)[1])
        self.archive.write_bytes(b"not a ZIP")
        code, _, error = self.invoke(self.archive)
        self.assertEqual(code, 1)
        self.assertIn("Error:", error)


class AuditProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=25, deadline=None, derandomize=True)
    @given(
        st.integers(min_value=1, max_value=20),
        st.sampled_from(["photo", "été🌅", "photo(35)"]),
        st.sampled_from([".jpg", ".JPG", ".mov"]),
    )
    def test_split_order_repetition_and_partition_invariants(index, stem, extension):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "one.zip", root / "two.zip"
            original = f"{stem}({index}){extension}"
            edit = f"{stem}-edited({index}){extension}"
            write_zip(
                first,
                {
                    "Album/" + original: b"original",
                    "Album/" + original + ".json": sidecar(original),
                },
            )
            write_zip(second, {"Album/" + edit: b"edited"})
            report = audit_report([first, second])
            assert report == audit_report([second, first]) == audit_report([first, second, first])
            assert report["numbered_files"] == 2
            assert report["numbered_edits"] == report["potentially_affected"] == 1
            assert sum(report["associations"].values()) == len(report["files"])
            assert (
                sum(t["numbered_media"] + t["numbered_edit"] for t in report["years"].values()) == 2
            )
            assert {row["path"] for row in report["files"]} == {
                "Album/" + original,
                "Album/" + edit,
            }


if __name__ == "__main__":
    unittest.main()
