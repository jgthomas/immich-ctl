"""Rejected own metadata must not disappear into inherited edit dates."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from immich_ctl.takeout import inspect_archives, selected_media_files
from immich_ctl.takeout_audit import audit_report
from immich_ctl.takeout_reconcile import JSON_LIMIT, reconcile_report
from tests.script_loader import load_script
from tests.test_takeout_audit import sidecar, write_zip

takeout = load_script("immich-takeout")
SELECTION = (date(2023, 1, 1), date(2023, 12, 31))


class FailedSidecarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "media.zip"
        self.other = self.root / "sidecar.zip"
        self.extracted = self.root / "extracted"
        self.extracted.mkdir()

    def export(self, *, timestamp="1704067200", numbered=False):
        original = "photo(1).jpg" if numbered else "photo.jpg"
        edit = "photo-edited(1).jpg" if numbered else "photo-edited.jpg"
        metadata_name = "photo.jpg.supplemental-metadata(1).json" if numbered else "photo.jpg.json"
        write_zip(
            self.archive,
            {
                "Year/" + original: b"original",
                "Year/" + edit: b"edited",
                "Year/" + metadata_name: sidecar("photo.jpg", "original", timestamp),
            },
        )
        return original, edit

    def test_rejected_own_sidecars_stay_export_wide_inside_and_outside_selected_dates(self):
        oversized = json.dumps(sidecar("photo-edited.jpg", "own")).encode().ljust(JSON_LIMIT + 1)
        cases = (
            (b"{", "Unreadable JSON files (skipped)"),
            (b"\xff", "Unreadable JSON files (skipped)"),
            (b"[]", "Non-object JSON files (skipped)"),
            (b'{"title":"photo-edited.jpg"}', "Unrecognised JSON files (skipped)"),
            (oversized, "JSON files larger than 8 MiB (skipped)"),
        )
        for timestamp in ("1672531200", "1704067200"):
            for payload, warning in cases:
                with self.subTest(timestamp=timestamp, warning=warning, prefix=payload[:8]):
                    self.export(timestamp=timestamp)
                    write_zip(self.other, {"Year/photo-edited.jpg.json": payload})
                    paths = [self.archive, self.other]
                    *_, coverage = inspect_archives(paths)
                    key = next(
                        k for k in coverage.unmatched_files if k[0] == "Year/photo-edited.jpg"
                    )
                    self.assertEqual(coverage.failed_sidecars[key], {"Year/photo-edited.jpg.json"})
                    self.assertNotIn(key, coverage.file_owners)
                    report = reconcile_report(paths, [self.extracted], SELECTION)
                    self.assertEqual(report["outside_date_files"], int(timestamp == "1704067200"))
                    self.assertEqual(report["selected"]["files"], int(timestamp == "1672531200"))
                    self.assertEqual(report["export_exception"]["files"], 1)
                    edit = next(
                        row for row in report["files"] if row["path"] == "Year/photo-edited.jpg"
                    )
                    self.assertEqual(edit["scope"], "export_exception")
                    self.assertEqual(edit["status"], "needs_review")
                    self.assertEqual(edit["capture_times"], [])
                    self.assertEqual(edit["failed_sidecars"], ["Year/photo-edited.jpg.json"])
                    self.assertEqual(edit["sidecars"], ["Year/photo-edited.jpg.json"])
                    self.assertIn("unusable source sidecar", edit["issues"])
                    if warning != "Unrecognised JSON files (skipped)":
                        self.assertIn("invalid source metadata", edit["issues"])
                        self.assertIn("missing source metadata", edit["issues"])
                    self.assertEqual(report["warnings"][warning], 1)

    def test_json_size_boundary_and_oversized_members_are_not_read(self):
        self.export()
        # A real album needs explicit album metadata; this test concerns JSON size.
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("Takeout/Google Photos/Year/metadata.json", '{"title":"Year"}')
        own = json.dumps(sidecar("photo-edited.jpg", "own")).encode()
        real_read = zipfile.ZipFile.read
        for size in (JSON_LIMIT, JSON_LIMIT + 1):
            with self.subTest(size=size):
                write_zip(self.other, {"Year/photo-edited.jpg.json": own.ljust(size)})

                def bounded_read(archive, member, *args, **kwargs):
                    info = (
                        member if isinstance(member, zipfile.ZipInfo) else archive.getinfo(member)
                    )
                    self.assertLessEqual(info.file_size, JSON_LIMIT)
                    return real_read(archive, member, *args, **kwargs)

                with patch.object(zipfile.ZipFile, "read", autospec=True, side_effect=bounded_read):
                    report = reconcile_report(
                        [self.archive, self.other], [self.extracted], SELECTION
                    )
                self.assertEqual(report["export_exception"]["files"], int(size > JSON_LIMIT))
                self.assertEqual(report["selected"]["files"], int(size == JSON_LIMIT))
                edit = next(
                    row for row in report["files"] if row["path"] == "Year/photo-edited.jpg"
                )
                self.assertEqual(
                    edit["status"], "missing" if size == JSON_LIMIT else "needs_review"
                )
                if size == JSON_LIMIT:
                    self.assertEqual(edit["issues"], [])

    def test_known_sidecar_name_forms_preserve_literal_case_and_numbering(self):
        for edit, original, bad in (
            ("photo-edited.jpg", "photo.jpg", "photo-edited.jpg.JSON"),
            (
                "photo-edited(1).jpg",
                "photo(1).jpg",
                "photo-edited.jpg.supplemental-metadata(1).json",
            ),
            ("photo-edited(1).jpg", "photo(1).jpg", "photo-edited.jpg(1).json"),
            ("photo(1)-edited.jpg", "photo(1).jpg", "photo(1)-edited.jpg.json"),
        ):
            with self.subTest(bad=bad):
                write_zip(
                    self.archive,
                    {
                        "Year/" + original: b"original",
                        "Year/" + edit: b"edit",
                        "Year/" + original + ".json": sidecar(original),
                        "Year/" + bad: b"broken",
                    },
                )
                *_, coverage = inspect_archives([self.archive])
                self.assertEqual({key[0] for key in coverage.failed_sidecars}, {"Year/" + edit})
                report = reconcile_report([self.archive], [self.extracted], SELECTION)
                self.assertEqual(report["export_exception"]["files"], 1)
                row = next(row for row in report["files"] if row["path"] == "Year/" + edit)
                self.assertEqual(row["failed_sidecars"], ["Year/" + bad])

    def test_unrelated_json_and_recognised_album_metadata_do_not_block_inheritance(self):
        self.export(timestamp="1672531200")
        write_zip(
            self.other,
            {
                "Elsewhere/photo-edited.jpg.json": b"broken",
                "Year/PHOTO-edited.jpg.json": b"broken",
                "Year/photo-edited(2).jpg.json": b"broken",
                "Year/unrelated.json": {"title": "photo-edited.jpg"},
                "Year/metadata.json": {"title": "Album"},
                "Year/photo-edited.jpg.json": {"albumData": {"title": "Album"}},
            },
        )
        *_, coverage = inspect_archives([self.archive, self.other])
        self.assertEqual(coverage.failed_sidecars, {})
        _, edits = selected_media_files(coverage, set(coverage.capture_times))
        self.assertEqual({key[0] for key in edits}, {"Year/photo-edited.jpg"})
        report = reconcile_report([self.archive, self.other], [self.extracted], SELECTION)
        self.assertEqual(report["export_exception"]["files"], 0)
        self.assertEqual(report["selected"]["files"], 2)

    def test_failed_extensionless_sidecar_preserves_all_ambiguous_candidates(self):
        self.export()
        write_zip(
            self.other,
            {
                "Year/photo-edited.png": b"png edit",
                "Year/photo-edited.json": b"broken",
            },
        )
        *_, coverage = inspect_archives([self.archive, self.other])
        self.assertEqual(
            {key[0] for key in coverage.failed_sidecars},
            {
                "Year/photo-edited.jpg",
                "Year/photo-edited.png",
            },
        )
        self.assertTrue(
            all(paths == {"Year/photo-edited.json"} for paths in coverage.failed_sidecars.values())
        )
        self.assertEqual(
            reconcile_report([self.archive, self.other], [self.extracted], SELECTION)[
                "export_exception"
            ]["files"],
            2,
        )

    def test_valid_and_failed_copies_of_same_own_sidecar_remain_unresolved(self):
        self.export(numbered=True)
        path = "Year/photo-edited(1).jpg.json"
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr(
                "Takeout/Google Photos/" + path, json.dumps(sidecar("photo-edited(1).jpg", "own"))
            )
        write_zip(self.other, {path: b"broken"})
        report = reconcile_report([self.archive, self.other], [self.extracted], SELECTION)
        self.assertEqual(report["selected"]["files"], 0)
        self.assertEqual(report["export_exception"]["files"], 1)
        row = next(
            row
            for row in audit_report([self.archive, self.other])["files"]
            if row["kind"] == "numbered_edit"
        )
        self.assertEqual(row["association"], "ambiguous")
        self.assertEqual(row["year"], "Unknown")
        self.assertEqual(row["failed_sidecars"], [path])

    def test_failed_original_metadata_also_prevents_inheritance(self):
        self.export(timestamp="1672531200", numbered=True)
        write_zip(self.other, {"Year/photo(1).jpg.json": b"broken"})
        *_, coverage = inspect_archives([self.archive, self.other])
        direct, edited = selected_media_files(coverage, set(coverage.capture_times))
        self.assertEqual(len(direct), 1)  # A valid filename match still exists.
        self.assertEqual(edited, set())
        report = reconcile_report([self.archive, self.other], [self.extracted], SELECTION)
        self.assertEqual(report["export_exception"]["files"], 2)
        for row in report["files"]:
            self.assertEqual(row["failed_sidecars"], ["Year/photo(1).jpg.json"])
            self.assertIn("Year/photo(1).jpg.json", row["sidecars"])
            self.assertIn("unusable source sidecar", row["issues"])
        audit = audit_report([self.archive, self.other])
        self.assertEqual(audit["associations"]["ambiguous"], 2)
        self.assertTrue(all(row["year"] == "Unknown" for row in audit["files"]))

    def test_original_with_only_rejected_metadata_keeps_both_files_ambiguous(self):
        write_zip(
            self.archive,
            {
                "Year/photo(1).jpg": b"original",
                "Year/photo-edited(1).jpg": b"edit",
                "Year/photo.jpg.supplemental-metadata(1).json": b"broken",
            },
        )
        audit = audit_report([self.archive])
        self.assertEqual(audit["associations"]["ambiguous"], 2)
        self.assertEqual(audit["associations"]["missing"], 0)
        report = reconcile_report([self.archive], [self.extracted], SELECTION)
        self.assertEqual(report["export_exception"]["files"], 2)
        self.assertEqual(report["selected"]["files"], 0)
        for row in report["files"]:
            self.assertEqual(row["association"], "ambiguous")
            self.assertEqual(
                row["failed_sidecars"], ["Year/photo.jpg.supplemental-metadata(1).json"]
            )
            self.assertIn("unusable source sidecar", row["issues"])

    def test_query_audit_and_recovery_do_not_accept_an_edit_with_rejected_own_json(self):
        self.export(timestamp="1672531200", numbered=True)
        path = "Year/photo-edited.jpg.supplemental-metadata(1).json"
        write_zip(self.other, {path: b"broken"})
        *_, coverage = inspect_archives([self.archive, self.other])
        direct, edited = selected_media_files(coverage, set(coverage.capture_times))
        self.assertEqual(len(direct), 1)
        self.assertEqual(edited, set())
        audit = audit_report([self.archive, self.other])
        row = next(row for row in audit["files"] if row["kind"] == "numbered_edit")
        self.assertEqual(row["association"], "ambiguous")
        self.assertEqual(row["year"], "Unknown")
        self.assertEqual(row["sidecars"], [path])
        self.assertEqual(row["failed_sidecars"], [path])
        output, error = io.StringIO(), io.StringIO()
        destination = self.root / "recovery"
        before = {str(p): p.read_bytes() if p.is_file() else None for p in self.root.rglob("*")}
        with (
            redirect_stdout(output),
            redirect_stderr(error),
            patch.object(takeout, "run_immich_go") as run,
        ):
            code = takeout.main(
                [
                    "recover",
                    "--date-range",
                    "2023",
                    "--extracted",
                    str(self.extracted),
                    "--output",
                    str(destination),
                    str(self.archive),
                    str(self.other),
                ]
            )
        self.assertEqual(code, 0, error.getvalue())
        self.assertIn("recover: 0; covered: 0; unresolved: 1", output.getvalue())
        run.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertEqual(
            before, {str(p): p.read_bytes() if p.is_file() else None for p in self.root.rglob("*")}
        )

    def test_invalid_google_ids_keep_distinct_items_separate(self):
        for url in ("", None, 17, ["invalid"]):
            with self.subTest(url=url):
                entries = {}
                for name in ("one", "two"):
                    entries[f"Year/{name}.jpg"] = name.encode()
                    entries[f"Year/{name}.jpg.json"] = sidecar(name + ".jpg") | {"url": url}
                write_zip(self.archive, entries)
                items, _, _, _, warnings, _, coverage = inspect_archives([self.archive])
                self.assertEqual(len(items), 2)
                self.assertTrue(all(len(identity) == 4 for identity in items))
                self.assertEqual(len(coverage.matched_items), 2)
                self.assertEqual(sum(warnings.values()), 2)

    def test_unsupported_format_does_not_hide_known_media_type_in_ambiguous_match(self):
        write_zip(
            self.archive,
            {
                "Year/image.jpg": b"photo",
                "Year/image.bin": b"unsupported",
                "Year/image.json": sidecar("image"),
            },
        )
        items, *_, coverage = inspect_archives([self.archive])
        self.assertEqual(items[("url", "photo")], ("2023", "Photos"))
        self.assertEqual(coverage.ambiguous_items, {("url", "photo")})


class FailedSidecarProperties(unittest.TestCase):
    @staticmethod
    @settings(max_examples=15, deadline=None, derandomize=True)
    @given(st.integers(0, 20), st.sampled_from([b"{", b"[]", b"{}", b"\xff"]))
    def test_rejected_numbered_sidecars_survive_split_order_and_repetition(index, payload):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media, metadata = root / "media.zip", root / "metadata.zip"
            write_zip(
                media,
                {
                    f"Year/photo({index}).jpg": b"original",
                    f"Year/photo-edited({index}).jpg": b"edit",
                    f"Year/photo.jpg.supplemental-metadata({index}).json": sidecar("photo.jpg"),
                },
            )
            path = f"Year/photo-edited.jpg.supplemental-metadata({index}).json"
            write_zip(metadata, {path: payload})
            inspection = inspect_archives([media, metadata])
            assert inspection == inspect_archives([metadata, media, metadata])
            coverage = inspection[-1]
            assert {key[0] for key in coverage.failed_sidecars} == {
                f"Year/photo-edited({index}).jpg"
            }
            assert list(coverage.failed_sidecars.values()) == [{path}]
            assert not selected_media_files(coverage, set(inspection[0]))[1]
